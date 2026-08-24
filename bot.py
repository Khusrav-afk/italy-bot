"""
ItalySecretPlaces - мультиагентная система (демо в Telegram).

Классификатор определяет тему и сегмент -> передаёт диалог одному из 6 субагентов.
У каждого субагента СВОЯ вкладка-база знаний в Google Sheets (плюс общие вкладки).
Лиды -> Sheets + Telegram. Бронь экскурсий -> Google Calendar.
Follow-up: если клиент замолчал - напоминаем через 7 мин / 3 часа / 5 часов / 20 часов (followups.py).
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from followups import followups   # <-- follow-up цепочка

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MANAGER_CHAT_ID = os.getenv("MANAGER_CHAT_ID")
MODEL = os.getenv("MODEL", "claude-sonnet-4-6")
SHOW_AGENT = os.getenv("SHOW_AGENT", "0") == "1"   # демо: показывать субагента/маршрутизацию
# ===================== РУБИЛЬНИК БОТА =====================
# False - бот ВЫКЛЮЧЕН и молчит во всех каналах (Telegram / Instagram / WhatsApp).
# Чтобы ВКЛЮЧИТЬ бота обратно: поменять на True, закоммитить и запушить в GitHub
# (Render передеплоит сам, если включён Auto-Deploy; иначе - Manual Deploy).
BOT_ENABLED = True
# =========================================================

SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_CREDS_FILE = os.getenv("GOOGLE_CREDS_FILE", "service_account.json")
CACHE_TTL = 300
CALENDAR_ID = os.getenv("CALENDAR_ID")
CALENDAR_TZ = os.getenv("CALENDAR_TZ", "Europe/Rome")

logging.basicConfig(level=logging.INFO)
claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

sessions: dict[int, dict] = {}
muted_chats: set = set()          # чаты, где Наталья написала стоп-слово - бот молчит
# Изменяемый словарь (не константы!) - можно переопределить из таблицы, вкладка "Настройки"
# (см. _apply_words). webhook_server.py импортирует этот же объект, мутация видна везде.
WORDS = {
    "stop": os.getenv("STOP_WORD", "un attimo").strip().lower(),
    "resume": os.getenv("RESUME_WORD", "✅✅✅").strip().lower(),
}
USE_WEB_SEARCH = os.getenv("USE_WEB_SEARCH", "1") == "1"
WEB_SEARCH_TOOL = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
LEAD_RE = re.compile(r"\[LEAD\](.*?)\[/LEAD\]", re.S)
BOOKING_RE = re.compile(r"\[BOOKING\](.*?)\[/BOOKING\]", re.S)
CLOSE_RE = re.compile(r"\[CLOSE\]")
JSON_RE = re.compile(r"\{.*\}", re.S)
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF\U00002190-\U000021FF\U0000FE00-\U0000FE0F\U0000200D]"
)

# ключ субагента -> (отображаемое имя, имя вкладки с его базой знаний)
AGENTS = {
    "excursions": "Экскурсии",
    "housing": "Жильё",
    "transfers": "Трансферы",
    "turnkey": "Отдых под ключ",
    "shopping": "Шопинг / стилист",
    "vip": "VIP-сервис",
    "freeform": "Свободный запрос",
}
AGENT_TABS = {
    "housing": "Жильё",
    "transfers": "Трансферы",
    "turnkey": "Отдых под ключ",
    "shopping": "Шопинг",
    "vip": "VIP",
    "freeform": "Свободный запрос",
}

# Экскурсии разбиты по отдельным вкладкам (по одной на город) вместо одной общей "Экскурсии".
# Первый город в списке - используется по умолчанию, если клиент город ещё не назвал.
EXCURSION_CITIES = ["Венеция", "Рим", "Флоренция", "Милан", "Неаполь", "Йезоло"]


def sanitize(text: str) -> str:
    if not text:
        return text
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    text = EMOJI_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +\n", "\n", text)
    return text.strip()


DEFAULTS = {
    "Приветствие": "Здравствуйте. Я ассистент Натальи, Italy Secret Places. Помогаю "
    "организовать отдых в Италии: экскурсии, жильё, трансферы, отдых под ключ, шопинг и "
    "VIP-сопровождение. Подскажите, что вас интересует.",
    "О сервисе": "Italy Secret Places - персональное сопровождение отдыха в Италии, более "
    "20 лет опыта. Продаём сервис, а не отдельные варианты: подбираем проверенное под ваш "
    "запрос и сопровождаем до и во время поездки.",
    "Цены": "Экскурсии: предоплата 50 евро. Отдых под ключ: предоплата 200 евро. Пакеты "
    "ориентир: 1-2 недели 150-200 евро, 3 недели и больше 250-300 евро.",
    "Предоплата": "Предоплата - старт подготовки и резерв времени эксперта. Оплату ты не "
    "принимаешь, её оформляет Наталья лично.",
    "Полезные ссылки": "italysecretplaces.com | visit-jesolo.com | jesoloappartment.com",
    "Ссылка на запись": "",
    "Контакт Натальи": "",
    "Примеры стиля": "",
    # Тексты follow-up (Наталья может переопределить; пустое -> заглушка из followups.py)
    "Follow-up 1": "",
    "Follow-up 2": "",
    "Follow-up 3": "",
    "Follow-up 4": "",
    # Стоп-слово / слово-разрешение (пустое -> остаётся значение по умолчанию из кода)
    "Стоп-слово": "",
    "Слово-разрешение": "",
}
DEFAULT_FAQ: list = []
DEFAULT_KB: list = []
DEFAULT_SUBKB: dict = {k: [] for k in AGENTS}

BASE_TEMPLATE = """\
Ты - персональный ассистент Натальи, сервис ItalySecretPlaces: сопровождение отдыха в Италии.
СЕГОДНЯ: @@TODAY@@ (для расчёта дат, часовой пояс Италии).

