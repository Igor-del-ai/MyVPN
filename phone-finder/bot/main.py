import asyncio
import hmac
import logging
import os
import re

import phonenumbers
from phonenumbers import carrier, geocoder, timezone
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

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("phone-finder")

MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("📞 Проверить номер", callback_data="phone")],
    [InlineKeyboardButton("👤 Проверить @username", callback_data="username")],
    [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")],
])

HELP = (
    "Как пользоваться:\n\n"
    "📞 Номер — отправьте номер в международном формате, например +33123456789. "
    "Я покажу страну, справочный регион, оператора (если доступен в базе) и часовые пояса.\n\n"
    "👤 Username — отправьте @username. Я проверю формат и сформирую ссылку на публичный профиль.\n\n"
    "Важно: бот не определяет владельца номера, точный адрес, местоположение человека "
    "или скрытые данные Telegram-профиля."
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
    if query.data == "phone":
        context.user_data["mode"] = "phone"
        await query.message.reply_text(
            "Введите номер в международном формате, например +33123456789."
        )
    elif query.data == "username":
        context.user_data["mode"] = "username"
        await query.message.reply_text(
            "Введите публичное имя Telegram, например @telegram."
        )
    else:
        await query.message.reply_text(HELP, reply_markup=MENU)


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
    return (
        "📞 Результат проверки\n\n"
        f"Номер: {phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.INTERNATIONAL)}\n"
        f"Страна/код региона: {country}\n"
        f"Регион (справочно): {region}\n"
        f"Оператор (если доступно): {operator}\n"
        f"Часовой пояс: {zones}\n\n"
        "⚠️ Это справочные сведения по телефонному плану. Они не устанавливают личность "
        "владельца и не показывают его текущее местоположение."
    )


def username_result(raw: str) -> str:
    value = raw.strip()
    value = re.sub(r"^https?://t\\.me/", "", value, flags=re.IGNORECASE)
    value = value.split("?", 1)[0].strip("/")
    if value.startswith("@"):
        value = value[1:]
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", value):
        return "❌ Неверный формат. Используйте 5–32 символа: латинские буквы, цифры и подчёркивание."
    return (
        "👤 Username распознан\n\n"
        f"Имя: @{value}\n"
        f"Публичная ссылка: https://t.me/{value}\n\n"
        "ℹ️ Я не могу подтвердить владельца аккаунта или получить скрытые сведения. "
        "Откройте ссылку, чтобы проверить публичную страницу в Telegram."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")
    message = update.effective_message
    text = (message.text or "").strip()
    if mode == "phone":
        await message.reply_text(phone_result(text), reply_markup=MENU)
        context.user_data.pop("mode", None)
    elif mode == "username":
        await message.reply_text(username_result(text), reply_markup=MENU)
        context.user_data.pop("mode", None)
    else:
        await message.reply_text(
            "Сначала выберите действие или отправьте /start.", reply_markup=MENU
        )


def build_application():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
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
