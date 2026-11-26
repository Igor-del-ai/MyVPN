import asyncio
import hmac
import ipaddress
import logging
import os
import re
from datetime import datetime

import aiohttp
import phonenumbers
from phonenumbers import carrier, geocoder, timezone
from phonenumbers.phonenumberutil import PhoneNumberType
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", "10000"))
HLR_API_KEY = os.getenv("HLR_API_KEY")
HLR_API_SECRET = os.getenv("HLR_API_SECRET")
FNS_API_KEY = os.getenv("FNS_API_KEY")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("phone-finder")

MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("📞 Проверить номер", callback_data="phone"),
     InlineKeyboardButton("👤 Telegram username", callback_data="username")],
    [InlineKeyboardButton("🌐 Домен / IP", callback_data="network")],
    [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")],
])

HELP = (
    "🔎 Phone Finder — справочная проверка открытых технических данных\n\n"
    "📞 Номер: отправьте номер в международном формате (+33123456789). "
    "Покажу формат, страну, справочный регион, тип номера, оператора (если есть в базе) и часовые пояса.\n\n"
    "👤 Telegram: отправьте публичный @username — сформирую ссылку на профиль.\n\n"
    "🌐 Домен / IP: отправьте домен (example.com) или IP-адрес — покажу публичные технические "
    "сведения реестра RDAP. Контактные данные частных лиц не отображаются.\n\n"
    "Ограничения: бот не устанавливает личность владельца телефона, не ищет паспорта, СНИЛС, "
    "домашние адреса, скрытые аккаунты или сведения из утечек. Публичные упоминания номера "
    "и HLR-статус не проверяются без подключённого легального API-провайдера."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("mode", None)
    await update.effective_message.reply_text(
        "🔎 Phone Finder\n\nВыберите действие:", reply_markup=MENU
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP, reply_markup=MENU)


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    modes = {
        "phone": ("phone", "Введите номер в международном формате, например +33123456789."),
        "username": ("username", "Введите публичное имя Telegram, например @telegram."),
        "network": ("network", "Введите домен (example.com) или IP-адрес (1.1.1.1)."),
    }
    if query.data in modes:
        mode, prompt = modes[query.data]
        context.user_data["mode"] = mode
        await query.message.reply_text(prompt)
    else:
        await query.message.reply_text(HELP, reply_markup=MENU)


def phone_type_name(number) -> str:
    kind = phonenumbers.number_type(number)
    names = {
        PhoneNumberType.MOBILE: "Мобильный",
        PhoneNumberType.FIXED_LINE: "Стационарный",
        PhoneNumberType.FIXED_LINE_OR_MOBILE: "Стационарный / мобильный (не уточняется)",
        PhoneNumberType.TOLL_FREE: "Бесплатный",
        PhoneNumberType.PREMIUM_RATE: "Платный сервисный",
        PhoneNumberType.SHARED_COST: "Совместная оплата",
        PhoneNumberType.VOIP: "VoIP",
        PhoneNumberType.PERSONAL_NUMBER: "Персональный",
        PhoneNumberType.PAGER: "Пейджер",
        PhoneNumberType.UAN: "Универсальный доступ",
        PhoneNumberType.VOICEMAIL: "Голосовая почта",
        PhoneNumberType.UNKNOWN: "Неизвестен",
    }
    return names.get(kind, "Неизвестен")


def phone_result(raw: str) -> str:
    try:
        number = phonenumbers.parse(raw.strip(), None)
    except phonenumbers.NumberParseException:
        return "❌ Не удалось распознать номер. Укажите код страны, например +33123456789."
    if not phonenumbers.is_possible_number(number):
        return "❌ Неподходящая длина или формат номера."
    if not phonenumbers.is_valid_number(number):
        return "⚠️ Номер распознан, но не проходит проверку плана нумерации."

    region = geocoder.description_for_number(number, "ru") or "Нет справочных данных"
    operator = carrier.name_for_number(number, "ru") or "Нет доступных данных"
    zones = ", ".join(timezone.time_zones_for_number(number)) or "Неизвестно"
    country = phonenumbers.region_code_for_number(number) or "Неизвестно"
    national = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.NATIONAL)
    international = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    e164 = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
    return (
        "📲 КОНТАКТЫ — РЕЗУЛЬТАТ ПРОВЕРКИ\n\n"
        f"Номер: {international}\n"
        f"Национальный формат: {national}\n"
        f"Формат E.164: {e164}\n"
        f"Страна / код региона: {country}\n"
        f"Регион (справочно): {region}\n"
        f"Тип номера: {phone_type_name(number)}\n"
        f"Оператор (справочная база): {operator}\n"
        f"Часовой пояс телефонного региона: {zones}\n\n"
        "👤 Личность владельца: не определяется по номеру.\n"
        "🌐 Публичные упоминания: для поиска нужен отдельный легальный поисковый API; "
        "в этой версии такой источник не подключён.\n"
        "🛡 Репутация / жалобы: источник данных не подключён.\n\n"
        "ℹ️ Сведения относятся к телефонному плану и могут быть неполными; "
        "они не подтверждают личность владельца или его местоположение."
    )


