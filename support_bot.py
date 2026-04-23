import telebot
from telebot import types
import time
import html
import os
import glob
import threading
import datetime
import sqlite3
import psycopg2 # Драйвер для Postgres

# --- КОНФИГУРАЦИЯ ---

PROJECT_NAME = os.getenv('PROJECT_NAME', 'VPN Support')
TOKEN = os.getenv('TELEGRAM_TOKEN')
ADMIN_GROUP_ID = int(os.getenv('ADMIN_GROUP_ID', '0'))
BANS_TOPIC_ID = int(os.getenv('BANS_TOPIC_ID', '1'))
AUTO_CLOSE_HOURS = int(os.getenv('AUTO_CLOSE_HOURS', '24'))

PG_HOST = os.getenv('PG_HOST', 'remnawave_bot_db')
PG_DB = os.getenv('PG_DB', 'remnawave_bot')
PG_USER = os.getenv('PG_USER', 'remnawave_user')
PG_PASS = os.getenv('PG_PASS', '')

# Локальная БД саппорта (для тикетов и банов)
DB_PATH = "support.db"
db_lock = threading.Lock()

bot = telebot.TeleBot(TOKEN)

# --- ФУНКЦИЯ ПРОВЕРКИ ПОДПИСКИ (Postgres) ---
def get_remnawave_info(tg_id):
    try:
        conn = psycopg2.connect(
            host=os.getenv('PG_HOST', 'remnawave_bot_db'),
            database=os.getenv('PG_DB', 'remnawave_bot'),
            user=os.getenv('PG_USER', 'remnawave_user'),
            password=os.getenv('PG_PASS', ''),
            connect_timeout=3
        )
        with conn.cursor() as cur:
            # Запрос: ищем юзера и его самую свежую подписку
            query = """
                SELECT 
                    u.balance_kopeks, 
                    s.status, 
                    s.end_date, 
                    s.traffic_limit_gb, 
                    s.traffic_used_gb
                FROM users u
                LEFT JOIN subscriptions s ON u.id = s.user_id
                WHERE u.telegram_id = %s
                ORDER BY s.created_at DESC LIMIT 1;
            """
            cur.execute(query, (tg_id,))
            res = cur.fetchone()
            
            if not res:
                return "❌ Не найден в базе продаж"
            
            balance = res[0] / 100 if res[0] else 0
            status = res[1] or "нет"
            # Форматируем дату (end_date — это datetime объект из Postgres)
            end_date = res[2].strftime("%d.%m.%Y") if res[2] else "—"
            t_limit = res[3] or 0
            t_used = round(res[4], 2) if res[4] else 0
            
            icon = "🟢" if status == "active" else "🔴"
            
            return (f"{icon} <b>Статус:</b> {status}\n"
                    f"📅 <b>До:</b> {end_date}\n"
                    f"📊 <b>Трафик:</b> {t_used}/{t_limit} GB\n"
                    f"💰 <b>Баланс:</b> {balance} руб.")
    except Exception as e:
        return f"⚠️ Ошибка связи с БД: {e}"
    finally:
        if 'conn' in locals(): conn.close()

# --- ЛОГИКА ЛОКАЛЬНОЙ БД (SQLite) ---
def run_query(query, params=(), fetch=False, fetchall=False):
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            if fetch: return cursor.fetchone()
            if fetchall: return cursor.fetchall()
            conn.commit()

def init_db():
    run_query("CREATE TABLE IF NOT EXISTS users (uid INTEGER PRIMARY KEY, is_banned INTEGER DEFAULT 0, ban_reason TEXT)")
    run_query("CREATE TABLE IF NOT EXISTS tickets (ticket_id TEXT PRIMARY KEY, uid INTEGER, thread_id INTEGER, status TEXT DEFAULT 'open', created_at REAL, last_activity REAL)")

init_db()

# --- КЛАВИАТУРЫ ---
def get_main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("🎫 Открыть новый тикет"))
    return markup

def get_active_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("❌ Закрыть текущий тикет"))
    return markup

def get_admin_buttons(user_id):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🔒 Закрыть", callback_data=f"force_close_{user_id}"),
        types.InlineKeyboardButton("🚫 Забанить", callback_data=f"banmenu_{user_id}")
    )
    return kb

