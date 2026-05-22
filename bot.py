import os
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ── Настройки ────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY   = os.getenv("GROQ_API_KEY")
MANAGER_CHAT_ID = int(os.getenv("MANAGER_CHAT_ID", "0"))  # ваш Telegram ID
GROQ_MODEL     = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", (
    "Ты — менеджер по продажам магазина SmartShop. "
    "Отвечай на языке клиента (украинский или русский). "
    "Будь дружелюбным, кратким и убедительным. "
    "Твоя цель — помочь клиенту выбрать товар и оформить покупку. "
    "Не придумывай цены — используй только данные из списка товаров.\n\n"
    "Товары и цены:\n"
    "iPhone 15 Pro 128GB — 45 000 ₴\n"
    "iPhone 15 Pro 256GB — 47 500 ₴\n"
    "MacBook Air M3 8GB — 54 000 ₴\n"
    "MacBook Air M3 16GB — 68 000 ₴\n"
    "AirPods Pro 2 — 8 500 ₴\n"
    "iPad Pro 11\" — 30 500 ₴\n"
    "iPad Pro 13\" — 41 000 ₴\n"
    "Apple Watch Ultra 2 — 25 000 ₴"
))

# ── Хранилище диалогов (в памяти) ────────────────────────────────────────────
# chat_id -> {"mode": "auto"|"manual", "history": [...]}
sessions = {}

def get_session(chat_id):
    if chat_id not in sessions:
        sessions[chat_id] = {"mode": "auto", "history": []}
    return sessions[chat_id]

# ── Groq API ─────────────────────────────────────────────────────────────────
async def ask_groq(history: list) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={"model": GROQ_MODEL, "max_tokens": 400, "temperature": 0.7, "messages": messages}
        )
    data = resp.json()
    if "error" in data:
        return f"⚠️ Ошибка Groq: {data['error']['message']}"
    return data["choices"][0]["message"]["content"]

# ── Пересылка менеджеру ───────────────────────────────────────────────────────
async def forward_to_manager(context, client_id, client_name, text):
    if not MANAGER_CHAT_ID:
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✍️ Ответить", callback_data=f"reply:{client_id}"),
        InlineKeyboardButton("🤖 Авто-ответ", callback_data=f"auto:{client_id}"),
    ]])
    await context.bot.send_message(
        chat_id=MANAGER_CHAT_ID,
        text=f"📩 *{client_name}* (id: `{client_id}`):\n{text}",
        parse_mode="Markdown",
        reply_markup=kb
    )

# ── Команды ───────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_chat.id)
    session["history"] = []
    await update.message.reply_text(
        "👋 Привет! Я менеджер SmartShop.\n"
        "Чем могу помочь? Спрашивайте о наших товарах!"
    )

async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Только для менеджера: переключить режим клиента"""
    if update.effective_chat.id != MANAGER_CHAT_ID:
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🤖 Авто (Groq)", callback_data="setmode:auto"),
        InlineKeyboardButton("✋ Ручной", callback_data="setmode:manual"),
    ]])
    await update.message.reply_text("Выберите режим по умолчанию для новых клиентов:", reply_markup=kb)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика для менеджера"""
    if update.effective_chat.id != MANAGER_CHAT_ID:
        return
    total = len(sessions)
    auto = sum(1 for s in sessions.values() if s["mode"] == "auto")
    manual = total - auto
    await update.message.reply_text(
        f"📊 *Статистика*\n"
        f"Всего диалогов: {total}\n"
        f"🤖 Авто-режим: {auto}\n"
        f"✋ Ручной режим: {manual}",
        parse_mode="Markdown"
    )

# ── Входящие сообщения от клиентов ───────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text
    user = update.effective_user
    client_name = user.full_name or f"Клиент {chat_id}"

    # Если пишет менеджер — это ответ клиенту (через reply)
    if chat_id == MANAGER_CHAT_ID:
        await handle_manager_message(update, context, text)
        return

    session = get_session(chat_id)
    session["history"].append({"role": "user", "content": text})

    # Пересылаем менеджеру в любом случае
    await forward_to_manager(context, chat_id, client_name, text)

    if session["mode"] == "auto":
        # Авто-ответ через Groq
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await ask_groq(session["history"])
        session["history"].append({"role": "assistant", "content": reply})
        await update.message.reply_text(reply)

        # Уведомляем менеджера об авто-ответе
        if MANAGER_CHAT_ID:
            await context.bot.send_message(
                chat_id=MANAGER_CHAT_ID,
                text=f"🤖 *Авто-ответ* для {client_name}:\n{reply}",
                parse_mode="Markdown"
            )
    else:
        # Ручной режим — уведомляем менеджера
        await update.message.reply_text("⏳ Менеджер скоро ответит вам!")

async def handle_manager_message(update, context, text):
    """Менеджер отвечает клиенту через reply на пересланное сообщение"""
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "ℹ️ Чтобы ответить клиенту, используйте кнопку «Ответить» под его сообщением."
        )
        return
    # Извлекаем client_id из текста пересланного сообщения
    fwd_text = update.message.reply_to_message.text or ""
    try:
        client_id = int(fwd_text.split("id: `")[1].split("`")[0])
    except Exception:
        await update.message.reply_text("⚠️ Не удалось определить клиента.")
        return

    session = get_session(client_id)
    session["history"].append({"role": "assistant", "content": text})
    await context.bot.send_message(chat_id=client_id, text=text)
    await update.message.reply_text(f"✅ Отправлено клиенту {client_id}")

# ── Кнопки (callback) ─────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # Менеджер нажал «Ответить» — просит написать ответ
    if data.startswith("reply:"):
        client_id = data.split(":")[1]
        await query.message.reply_text(
            f"✍️ Ответьте на *это* сообщение, чтобы отправить клиенту `{client_id}`:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="cancel")
            ]])
        )

    # Менеджер нажал «Авто-ответ» — Groq отвечает за него
    elif data.startswith("auto:"):
        client_id = int(data.split(":")[1])
        session = get_session(client_id)
        await context.bot.send_chat_action(chat_id=client_id, action="typing")
        reply = await ask_groq(session["history"])
        session["history"].append({"role": "assistant", "content": reply})
        await context.bot.send_message(chat_id=client_id, text=reply)
        await query.message.reply_text(f"🤖 Groq ответил клиенту {client_id}:\n\n{reply}")

    # Переключение режима по умолчанию
    elif data.startswith("setmode:"):
        mode = data.split(":")[1]
        # Применяем ко всем новым сессиям через context
        context.bot_data["default_mode"] = mode
        label = "🤖 Авто (Groq)" if mode == "auto" else "✋ Ручной"
        await query.edit_message_text(f"Режим по умолчанию установлен: {label}")

    elif data == "cancel":
        await query.edit_message_text("❌ Отменено.")

# ── Запуск ────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ Бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