def username_result(raw: str) -> str:
    value = raw.strip()
    value = re.sub(r"^https?://t\.me/", "", value, flags=re.IGNORECASE)
    value = value.split("?", 1)[0].strip("/")
    if value.startswith("@"):
        value = value[1:]
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", value):
        return "❌ Неверный формат. Используйте 5–32 символа: латинские буквы, цифры и подчёркивание."
    return (
        "💬 TELEGRAM — ПУБЛИЧНЫЙ ПРОФИЛЬ\n\n"
        f"Username: @{value}\n"
        f"Ссылка: https://t.me/{value}\n\n"
        "ℹ️ Бот может сформировать ссылку, но не может получить скрытые сведения, "
        "номер телефона или подтвердить личность владельца."
    )


def _rdap_date(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return value or "Нет данных"


async def network_result(raw: str) -> str:
    value = raw.strip()
    is_ip = False
    try:
        parsed_ip = ipaddress.ip_address(value)
        is_ip = True
        endpoint = f"https://rdap.org/ip/{parsed_ip}"
        heading = "🌐 IP-АДРЕС — ПУБЛИЧНЫЕ ТЕХНИЧЕСКИЕ ДАННЫЕ"
    except ValueError:
        domain = value.lower().rstrip(".")
        if domain.startswith(("http://", "https://")):
            domain = domain.split("://", 1)[1].split("/", 1)[0]
        if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain):
            return "❌ Введите корректный домен (example.com) или IP-адрес (1.1.1.1)."
        endpoint = f"https://rdap.org/domain/{domain}"
        heading = "🌐 ДОМЕН — ПУБЛИЧНЫЕ ТЕХНИЧЕСКИЕ ДАННЫЕ"

    timeout = aiohttp.ClientTimeout(total=12)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "PhoneFinder/1.0"}) as session:
            async with session.get(endpoint) as response:
                if response.status != 200:
                    return f"{heading}\n\nЗапрос к публичному реестру RDAP не дал результата (HTTP {response.status})."
                data = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return f"{heading}\n\nНе удалось получить ответ от публичного реестра RDAP. Попробуйте позже."

    if is_ip:
        name = data.get("name") or data.get("handle") or "Нет данных"
        country = data.get("country") or "Нет данных"
        start = data.get("startAddress") or "Нет данных"
        end = data.get("endAddress") or "Нет данных"
        events = {e.get("eventAction"): _rdap_date(e.get("eventDate", "")) for e in data.get("events", [])}
        return (
            f"{heading}\n\nIP: {value}\nСеть / запись: {name}\nСтрана реестра: {country}\n"
            f"Диапазон: {start} — {end}\nДата регистрации: {events.get('registration', 'Нет данных')}\n\n"
            "ℹ️ Это сведения о сетевом ресурсе, а не о конкретном пользователе или его физическом адресе."
        )

    events = {e.get("eventAction"): _rdap_date(e.get("eventDate", "")) for e in data.get("events", [])}
    registrar = "Нет данных"
    for entity in data.get("entities", []):
        if "registrar" in entity.get("roles", []):
            vcard = entity.get("vcardArray", [])
            if len(vcard) > 1:
                registrar = next((item[3] for item in vcard[1] if item[0] == "fn" and len(item) > 3), "Нет данных")
            break
    nameservers = ", ".join(ns.get("ldhName", "") for ns in data.get("nameservers", [])[:6]) or "Нет данных"
    status = ", ".join(data.get("status", [])) or "Нет данных"
    return (
        f"{heading}\n\nДомен: {value}\nРегистратор: {registrar}\n"
        f"Дата регистрации: {events.get('registration', 'Нет данных')}\n"
        f"Истечение регистрации: {events.get('expiration', 'Нет данных')}\n"
        f"Статус: {status}\nDNS-серверы: {nameservers}\n\n"
        "ℹ️ Контактные данные частных регистрантов не выводятся."
    )