СТИЛЬ - КРИТИЧЕСКИ ВАЖНО (клиент жёстко требует, нарушение недопустимо):
- Ты ассистент Натальи. Пиши как живой человек, уважительно, на "вы".
- ГЛАВНОЕ ПРАВИЛО: сообщение = ТОЛЬКО следующий вопрос (или короткий ответ по делу).
  НИКАКИХ вступлений, рассуждений, пояснений "почему это важно", советов, описаний.
- НЕ пересказывай слова клиента: запрещено "Хорошо, вас четверо", "Отлично, первый раз".
  Услышал ответ - сразу задавай следующий вопрос, без подтверждений.
- НЕ продавай впечатления и не философствуй: запрещены фразы вроде "важно правильно
  расставить акценты", "чтобы не потеряться в толпе", "это особенное впечатление",
  "для первого знакомства важно".
- Без эмодзи. Без длинного тире (только дефис). Без штампов: "Отличный выбор", "Здорово",
  "С удовольствием помогу", "Прекрасно", "Замечательно".
- Один вопрос - одно короткое сообщение.
- ЯЗЫК: определи язык клиента по его сообщению и отвечай на нём (русский, итальянский,
  английский, немецкий и любой другой). База знаний на русском - переводи её на язык клиента
  на лету; цены, ссылки и имена собственные оставляй как есть.

ПРИМЕРЫ (строго следуй этому образцу):
ПЛОХО: "Хорошо, компания из 4 человек. Вы уже бывали в Венеции или это первый визит?"
ХОРОШО: "Вы уже бывали в Венеции или едете впервые?"
ПЛОХО: "Для первого знакомства с Венецией важно правильно расставить акценты, чтобы не потеряться в толпе. В какое время удобно начать?"
ХОРОШО: "В какое время вам удобно начать экскурсию?"
ПЛОХО: "Отличный выбор! Венеция прекрасна. Сколько вас будет человек?"
ХОРОШО: "Сколько человек поедет?"
ПЛОХО: "Чем могу помочь с Италией?"
ХОРОШО: "Чем могу помочь?"
@@STYLE@@

О СЕРВИСЕ: @@ABOUT@@
ЦЕНЫ: @@PRICES@@
ПРЕДОПЛАТА: @@PREPAY@@ Оплату не принимаешь, её оформляет Наталья лично.
ПОЛЕЗНЫЕ ССЫЛКИ: @@LINKS@@
ЗАПИСЬ: @@BOOKING_LINK@@   КОНТАКТ НАТАЛЬИ: @@CONTACT@@

ОБЩАЯ БАЗА ЗНАНИЙ:
@@KNOWLEDGE@@

