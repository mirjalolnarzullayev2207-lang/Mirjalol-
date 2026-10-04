import asyncio
import html
import logging
import os
import signal

import aiohttp
import asyncpg
from aiohttp import web

# ------------------------------------------------------------------
# Sozlamalar (hammasi Render > Environment orqali beriladi)
# ------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN muhit o'zgaruvchisi topilmadi (Render > Environment).")

DATABASE_URL = os.environ.get("DATABASE_URL")  # Neon / Supabase Postgres
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}
PORT = int(os.environ.get("PORT", 10000))
TIMEZONE = "Asia/Tashkent"
MAX_CONCURRENCY = 30
BROADCAST_DELAY = 0.05  # soniyada ~20 ta xabar (Telegram limiti ~30)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("join-bot")

WELCOME_TEXT = (
    "<b>Assalomu alaykum! Kanalga qo'shilish so'rovingiz tasdiqlandi! 🎉</b>\n\n"
    "Kanalimizga xush kelibsiz!"
)
START_TEXT = (
    "Salom, <b>{name}</b>! 👋\n\n"
    "Men kanallarga a'zo bo'lish so'rovlarini avtomatik tasdiqlovchi botman."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     BIGINT PRIMARY KEY,
    first_name  TEXT,
    username    TEXT,
    started     BOOLEAN NOT NULL DEFAULT FALSE,
    blocked     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS join_events (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    chat_id     BIGINT NOT NULL,
    chat_title  TEXT,
    approved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_join_events_time ON join_events (approved_at);
"""


# ------------------------------------------------------------------
# Ma'lumotlar bazasi
# ------------------------------------------------------------------
class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(
            self.dsn,
            min_size=1,
            max_size=5,
            command_timeout=30,
            max_inactive_connection_lifetime=60,  # Neon bo'sh ulanishlarni yopadi
            statement_cache_size=0,  # pgbouncer/pooler bilan mos
        )
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def upsert_user(self, user: dict, started: bool = False):
        await self.pool.execute(
            """
            INSERT INTO users (user_id, first_name, username, started)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE SET
                first_name = EXCLUDED.first_name,
                username   = EXCLUDED.username,
                started    = users.started OR EXCLUDED.started,
                blocked    = CASE WHEN EXCLUDED.started THEN FALSE ELSE users.blocked END
            """,
            user["id"],
            user.get("first_name"),
            user.get("username"),
            started,
        )

    async def log_join(self, user_id: int, chat_id: int, title: str | None):
        await self.pool.execute(
            "INSERT INTO join_events (user_id, chat_id, chat_title) VALUES ($1, $2, $3)",
            user_id,
            chat_id,
            title,
        )

    async def mark_blocked(self, user_id: int):
        await self.pool.execute("UPDATE users SET blocked = TRUE WHERE user_id = $1", user_id)

    async def broadcast_targets(self) -> list[int]:
        rows = await self.pool.fetch(
            "SELECT user_id FROM users WHERE started AND NOT blocked ORDER BY user_id"
        )
        return [r["user_id"] for r in rows]

    async def stats(self) -> dict:
        joins = await self.pool.fetchrow(
            """
            SELECT
                count(*) AS total,
                count(*) FILTER (
                    WHERE (approved_at AT TIME ZONE $1)::date = (now() AT TIME ZONE $1)::date
                ) AS today,
                count(*) FILTER (WHERE approved_at >= now() - interval '7 days') AS week,
                count(DISTINCT user_id) AS unique_users
            FROM join_events
            """,
            TIMEZONE,
        )
        users = await self.pool.fetchrow(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE started AND NOT blocked) AS active
            FROM users
            """
        )
        chats = await self.pool.fetch(
            """
            SELECT COALESCE(chat_title, chat_id::text) AS name, count(*) AS n
            FROM join_events GROUP BY chat_id, chat_title ORDER BY n DESC LIMIT 5
            """
        )
        return {"joins": joins, "users": users, "chats": chats}


# ------------------------------------------------------------------
# Telegram bot
# ------------------------------------------------------------------
class TelegramBot:
    def __init__(self, token: str, db: Database | None, stop: asyncio.Event):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.db = db
        self.stop = stop
        self.session: aiohttp.ClientSession | None = None
        self.sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self.tasks: set[asyncio.Task] = set()
        self.broadcasting = False

    # ---------- Past darajadagi API ----------

    async def request(
        self, method: str, http_timeout: int = 10, retries: int = 3, **params
    ) -> dict:
        """To'liq javobni qaytaradi. 429 va tarmoq xatolarida qayta uriladi."""
        data = {"ok": False, "error_code": 0, "description": "tarmoq xatosi"}
        for attempt in range(1, retries + 1):
            try:
                async with self.session.post(
                    self.base + method,
                    json=params,
                    timeout=aiohttp.ClientTimeout(total=http_timeout),
                ) as resp:
                    data = await resp.json()
                if data.get("ok"):
                    return data
                if data.get("error_code") == 429:
                    wait = data.get("parameters", {}).get("retry_after", 1)
                    log.warning("Flood limit: %s soniya kutiladi", wait)
                    await asyncio.sleep(wait + 0.5)
                    continue
                return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("%s tarmoq xatosi (urinish %d): %s", method, attempt, e)
                await asyncio.sleep(attempt)
        return data

    async def call(self, method: str, **params):
        data = await self.request(method, **params)
        if data.get("ok"):
            return data["result"]
        log.warning("%s xato: %s", method, data.get("description"))
        return None

    async def send(self, chat_id: int, text: str):
        return await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # ---------- Join request ----------

    async def on_join_request(self, req: dict):
        chat = req["chat"]
        user = req["from"]
        # user_chat_id: foydalanuvchi /start bosmagan bo'lsa ham xabar yuborishga imkon beradi
        user_chat_id = req.get("user_chat_id", user["id"])

        async with self.sem:
            approved = await self.call(
                "approveChatJoinRequest", chat_id=chat["id"], user_id=user["id"]
            )
            if approved is None:
                log.error("Tasdiqlanmadi: user=%s chat=%s", user["id"], chat["id"])
                return
            log.info("Tasdiqlandi: user=%s chat=%s", user["id"], chat["id"])

            # Baza xatosi tasdiqlashga xalaqit bermasligi kerak
            if self.db:
                try:
                    await self.db.upsert_user(user)
                    await self.db.log_join(user["id"], chat["id"], chat.get("title"))
                except Exception:
                    log.exception("Bazaga yozishda xato")

            await self.send(user_chat_id, WELCOME_TEXT)

    # ---------- Xabarlar va buyruqlar ----------

    async def on_message(self, msg: dict):
        if msg["chat"].get("type") != "private":
            return
        text = msg.get("text", "")
        if not text.startswith("/"):
            return

        cmd = text.split()[0].split("@")[0].lower()
        user = msg["from"]
        chat_id = msg["chat"]["id"]
        is_admin = user["id"] in ADMIN_IDS

        if cmd == "/start":
            if self.db:
                try:
                    await self.db.upsert_user(user, started=True)
                except Exception:
                    log.exception("Foydalanuvchini saqlashda xato")
            name = html.escape(user.get("first_name", "Do'stim"))
            await self.send(chat_id, START_TEXT.format(name=name))

        elif cmd == "/stats" and is_admin:
            await self.send_stats(chat_id)

        elif cmd == "/broadcast" and is_admin:
            body = text.partition(" ")[2].strip()
            if not body:
                await self.send(
                    chat_id,
                    "Foydalanish: <code>/broadcast xabar matni</code>\n"
                    "HTML ishlaydi: &lt;b&gt;qalin&lt;/b&gt;",
                )
            else:
                self.spawn(self.broadcast(chat_id, body))

    async def send_stats(self, chat_id: int):
        if not self.db:
            await self.send(chat_id, "⚠️ Baza ulanmagan (DATABASE_URL kiritilmagan).")
            return
        try:
            s = await self.db.stats()
        except Exception:
            log.exception("Statistika olishda xato")
            await self.send(chat_id, "⚠️ Statistikani olib bo'lmadi, loglarni tekshiring.")
            return

        j, u = s["joins"], s["users"]
        lines = [
            "📊 <b>Statistika</b>",
            "",
            f"✅ Jami tasdiqlangan so'rovlar: <b>{j['total']}</b>",
            f"📅 Bugun: <b>{j['today']}</b>",
            f"🗓 Oxirgi 7 kun: <b>{j['week']}</b>",
            f"👤 Noyob foydalanuvchilar: <b>{j['unique_users']}</b>",
            "",
            f"🤖 Botga /start bosganlar: <b>{u['active']}</b> faol (jami bazada {u['total']})",
        ]
        if s["chats"]:
            lines += ["", "🏆 <b>Eng ko'p so'rov kelgan kanallar:</b>"]
            lines += [f"• {html.escape(str(c['name']))}: {c['n']}" for c in s["chats"]]
        await self.send(chat_id, "\n".join(lines))

    async def broadcast(self, admin_chat: int, text: str):
        if not self.db:
            await self.send(admin_chat, "⚠️ Baza ulanmagan, xabar yuborib bo'lmaydi.")
            return
        if self.broadcasting:
            await self.send(admin_chat, "⏳ Boshqa xabar yuborilmoqda, tugashini kuting.")
            return

        self.broadcasting = True
        try:
            targets = await self.db.broadcast_targets()
            await self.send(admin_chat, f"📤 Yuborish boshlandi: {len(targets)} ta foydalanuvchi.")
            ok = blocked = failed = 0
            for uid in targets:
                data = await self.request(
                    "sendMessage", chat_id=uid, text=text, parse_mode="HTML"
                )
                if data.get("ok"):
                    ok += 1
                elif data.get("error_code") == 403:
                    blocked += 1
                    await self.db.mark_blocked(uid)
                else:
                    failed += 1
                await asyncio.sleep(BROADCAST_DELAY)
            await self.send(
                admin_chat,
                f"✅ Tugadi.\nYetkazildi: <b>{ok}</b>\nBotni bloklaganlar: {blocked}\nXato: {failed}",
            )
        except Exception:
            log.exception("Broadcast xatosi")
            await self.send(admin_chat, "⚠️ Yuborishda xatolik yuz berdi.")
        finally:
            self.broadcasting = False

    def dispatch(self, update: dict):
        if "chat_join_request" in update:
            self.spawn(self.on_join_request(update["chat_join_request"]))
        elif "message" in update:
            self.spawn(self.on_message(update["message"]))

    # ---------- Polling ----------

    async def poll(self):
        # Ilgari webhook o'rnatilgan bo'lsa, getUpdates 409 bilan ishlamaydi
        await self.call("deleteWebhook", drop_pending_updates=False)

        offset = 0
        log.info("Polling boshlandi")
        while True:
            data = await self.request(
                "getUpdates",
                http_timeout=35,
                retries=1,
                offset=offset,
                timeout=25,  # Telegram long polling (soniya)
                allowed_updates=["message", "chat_join_request"],
            )
            if not data.get("ok"):
                code = data.get("error_code")
                if code == 401:
                    log.critical("Token noto'g'ri (401). Bot to'xtatilmoqda.")
                    self.stop.set()
                    return
                if code == 409:
                    log.error("Conflict: bu bot boshqa joyda ham ishlayapti. Keraksizini o'chiring.")
                    await asyncio.sleep(5)
                else:
                    await asyncio.sleep(2)
                continue

            for update in data["result"]:
                offset = update["update_id"] + 1
                self.dispatch(update)


# ------------------------------------------------------------------
# Ishga tushirish
# ------------------------------------------------------------------
async def health(_request):
    return web.Response(text="OK")


async def main():
    stop = asyncio.Event()

    db: Database | None = None
    if DATABASE_URL:
        candidate = Database(DATABASE_URL)
        try:
            await candidate.connect()
            db = candidate
            log.info("Bazaga ulandi")
        except Exception:
            log.exception("Bazaga ulanib bo'lmadi, bot bazasiz ishlaydi")
    else:
        log.warning("DATABASE_URL yo'q: statistika va broadcast ishlamaydi")

    if not ADMIN_IDS:
        log.warning("ADMIN_IDS yo'q: /stats va /broadcast hech kimga ochiq emas")

    bot = TelegramBot(BOT_TOKEN, db, stop)

    # Health-check server (Render port talabi va UptimeRobot uchun)
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Health server %s portda ishga tushdi", PORT)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    async with aiohttp.ClientSession() as session:
        bot.session = session
        poll_task = asyncio.create_task(bot.poll())
        await stop.wait()

        log.info("To'xtatilmoqda...")
        poll_task.cancel()
        if bot.tasks:
            await asyncio.gather(*bot.tasks, return_exceptions=True)

    if db:
        await db.close()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