async def inn_result(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) not in (10, 12):
        return "❌ ИНН должен содержать 10 цифр (организация) или 12 цифр (физлицо / ИП)."
    def checksum(number: str, weights) -> int:
        return sum(int(d) * w for d, w in zip(number, weights)) % 11 % 10
    if len(digits) == 10:
        valid = checksum(digits[:9], (2, 4, 10, 3, 5, 9, 4, 6, 8)) == int(digits[9])
    else:
        valid = (
            checksum(digits[:10], (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)) == int(digits[10])
            and checksum(digits[:11], (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)) == int(digits[11])
        )
    return (
        "🏢 ЮРИДИЧЕСКОЕ ЛИЦО — ПРОВЕРКА ИНН\n\n"
        f"ИНН: {digits}\nКонтрольные цифры: {'проверка пройдена' if valid else 'не совпадают'}\n\n"
        "ℹ️ Это только проверка структуры номера. Название организации и сведения из ЕГРЮЛ "
        "не запрашиваются: официальный реестровый источник/API ещё не подключён."
    )


async def hlr_lookup(e164: str) -> str:
    """Optional paid HLR lookup. Requires the user's own HLR Lookup API credentials."""
    if not HLR_API_KEY or not HLR_API_SECRET:
        return "📡 HLR: не подключён — добавьте HLR_API_KEY и HLR_API_SECRET в Render."
    digits = re.sub(r"\D", "", e164)
    payload = {
        "api_key": HLR_API_KEY,
        "api_secret": HLR_API_SECRET,
        "requests": [{"telephone_number": digits, "cache_days_private": 0, "cache_days_global": 0}],
    }
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("https://api.hlrlookup.com/apiv2/hlr", json=payload) as response:
                data = await response.json(content_type=None)
                if response.status != 200:
                    return f"📡 HLR: провайдер вернул HTTP {response.status}."
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return "📡 HLR: сервис временно недоступен."
    try:
        result = data["body"]["results"][0]
    except (KeyError, IndexError, TypeError):
        return "📡 HLR: провайдер не вернул результат."
    if result.get("error") not in (None, "NONE"):
        return f"📡 HLR: ошибка проверки ({result.get('error')})."
    network = result.get("current_network_details") or {}
    status = result.get("live_status") or "Нет данных"
    number_type = result.get("telephone_number_type") or "Нет данных"
    operator = network.get("name") or "Нет данных"
    ported = result.get("is_ported") or "Неизвестно"
    stamp = result.get("timestamp") or "не указано"
    return (
        "\n\n📡 HLR — ПРОВЕРКА СЕТИ (платный внешний сервис)\n"
        f"Статус доступности: {status}\nТип номера: {number_type}\n"
        f"Текущая сеть: {operator}\nПеренос номера: {ported}\n"
        f"Время проверки: {stamp}\n"
        "ℹ️ HLR не раскрывает владельца, адрес или местоположение человека."
    )


