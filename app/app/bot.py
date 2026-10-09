import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from dotenv import load_dotenv

from app.links import classify_social_url, is_https_url

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

dp = Dispatcher()

HELP_TEXT = """
📥 MEDIA HELPER BOT

Instagram, YouTube va X havolalarini yuboring.

Bot hozircha havolalarni aniqlaydi va tegishli
platforma uchun ma'lumot beradi.

Shaxsiy login, parol yoki API tokenlaringizni
botga yubormang.
"""


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Salom! Media Helper Bot ishga tushdi.\n"
        + HELP_TEXT
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(HELP_TEXT)


@dp.message()
async def handle_message(message: Message):
    text = (message.text or "").strip()

    if not text:
        await message.answer("Iltimos, HTTPS havola yuboring.")
        return

    if len(text) > 2048:
        await message.answer("Havola juda uzun.")
        return

    # Faqat bitta havolani qabul qilamiz.
    if len(text.split()) != 1:
        await message.answer(
            "Iltimos, bitta havolani yuboring."
        )
        return

    if not is_https_url(text):
        await message.answer(
            "Faqat HTTPS havolalar qabul qilinadi."
        )
        return

    platform = classify_social_url(text)

    if platform == "instagram":
        await message.answer(
            "📸 Instagram havolasi aniqlandi.\n\n"
            "Media olish uchun rasmiy API va "
            "tegishli ruxsatlar talab qilinishi mumkin."
        )

    elif platform == "youtube":
        await message.answer(
            "▶️ YouTube havolasi aniqlandi.\n\n"
            "Videoni platformaning rasmiy imkoniyatlari "
            "orqali oching yoki boshqaring."
        )

    elif platform == "x":
        await message.answer(
            "𝕏 X (Twitter) havolasi aniqlandi.\n\n"
            "Media bilan ishlash mavjud API ruxsatlari "
            "va kontent huquqlariga bog‘liq."
        )

    else:
        await message.answer(
            "Bu havola qo‘llab-quvvatlanadigan "
            "ijtimoiy tarmoqlardan emas."
        )


async def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN maxfiy o‘zgaruvchisi sozlanmagan."
        )

    bot = Bot(token=BOT_TOKEN)

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