ЧАСТЫЕ ВОПРОСЫ:
@@FAQ@@

ПРАВИЛА: не присылай каталог жилья в чат; не консультируй по медицине и юр.вопросам; не
запрашивай карты и паспорта.
ОБЯЗАТЕЛЬНО: перед передачей Наталье уточни ИМЯ и КОНТАКТ (WhatsApp/телефон).

НИКОГДА НЕ ГОВОРИ "не знаю", "у меня нет информации", "нет готового списка", "уточните у
Натальи". Даже если точных данных нет в базе - ВСЁ РАВНО веди диалог и собирай обязательные
данные: город, отель (если есть), сколько человек, возраст детей (если есть), желаемые даты,
что именно интересует. Эти данные нужно получить в любом случае и передать Наталье. Отсутствие
данных в базе - не повод отказать, а повод собрать заявку.

ИНТЕРНЕТ: если клиент спрашивает то, чего нет в базе (погода, какие города где находятся,
расстояния, общие факты об Италии) - используй веб-поиск и ответь по существу, затем вернись к
сбору данных и услуге. Цены, ссылки и условия бери ТОЛЬКО из базы, не выдумывай их из интернета.

ПЕРЕДАЧА: когда собрал тему, даты, состав, что нужно, имя и контакт - спокойно сообщи, что
передаёшь Наталье, и добавь в конце служебный блок (клиент не увидит):
[LEAD]{"direction":"","name":"","dates":"","duration":"","people":"","city":"","budget":"","services":"","language":"ru","contact":"","urgency":"","status":"warm","summary":""}[/LEAD]
Выдавай [LEAD] один раз.

