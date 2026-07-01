"""
Точка входа: запускает одновременно
- FastAPI webhook-сервер (Instagram + WhatsApp) на порту $PORT
- Telegram-бот (aiogram polling)

Добавление WhatsApp: просто добавь маршрут /whatsapp в webhook_server.py —
main.py менять не нужно, он автоматически подхватит новый маршрут.
"""

import asyncio
import os
import logging

import uvicorn

logging.basicConfig(level=logging.INFO)


async def run_telegram():
    """Запускает Telegram-бот через polling."""
    try:
        from bot import bot, dp
        logging.info("Telegram-бот запускается...")
        await dp.start_polling(bot)
    except Exception:
        logging.exception("Telegram-бот упал, перезапускаем через 5 сек...")
        await asyncio.sleep(5)
        await run_telegram()


async def run_webhook():
    """Запускает FastAPI сервер (Instagram + WhatsApp + любые новые каналы)."""
    try:
        from webhook_server import app
        port = int(os.getenv("PORT", 8000))
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_level="info",
            access_log=True,
        )
        server = uvicorn.Server(config)
        logging.info("Webhook-сервер запускается на порту %s (Instagram + WhatsApp)...", port)
        await server.serve()
    except Exception:
        logging.exception("Webhook-сервер упал, перезапускаем через 5 сек...")
        await asyncio.sleep(5)
        await run_webhook()


async def main():
    logging.info("Запуск всех каналов: Telegram + Instagram + WhatsApp (webhook)")
    await asyncio.gather(
        run_telegram(),
        run_webhook(),
    )


if __name__ == "__main__":
    asyncio.run(main())
