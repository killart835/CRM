import os
import httpx
import logging
import asyncio
from threading import Thread
from flask import Flask, jsonify, request, render_template
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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
sessions = {}
tg_application = None  # Ссылка на инстанс инстанса Telegram-приложения
loop = None            # Асинхронный цикл для передачи задач из Flask в Telegram

def get_session(chat_id, client_name="Клиент"):
    if chat_id not in sessions:
        # Генерируем случайный красивый цвет аватара для CRM панели
        bgs = ["#E8F0FE", "#FCE8E6", "#E6F4EA", "#FEF9E7"]
        tcs = ["#1557B0", "#C5221F", "#1E7E34", "#856404"]
        i = len(sessions) % 4
        
        # Инициалы для CRM
        ini = "".join([w[0].upper() for w in client_name.split() if w])[:2] or "КЛ"
        
        sessions[chat_id] = {
            "id": chat_id,
            "name": client_name,
            "ini": ini,
            "phone": "+380 (XX) XXX-XX-XX",
            "email": f"tg_{chat_id}@smartcrm.io",
            "bg": bgs[i],
            "tc": tcs[i],
            "status": "new",
            "src": "Telegram Bot",
            "prod": "Уточняется",
            "budget": "—",
            "mode": "auto",
            "msgs": []
        }
    return sessions[chat_id]

# ── Groq API ─────────────────────────────────────────────────────────────────
async def ask_groq(history: list) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={"model": GROQ_MODEL, "max_tokens": 400, "temperature": 0.7, "messages": messages}
            )
            data = resp.json()
            if "error" in data:
                return f"⚠️ Ошибка Groq: {data['error']['message']}"
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            return f"⚠️ Ошибка подключения к Groq API: {str(e)}"

# ── Пересылка менеджеру в Telegram ───────────────────────────────────────────
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

# ── Команды Бота ─────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    client_name = user.full_name or f"Клиент {update.effective_chat.id}"
    session = get_session(update.effective_chat.id, client_name)
    session["msgs"] = [] # сброс истории при /start
    await update.message.reply_text(
        "👋 Привет! Я менеджер SmartShop.\n"
        "Чем могу помочь? Спрашивайте о наших товарах!"
    )

async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != MANAGER_CHAT_ID:
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🤖 Авто (Groq)", callback_data="setmode:auto"),
        InlineKeyboardButton("✋ Ручной", callback_data="setmode:manual"),
    ]])
    await update.message.reply_text("Выберите режим по умолчанию для новых клиентов:", reply_markup=kb)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != MANAGER_CHAT_ID:
        return
    total = len(sessions)
    auto = sum(1 for s in sessions.values() if s["mode"] == "auto")
    manual = total - auto
    await update.message.reply_text(
        f"📊 *Статистика*\nВсего диалогов: {total}\n🤖 Авто-режим: {auto}\n✋ Ручной режим: {manual}",
        parse_mode="Markdown"
    )

# ── Входящие сообщения от клиентов в Telegram ────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text
    user = update.effective_user
    client_name = user.full_name or f"Клиент {chat_id}"

    if chat_id == MANAGER_CHAT_ID:
        await handle_manager_message(update, context, text)
        return

    session = get_session(chat_id, client_name)
    
    # Сохраняем время
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M")
    
    # Добавляем в историю для Groq и отображения в CRM панели
    groq_history = [{"role": "user", "content": m["t"]} for m in session["msgs"] if m["r"] == "client"]
    groq_history.append({"role": "user", "content": text})
    
    session["msgs"].append({"r": "client", "t": text, "ts": ts})

    await forward_to_manager(context, chat_id, client_name, text)

    if session["mode"] == "auto":
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await ask_groq(groq_history)
        session["msgs"].append({"r": "ai", "t": reply, "ts": ts})
        await update.message.reply_text(reply)

        if MANAGER_CHAT_ID:
            await context.bot.send_message(
                chat_id=MANAGER_CHAT_ID,
                text=f"🤖 *Авто-ответ* для {client_name}:\n{reply}",
                parse_mode="Markdown"
            )
    else:
        await update.message.reply_text("⏳ Менеджер скоро ответит вам!")