ЗАВЕРШЕНИЕ РАЗГОВОРА: если клиент явно прощается или завершает разговор (благодарит и не
задаёт вопросов, говорит "вернусь позже/сам напишу/подумаю и вернусь" и т.п.) - вежливо
попрощайся и добавь в конце служебный блок (клиент не увидит): [CLOSE]
Это отключает автоматические напоминания для этого диалога, чтобы не беспокоить клиента,
который уже сказал, что вернётся сам. НЕ добавляй [CLOSE], если клиент просто не ответил на
твой вопрос - это не то же самое, что явное прощание.
"""

SUBAGENTS = {
    "excursions": "ТВОЯ РОЛЬ: субагент ЭКСКУРСИИ. ЗНАНИЯ НАПРАВЛЕНИЯ ниже уже отфильтрованы по "
    "городу, который назвал клиент (или по городу по умолчанию, если ещё не называл) - искать "
    "по другим городам в них не нужно, там только один город. "
    "ПЕРВЫМ делом уточни: групповые или индивидуальные "
    "экскурсии и сколько человек. "
    "ЕСЛИ ГРУППОВЫЕ - дай ссылку на бронь групповых из знаний направления, предложи помощь; при "
    "интересе возьми имя и контакт. "
    "ЕСЛИ ИНДИВИДУАЛЬНЫЕ - спрашивай строго по ОДНОМУ вопросу в сообщении и в порядке: 1) город "
    "(по умолчанию Венеция, если уже назван - не переспрашивай); 2) первый раз в этом городе или "
    "уже бывал; 3) сколько человек; 4) если есть дети - их возраст; 5) желаемые даты. "
    "Когда данные собраны и клиент просит варианты/стоимость - выдай блок КРАТКИЙ СПИСОК из знаний "
    "направления ДОСЛОВНО, включая завершающую фразу. "
    "Когда клиент выбрал вариант - выдай ДОСЛОВНО описание выбранной из блока ПОДРОБНОЕ ОПИСАНИЕ, "
    "без повтора завершающей фразы списка. "
    "После выбора варианта уточни ЖЕЛАЕМЫЕ ДАТУ И ВРЕМЯ и сообщи, что рекомендуемое время - утром, "
    "в первой половине дня (слоты каждый час начиная с 08:00; точные данные бери из блока "
    "РЕКОМЕНДОВАННОЕ ВРЕМЯ в знаниях направления). "
    "Когда клиент назвал дату и время - подведи к брони (предоплата 50 евро) и ОБЯЗАТЕЛЬНО пришли "
    "МЕСТО ВСТРЕЧИ ДОСЛОВНО из блока МЕСТО ВСТРЕЧИ в знаниях направления, вместе со ссылкой. "
    "Для русского - слово в слово; для другого языка переведи, сохраняя цены, названия и ссылки. "
    "Если выбраны экскурсия + дата + время + имя + контакт - добавь ОБА служебных блока подряд "
    "(клиент не увидит), сначала бронь, потом лид - бронь без лида НЕ считается завершённой передачей:\n"
    '[BOOKING]{"title":"","datetime":"2026-07-15T09:00:00","duration_min":120,"name":"","contact":"","people":"","notes":""}[/BOOKING]\n'
    '[LEAD]{"direction":"excursions","name":"","dates":"","duration":"","people":"","city":"","budget":"","services":"","language":"ru","contact":"","urgency":"","status":"hot","summary":""}[/LEAD]\n'
    "datetime в ISO по времени Италии. Если время не названо - уточни, не выдумывай.",
    "housing": "ТВОЯ РОЛЬ: субагент ЖИЛЬЁ. Стиль по делу. Спрашивай по одному вопросу: 1) город или "
    "курорт (Йезоло у моря, Венеция и т.д.); 2) даты заезда и выезда; 3) сколько человек (взрослые "
    "и дети); 4) апартаменты или отель; 5) бюджет; 6) ключевые пожелания (вид на канал, рядом с "
    "морем и т.п.). Условия брони и нюансы (турналог, уборка, предоплата) бери из знаний направления. "
    "Конкретные варианты с ценами в чат не присылай - их подбирает Наталья после брифа; объясни, что "
    "за подбор и сопровождение есть отдельная услуга. Возьми имя и контакт, передай Наталье.",
    "transfers": "ТВОЯ РОЛЬ: субагент ТРАНСФЕРЫ. Уточни: откуда и куда, дата и время, сколько "
    "человек и багажа, тип. Собери и передай Наталье.",
    "turnkey": "ТВОЯ РОЛЬ: субагент ОТДЫХ ПОД КЛЮЧ. Стиль строго по делу, без воды. Спрашивай по "
    "одному вопросу: 1) какие города планируют; 2) сколько человек (и возраст детей); 3) даты; "
    "4) нужна ли помощь с жильём и трансферами. Подскажи правило: закладывать ~1,5 дня на город, "
    "лучше выбрать 2-3 города. Используй цены и состав из знаний направления. Назови оплату: "
    "организация 200 евро, предоплата 50% и 50% после утверждения программы. Возьми имя и контакт, "
    "передай Наталье.",
    "shopping": "ТВОЯ РОЛЬ: субагент ШОПИНГ / СТИЛИСТ. Уточни формат и пожелания, даты, бюджет. "
    "Собери и передай Наталье.",
    "vip": "ТВОЯ РОЛЬ: субагент VIP-СЕРВИС. Тон премиум-консьержа, сдержанно. Выясни задачу и "
    "ориентир бюджета, обозначь, что предложение персональное. Передай Наталье.",
    "freeform": "ТВОЯ РОЛЬ: субагент СВОБОДНЫЙ ЗАПРОС. Кратко ответь на вопрос об Италии, предложи "
    "релевантную услугу, возьми контакт и скажи, что Наталья свяжется в ближайшее время.",
}

CLASSIFIER_SYSTEM = (
    "Ты - классификатор диалога турагентства. По переписке определи нишу и сегмент.\n"
    "Ниши: excursions (экскурсия, гид, музей, достопримечательности); housing (жильё, отель, "
    "апартаменты, снять, Йезоло, Абано); transfers (трансфер, аэропорт, такси, катер, доехать); "
    "turnkey (отдых под ключ, организовать поездку, маршрут, отпуск); shopping (шопинг, стилист, "
    "образ, преображение, аутлет); vip (vip, luxury, private, яхта, эксклюзив); freeform (общие "
    "вопросы об Италии и всё остальное).\n"
    "Сегменты: elegant, premium, luxury. Если непонятно - premium.\n"
    "Текущий субагент: {current}. Сохрани его, если клиент явно не сменил тему.\n"
    'Ответь СТРОГО одним JSON: {{"agent":"...","segment":"..."}}'
)

# ---------- Google Sheets ----------
_cache = {"settings": {}, "faq": [], "kb": [], "sub": {}, "ts": 0.0}
_sheets_on = bool(SHEET_ID)
_calendar_on = bool(CALENDAR_ID)


def _load_google_creds(scopes):
    """Креды сервисного аккаунта. Приоритет - переменная окружения GOOGLE_CREDS_JSON
    (весь JSON строкой, как на Render). Если её нет - файл GOOGLE_CREDS_FILE.
    Секрет в коде не хранится, только в окружении."""
    from google.oauth2.service_account import Credentials
    raw = os.getenv("GOOGLE_CREDS_JSON")
    if raw and raw.strip():
        info = json.loads(raw)
        return Credentials.from_service_account_info(info, scopes=scopes)
    return Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)


def _open_sheet():
    import gspread
    creds = _load_google_creds(["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def _col_a(ws):
    return [r[0].strip() for r in ws.get_all_values()[1:] if r and r[0].strip()]


def _read_sheet():
    sh = _open_sheet()
    s = sh.worksheet("Настройки").get_all_values()[1:]
    settings = {r[0].strip(): r[1].strip() for r in s if len(r) >= 2 and r[0].strip()}
    f = sh.worksheet("FAQ").get_all_values()[1:]
    faq = [(r[0].strip(), r[1].strip()) for r in f if len(r) >= 2 and r[0].strip()]
    kb = []
    try:
        kb = _col_a(sh.worksheet("База знаний"))
    except Exception:
        pass
    sub = {}
    for key, tab in AGENT_TABS.items():
        try:
            sub[key] = _col_a(sh.worksheet(tab))
        except Exception:
            sub[key] = []
    excursions_by_city = {}
    for city in EXCURSION_CITIES:
        try:
            excursions_by_city[city] = _col_a(sh.worksheet(city))
        except Exception:
            excursions_by_city[city] = []
    sub["excursions_by_city"] = excursions_by_city
    return settings, faq, kb, sub


def _apply_followup_texts(settings: dict):
    """Обновить тексты follow-up из настроек (пустые игнорируются - остаётся заглушка)."""
    followups.set_messages({
        "m1": settings.get("Follow-up 1", ""),
        "m2": settings.get("Follow-up 2", ""),
        "m3": settings.get("Follow-up 3", ""),
        "m4": settings.get("Follow-up 4", ""),
    })


def _apply_words(settings: dict):
    """Обновить стоп-слово/слово-разрешение из настроек (пустое - оставляем текущее)."""
    stop = settings.get("Стоп-слово", "").strip().lower()
    if stop:
        WORDS["stop"] = stop
    resume = settings.get("Слово-разрешение", "").strip().lower()
    if resume:
        WORDS["resume"] = resume


async def get_content():
    if not _sheets_on:
        _apply_followup_texts(DEFAULTS)
        _apply_words(DEFAULTS)
        return DEFAULTS, DEFAULT_FAQ, DEFAULT_KB, DEFAULT_SUBKB
    if _cache["settings"] and time.time() - _cache["ts"] < CACHE_TTL:
        return _cache["settings"], _cache["faq"], _cache["kb"], _cache["sub"]
    try:
        settings, faq, kb, sub = await asyncio.to_thread(_read_sheet)
        merged = {**DEFAULTS, **{k: v for k, v in settings.items() if v}}
        _cache.update(settings=merged, faq=faq, kb=kb, sub=sub, ts=time.time())
        _apply_followup_texts(merged)
        _apply_words(merged)
        return merged, faq, kb, sub
    except Exception:
        logging.exception("Не удалось прочитать таблицу")
        if _cache["settings"]:
            return _cache["settings"], _cache["faq"], _cache["kb"], _cache["sub"]
        _apply_followup_texts(DEFAULTS)
        _apply_words(DEFAULTS)
        return DEFAULTS, DEFAULT_FAQ, DEFAULT_KB, DEFAULT_SUBKB


def detect_excursion_city(history) -> str:
    """Ищет в сообщениях клиента последнее упоминание города из EXCURSION_CITIES
    (без учёта регистра). Если ничего не найдено - город по умолчанию (первый в списке)."""
    found = None
    for msg in history:
        if msg.get("role") != "user":
            continue
        text = (msg.get("content") or "").lower()
        for city in EXCURSION_CITIES:
            if city.lower() in text:
                found = city
    return found or EXCURSION_CITIES[0]


def build_prompt(agent, segment, settings, faq, kb, sub, excursion_city=None):
    faq_text = "\n".join(f"В: {q}\nО: {a}" for q, a in faq) or "-"
    kb_text = "\n".join(f"- {x}" for x in kb) or "-"
    if agent == "excursions":
        city = excursion_city or EXCURSION_CITIES[0]
        sub_items = sub.get("excursions_by_city", {}).get(city, [])
        sub_label = f"{AGENTS[agent]} - {city}"
    else:
        sub_items = sub.get(agent, [])
        sub_label = AGENTS[agent]
    sub_text = "\n".join(f"- {x}" for x in sub_items) or "-"
    style = settings.get("Примеры стиля", "")
    style_block = ("\nОРИЕНТИР ПО СТИЛЮ:\n" + style) if style else ""
    base = (BASE_TEMPLATE
            .replace("@@TODAY@@", datetime.now().strftime("%Y-%m-%d"))
            .replace("@@STYLE@@", style_block)
            .replace("@@ABOUT@@", settings.get("О сервисе", ""))
            .replace("@@PRICES@@", settings.get("Цены", ""))
            .replace("@@PREPAY@@", settings.get("Предоплата", ""))
            .replace("@@LINKS@@", settings.get("Полезные ссылки", "") or "-")
            .replace("@@BOOKING_LINK@@", settings.get("Ссылка на запись", "") or "-")
            .replace("@@CONTACT@@", settings.get("Контакт Натальи", "") or "-")
            .replace("@@KNOWLEDGE@@", kb_text)
            .replace("@@FAQ@@", faq_text))
    return (f"{base}\n\n{SUBAGENTS[agent]}\n\n"
            f"ЗНАНИЯ НАПРАВЛЕНИЯ «{sub_label}» (используй в первую очередь):\n{sub_text}\n\n"
            f"СЕГМЕНТ КЛИЕНТА: {segment} - подстрой уровень предложений и тон.")


async def classify(history, current):
    try:
        resp = await claude.messages.create(
            model=MODEL, max_tokens=60,
            system=CLASSIFIER_SYSTEM.format(current=current or "нет"),
            messages=history[-6:])
        raw = "".join(b.text for b in resp.content if b.type == "text")
        m = JSON_RE.search(raw)
        data = json.loads(m.group(0)) if m else {}
        agent = data.get("agent") if data.get("agent") in AGENTS else (current or "freeform")
        seg = data.get("segment") if data.get("segment") in ("elegant", "premium", "luxury") else "premium"
        return agent, seg
    except Exception:
        logging.exception("Ошибка классификатора")
        return current or "freeform", "premium"


# ---------- Хэндлеры ----------
@dp.message(CommandStart())
async def on_start(message: Message):
    if not BOT_ENABLED:
        return
    sessions[message.chat.id] = {"history": [], "agent": None, "segment": None}
    followups.cancel(f"tg:{message.chat.id}")   # новый диалог - снять старые follow-up
    settings, _, _, _ = await get_content()
    await message.answer(sanitize(settings.get("Приветствие", DEFAULTS["Приветствие"])))


@dp.message(Command("reload"))
async def on_reload(message: Message):
    _cache["ts"] = 0.0
    await get_content()
    await message.answer("Контент перечитан из таблицы.")


@dp.message(Command("agent"))
async def on_agent(message: Message):
    s = sessions.get(message.chat.id)
    if not s or not s.get("agent"):
        await message.answer("Субагент ещё не назначен.")
    else:
        await message.answer(f"Субагент: {AGENTS[s['agent']]} | сегмент: {s.get('segment')}")


@dp.message(Command("caltest"))
async def on_caltest(message: Message):
    if not _calendar_on:
        await message.answer("CALENDAR_ID не задан в .env (Calendar выключен).")
        return
    b = {"title": "Тест Italy Secret Places", "name": "Тест", "contact": "-",
         "duration_min": 30,
         "datetime": (datetime.now() + timedelta(hours=1)).replace(microsecond=0).isoformat()}
    try:
        link = await asyncio.to_thread(_create_event, b)
        await message.answer(f"OK. Событие создано в календаре.\n{link or '(ссылка не вернулась)'}")
    except Exception as e:
        await message.answer(f"ОШИБКА календаря: {type(e).__name__}: {e}")


@dp.message(F.text)
async def on_text(message: Message):
    if not BOT_ENABLED:
        return
    chat_id = message.chat.id
    text_lower = message.text.strip().lower()
    # Стоп-слово от Натальи: если этот чат уже "заглушён" - бот молчит,
    # пока не придёт слово-разрешение (WORDS["resume"]).
    if chat_id in muted_chats:
        if WORDS["resume"] in text_lower:
            muted_chats.discard(chat_id)
            logging.info("Стоп-слово снято в чате %s - бот снова отвечает", chat_id)
        return
    if WORDS["stop"] in text_lower:
        muted_chats.add(chat_id)
        followups.cancel(f"tg:{chat_id}")        # Наталья вмешалась - снять follow-up
        logging.info("Стоп-слово в чате %s - бот замолчал", chat_id)
        return

    s = sessions.setdefault(chat_id, {"history": [], "agent": None, "segment": None})
    s["history"].append({"role": "user", "content": message.text})
    if len(s["history"]) > 40:
        del s["history"][:-40]

    followups.on_incoming(f"tg:{chat_id}")        # клиент написал - (пере)планируем follow-up

    settings, faq, kb, sub = await get_content()
    await bot.send_chat_action(chat_id, "typing")

    prev = s["agent"]
    agent, segment = await classify(s["history"], prev)
    s["agent"], s["segment"] = agent, segment
    logging.info("Маршрут: %s | сегмент: %s", AGENTS[agent], segment)
    if SHOW_AGENT and agent != prev and MANAGER_CHAT_ID:
        try:
            await bot.send_message(int(MANAGER_CHAT_ID),
                f"Маршрутизация: {message.from_user.full_name} -> «{AGENTS[agent]}» ({segment})")
        except Exception:
            logging.exception("route note failed")

    excursion_city = detect_excursion_city(s["history"]) if agent == "excursions" else None
    system_prompt = build_prompt(agent, segment, settings, faq, kb, sub, excursion_city)
    try:
        kwargs = dict(model=MODEL, max_tokens=1024, system=system_prompt, messages=s["history"])
        if USE_WEB_SEARCH:
            kwargs["tools"] = WEB_SEARCH_TOOL
        resp = await claude.messages.create(**kwargs)
    except Exception:
        logging.exception("Claude API error")
        await message.answer("Секунду, уточню детали и вернусь.")
        return

    reply = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    s["history"].append({"role": "assistant", "content": reply})

    lead = LEAD_RE.search(reply)
    if lead:
        await handle_lead(message, lead.group(1).strip(), agent, segment)
        followups.cancel(f"tg:{chat_id}")   # заявка собрана и передана Наталье - не дожимаем клиента
        reply = LEAD_RE.sub("", reply).strip()
    book = BOOKING_RE.search(reply)
    if book:
        tg_source = f"Telegram: {message.from_user.full_name} (@{message.from_user.username or '-'})"
        await handle_booking(book.group(1).strip(), source=tg_source)
        reply = BOOKING_RE.sub("", reply).strip()
    if CLOSE_RE.search(reply):
        followups.cancel(f"tg:{chat_id}")   # клиент явно попрощался - не беспокоим напоминаниями
        reply = CLOSE_RE.sub("", reply).strip()

    reply = sanitize(reply)
    if SHOW_AGENT:
        reply = f"[субагент: {AGENTS[agent]} | {segment}]\n{reply}"
    if reply:
        await message.answer(reply)


@dp.message()
async def on_other(message: Message):
    await message.answer("Напишите, пожалуйста, текстом, так я смогу помочь.")


def _safe(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


async def save_lead(lead: dict, source: str):
    """Сохранить лид (jsonl + таблица + уведомление менеджеру). Канало-независимо -
    вызывается и из Telegram (handle_lead), и из Instagram/WhatsApp (webhook_server.think)."""
    lead["source"] = source
    lead["time"] = datetime.now().isoformat(timespec="seconds")
    with open("leads.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(lead, ensure_ascii=False) + "\n")
    if _sheets_on:
        try:
            await asyncio.to_thread(_append_lead_row, lead)
        except Exception:
            logging.exception("sheet lead failed")
    if MANAGER_CHAT_ID:
        try:
            await bot.send_message(int(MANAGER_CHAT_ID), format_lead(lead))
        except Exception:
            logging.exception("manager lead failed")


async def handle_lead(message: Message, raw, agent, segment):
    try:
        lead = json.loads(raw)
    except json.JSONDecodeError:
        lead = {"summary": raw}
    lead["agent"] = AGENTS.get(agent, agent)
    lead["segment"] = segment
    source = f"{message.from_user.full_name} (@{message.from_user.username or '-'})"
    await save_lead(lead, source)


def _append_lead_row(lead):
    ws = _open_sheet().worksheet("Лиды")
    row = [lead.get("time", ""), lead.get("agent", ""), lead.get("segment", ""),
           lead.get("name", ""), lead.get("dates", ""), lead.get("duration", ""),
           lead.get("people", ""), lead.get("city", ""), lead.get("budget", ""),
           lead.get("services", ""), lead.get("language", ""), lead.get("contact", ""),
           lead.get("urgency", ""), lead.get("status", ""), lead.get("summary", ""),
           lead.get("source", "")]
    ws.append_row([_safe(c) for c in row], value_input_option="USER_ENTERED")


async def handle_booking(raw, source=""):
    try:
        b = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not _calendar_on:
        logging.info("Бронь, календарь выключен: %s", b)
        return
    try:
        link = await asyncio.to_thread(_create_event, b, source)
        if MANAGER_CHAT_ID:
            await bot.send_message(int(MANAGER_CHAT_ID),
                f"БРОНЬ В КАЛЕНДАРЕ\n{b.get('title','Экскурсия')}\nКогда: {b.get('datetime','')}\n"
                f"Имя: {b.get('name','')}\nКонтакт: {b.get('contact','')}\n"
                f"Источник: {source or '-'}\n{link}")
    except Exception as e:
        logging.exception("calendar failed")
        if MANAGER_CHAT_ID:
            try:
                await bot.send_message(int(MANAGER_CHAT_ID),
                    f"Бронь НЕ создана в календаре. Ошибка: {type(e).__name__}: {e}")
            except Exception:
                pass


def _parse_dt(s: str) -> datetime:
    s = (s or "").strip().replace("Z", "")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        raise


def _create_event(b, source=""):
    from googleapiclient.discovery import build
    creds = _load_google_creds(["https://www.googleapis.com/auth/calendar"])
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    start = _parse_dt(b["datetime"])
    end = start + timedelta(minutes=int(b.get("duration_min") or 120))
    ev = {"summary": b.get("title") or "Экскурсия",
          "description": f"Имя: {b.get('name','')}\nКонтакт: {b.get('contact','')}\n"
                         f"Человек: {b.get('people','')}\nОткуда клиент: {source or '-'}\n"
                         f"{b.get('notes','')}",
          "start": {"dateTime": start.isoformat(), "timeZone": CALENDAR_TZ},
          "end": {"dateTime": end.isoformat(), "timeZone": CALENDAR_TZ}}
    return svc.events().insert(calendarId=CALENDAR_ID, body=ev).execute().get("htmlLink", "")


def format_lead(lead):
    fields = [("Субагент", "agent"), ("Сегмент", "segment"), ("Имя", "name"),
              ("Даты", "dates"), ("Длительность", "duration"), ("Человек", "people"),
              ("Город", "city"), ("Бюджет", "budget"), ("Услуги", "services"),
              ("Язык", "language"), ("Контакт", "contact"), ("Срочность", "urgency"),
              ("Статус", "status"), ("Кратко", "summary")]
    mark = "ГОРЯЧИЙ" if lead.get("status") == "hot" else "тёплый"
    lines = [f"НОВЫЙ ЛИД ({mark})"]
    for label, key in fields:
        if lead.get(key):
            lines.append(f"{label}: {lead[key]}")
    uname = f"@{lead['tg_username']}" if lead.get("tg_username") else "-"
    lines.append(f"\nTelegram: {lead.get('tg_user','')} ({uname})")
    lines.append(f"Время: {lead.get('time','')}")
    return "\n".join(lines)