# --- ОБРАБОТКА ТИКЕТОВ ---
@bot.message_handler(commands=['start'])
def handle_start(message):
    row = run_query("SELECT is_banned FROM users WHERE uid=?", (message.from_user.id,), fetch=True)
    if row and row[0] == 1: return bot.send_message(message.chat.id, "❌ Доступ закрыт.")
    bot.send_message(message.chat.id, f"👋 {PROJECT_NAME}. Нажмите кнопку ниже для связи.", reply_markup=get_main_menu())
@bot.message_handler(content_types=['text', 'photo', 'video', 'document', 'voice'], func=lambda m: m.chat.type == 'private')
def handle_private(message):
    uid = message.from_user.id
    # Проверка бана
    row = run_query("SELECT is_banned FROM users WHERE uid=?", (uid,), fetch=True)
    if row and row[0] == 1: return

    ticket = run_query("SELECT ticket_id, thread_id FROM tickets WHERE uid=? AND status='open'", (uid,), fetch=True)

    if message.text == "🎫 Открыть новый тикет":
        if ticket: return bot.send_message(message.chat.id, "У вас уже есть открытый тикет.")
        
        # ID тикета: T-Дата-Номер
        date_prefix = datetime.datetime.now().strftime("%d%m%y")
        count = run_query("SELECT COUNT(*) FROM tickets WHERE ticket_id LIKE ?", (f"T-{date_prefix}-%",), fetch=True)[0]
        t_id = f"T-{date_prefix}-{count + 1}"
        
        # Пробиваем инфу из базы RemnaWave
        user_info = get_remnawave_info(uid)
        
        try:
            topic = bot.create_forum_topic(ADMIN_GROUP_ID, f"⏳ {t_id} | {message.from_user.first_name}")
            bot.send_message(
                ADMIN_GROUP_ID, 
                f"🆕 <b>Новое обращение: {t_id}</b>\n"
                f"👤 От: {html.escape(message.from_user.first_name)} (ID: <code>{uid}</code>)\n\n"
                f"💳 <b>Данные подписки:</b>\n{user_info}",
                message_thread_id=topic.message_thread_id, 
                parse_mode="HTML", 
                reply_markup=get_admin_buttons(uid)
            )
            run_query("INSERT INTO tickets (ticket_id, uid, thread_id, status, created_at, last_activity) VALUES (?, ?, ?, 'open', ?, ?)", 
                      (t_id, uid, topic.message_thread_id, time.time(), time.time()))
            bot.send_message(message.chat.id, "✅ Тикет открыт. Напишите ваш вопрос.", reply_markup=get_active_menu())
        except Exception as e:
            bot.send_message(message.chat.id, "⚠️ Ошибка при создании тикета. Попробуйте позже.")
            print(f"Topic error: {e}")

    elif message.text == "❌ Закрыть текущий тикет":
        if ticket:
            run_query("UPDATE tickets SET status='closed' WHERE uid=? AND status='open'", (uid,))
            bot.close_forum_topic(ADMIN_GROUP_ID, ticket[1])
            bot.send_message(message.chat.id, "🏁 Тикет закрыт.", reply_markup=get_main_menu())
    else:
        if not ticket: return bot.send_message(message.chat.id, "⚠️ Нажмите «Открыть новый тикет».")
        bot.copy_message(ADMIN_GROUP_ID, message.chat.id, message.message_id, message_thread_id=ticket[1])
        run_query("UPDATE tickets SET last_activity=? WHERE uid=? AND status='open'", (time.time(), uid))

@bot.message_handler(func=lambda m: m.chat.id == ADMIN_GROUP_ID and m.message_thread_id is not None)
def handle_admin_reply(message):
    ticket = run_query("SELECT uid FROM tickets WHERE thread_id=? AND status='open'", (message.message_thread_id,), fetch=True)
    if ticket:
        try: bot.copy_message(ticket[0], ADMIN_GROUP_ID, message.message_id)
        except: pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("force_close_"))
def admin_close(call):
    uid = int(call.data.split("_")[2])
    ticket = run_query("SELECT thread_id, ticket_id FROM tickets WHERE uid=? AND status='open'", (uid,), fetch=True)
    if ticket:
        run_query("UPDATE tickets SET status='closed' WHERE uid=? AND status='open'", (uid,))
        bot.close_forum_topic(ADMIN_GROUP_ID, ticket[0])
        bot.send_message(uid, "🔒 Ваш тикет был закрыт поддержкой.", reply_markup=get_main_menu())
        bot.answer_callback_query(call.id, "Тикет закрыт")

bot.infinity_polling()
