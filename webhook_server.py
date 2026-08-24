"""
Вебхук-сервер для Instagram Direct + WhatsApp Cloud API.

Переиспользует "мозг" из bot.py: get_content (база знаний из Google Sheets),
classify (классификатор -> субагент), build_prompt (сборка системного промпта),
sanitize, разбор [LEAD]/[BOOKING]. Telegram-бот (bot.py) при этом не трогается -
это отдельный процесс/файл, который слушает входящие от Meta по HTTP.

Follow-up (дожим молчащих клиентов) подключён через followups.py: планируется при
входящем сообщении клиента, отменяется когда клиент/Наталья отвечают. Для WhatsApp
учитывается 24-часовое сервисное окно (вне окна follow-up не шлётся).

Каналы разведены по путям:
    /instagram  - Instagram Direct
    /whatsapp   - WhatsApp Cloud API (Coexistence)

Запуск:
    pip install fastapi uvicorn httpx apscheduler
    uvicorn webhook_server:app --host 0.0.0.0 --port 8000
"""

import hashlib
import hmac
import json
import logging
import os

import httpx
from aiogram.types import Update
from fastapi import FastAPI, Request, Response

# --- переиспользуем мозг из bot.py (Telegram-бот через вебхук, не через polling) ---
from bot import (
    get_content, classify, build_prompt, detect_excursion_city, sanitize, claude, MODEL,
    AGENTS, LEAD_RE, BOOKING_RE, CLOSE_RE, muted_chats, WORDS,
    USE_WEB_SEARCH, WEB_SEARCH_TOOL, CALENDAR_TZ, bot, dp, BOT_ENABLED,
    save_lead, handle_booking,
)
from followups import followups   # <-- follow-up цепочка

logging.basicConfig(level=logging.INFO)

# ========================= Instagram =========================

IG_VERIFY_TOKEN = os.getenv("IG_VERIFY_TOKEN", "italy_verify_123")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "")
IG_APP_SECRET = os.getenv("IG_APP_SECRET", "")
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v21.0")
GRAPH_URL = f"https://graph.instagram.com/{GRAPH_VERSION}"


# Несколько Instagram-аккаунтов: карта "id аккаунта -> токен".
# Формат в .env: IG_TOKENS=17841400931469869:TOKEN1,17841411843707162:TOKEN2
# Если IG_TOKENS пуст - используется один IG_ACCESS_TOKEN (для любого аккаунта).
def _parse_ig_tokens() -> dict:
    raw = os.getenv("IG_TOKENS", "").strip()
    tokens = {}
    if raw:
        for pair in raw.split(","):
            if ":" in pair:
                acc_id, tok = pair.split(":", 1)
                tokens[acc_id.strip()] = tok.strip()
    return tokens


IG_TOKENS = _parse_ig_tokens()


def _token_for(account_id: str) -> str:
    """Токен для конкретного аккаунта-получателя; фолбэк на общий IG_ACCESS_TOKEN."""
    if account_id and account_id in IG_TOKENS:
        return IG_TOKENS[account_id]
    return IG_ACCESS_TOKEN


# ========================= Telegram (webhook) =========================

# Секрет для проверки заголовка X-Telegram-Bot-Api-Secret-Token у входящих запросов
# от Telegram. Свой, per-bot, детерминированный от BOT_TOKEN - отдельная переменная
# окружения на Render не нужна.
TELEGRAM_WEBHOOK_SECRET = hashlib.sha256(f"tg-webhook:{bot.token}".encode()).hexdigest()
TELEGRAM_WEBHOOK_PATH = "/telegram"


# ========================= WhatsApp Cloud API =========================

WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "italy_wa_verify_123")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
# Тот же секрет приложения, что у Instagram (один Meta-app). На тесте можно пустым.
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "") or IG_APP_SECRET
# ВНИМАНИЕ: WhatsApp Cloud API живёт на graph.facebook.com, а НЕ на graph.instagram.com
WA_GRAPH_URL = f"https://graph.facebook.com/{GRAPH_VERSION}"


app = FastAPI()

# Память диалогов. Ключи разные (IG id vs wa_id клиента), поэтому храним раздельно.
ig_sessions: dict[str, dict] = {}
wa_sessions: dict[str, dict] = {}

# Карта "wa_id клиента -> phone_number_id", через который пришло его сообщение.
# Нужна, чтобы follow-up знал, с какого номера отправлять ответ этому клиенту.
_wa_pnid_by_user: dict[str, str] = {}

# ID уже обработанных сообщений (IG mid / WA id) - Meta может повторно доставить один и тот
# же вебхук (retry при таймауте/сбое), без этого клиент получал бы задвоенные ответы.
_seen_msg_ids: set[str] = set()


