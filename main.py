"""Точка входа: python main.py"""
import asyncio
import logging
import re
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand, LinkPreviewOptions

from app.config import ENV_PATH, load_config
from app.db import Database
from app.handlers import AccessMiddleware, router
from app.poller import Poller
from app.services import Hub

TOKEN_RE = re.compile(r"^\d{5,}:[\w-]{30,}$")


def ask_token_interactively() -> None:
    """Первый запуск на компьютере: спрашиваем токен бота и сохраняем в .env."""
    if not sys.stdin or not sys.stdin.isatty():
        raise SystemExit("Не задан BOT_TOKEN. Укажите его в файле .env или переменных окружения.")
    print("\nПервый запуск. Нужен токен Telegram-бота.")
    print("Получить: откройте @BotFather в Telegram → /newbot → скопируйте токен.\n")
    while True:
        token = input("Вставьте токен бота и нажмите Enter: ").strip()
        if TOKEN_RE.match(token):
            break
        print("Это не похоже на токен (пример: 123456789:AAH...). Попробуйте ещё раз.")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    lines = [ln for ln in lines if not ln.startswith("BOT_TOKEN=")] + [f"BOT_TOKEN={token}"]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Токен сохранён в .env\n")


async def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # эмодзи в консоли Windows
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    cfg = load_config()
    if not cfg.bot_token:
        ask_token_interactively()
        cfg = load_config()

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML, link_preview=LinkPreviewOptions(is_disabled=True)))
    try:
        me = await bot.me()
    except TelegramUnauthorizedError:
        await bot.session.close()
        raise SystemExit("Telegram не принял токен бота. Проверьте BOT_TOKEN в файле .env.")

    db = Database(cfg.db_path)
    await db.connect()
    hub = Hub(bot, db, cfg)
    poller = Poller(hub, cfg.poll_interval)

    dp = Dispatcher()
    router.message.outer_middleware(AccessMiddleware())
    router.callback_query.outer_middleware(AccessMiddleware())
    dp.include_router(router)

    await bot.set_my_commands([BotCommand(command="start", description="Главное меню"),
                               BotCommand(command="help", description="Как пользоваться")])

    logging.info("Бот @%s запущен. Откройте его в Telegram и нажмите Start.", me.username)
    print(f"\n✅ Бот работает: https://t.me/{me.username}\n   Не закрывайте это окно.\n", flush=True)

    poll_task = asyncio.create_task(poller.run())
    try:
        await dp.start_polling(bot, hub=hub, poller=poller)
    finally:
        poll_task.cancel()
        await hub.client.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except SystemExit as e:
        if isinstance(e.code, str):  # ошибка настройки: перезапуск не поможет
            print(f"\n❌ {e.code}", file=sys.stderr)
            sys.exit(2)
        raise
