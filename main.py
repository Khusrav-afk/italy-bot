"""
Точка входа: запускает FastAPI-сервер, который обслуживает все каналы через вебхуки
- Telegram (aiogram, POST /telegram)
- Instagram Direct (POST /instagram)
- WhatsApp Cloud API (POST /whatsapp)

Раньше Telegram работал через long polling (dp.start_polling), и на Render это
приводило к TelegramConflictError при каждом деплое: пока новый инстанс уже
стартовал, а старый ещё не остановился, оба одновременно опрашивали Telegram
(getUpdates), а Telegram допускает только одно активное подключение на бота.
Вебхук убирает эту гонку - Telegram сам шлёт апдейты на HTTP-эндпоинт, регистрация
вебхука происходит при старте FastAPI (см. webhook_server.py).

Добавление нового канала: просто добавь маршрут в webhook_server.py - main.py
менять не нужно, он автоматически подхватит новый маршрут.
"""
import asyncio
import os
import logging

import uvicorn

logging.basicConfig(level=logging.INFO)


async def run_webhook():
    """Запускает FastAPI сервер (Telegram + Instagram + WhatsApp, всё через вебхуки)."""
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
        logging.info("Сервер запускается на порту %s (Telegram + Instagram + WhatsApp)...", port)
        await server.serve()
    except Exception:
        logging.exception("Сервер упал, перезапускаем через 5 сек...")
        await asyncio.sleep(5)
        await run_webhook()


if __name__ == "__main__":
    asyncio.run(run_webhook())