def _already_seen(msg_id: str | None) -> bool:
    if not msg_id:
        return False
    if msg_id in _seen_msg_ids:
        return True
    _seen_msg_ids.add(msg_id)
    return False


# ---------- Follow-up: единая отправка для Telegram, IG и WA ----------
async def _fu_send(chat_key: str, text: str):
    """Как follow-up отправляет сообщение. chat_key: 'tg:<chat_id>' / 'ig:<id>' / 'wa:<wa_id>'."""
    try:
        channel, uid = chat_key.split(":", 1)
    except ValueError:
        return

    if channel == "tg":
        try:
            chat_id = int(uid)
        except ValueError:
            return
        if chat_id in muted_chats:   # muted_chats для Telegram хранит int chat_id
            return
        await bot.send_message(chat_id, sanitize(text))
        return

    if uid in muted_chats:          # Наталья вмешалась вручную - молчим
        return
    if channel == "ig":
        await send_ig_message(uid, sanitize(text))
    elif channel == "wa":
        pnid = _wa_pnid_by_user.get(uid, WHATSAPP_PHONE_NUMBER_ID)
        await send_wa_message(uid, sanitize(text), pnid)


@app.on_event("startup")
async def _start_followups():
    followups.configure(send_func=_fu_send, tz=CALENDAR_TZ)
    await followups.start()
    await get_content()   # подтянуть тексты follow-up из таблицы (если заданы)


@app.on_event("startup")
async def _start_telegram_webhook():
    """Регистрирует Telegram webhook вместо polling - убирает TelegramConflictError
    при редеплое на Render (раньше старый и новый инстансы одновременно опрашивали
    getUpdates, теперь апдейты просто шлются на HTTP-эндпоинт этого сервиса)."""
    base_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_BASE_URL", "")
    if not base_url:
        logging.warning(
            "RENDER_EXTERNAL_URL/WEBHOOK_BASE_URL не заданы - Telegram webhook не "
            "настроен (ожидаемо при локальном запуске)."
        )
        return
    url = base_url.rstrip("/") + TELEGRAM_WEBHOOK_PATH
    await bot.set_webhook(
        url=url,
        secret_token=TELEGRAM_WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types(),
    )
    logging.info("Telegram webhook установлен: %s", url)


