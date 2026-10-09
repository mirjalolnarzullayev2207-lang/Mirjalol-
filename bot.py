import asyncio
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path

import yt_dlp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from shazamio import Shazam

TOKEN = os.getenv("BOT_TOKEN", "")
MAX_SIZE = 50 * 1024 * 1024  # Telegram Bot API yuborish limiti

URL_RE = re.compile(
    r"https?://(?:www\.|m\.|vm\.|vt\.|mobile\.)?"
    r"(?:instagram\.com|youtube\.com|youtu\.be|tiktok\.com|twitter\.com|x\.com)/\S+",
    re.I,
)

WELCOME = (
    "Salom men Izlaydibotman!\n\n"
    "✅ Mening xususiyatlarim:\n\n"
    " • Qo'shiq matni, nomi yoki ijrochi ismi orqali musiqa topaman\n"
    " • Instagram, YouTube, TikTok va Twitter (X) dan video yuklab beraman\n"
    " • Ovozli xabar, video yoki audiodagi musiqani topib beraman"
)

router = Router()
LINKS: dict[str, str] = {}  # callback uchun qisqa kalit -> havola


# ---------- yt-dlp yordamchilari (bloklovchi, thread ichida ishlatiladi) ----------
def _download_video(url: str, out: str) -> Path | None:
    opts = {
        "outtmpl": f"{out}/%(id)s.%(ext)s",
        "format": "best[ext=mp4]/bv*+ba/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "max_filesize": MAX_SIZE,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    files = sorted(Path(out).glob("*"), key=lambda p: p.stat().st_size, reverse=True)
    return files[0] if files else None


def _download_audio(url: str, out: str) -> tuple[Path | None, dict]:
    opts = {
        "outtmpl": f"{out}/%(id)s.%(ext)s",
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "max_filesize": MAX_SIZE,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = list(Path(out).glob("*.mp3"))
    return (files[0] if files else None), info or {}


def _search(query: str, n: int = 5) -> list[dict]:
    opts = {"quiet": True, "extract_flat": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    return [e for e in (data or {}).get("entries", []) if e and e.get("id")]


def _fmt_duration(sec) -> str:
    if not sec:
        return ""
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


# ---------- Avto-tasdiqlash ----------
@router.chat_join_request()
async def approve_request(req: ChatJoinRequest, bot: Bot):
    await req.approve()
    try:
        await bot.send_message(req.user_chat_id, "✅ So'rovingiz tasdiqlandi. Xush kelibsiz!")
    except Exception:
        pass  # foydalanuvchi botga yozishni cheklagan bo'lishi mumkin


# ---------- /start ----------
@router.message(CommandStart())
async def start(message: Message):
    await message.answer(WELCOME)


# ---------- Havola bo'yicha video yuklash ----------
@router.message(F.text.regexp(URL_RE))
async def handle_link(message: Message):
    url = URL_RE.search(message.text).group(0)
    status = await message.answer("⏳ Yuklanmoqda...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = await asyncio.to_thread(_download_video, url, tmp)
            if not path or path.stat().st_size > MAX_SIZE:
                await status.edit_text("❌ Video topilmadi yoki 50 MB dan katta.")
                return
            key = uuid.uuid4().hex[:8]
            LINKS[key] = url
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🎵 Musiqasini yuklash", callback_data=f"a:{key}")]]
            )
            try:
                await message.answer_video(FSInputFile(path), reply_markup=kb)
            except Exception:
                await message.answer_document(FSInputFile(path), reply_markup=kb)
        await status.delete()
    except Exception as e:
        logging.exception("video yuklashda xato")
        await status.edit_text("❌ Yuklab bo'lmadi. Havola yopiq (private) yoki qo'llab-quvvatlanmaydi.")


@router.callback_query(F.data.startswith("a:"))
async def audio_from_link(cb: CallbackQuery):
    url = LINKS.get(cb.data[2:])
    if not url:
        await cb.answer("Havola eskirgan, qaytadan yuboring.", show_alert=True)
        return
    await cb.answer("⏳ Musiqa tayyorlanmoqda...")
    await send_audio_for(cb.message, url)


async def send_audio_for(message: Message, url: str, title: str | None = None, performer: str | None = None):
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path, info = await asyncio.to_thread(_download_audio, url, tmp)
            if not path or path.stat().st_size > MAX_SIZE:
                await message.answer("❌ Audio topilmadi yoki 50 MB dan katta.")
                return
            await message.answer_audio(
                FSInputFile(path),
                title=title or info.get("title"),
                performer=performer or info.get("uploader"),
            )
    except Exception:
        logging.exception("audio yuklashda xato")
        await message.answer("❌ Audioni yuklab bo'lmadi.")


# ---------- Ovoz / video / audio ichidagi musiqani aniqlash ----------
@router.message(F.voice | F.audio | F.video | F.video_note)
async def recognize(message: Message, bot: Bot):
    media = message.voice or message.audio or message.video or message.video_note
    status = await message.answer("🔎 Musiqa aniqlanmoqda...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input"
            await bot.download(media, destination=src)  # Bot API: 20 MB gacha
            result = await Shazam().recognize(str(src))
        track = result.get("track")
        if not track:
            await status.edit_text("❌ Musiqa topilmadi.")
            return
        title, artist = track["title"], track["subtitle"]
        await status.edit_text(f"🎵 {artist} — {title}\n⏳ To'liq versiyasi yuklanmoqda...")
        found = await asyncio.to_thread(_search, f"{artist} {title}", 1)
        if found:
            await send_audio_for(
                message, f"https://www.youtube.com/watch?v={found[0]['id']}", title, artist
            )
        await status.delete()
    except Exception:
        logging.exception("aniqlashda xato")
        await status.edit_text("❌ Aniqlab bo'lmadi (fayl 20 MB dan katta bo'lishi mumkin).")


# ---------- Matn bo'yicha qidirish (nomi, ijrochi, qo'shiq matni) ----------
@router.message(F.text & ~F.text.startswith("/"))
async def search_music(message: Message):
    status = await message.answer("🔎 Qidirilmoqda...")
    try:
        results = await asyncio.to_thread(_search, message.text, 5)
    except Exception:
        results = []
    if not results:
        await status.edit_text("❌ Hech narsa topilmadi.")
        return
    rows = [
        [
            InlineKeyboardButton(
                text=f"{r.get('title', '?')[:48]} {_fmt_duration(r.get('duration'))}".strip(),
                callback_data=f"s:{r['id']}",
            )
        ]
        for r in results
    ]
    await status.edit_text("Natijalar:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("s:"))
async def pick_result(cb: CallbackQuery):
    await cb.answer("⏳ Yuklanmoqda...")
    await send_audio_for(cb.message, f"https://www.youtube.com/watch?v={cb.data[2:]}")


async def main():
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise SystemExit("BOT_TOKEN o'rnatilmagan")
    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
