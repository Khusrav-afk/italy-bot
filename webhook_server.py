"""
Вебхук-сервер для Instagram Direct (и задел под WhatsApp).

Переиспользует "мозг" из bot.py: get_content (база знаний из Google Sheets),
classify (классификатор -> субагент), build_prompt (сборка системного промпта),
sanitize, разбор [LEAD]/[BOOKING]. Telegram-бот (bot.py) при этом не трогается -
это отдельный процесс/файл, который слушает входящие от Meta по HTTP.

Запуск:
    pip install fastapi uvicorn httpx
    uvicorn webhook_server:app --host 0.0.0.0 --port 8000
Затем ngrok:
    ngrok http 8000
URL вида https://xxxx.ngrok.io/instagram вписываешь в Meta -> Webhooks.

Переменные окружения (в .env рядом):
    IG_VERIFY_TOKEN   - строка, которую ты придумал (то же впишешь в Meta "Подтверждение маркера")
    IG_ACCESS_TOKEN   - маркер доступа Instagram, который ты сгенерировал
    IG_APP_SECRET     - "Секрет приложения Instagram" (для проверки подписи; можно оставить пустым на тесте)
    GRAPH_VERSION     - версия Graph API (по умолчанию v21.0)
"""

import hashlib
import hmac
import json
import logging
import os

import httpx
from fastapi import FastAPI, Request, Response

# --- переиспользуем мозг из bot.py (Telegram-бот не запускается при импорте) ---
from bot import (
    get_content, classify, build_prompt, sanitize, claude, MODEL,
    AGENTS, LEAD_RE, BOOKING_RE, muted_chats, STOP_WORD,
    USE_WEB_SEARCH, WEB_SEARCH_TOOL,
)

logging.basicConfig(level=logging.INFO)

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

app = FastAPI()

# Память диалогов по Instagram-пользователю (ключ = его IG id).
# Для начала в памяти процесса; на проде вынести в БД (как и в Telegram-версии).
ig_sessions: dict[str, dict] = {}

# Кого не обрабатываем как входящее: эхо собственных сообщений бизнеса.
# (Instagram присылает и наши же ответы - их игнорируем по флагу is_echo / is_self.)


@app.get("/instagram")
async def verify(request: Request):
    """Проверка вебхука: Meta присылает hub.challenge - возвращаем его обратно."""
    params = request.query_params
    if (params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == IG_VERIFY_TOKEN):
        logging.info("Webhook verified")
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
    msg = ev.get("message") or {}
    # Эхо: сообщение, отправленное самим бизнес-аккаунтом (Наталья пишет вручную с телефона).
    if msg.get("is_echo"):
        # Стоп-слово от Натальи в конкретном чате -> бот замолкает в этом чате навсегда.
        recipient = (ev.get("recipient") or {}).get("id")
        text = (msg.get("text") or "").strip().lower()
        if recipient and text == STOP_WORD:
            muted_chats.add(recipient)
            logging.info("IG стоп-слово: бот замолчал в чате с %s", recipient)
        elif recipient:
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

    reply = await think(sender, text)
    if reply:
        await send_ig_message(sender, reply, account_id)


async def think(user_id: str, text: str) -> str:
    """Тот же мозг, что и в Telegram: классификатор -> субагент -> ответ."""
    s = ig_sessions.setdefault(user_id, {"history": [], "agent": None, "segment": None})
    s["history"].append({"role": "user", "content": text})
    if len(s["history"]) > 40:
        del s["history"][:-40]

    settings, faq, kb, sub = await get_content()
    prev = s["agent"]
    agent, segment = await classify(s["history"], prev)
    s["agent"], s["segment"] = agent, segment
    logging.info("IG Маршрут: %s | сегмент: %s", AGENTS.get(agent, agent), segment)

    system_prompt = build_prompt(agent, segment, settings, faq, kb, sub)
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

    # Лиды/брони: вырезаем служебные блоки из текста клиента.
    # (Передачу лида менеджеру в Instagram-версии добавим отдельно - пока просто чистим.)
    m = LEAD_RE.search(reply)
    if m:
        logging.info("IG LEAD: %s", m.group(1).strip())
        reply = LEAD_RE.sub("", reply).strip()
    b = BOOKING_RE.search(reply)
    if b:
        logging.info("IG BOOKING: %s", b.group(1).strip())
        reply = BOOKING_RE.sub("", reply).strip()

    return sanitize(reply)


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


@app.get("/")
async def health():
    return {"status": "ok", "channel": "instagram"}
