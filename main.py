import asyncio
import html
import logging
import os
import signal

import aiohttp
from aiohttp import web

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN muhit o'zgaruvchisi topilmadi (Render > Environment).")

PORT = int(os.environ.get("PORT", 10000))
MAX_CONCURRENCY = 30

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


class TelegramBot:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.session: aiohttp.ClientSession | None = None
        self.sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self.tasks: set[asyncio.Task] = set()

    async def call(self, method: str, http_timeout: int = 10, **params):
        """Telegram API so'rovi. 429 va tarmoq xatolarida 3 martagacha qayta urinadi."""
        for attempt in range(1, 4):
            try:
                async with self.session.post(
                    self.base + method,
                    json=params,
                    timeout=aiohttp.ClientTimeout(total=http_timeout),
                ) as resp:
                    data = await resp.json()
                    status = resp.status

                if data.get("ok"):
                    return data["result"]

                if status == 429:
                    wait = data.get("parameters", {}).get("retry_after", 1)
                    log.warning("Flood limit: %s soniya kutiladi", wait)
                    await asyncio.sleep(wait + 0.5)
                    continue

                log.warning("%s xato: %s", method, data.get("description"))
                return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("%s tarmoq xatosi (urinish %d): %s", method, attempt, e)
                await asyncio.sleep(attempt)
        return None

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # ---------- Handlerlar ----------

    async def on_join_request(self, req: dict):
        chat_id = req["chat"]["id"]
        user = req["from"]
        # user_chat_id: foydalanuvchi /start bosmagan bo'lsa ham xabar yuborish imkonini beradi
        user_chat_id = req.get("user_chat_id", user["id"])

        async with self.sem:
            approved = await self.call(
                "approveChatJoinRequest", chat_id=chat_id, user_id=user["id"]
            )
            if approved is None:
                log.error("Tasdiqlanmadi: user=%s chat=%s", user["id"], chat_id)
                return
            log.info("Tasdiqlandi: user=%s chat=%s", user["id"], chat_id)

            await self.call(
                "sendMessage",
                chat_id=user_chat_id,
                text=WELCOME_TEXT,
                parse_mode="HTML",
            )

    async def on_message(self, msg: dict):
        if not msg.get("text", "").startswith("/start"):
            return
        name = html.escape(msg["from"].get("first_name", "Do'stim"))
        async with self.sem:
            await self.call(
                "sendMessage",
                chat_id=msg["chat"]["id"],
                text=START_TEXT.format(name=name),
                parse_mode="HTML",
            )

    def dispatch(self, update: dict):
        if "chat_join_request" in update:
            self.spawn(self.on_join_request(update["chat_join_request"]))
        elif "message" in update:
            self.spawn(self.on_message(update["message"]))

    # ---------- Polling ----------

    async def poll(self):
        # Agar ilgari webhook o'rnatilgan bo'lsa, getUpdates ishlamaydi (409)
        await self.call("deleteWebhook", drop_pending_updates=False)

        offset = 0
        log.info("Polling boshlandi")
        while True:
            updates = await self.call(
                "getUpdates",
                http_timeout=35,
                offset=offset,
                timeout=25,  # Telegram long polling (soniya)
                allowed_updates=["message", "chat_join_request"],
            )
            if updates is None:
                await asyncio.sleep(2)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                self.dispatch(update)


async def health(_request):
    return web.Response(text="OK")


async def main():
    bot = TelegramBot(BOT_TOKEN)

    # Health-check server (Render port talabi uchun)
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Health server %s portda ishga tushdi", PORT)

    stop = asyncio.Event()
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

    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