async def handle_manager_message(update, context, text):
    if not update.message.reply_to_message:
        await update.message.reply_text("ℹ️ Чтобы ответить клиенту, используйте кнопку «Ответить» под его сообщением.")
        return
    fwd_text = update.message.reply_to_message.text or ""
    try:
        client_id = int(fwd_text.split("id: `")[1].split("`")[0])
    except Exception:
        await update.message.reply_text("⚠️ Не удалось определить клиента.")
        return

    from datetime import datetime
    ts = datetime.now().strftime("%H:%M")

    session = get_session(client_id)
    session["msgs"].append({"r": "mgr", "t": text, "ts": ts})
    await context.bot.send_message(chat_id=client_id, text=text)
    await update.message.reply_text(f"✅ Отправлено клиенту {client_id}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("reply:"):
        client_id = data.split(":")[1]
        await query.message.reply_text(
            f"✍️ Ответьте на *это* сообщение, чтобы отправить клиенту `{client_id}`:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])
        )
    elif data.startswith("auto:"):
        client_id = int(data.split(":")[1])
        session = get_session(client_id)
        await context.bot.send_chat_action(chat_id=client_id, action="typing")
        
        groq_history = [{"role": "user", "content": m["t"]} for m in session["msgs"] if m["r"] == "client"]
        reply = await ask_groq(groq_history)
        
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M")
        session["msgs"].append({"r": "ai", "t": reply, "ts": ts})
        
        await context.bot.send_message(chat_id=client_id, text=reply)
        await query.message.reply_text(f"🤖 Groq ответил клиенту {client_id}:\n\n{reply}")
    elif data.startswith("setmode:"):
        mode = data.split(":")[1]
        context.bot_data["default_mode"] = mode
        label = "🤖 Авто (Groq)" if mode == "auto" else "✋ Ручной"
        await query.edit_message_text(f"Режим по умолчанию установлен: {label}")
    elif data == "cancel":
        await query.edit_message_text("❌ Отменено.")


# ── СЕРВЕР FLASK ДЛЯ СРМ ПАНЕЛИ ──────────────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def index():
    # Отдает страницу HTML интерфейса из templates/index.html
    return render_template('index.html')

@app.route('/api/clients', methods=['GET'])
def get_clients():
    # Возвращает массив клиентов и их сообщения на сайт-панель
    return jsonify(list(sessions.values()))

@app.route('/api/send', methods=['POST'])
def send_from_crm():
    # Получает сообщение из веб-админки и пересылает его в реальный Telegram
    global tg_application, loop
    data = request.json or {}
    chat_id = int(data.get("chat_id", 0))
    text = data.get("text", "").strip()
    
    if not chat_id or not text:
        return jsonify({"status": "error", "message": "Missing chat_id or text"}), 400
        
    session = sessions.get(chat_id)
    if not session:
        return jsonify({"status": "error", "message": "Client session not found"}), 404
        
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M")
    session["msgs"].append({"r": "mgr", "t": text, "ts": ts})
    
    # Асинхронно отправляем сообщение пользователю в Telegram через запущенный инстанс бота
    if tg_application and loop:
        asyncio.run_coroutine_threadsafe(
            tg_application.bot.send_message(chat_id=chat_id, text=text), 
            loop
        )
        return jsonify({"status": "success"})
    else:
        return jsonify({"status": "error", "message": "Telegram app context unavailable"}), 500


def run_telegram():
    """Запуск Telegram бота в отдельном потоке с собственным циклом событий"""
    global tg_application, loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    tg_application = Application.builder().token(TELEGRAM_TOKEN).build()
    tg_application.add_handler(CommandHandler("start", cmd_start))
    tg_application.add_handler(CommandHandler("mode", cmd_mode))
    tg_application.add_handler(CommandHandler("stats", cmd_stats))
    tg_application.add_handler(CallbackQueryHandler(handle_callback))
    tg_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("Инициализация Telegram бота...")
    
    # Правильный асинхронный запуск без блокировки потока
    loop.run_until_complete(tg_application.initialize())
    loop.run_until_complete(tg_application.start())
    loop.run_until_complete(tg_application.updater.start_polling(drop_pending_updates=True))
    
    # Запускаем бесконечный цикл обработки событий бота
    loop.run_forever()

if __name__ == "__main__":
    # 1. Запускаем Телеграм-бота в отдельном фоновом потоке
    t = Thread(target=run_telegram, daemon=True)
    t.start()
    
    # 2. Запускаем веб-сервер Flask
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Запуск Flask CRM на порту {port}...")
    try:
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    except OSError as e:
        if e.errno == 98:
            logger.warning(f"Порт {port} занят. Пробуем запуститься на резервном...")
            app.run(host='0.0.0.0', port=0, debug=False, use_reloader=False)
        else:
            raise e
