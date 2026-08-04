"""
Follow-up (дожим молчащих клиентов).

Если клиент замолчал (не ответил), бот сам напоминает о себе по расписанию:
    1) через 7 минут после последнего сообщения клиента,
    2) через 3 часа,
    3) через 5 часов,
    4) через 20 часов.
Цель — не потерять лид.

Работает для всех каналов (Telegram / Instagram / WhatsApp): канал не знает деталей
follow-up, он только даёт функцию отправки. Планировщик — APScheduler в памяти процесса.

ВАЖНО про WhatsApp: free-form сообщение можно слать только в течение 24 часов после
последнего сообщения клиента (сервисное окно). Поэтому перед отправкой проверяем окно;
если оно закрыто — follow-up тихо пропускается (для WhatsApp). Для Telegram/Instagram
окна нет, шлём всегда.

ОГРАНИЧЕНИЕ (осознанное, т.к. без БД): состояние в памяти. При перезапуске процесса
(Render засыпает на free-плане) запланированные задачи теряются. Для +7 мин это почти
всегда не проблема; для +3ч/+5ч/+20ч — возможен пропуск после сна. Если понадобится
надёжность — вынести планировщик в персистентный jobstore (SQLAlchemy/Redis).

Подключение (см.低 в конце файла — примеры для bot.py и webhook_server.py):
    from followups import followups
    followups.configure(send_func=<async функция отправки>, tz="Europe/Rome")
    await followups.start()
    ...
    followups.on_incoming(chat_key)          # клиент написал -> (пере)планируем
    followups.cancel(chat_key)               # клиент ответил / Наталья вмешалась -> отмена
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Тексты по умолчанию (заглушки). Наталья заменит через таблицу/настройки позже.
DEFAULT_MESSAGES = {
    "m1": "Жду ваш ответ",
    "m2": "Добрый день, какие остались вопросы?",
    "m3": "Здравствуйте, какие остались вопросы?",
    "m4": "Если вам еще нужны наши услуги, поставьте +",
}

# 24ч-окно WhatsApp: сколько секунд free-form сообщения разрешены после сообщения клиента.
WA_SERVICE_WINDOW_SEC = 24 * 60 * 60


class FollowupManager:
    def __init__(self):
        self._scheduler = None
        self._tz = ZoneInfo("Europe/Rome")
        # send_func(chat_key, text) -> Awaitable; отправляет сообщение в нужный канал.
        self._send_func: Optional[Callable[[str, str], Awaitable]] = None
        # last_seen[chat_key] = datetime последнего сообщения клиента (для 24ч-окна WhatsApp).
        self._last_seen: dict[str, datetime] = {}
        # какие chat_key относятся к WhatsApp (для них проверяем 24ч-окно).
        self._wa_keys: set[str] = set()
        self._messages = dict(DEFAULT_MESSAGES)
        self._enabled = True

    def configure(
        self,
        send_func: Callable[[str, str], Awaitable],
        tz: str = "Europe/Rome",
        messages: Optional[dict] = None,
        enabled: bool = True,
    ):
        """Обязательно вызвать один раз до start().
        send_func — async(chat_key, text): как канал отправляет сообщение клиенту.
        """
        self._send_func = send_func
        self._tz = ZoneInfo(tz)
        if messages:
            self._messages.update({k: v for k, v in messages.items() if v})
        self._enabled = enabled

    def set_messages(self, messages: dict):
        """Обновить тексты на лету (например, после перечитывания таблицы)."""
        self._messages.update({k: v for k, v in messages.items() if v})

    async def start(self):
        if not self._enabled:
            logger.info("Follow-up отключён (enabled=False)")
            return
        if self._send_func is None:
            raise RuntimeError("followups.configure(send_func=...) не вызван до start()")
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        self._scheduler = AsyncIOScheduler(timezone=self._tz)
        self._scheduler.start()
        logger.info("Follow-up планировщик запущен (tz=%s)", self._tz)

    def _job_id(self, chat_key: str, stage: str) -> str:
        return f"fu:{chat_key}:{stage}"

    def on_incoming(self, chat_key: str, is_whatsapp: bool = False):
        """Клиент прислал сообщение -> отменяем старые follow-up и планируем новые.
        Вызывать при КАЖДОМ входящем сообщении клиента (не эхо Натальи)."""
        if not self._enabled or self._scheduler is None:
            return
        now = datetime.now(self._tz)
        self._last_seen[chat_key] = now
        if is_whatsapp:
            self._wa_keys.add(chat_key)

        # Сбрасываем прежние задачи этого чата (клиент активен -> счётчик молчания заново).
        self.cancel(chat_key, forget_last_seen=False)

        # 1) через 7 минут
        self._schedule_at(chat_key, "m1", now + timedelta(minutes=7))
        # 2) через 3 часа
        self._schedule_at(chat_key, "m2", now + timedelta(hours=3))
        # 3) через 5 часов
        self._schedule_at(chat_key, "m3", now + timedelta(hours=5))
        # 4) через 20 часов
        self._schedule_at(chat_key, "m4", now + timedelta(hours=20))

    def _schedule_at(self, chat_key: str, stage: str, run_dt: datetime):
        from apscheduler.triggers.date import DateTrigger
        self._scheduler.add_job(
            self._fire,
            trigger=DateTrigger(run_date=run_dt),
            id=self._job_id(chat_key, stage),
            args=[chat_key, stage],
            replace_existing=True,
            misfire_grace_time=300,  # если процесс подтормозил — допускаем запуск в пределах 5 мин
        )

    def cancel(self, chat_key: str, forget_last_seen: bool = True):
        """Отменить все follow-up этого чата (клиент ответил / Наталья вмешалась)."""
        if self._scheduler is None:
            return
        for stage in ("m1", "m2", "m3", "m4"):
            job = self._scheduler.get_job(self._job_id(chat_key, stage))
            if job:
                job.remove()
        if forget_last_seen:
            self._last_seen.pop(chat_key, None)
            self._wa_keys.discard(chat_key)

    async def _fire(self, chat_key: str, stage: str):
        """Сработал таймер follow-up конкретной стадии."""
        text = self._messages.get(stage)
        if not text:
            return
        # WhatsApp: проверяем 24ч сервисное окно.
        if chat_key in self._wa_keys:
            last = self._last_seen.get(chat_key)
            if last is None:
                return
            age = (datetime.now(self._tz) - last).total_seconds()
            if age > WA_SERVICE_WINDOW_SEC:
                logger.info("WA follow-up %s для %s пропущен: вне 24ч окна", stage, chat_key)
                return
        try:
            await self._send_func(chat_key, text)
            logger.info("Follow-up %s отправлен в %s", stage, chat_key)
        except Exception:
            logger.exception("Follow-up %s для %s: ошибка отправки", stage, chat_key)


# Единый экземпляр на процесс.
followups = FollowupManager()