@app.post(TELEGRAM_WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TELEGRAM_WEBHOOK_SECRET:
        return Response(status_code=403)
    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return Response(status_code=200)


# ------------------------------- Instagram: маршруты -------------------------------

@app.get("/instagram")
async def verify(request: Request):
    """Проверка вебхука: Meta присылает hub.challenge - возвращаем его обратно."""
    params = request.query_params
    if (params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == IG_VERIFY_TOKEN):
        logging.info("Instagram webhook verified")
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    return Response(content="forbidden", status_code=403)


def _valid_signature(app_secret: str, body: bytes, header: str) -> bool:
    """Проверка подписи X-Hub-Signature-256 (если задан секрет)."""
    if not app_secret:
        return True  # на тесте можно без проверки
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


@app.post("/instagram")
async def incoming(request: Request):
    raw = await request.body()
    if not _valid_signature(IG_APP_SECRET, raw, request.headers.get("X-Hub-Signature-256", "")):
        return Response(content="bad signature", status_code=403)

    data = json.loads(raw or b"{}")
    # Структура Instagram: { "object": "instagram", "entry": [ { "id": "<account_id>", "messaging": [ {...} ] } ] }
    for entry in data.get("entry", []):
        account_id = entry.get("id")  # ID бизнес-аккаунта, на который пришло сообщение
        for ev in entry.get("messaging", []):
            await _handle_event(ev, account_id)
    return Response(content="EVENT_RECEIVED", media_type="text/plain")


async def _handle_event(ev: dict, account_id: str = ""):
    if not BOT_ENABLED:
        return
    msg = ev.get("message") or {}
    if _already_seen(msg.get("mid")):
        logging.info("IG: дубликат сообщения %s, пропускаю", msg.get("mid"))
        return
    # Эхо: сообщение, отправленное самим бизнес-аккаунтом (Наталья пишет вручную с телефона).
    if msg.get("is_echo"):
        recipient = (ev.get("recipient") or {}).get("id")
        text = (msg.get("text") or "").strip().lower()
        if recipient and WORDS["stop"] in text:
            muted_chats.add(recipient)
            followups.cancel(f"ig:{recipient}")          # стоп-слово - снять follow-up
            logging.info("IG стоп-слово: бот замолчал в чате с %s", recipient)
        elif recipient and WORDS["resume"] in text:
            muted_chats.discard(recipient)
            logging.info("IG стоп-слово снято - бот снова отвечает в чате с %s", recipient)
        elif recipient:
            followups.cancel(f"ig:{recipient}")          # Наталья ответила сама - не дожимаем
            logging.info("IG: ручной ответ Натальи в чате с %s", recipient)
        return
    sender = (ev.get("sender") or {}).get("id")
    text = msg.get("text")
    if not sender or not text:
        return
    # Если чат заглушён стоп-словом - бот молчит.
    if sender in muted_chats:
        logging.info("IG: чат с %s заглушён, пропускаю", sender)
        return

    followups.on_incoming(f"ig:{sender}")                # клиент написал - (пере)планируем

    reply = await think(sender, text, ig_sessions, "ig")
    if reply:
        await send_ig_message(sender, reply, account_id)


# ------------------------------- WhatsApp: маршруты -------------------------------

@app.get("/whatsapp")
async def wa_verify(request: Request):
    """Проверка вебхука WhatsApp (свой verify-token, отдельно от Instagram)."""
    params = request.query_params
    if (params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == WHATSAPP_VERIFY_TOKEN):
        logging.info("WhatsApp webhook verified")
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    return Response(content="forbidden", status_code=403)


@app.post("/whatsapp")
async def wa_incoming(request: Request):
    raw = await request.body()
    if not _valid_signature(WHATSAPP_APP_SECRET, raw, request.headers.get("X-Hub-Signature-256", "")):
        return Response(content="bad signature", status_code=403)

    data = json.loads(raw or b"{}")
    # Структура WhatsApp:
    # { "object": "whatsapp_business_account",
    #   "entry": [ { "id": "<WABA_ID>",
    #               "changes": [ { "field": "messages", "value": { ... } } ] } ] }
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            field = change.get("field")
            value = change.get("value") or {}
            phone_number_id = (value.get("metadata") or {}).get("phone_number_id", "")

            if field == "messages":
                # value.messages[] - входящие от клиентов; value.statuses[] - статусы доставки (игнор)
                for m in value.get("messages", []):
                    await _handle_wa_message(m, phone_number_id)

            elif field == "smb_message_echoes":
                # Coexistence: Наталья ответила ВРУЧНУЮ из приложения WhatsApp Business.
                for m in value.get("message_echoes", []):
                    await _handle_wa_echo(m)

            # Прочие поля Coexistence (history, smb_app_state_sync) пока пропускаем.
    return Response(content="EVENT_RECEIVED", media_type="text/plain")


async def _handle_wa_message(m: dict, phone_number_id: str = ""):
    if not BOT_ENABLED:
        return
    if _already_seen(m.get("id")):
        logging.info("WA: дубликат сообщения %s, пропускаю", m.get("id"))
        return
    # Пока обрабатываем только текст. Медиа/документы - задел на будущее.
    if m.get("type") != "text":
        logging.info("WA: не-текстовое сообщение type=%s, пропускаю", m.get("type"))
        return
    sender = m.get("from")  # wa_id клиента (номер в межд. формате без +)
    text = (m.get("text") or {}).get("body")
    if not sender or not text:
        return
    if sender in muted_chats:
        logging.info("WA: чат с %s заглушён, пропускаю", sender)
        return

    if phone_number_id:
        _wa_pnid_by_user[sender] = phone_number_id       # запомнить, с какого номера отвечать
    followups.on_incoming(f"wa:{sender}", is_whatsapp=True)   # клиент написал - планируем (с 24ч окном)

    reply = await think(sender, text, wa_sessions, "wa")
    if reply:
        await send_wa_message(sender, reply, phone_number_id)


async def _handle_wa_echo(m: dict):
    """Эхо ручного ответа Натальи из приложения (Coexistence): гасим бота в этом чате."""
    if _already_seen(m.get("id")):
        logging.info("WA echo: дубликат сообщения %s, пропускаю", m.get("id"))
        return
    customer = m.get("to")  # с кем Наталья переписывается
    text = ""
    if m.get("type") == "text":
        text = ((m.get("text") or {}).get("body") or "").strip().lower()
    if customer and WORDS["stop"] in text:
        muted_chats.add(customer)
        followups.cancel(f"wa:{customer}")               # стоп-слово - снять follow-up
        logging.info("WA стоп-слово: бот замолчал в чате с %s", customer)
    elif customer and WORDS["resume"] in text:
        muted_chats.discard(customer)
        logging.info("WA стоп-слово снято - бот снова отвечает в чате с %s", customer)
    elif customer:
        followups.cancel(f"wa:{customer}")               # Наталья ответила сама - не дожимаем
        logging.info("WA: ручной ответ Натальи в чате с %s", customer)


# ------------------------------- Общий "мозг" -------------------------------

async def think(user_id: str, text: str, sessions: dict | None = None, channel: str = "ig") -> str:
    """Тот же мозг, что и в Telegram: классификатор -> субагент -> ответ."""
    if sessions is None:
        sessions = ig_sessions
    s = sessions.setdefault(user_id, {"history": [], "agent": None, "segment": None})
    s["history"].append({"role": "user", "content": text})
    if len(s["history"]) > 40:
        del s["history"][:-40]

    settings, faq, kb, sub = await get_content()
    prev = s["agent"]
    agent, segment = await classify(s["history"], prev)
    s["agent"], s["segment"] = agent, segment
    logging.info("Маршрут: %s | сегмент: %s", AGENTS.get(agent, agent), segment)

    excursion_city = detect_excursion_city(s["history"]) if agent == "excursions" else None
    system_prompt = build_prompt(agent, segment, settings, faq, kb, sub, excursion_city)
    try:
        kwargs = dict(model=MODEL, max_tokens=1024, system=system_prompt, messages=s["history"])
        if USE_WEB_SEARCH:
            kwargs["tools"] = WEB_SEARCH_TOOL
        resp = await claude.messages.create(**kwargs)
    except Exception:
        logging.exception("Claude API error")
        return "Секунду, уточню детали и вернусь."

    reply = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    s["history"].append({"role": "assistant", "content": reply})

    # Лиды/брони: вырезаем служебные блоки из текста клиента и сохраняем (таблица + менеджеру).
    m = LEAD_RE.search(reply)
    if m:
        raw = m.group(1).strip()
        try:
            lead = json.loads(raw)
        except json.JSONDecodeError:
            lead = {"summary": raw}
        lead["agent"] = AGENTS.get(agent, agent)
        lead["segment"] = segment
        label = "Instagram" if channel == "ig" else "WhatsApp"
        await save_lead(lead, f"{label}: {user_id}")
        followups.cancel(f"{channel}:{user_id}")   # заявка собрана и передана Наталье - не дожимаем клиента
        reply = LEAD_RE.sub("", reply).strip()
    b = BOOKING_RE.search(reply)
    if b:
        book_source = f"{'Instagram' if channel == 'ig' else 'WhatsApp'}: {user_id}"
        await handle_booking(b.group(1).strip(), source=book_source)
        reply = BOOKING_RE.sub("", reply).strip()
    if CLOSE_RE.search(reply):
        followups.cancel(f"{channel}:{user_id}")   # клиент явно попрощался - не напоминаем
        reply = CLOSE_RE.sub("", reply).strip()

    return sanitize(reply)


# ------------------------------- Отправка: Instagram -------------------------------

async def send_ig_message(recipient_id: str, text: str, account_id: str = ""):
    """Отправка ответа в Instagram Direct через Graph API (токеном нужного аккаунта)."""
    token = _token_for(account_id)
    if not token:
        logging.error("Нет токена для аккаунта %s - не могу отправить ответ", account_id)
        return
    url = f"{GRAPH_URL}/me/messages"
    payload = {"recipient": {"id": recipient_id}, "message": {"text": text}}
    params = {"access_token": token}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, params=params, json=payload)
        if r.status_code >= 400:
            logging.error("IG send error %s: %s", r.status_code, r.text)