async def fns_company_lookup(inn: str) -> str:
    """Optional company-only registry lookup through API-FNS; 10-digit INN only."""
    if not FNS_API_KEY:
        return "Для получения карточки организации добавьте FNS_API_KEY в Render."
    if len(inn) != 10:
        return "Поиск в реестре организаций доступен только для 10-значного ИНН юрлица."
    url = "https://api-fns.ru/api/egr"
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params={"req": inn, "key": FNS_API_KEY}) as response:
                data = await response.json(content_type=None)
                if response.status != 200:
                    return f"Реестр ФНС: HTTP {response.status}."
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return "Реестр ФНС временно недоступен."
    # Provider response schemas can vary by account/plan; show only verified company fields.
    try:
        items = data.get("items") or data.get("КРСБ") or []
        if isinstance(items, dict):
            items = [items]
        item = items[0] if items else {}
        company = item.get("ЮЛ") or item.get("ИП") or item
        name = company.get("НаимПолнЮЛ") or company.get("НаимСокрЮЛ") or company.get("НаимПолн") or company.get("Наим") or "Нет данных"
        ogrn = company.get("ОГРН") or company.get("ОГРНИП") or "Нет данных"
        status = company.get("Статус") or company.get("СведСтат") or "Нет данных"
        return (
            "🏢 ЕГРЮЛ/ЕГРИП — ОТКРЫТЫЕ СВЕДЕНИЯ\n\n"
            f"ИНН: {inn}\nНаименование: {name}\nОГРН/ОГРНИП: {ogrn}\nСтатус: {status}\n"
            "Источник: API-ФНС; сведения могут зависеть от тарифа и формата ответа."
        )
    except (AttributeError, IndexError, TypeError):
        return "Реестр ответил, но формат карточки не распознан. Проверьте тариф и документацию API-ФНС."


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")
    message = update.effective_message
    value = (message.text or "").strip()
    if mode == "phone":
        result = phone_result(value)
        if result.startswith("📲 КОНТАКТЫ"):
            try:
                parsed = phonenumbers.parse(value, None)
                e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
                result += await hlr_lookup(e164)
            except phonenumbers.NumberParseException:
                pass
    elif mode == "username":
        result = username_result(value)
    elif mode == "network":
        result = await network_result(value)
    elif mode == "inn":
        result = await inn_result(value)
        digits = re.sub(r"\D", "", value)
        if len(digits) == 10:
            result += "\n\n" + await fns_company_lookup(digits)
    else:
        await message.reply_text(
            "Сначала выберите действие или отправьте /start.", reply_markup=MENU
        )
        return
    await message.reply_text(result, reply_markup=MENU, disable_web_page_preview=True)
    context.user_data.pop("mode", None)


async def inn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = " ".join(context.args).strip()
    if not value:
        context.user_data["mode"] = "inn"
        await update.effective_message.reply_text("Отправьте ИНН из 10 или 12 цифр.")
        return
    base = await inn_result(value)
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10:
        base += "\n\n" + await fns_company_lookup(digits)
    await update.effective_message.reply_text(base, reply_markup=MENU)


async def ip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = " ".join(context.args).strip()
    if not value:
        context.user_data["mode"] = "network"
        await update.effective_message.reply_text("Отправьте IP-адрес или домен.")
        return
    await update.effective_message.reply_text(await network_result(value), reply_markup=MENU)


def build_application():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("inn", inn_cmd))
    app.add_handler(CommandHandler("ip", ip_cmd))
    app.add_handler(CommandHandler("domain", ip_cmd))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


async def run_server():
    if not TOKEN or not WEBHOOK_SECRET or not PUBLIC_URL:
        raise RuntimeError("Нужны BOT_TOKEN, WEBHOOK_SECRET и RENDER_EXTERNAL_URL.")

    bot_app = build_application()
    await bot_app.initialize()
    await bot_app.start()

    async def health(_request):
        return web.Response(text="Phone Finder is running")

    async def telegram_webhook(request):
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied, WEBHOOK_SECRET):
            raise web.HTTPForbidden()
        try:
            payload = await request.json()
            update = Update.de_json(payload, bot_app.bot)
            await bot_app.process_update(update)
        except Exception:
            log.exception("Failed to process Telegram update")
            raise web.HTTPBadRequest()
        return web.Response(text="ok")

    server = web.Application()
    server.router.add_get("/health", health)
    server.router.add_post("/telegram", telegram_webhook)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    webhook_url = PUBLIC_URL.rstrip("/") + "/telegram"
    await bot_app.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
    )
    log.info("Phone Finder webhook started at %s", webhook_url)

    try:
        await asyncio.Event().wait()
    finally:
        await bot_app.bot.delete_webhook()
        await runner.cleanup()
        await bot_app.stop()
        await bot_app.shutdown()


def main():
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
