import telebot
import random
import os
import threading
import time
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from flask import Flask, request, abort

# Load env vars
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g., https://your-project.vercel.app/api/webhook

# Setup Flask app
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)

# Setup logging
logging.basicConfig(filename='bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger('').addHandler(console)

# Daftar kata terlarang
FORBIDDEN_WORDS = ['spam', 'badword']

# Locks untuk thread-safety
queue_lock = threading.Lock()
chats_lock = threading.Lock()
genders_lock = threading.Lock()
rate_limit_lock = threading.Lock()

# Rate limiting: {user_id: [timestamp]}
rate_limits = defaultdict(list)

# Postgres connection pool (min 4, max 50 untuk scale tinggi)
db_pool = psycopg2.pool.ThreadedConnectionPool(4, 50, DATABASE_URL)

# Daftar pseudonym
MALE_NICKNAMES = ['AnonGuy', 'MisterX', 'BroChat', 'DudeAnon']
FEMALE_NICKNAMES = ['AnonGal', 'MissY', 'SisChat', 'LadyAnon']

def generate_pseudonym(gender):
    if gender == 'male':
        base = random.choice(MALE_NICKNAMES)
    else:
        base = random.choice(FEMALE_NICKNAMES)
    return base + str(random.randint(100, 999))

def check_forbidden(text):
    return any(word in text.lower() for word in FORBIDDEN_WORDS)

def get_db_connection():
    return db_pool.getconn()

def release_db_connection(conn):
    db_pool.putconn(conn)

def is_rate_limited(user_id):
    with rate_limit_lock:
        now = time.time()
        rate_limits[user_id] = [t for t in rate_limits[user_id] if now - t < 1]  # 1 pesan/detik
        if len(rate_limits[user_id]) >= 1:
            return True
        rate_limits[user_id].append(now)
        return False

def init_db():
    """Init tables dan indexes untuk scale"""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Tables
        cur.execute('''
        CREATE TABLE IF NOT EXISTS queues (
            user_id BIGINT PRIMARY KEY,
            gender TEXT NOT NULL,
            nick TEXT,
            timestamp TIMESTAMPTZ DEFAULT NOW()
        );
        ''')
        cur.execute('''
        CREATE TABLE IF NOT EXISTS active_chats (
            user_id BIGINT PRIMARY KEY,
            partner_id BIGINT,
            timestamp TIMESTAMPTZ DEFAULT NOW()
        );
        ''')
        cur.execute('''
        CREATE TABLE IF NOT EXISTS user_genders (
            user_id BIGINT PRIMARY KEY,
            gender TEXT
        );
        ''')
        # Indexes untuk fast query
        cur.execute('CREATE INDEX IF NOT EXISTS idx_queues_gender ON queues(gender);')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_active_chats_partner ON active_chats(partner_id);')
        conn.commit()
        logging.info("DB initialized successfully.")
    except Exception as e:
        logging.error(f"DB init error: {e}")
    finally:
        cur.close()
        release_db_connection(conn)

def cleanup_inactive():
    """Auto-cleanup chats >5 menit"""
    while True:
        time.sleep(300)  # 5 menit
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM active_chats WHERE timestamp < NOW() - INTERVAL '5 minutes';")
            conn.commit()
            logging.info("Cleanup inactive chats done.")
        except Exception as e:
            logging.error(f"Cleanup error: {e}")
        finally:
            cur.close()
            release_db_connection(conn)

# Start cleanup thread
threading.Thread(target=cleanup_inactive, daemon=True).start()

def create_gender_keyboard():
    markup = InlineKeyboardMarkup()
    markup.row_width = 2
    markup.add(
        InlineKeyboardButton("🧔 *Pria/Male*", callback_data="gender_male"),
        InlineKeyboardButton("👩 *Wanita/Female*", callback_data="gender_female")
    )
    return markup

def create_action_keyboard():
    markup = InlineKeyboardMarkup()
    markup.row_width = 2
    markup.add(
        InlineKeyboardButton("🔄 *Next*", callback_data="action_next"),
        InlineKeyboardButton("🛑 *Stop*", callback_data="action_stop")
    )
    return markup

def get_queue(gender):
    """Ambil queue dari DB dengan lock untuk concurrency"""
    conn = get_db_connection()
    try:
        with queue_lock:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT user_id, nick, gender FROM queues WHERE gender=%s ORDER BY timestamp FOR UPDATE SKIP LOCKED LIMIT 1;", (gender,))
            row = cur.fetchone()
            if row:
                cur.execute("DELETE FROM queues WHERE user_id=%s;", (row['user_id'],))
                conn.commit()
                return (row['user_id'], row['nick'], row['gender'])
            return None
    except Exception as e:
        logging.error(f"Get queue error: {e}")
        return None
    finally:
        cur.close()
        release_db_connection(conn)

def add_to_queue(user_id, gender, nick):
    with queue_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO queues (user_id, gender, nick) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET gender=%s, nick=%s, timestamp=NOW();", (user_id, gender, nick, gender, nick))
            conn.commit()
        except Exception as e:
            logging.error(f"Add to queue error: {e}")
        finally:
            cur.close()
            release_db_connection(conn)

def remove_from_queue(user_id):
    with queue_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM queues WHERE user_id=%s;", (user_id,))
            conn.commit()
        except Exception as e:
            logging.error(f"Remove from queue error: {e}")
        finally:
            cur.close()
            release_db_connection(conn)

def try_pair_gender(user_id, gender):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT partner_id FROM active_chats WHERE user_id=%s;", (user_id,))
        if cur.fetchone():
            cur.close()
            release_db_connection(conn)
            return  # Sudah paired
        cur.close()
        release_db_connection(conn)

        if gender == 'male':
            partner_data = get_queue('female')
            if partner_data:
                partner_id, partner_nick, _ = partner_data
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("INSERT INTO active_chats (user_id, partner_id) VALUES (%s, %s), (%s, %s) ON CONFLICT (user_id) DO UPDATE SET partner_id=%s;", (user_id, partner_id, partner_id, user_id, partner_id))
                cur.execute("UPDATE active_chats SET timestamp=NOW() WHERE user_id IN (%s, %s);", (user_id, partner_id))
                conn.commit()
                cur.close()
                release_db_connection(conn)
                user_nick = generate_pseudonym(gender)
                partner_nick = generate_pseudonym('female')
                markup = create_action_keyboard()
                bot.send_message(user_id, f"*Chat dimulai!* 🎉\nKamu dipasangkan dengan `{partner_nick}` \\(lawan gender\\).\nKirim pesan/foto/sticker untuk mulai!", parse_mode='Markdown', reply_markup=markup)
                bot.send_message(partner_id, f"*Chat dimulai!* 🎉\nKamu dipasangkan dengan `{user_nick}` \\(lawan gender\\).\nKirim pesan/foto/sticker untuk mulai!", parse_mode='Markdown', reply_markup=markup)
                logging.info(f"Paired {user_id} (male) with {partner_id} (female)")
                return
        elif gender == 'female':
            partner_data = get_queue('male')
            if partner_data:
                partner_id, partner_nick, _ = partner_data
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("INSERT INTO active_chats (user_id, partner_id) VALUES (%s, %s), (%s, %s) ON CONFLICT (user_id) DO UPDATE SET partner_id=%s;", (user_id, partner_id, partner_id, user_id, partner_id))
                cur.execute("UPDATE active_chats SET timestamp=NOW() WHERE user_id IN (%s, %s);", (user_id, partner_id))
                conn.commit()
                cur.close()
                release_db_connection(conn)
                user_nick = generate_pseudonym(gender)
                partner_nick = generate_pseudonym('male')
                markup = create_action_keyboard()
                bot.send_message(user_id, f"*Chat dimulai!* 🎉\nKamu dipasangkan dengan `{partner_nick}` \\(lawan gender\\).\nKirim pesan/foto/sticker untuk mulai!", parse_mode='Markdown', reply_markup=markup)
                bot.send_message(partner_id, f"*Chat dimulai!* 🎉\nKamu dipasangkan dengan `{user_nick}` \\(lawan gender\\).\nKirim pesan/foto/sticker untuk mulai!", parse_mode='Markdown', reply_markup=markup)
                logging.info(f"Paired {user_id} (female) with {partner_id} (male)")
                return

        # Masuk queue
        nick = generate_pseudonym(gender)
        add_to_queue(user_id, gender, nick)
        markup = create_action_keyboard()
        if gender == 'male':
            bot.send_message(user_id, f"*Sedang mencari...* ⏳\nTunggu pasangan wanita \\({nick}\\).", parse_mode='Markdown', reply_markup=markup)
        else:
            bot.send_message(user_id, f"*Sedang mencari...* ⏳\nTunggu pasangan pria \\({nick}\\).", parse_mode='Markdown', reply_markup=markup)
        logging.info(f"Added {user_id} ({gender}) to queue")
    except Exception as e:
        logging.error(f"Error in try_pair_gender: {e}")
        bot.send_message(user_id, "Maaf, ada error. Coba /start lagi.")

@bot.message_handler(commands=['start'])
def start(message):
    try:
        user_id = message.from_user.id
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT partner_id FROM active_chats WHERE user_id=%s;", (user_id,))
        if cur.fetchone():
            markup = create_action_keyboard()
            bot.reply_to(message, "*Kamu sudah dalam chat!* 📱\nGunakan tombol di bawah untuk *Next* atau *Stop*.", parse_mode='Markdown', reply_markup=markup)
            cur.close()
            release_db_connection(conn)
            return

        cur.execute("DELETE FROM user_genders WHERE user_id=%s;", (user_id,))
        conn.commit()
        cur.close()
        release_db_connection(conn)

        markup = create_gender_keyboard()
        bot.reply_to(message, "*Selamat datang di Anonymous Chat!* 👋\nPilih gender kamu untuk dipasangkan dengan lawan gender.\nGunakan tombol *Next* untuk match selanjutnya.\nGunakan tombol *Stop* untuk hentikan percakapan.", parse_mode='Markdown', reply_markup=markup)
    except Exception as e:
        logging.error(f"Error in start: {e}")
        bot.reply_to(message, "Maaf, ada error. Coba lagi nanti.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('gender_'))
def handle_gender(call):
    try:
        user_id = call.from_user.id
        gender = call.data.split('_')[1]
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO user_genders (user_id, gender) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET gender=%s;", (user_id, gender, gender))
        conn.commit()
        cur.close()
        release_db_connection(conn)

        bot.answer_callback_query(call.id)
        bot.edit_message_text("*Gender dipilih!* ✅\nSekarang cari pasangan...", call.message.chat.id, call.message.message_id, parse_mode='Markdown')

        try_pair_gender(user_id, gender)
    except Exception as e:
        logging.error(f"Error in handle_gender: {e}")
        bot.answer_callback_query(call.id, "Error, coba /start lagi.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('action_'))
def handle_action(call):
    try:
        user_id = call.from_user.id
        action = call.data.split('_')[1]
        bot.answer_callback_query(call.id)

        if action == 'next':
            next_match(user_id)
            bot.edit_message_text("*Match selanjutnya dimulai!* 🔄\nSedang mencari pasangan baru...", call.message.chat.id, call.message.message_id, parse_mode='Markdown')
        elif action == 'stop':
            stop(user_id)
            bot.edit_message_text("*Percakapan dihentikan.* 👋\n/start untuk mulai lagi.", call.message.chat.id, call.message.message_id, parse_mode='Markdown')
    except Exception as e:
        logging.error(f"Error in handle_action: {e}")
        bot.answer_callback_query(call.id, "Error, coba lagi.")

def next_match(user_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT partner_id FROM active_chats WHERE user_id=%s;", (user_id,))
        partner_row = cur.fetchone()
        partner_id = partner_row[0] if partner_row else None

        if partner_id:
            bot.send_message(partner_id, "*Pasanganmu pindah ke match selanjutnya.* 🔄\nKamu akan dicocokkan lagi.", parse_mode='Markdown')
            cur.execute("DELETE FROM active_chats WHERE user_id IN (%s, %s);", (user_id, partner_id))
            conn.commit()
            remove_from_queue(partner_id)
            cur.execute("SELECT gender FROM user_genders WHERE user_id=%s;", (user_id,))
            gender_row = cur.fetchone()
            gender = gender_row[0] if gender_row else None
            cur.close()
            release_db_connection(conn)
            if gender:
                try_pair_gender(user_id, gender)
        else:
            markup = create_action_keyboard()
            bot.send_message(user_id, "*Kamu belum dalam chat.* 📱\nGunakan /start dulu.", parse_mode='Markdown', reply_markup=markup)
    except Exception as e:
        logging.error(f"Error in next_match: {e}")

def stop(user_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT partner_id FROM active_chats WHERE user_id=%s;", (user_id,))
        partner_row = cur.fetchone()
        partner_id = partner_row[0] if partner_row else None

        if partner_id:
            bot.send_message(partner_id, "*Pasanganmu stop percakapan.* 🛑", parse_mode='Markdown')
            cur.execute("DELETE FROM active_chats WHERE user_id IN (%s, %s);", (user_id, partner_id))
            conn.commit()
            remove_from_queue(partner_id)

        cur.execute("DELETE FROM active_chats WHERE user_id=%s;", (user_id,))
        cur.execute("DELETE FROM user_genders WHERE user_id=%s;", (user_id,))
        remove_from_queue(user_id)
        conn.commit()
        cur.close()
        release_db_connection(conn)
    except Exception as e:
        logging.error(f"Error in stop: {e}")

@bot.message_handler(commands=['next'])
def cmd_next(message):
    next_match(message.from_user.id)

@bot.message_handler(commands=['stop'])
def cmd_stop(message):
    stop(message.from_user.id)
    bot.reply_to(message, "*Percakapan dihentikan.* 👋\n/start untuk mulai lagi.", parse_mode='Markdown')

def handle_generic_message(message, content_type):
    try:
        user_id = message.from_user.id
        if is_rate_limited(user_id):
            bot.reply_to(message, "*Terlalu cepat!* ⏱️\nTunggu 1 detik sebelum kirim lagi.")
            return

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT partner_id FROM active_chats WHERE user_id=%s;", (user_id,))
        partner_row = cur.fetchone()
        partner_id = partner_row[0] if partner_row else None
        cur.close()
        release_db_connection(conn)

        if not partner_id:
            markup = create_action_keyboard()
            bot.reply_to(message, "*Belum ada pasangan.* ⏳\nTunggu sebentar atau gunakan tombol *Next* di bawah.", parse_mode='Markdown', reply_markup=markup)
            return

        if content_type == 'text':
            text = message.text
            if check_forbidden(text):
                bot.reply_to(message, "*Pesanmu mengandung kata terlarang.* ❌\nCoba lagi.", parse_mode='Markdown')
                return
            bot.send_message(partner_id, f"*Anon:* {text}", parse_mode='Markdown')
        else:
            # Forward media
            caption = getattr(message, 'caption', None)
            if caption:
                caption = f"*Anon:* {caption}"
            bot.forward_message(partner_id, message.chat.id, message.message_id, caption=caption, parse_mode='Markdown' if caption else None)

        logging.info(f"Forwarded {content_type} from {user_id} to {partner_id}")
    except Exception as e:
        logging.error(f"Error in handle_{content_type}: {e}")
        bot.reply_to(message, "Maaf, error kirim pesan. Coba lagi.")

# Handlers
@bot.message_handler(content_types=['text'])
def handle_text(message):
    handle_generic_message(message, 'text')

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    handle_generic_message(message, 'photo')

@bot.message_handler(content_types=['sticker'])
def handle_sticker(message):
    handle_generic_message(message, 'sticker')

@bot.message_handler(content_types=['video'])
def handle_video(message):
    handle_generic_message(message, 'video')

@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    handle_generic_message(message, 'voice')

@bot.message_handler(content_types=['document'])
def handle_document(message):
    handle_generic_message(message, 'document')

@bot.message_handler(content_types=['audio', 'animation', 'video_note', 'location', 'contact', 'poll', 'dice'])
def handle_other(message):
    handle_generic_message(message, 'other')

# Flask webhook endpoint
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    else:
        abort(403)

# Health check
@app.route('/')
def index():
    return "Bot is running!"

# Init & set webhook on startup
def init_bot():
    init_db()
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    logging.info(f"Webhook set to {WEBHOOK_URL}")

if __name__ == '__main__':
    init_bot()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