# ------------------------------- Отправка: WhatsApp -------------------------------

async def send_wa_message(to_wa_id: str, text: str, phone_number_id: str = ""):
    """Текстовый ответ в WhatsApp Cloud API.

    Работает бесплатно внутри 24ч после сообщения клиента (сервисное окно).
    """
    token = WHATSAPP_TOKEN
    pnid = phone_number_id or WHATSAPP_PHONE_NUMBER_ID
    if not token or not pnid:
        logging.error("WA: нет WHATSAPP_TOKEN или PHONE_NUMBER_ID - не могу отправить")
        return
    url = f"{WA_GRAPH_URL}/{pnid}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_wa_id,
        "type": "text",
        "text": {"body": text},
    }
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code >= 400:
            logging.error("WA send error %s: %s", r.status_code, r.text)


async def send_wa_document(to_wa_id: str, link: str, filename: str = "guide.pdf",
                           caption: str | None = None, phone_number_id: str = ""):
    """Отправка PDF по прямой ссылке. Задел под триггер-слова (кодовое слово -> PDF).
    Лимит WhatsApp: PDF до 25 МБ.
    """
    token = WHATSAPP_TOKEN
    pnid = phone_number_id or WHATSAPP_PHONE_NUMBER_ID
    if not token or not pnid:
        logging.error("WA: нет WHATSAPP_TOKEN или PHONE_NUMBER_ID - не могу отправить документ")
        return
    doc = {"link": link, "filename": filename}
    if caption:
        doc["caption"] = caption
    url = f"{WA_GRAPH_URL}/{pnid}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_wa_id,
        "type": "document",
        "document": doc,
    }
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code >= 400:
            logging.error("WA doc send error %s: %s", r.status_code, r.text)


# ------------------------------- Health -------------------------------

@app.get("/")
async def health():
    return {"status": "ok", "channels": ["telegram", "instagram", "whatsapp"]}
