import os
import html
import hashlib
import sqlite3
import uuid
import threading
import time
import telebot
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

TOKEN = os.getenv("ANON_BOT_TOKEN")
if not TOKEN:
    TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("Telegram Bot Token topilmadi! .env faylini tekshiring.")

def _parse_int_env(name):
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} butun son bo'lishi kerak, hozir: {raw!r}")

SUPER_ADMIN_ID = _parse_int_env("SUPER_ADMIN_ID")
if SUPER_ADMIN_ID is None:
    raise ValueError("SUPER_ADMIN_ID topilmadi! .env faylida o'z chat_id ingizni ko'rsating.")

# .env orqali qo'shimcha adminlar (ixtiyoriy). Bular DB'dagi adminlar bilan birga ishlaydi.
ADMIN_IDS = set()
admin_ids_str = os.getenv("ADMIN_IDS")
if admin_ids_str:
    try:
        ADMIN_IDS = {int(x.strip()) for x in admin_ids_str.split(",") if x.strip()}
    except ValueError as e:
        raise ValueError(f"ADMIN_IDS noto'g'ri formatda: {e}")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "anon_qa_bot.db")

# Taxalluslarni tokendan hosil qilamiz — alohida sozlama talab qilmaydi,
# lekin tashqi odam chat_id ni taxallusdan tiklay olmaydi.
ANON_TAG_SALT = hashlib.sha256(TOKEN.encode()).hexdigest()
bot = telebot.TeleBot(TOKEN)

# Global state trackers for admin broadcasting and support mode.
# CPython'da dict amallari GIL ostida atomik — alohida qulf klassi kerak emas.
ADMIN_BROADCAST_STATES = {}
ADMIN_BROADCAST_PENDING_MSGS = {}
USER_SUPPORT_STATES = {}

# ---------------- SQLite Database Setup ----------------

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        
        # Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                link_code TEXT UNIQUE,
                active_target_id INTEGER,
                last_target_id INTEGER
            )
        """)
        
        # DIQQAT: bu yerda ataylab "message_mappings" jadvali YO'Q.
        # Kim kimga yozgani umuman diskka yozilmaydi — javob berish uchun kerakli
        # sender_id xabarning inline tugmasidagi callback_data dan o'qiladi
        # (resolve_sender_from_reply). Shu sababli javob berish muddatsiz ishlaydi.

        # Blocked users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS blocked_users (
                owner_id INTEGER,
                blocked_id INTEGER,
                PRIMARY KEY (owner_id, blocked_id)
            )
        """)
        
        # Reports table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER,
                reported_id INTEGER,
                message_content TEXT,
                message_type TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending'
            )
        """)
        
        # Banned users table (global bans)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                chat_id INTEGER PRIMARY KEY,
                banned_by INTEGER,
                reason TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Username bo'yicha qidiruvni tezlashtirish (/ban @user, /addadmin @user)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users (username)")

        # Eski bazalarni yangi ustunlar bilan moslashtirish
        for column_sql in (
            "ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN active_support_target INTEGER",
            # Faqat sonli hisoblagichlar — kim bilan aloqada bo'lgani saqlanmaydi
            "ALTER TABLE users ADD COLUMN sent_count INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN received_count INTEGER DEFAULT 0",
        ):
            try:
                cursor.execute(column_sql)
            except sqlite3.OperationalError:
                pass

        # Eski o'rnatishlarda qolgan xabar jurnalini butunlay o'chirish
        cursor.execute("DROP TABLE IF EXISTS message_mappings")

        # Support messages table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message_text TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending'
            )
        """)
            
        # Super Admin allaqachon /start bosgan bo'lsa, unga is_admin = 2 beriladi.
        # Ataylab INSERT qilinmaydi: soxta qator statistikani buzadi va
        # oldindan ma'lum link_code xavfsizlik teshigi bo'ladi.
        cursor.execute("UPDATE users SET is_admin = 2 WHERE chat_id = ?", (SUPER_ADMIN_ID,))

        # .env dagi ADMIN_IDS ni ham belgilab qo'yish (faqat mavjud foydalanuvchilar uchun)
        for admin_id in ADMIN_IDS:
            cursor.execute(
                "UPDATE users SET is_admin = 1 WHERE chat_id = ? AND is_admin = 0",
                (admin_id,)
            )

        conn.commit()
    finally:
        conn.close()

def is_super_admin(chat_id):
    return chat_id == SUPER_ADMIN_ID

def is_admin(chat_id):
    if chat_id == SUPER_ADMIN_ID or chat_id in ADMIN_IDS:
        return True
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT is_admin FROM users WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        return row is not None and row['is_admin'] >= 1
    except Exception as e:
        print(f"Error checking admin status: {e}")
        return False
    finally:
        conn.close()

def is_globally_banned(chat_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM banned_users WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        return row is not None
    except Exception as e:
        print(f"Error checking global ban: {e}")
        return False
    finally:
        conn.close()

def ban_user(chat_id, banned_by, reason=""):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO banned_users (chat_id, banned_by, reason) VALUES (?, ?, ?)",
            (chat_id, banned_by, reason)
        )
        conn.commit()
        return True
    except Exception as e:
        print(f"Error banning user: {e}")
        return False
    finally:
        conn.close()

def unban_user(chat_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM banned_users WHERE chat_id = ?", (chat_id,))
        conn.commit()
        return True
    except Exception as e:
        print(f"Error unbanning user: {e}")
        return False
    finally:
        conn.close()

def get_banned_users():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT bu.chat_id, bu.reason, bu.timestamp, u.username 
            FROM banned_users bu
            LEFT JOIN users u ON bu.chat_id = u.chat_id
            ORDER BY bu.timestamp DESC
        """)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()

def add_report(reporter_id, reported_id, content, content_type):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO reports (reporter_id, reported_id, message_content, message_type) VALUES (?, ?, ?, ?)",
            (reporter_id, reported_id, content, content_type)
        )
        conn.commit()
        return True
    except Exception as e:
        print(f"Error adding report: {e}")
        return False
    finally:
        conn.close()

def bump_message_counters(sender_id, recipient_id):
    """Faqat sonli hisoblagich. Kim kimga yozgani saqlanmaydi."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET sent_count = COALESCE(sent_count, 0) + 1 WHERE chat_id = ?",
            (sender_id,)
        )
        cursor.execute(
            "UPDATE users SET received_count = COALESCE(received_count, 0) + 1 WHERE chat_id = ?",
            (recipient_id,)
        )
        conn.commit()
    except Exception as e:
        print(f"Error updating message counters: {e}")
    finally:
        conn.close()

def add_support_message(user_id, text):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO support_messages (user_id, message_text) VALUES (?, ?)",
            (user_id, text)
        )
        msg_id = cursor.lastrowid
        conn.commit()
        return msg_id
    except Exception as e:
        print(f"Error adding support message: {e}")
        return None
    finally:
        conn.close()

def mark_support_resolved_for_user(user_id):
    """Foydalanuvchining ochiq murojaatlarini 'javob berilgan' deb belgilaydi."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE support_messages SET status = 'answered' WHERE user_id = ? AND status = 'in_progress'",
            (user_id,)
        )
        conn.commit()
    except Exception as e:
        print(f"Error resolving support ticket: {e}")
    finally:
        conn.close()

def set_admin_support_target(admin_chat_id, user_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET active_support_target = ? WHERE chat_id = ?", (user_id, admin_chat_id))
        conn.commit()
    except Exception as e:
        print(f"Error setting admin support target: {e}")
    finally:
        conn.close()

def clear_admin_support_target(admin_chat_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET active_support_target = NULL WHERE chat_id = ?", (admin_chat_id,))
        conn.commit()
    except Exception as e:
        print(f"Error clearing admin support target: {e}")
    finally:
        conn.close()

def register_user(chat_id, username):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        if not row:
            # Generate a unique 9-character hex link code
            while True:
                code = uuid.uuid4().hex[:9]
                cursor.execute("SELECT 1 FROM users WHERE link_code = ?", (code,))
                if not cursor.fetchone():
                    break
            cursor.execute(
                "INSERT INTO users (chat_id, username, link_code) VALUES (?, ?, ?)",
                (chat_id, username, code)
            )
            conn.commit()
        else:
            # Update username if it has changed
            if row['username'] != username:
                cursor.execute("UPDATE users SET username = ? WHERE chat_id = ?", (username, chat_id))
                conn.commit()
    finally:
        conn.close()

def get_user(chat_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def get_user_by_username(username):
    if not username:
        return None
    username_clean = username.lstrip('@')
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username_clean,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def resolve_target_arg(message, arg, usage):
    """chat_id yoki @username bo'yicha foydalanuvchini topadi.
    Topilmasa foydalanuvchiga javob yozib, None qaytaradi."""
    try:
        target_id = int(arg)
    except ValueError:
        target_info = get_user_by_username(arg)
        if not target_info:
            bot.reply_to(
                message,
                f"Foydalanuvchi topilmadi ({arg}). Foydalanish: {usage} <chat_id yoki @username>"
            )
        return target_info

    target_info = get_user(target_id)
    if not target_info:
        bot.reply_to(
            message,
            f"Foydalanuvchi topilmadi ({target_id}). Avval botga /start bosgan bo'lishi kerak."
        )
    return target_info

def get_user_by_link_code(code):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE link_code = ?", (code,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def set_active_target(chat_id, target_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if target_id is not None:
            cursor.execute(
                "UPDATE users SET active_target_id = ?, last_target_id = ? WHERE chat_id = ?",
                (target_id, target_id, chat_id)
            )
        else:
            cursor.execute(
                "UPDATE users SET active_target_id = NULL WHERE chat_id = ?" ,
                (chat_id,)
            )
        conn.commit()
    finally:
        conn.close()

def clear_target(chat_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET active_target_id = NULL, last_target_id = NULL WHERE chat_id = ?",
            (chat_id,)
        )
        conn.commit()
    finally:
        conn.close()

def remember_last_target(chat_id, target_id):
    """Faol nishonni tozalab, 'oxirgi yozilgan odam'ni bitta so'rovda saqlaydi."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET active_target_id = NULL, last_target_id = ? WHERE chat_id = ?",
            (target_id, chat_id)
        )
        conn.commit()
    finally:
        conn.close()

# Ishga tushuvchi/skript fayllar — anonim kanal orqali tarqatilmasligi kerak
BLOCKED_DOC_EXTS = {
    '.apk', '.exe', '.msi', '.msix', '.bat', '.cmd', '.sh', '.com', '.vbs',
    '.js', '.jse', '.scr', '.pif', '.ps1', '.psm1', '.jar', '.lnk', '.reg',
    '.hta', '.wsf', '.cpl', '.dll', '.iso', '.img', '.deb', '.rpm', '.dmg',
}

def is_blocked_document(file_name):
    if not file_name:
        return False
    return os.path.splitext(file_name.lower())[1] in BLOCKED_DOC_EXTS

def anon_tag(chat_id):
    """Anonim jo'natuvchi uchun barqaror taxallus: adminga xom chat_id ko'rsatmaslik uchun.
    Bir xil odam har doim bir xil belgini oladi, ya'ni takroriy qoidabuzarni ajratsa bo'ladi."""
    digest = hashlib.sha256(f"{ANON_TAG_SALT}:{chat_id}".encode()).hexdigest()
    return f"anon#{digest[:6]}"

# ---------------- Reply routing (disksiz) ----------------
#
# Javob berish uchun "bu xabar kimdan kelgan" ma'lumoti kerak. Uni bazada
# saqlash o'rniga xabarning o'zidan o'qiymiz: har bir anonim xabarga
# "🔄 Javob berish" tugmasi biriktirilgan va uning callback_data si
# "reply_<sender_id>" ko'rinishida. Telegram xabarni o'zida saqlaganligi uchun
# bu ma'lumot muddatsiz mavjud bo'ladi — bizga hech narsa yozish kerak emas.

def resolve_sender_from_reply(replied_msg):
    """Javob berilayotgan xabarning inline tugmalaridan sender_id ni oladi."""
    markup = getattr(replied_msg, 'reply_markup', None)
    if not markup:
        return None
    for row in (getattr(markup, 'keyboard', None) or []):
        for button in row:
            data = getattr(button, 'callback_data', None)
            if data and data.startswith('reply_'):
                try:
                    return int(data.split('_', 1)[1])
                except ValueError:
                    return None
    return None

def is_blocked(owner_id, blocked_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM blocked_users WHERE owner_id = ? AND blocked_id = ?",
            (owner_id, blocked_id)
        )
        row = cursor.fetchone()
        return row is not None
    finally:
        conn.close()

def block_user(owner_id, blocked_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO blocked_users (owner_id, blocked_id) VALUES (?, ?)",
                (owner_id, blocked_id)
            )
            conn.commit()
            success = True
        except sqlite3.IntegrityError:
            success = False
        return success
    finally:
        conn.close()

# ---------------- Keyboard Keyboards ----------------

def get_share_markup(bot_user, link_code):
    markup = telebot.types.InlineKeyboardMarkup()
    share_url = f"https://t.me/share/url?url=https://t.me/{bot_user}?start={link_code}&text=Menga%20anonim%20savol%20yuboring!"
    markup.add(telebot.types.InlineKeyboardButton("🔗 Ulashish", url=share_url))
    return markup

def get_cancel_markup():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel"))
    return markup

def get_again_markup(target_id):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✍️ Yana xabar yuborish", callback_data=f"again_{target_id}"))
    return markup

def get_recipient_markup(sender_id):
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        telebot.types.InlineKeyboardButton("🔄 Javob berish", callback_data=f"reply_{sender_id}"),
        telebot.types.InlineKeyboardButton("⚠️ Shikoyat", callback_data=f"report_{sender_id}"),
        telebot.types.InlineKeyboardButton("🚫 Taqiqlash", callback_data=f"block_{sender_id}")
    )
    return markup

# ---------------- Bot Utility Functions ----------------

BOT_USERNAME = None

def get_bot_username():
    global BOT_USERNAME
    if not BOT_USERNAME:
        me = bot.get_me()
        BOT_USERNAME = me.username
    return BOT_USERNAME

def get_msg_id(res):
    if hasattr(res, 'message_id'):
        return res.message_id
    return None

def send_user_link(chat_id, username):
    """DIQQAT: username FOYDALANUVCHInikidan olinishi shart.
    Callback ichida call.message.from_user — bu bot, shuning uchun
    xabar obyekti emas, aniq qiymatlar uzatiladi."""
    register_user(chat_id, username)

    user_info = get_user(chat_id)
    link_code = user_info['link_code']
    bot_user = get_bot_username()
    
    msg_text = (
        "Shaxsiy havolangiz:\n\n"
        f"https://t.me/{bot_user}?start={link_code}\n\n"
        "Havolani ulashib, anonim suhbatni boshlashingiz mumkin."
    )
    bot.send_message(chat_id, msg_text, reply_markup=get_share_markup(bot_user, link_code))

# ---------------- Bot Message Forwarder ----------------

def forward_anonymous_message(sender_id, recipient_id, original_msg):
    try:
        # Check if blocked
        if is_blocked(recipient_id, sender_id):
            bot.send_message(
                sender_id,
                "Siz ushbu foydalanuvchi tomonidan bloklangansiz. Xabaringiz yetkazilmadi.",
                reply_markup=telebot.types.ReplyKeyboardRemove()
            )
            return False
            
        # Check document extension safety
        if original_msg.document and is_blocked_document(original_msg.document.file_name):
            bot.reply_to(
                original_msg,
                "Xavfsizlik nuqtai nazaridan bot ushbu xabarni qo'llab quvvatlamaydi ❌",
                reply_markup=get_again_markup(recipient_id)
            )
            remember_last_target(sender_id, recipient_id)
            return False

        # Determine content type and copy
        if original_msg.text:
            text = f"📩 Sizda yangi anonim xabar bor!\n\n{original_msg.text}"
            bot.send_message(
                chat_id=recipient_id,
                text=text,
                reply_markup=get_recipient_markup(sender_id)
            )

        elif original_msg.photo or original_msg.video or original_msg.voice or original_msg.audio or original_msg.document:
            caption_text = "📩 Sizda yangi anonim xabar bor!"
            if original_msg.caption:
                caption_text += f"\n\n{original_msg.caption}"
            
            try:
                bot.copy_message(
                    chat_id=recipient_id,
                    from_chat_id=sender_id,
                    message_id=original_msg.message_id,
                    caption=caption_text,
                    reply_markup=get_recipient_markup(sender_id)
                )
            except Exception as e:
                print(f"Error copying media: {e}")
                if isinstance(e, telebot.apihelper.ApiTelegramException) and ("blocked" in str(e).lower() or "forbidden" in str(e).lower()):
                    raise e
                bot.send_message(sender_id, "Fayl/media yuborishda xatolik yuz berdi. Iltimos, qayta urunib ko'ring.")
                return False
                
        else:
            # Stickers, video notes va boshqa izohsiz turlar
            try:
                # Tugmalar mediyaning O'ZIGA biriktiriladi — shunda foydalanuvchi
                # mediyaga javob yozganda ham jo'natuvchi aniqlanadi.
                res = bot.copy_message(
                    chat_id=recipient_id,
                    from_chat_id=sender_id,
                    message_id=original_msg.message_id,
                    reply_markup=get_recipient_markup(sender_id)
                )
                msg_id = get_msg_id(res)

                # Sarlavhani ham tugmalar bilan yuboramiz (ikkalasiga javob berish ishlaydi)
                bot.send_message(
                    chat_id=recipient_id,
                    text="📩 Sizda yangi anonim xabar bor!",
                    reply_markup=get_recipient_markup(sender_id),
                    reply_to_message_id=msg_id
                )
            except Exception as e:
                print(f"Error copying sticker/unsupported media: {e}")
                if isinstance(e, telebot.apihelper.ApiTelegramException) and ("blocked" in str(e).lower() or "forbidden" in str(e).lower()):
                    raise e
                bot.send_message(sender_id, "Ushbu turdagi xabar (stiker yoki media) yuborilmadi yoki qo'llab-quvvatlanmaydi.")
                return False
                
        # Send confirmation to the sender (replying to their message)
        bot.reply_to(
            original_msg,
            "Xabaringiz yuborildi!",
            reply_markup=get_again_markup(recipient_id)
        )

        bump_message_counters(sender_id, recipient_id)
        remember_last_target(sender_id, recipient_id)

        return True
    except telebot.apihelper.ApiTelegramException as e:
        print(f"Telegram API Error in forward: {e}")
        if "blocked" in str(e).lower() or "forbidden" in str(e).lower():
            bot.send_message(
                sender_id,
                "Ushbu foydalanuvchi botni bloklaganligi (yoki o'chirib qo'yganligi) sababli xabaringiz yetkazilmadi. ❌",
                reply_markup=telebot.types.ReplyKeyboardRemove()
            )
        else:
            bot.send_message(
                sender_id,
                "Xabarni yuborishda xatolik yuz berdi. Iltimos keyinroq qayta urinib ko'ring.",
                reply_markup=telebot.types.ReplyKeyboardRemove()
            )
        return False
    except Exception as e:
        print(f"Error in forward_anonymous_message outer block: {e}")
        bot.send_message(sender_id, "Xabarni yuborishda xatolik yuz berdi.", reply_markup=telebot.types.ReplyKeyboardRemove())
        return False

@bot.message_handler(commands=['ban'])
def handle_ban_command(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        return
        
    params = message.text.split(maxsplit=2)
    if len(params) < 2:
        bot.reply_to(message, "Foydalanish: /ban <chat_id yoki @username> [sabab]")
        return

    reason = params[2] if len(params) > 2 else "Belgilanmagan"

    target_info = resolve_target_arg(message, params[1], "/ban")
    if not target_info:
        return
    target_id = target_info['chat_id']

    if target_id == SUPER_ADMIN_ID:
        bot.reply_to(message, "Bosh adminni (Owner) bloklash taqiqlangan! ❌")
        return

    # Admin boshqa adminni bloklay olmaydi — faqat Super Admin qila oladi
    if is_admin(target_id) and not is_super_admin(chat_id):
        bot.reply_to(message, "Boshqa administratorni bloklash uchun Super Admin huquqi kerak ❌")
        return

    if ban_user(target_id, chat_id, reason):
        try:
            bot.send_message(target_id, "Siz xavfsizlik qoidalarini buzganligingiz sababli botdan administrator tomonidan bloklandingiz ❌")
        except Exception:
            pass
        username_display = target_info['username'] or 'No Name'
        bot.reply_to(message, f"Foydalanuvchi {target_id} ({username_display}) muvaffaqiyatli global bloklandi. Sababi: {reason}")
    else:
        bot.reply_to(message, "Bloklashda xatolik yuz berdi.")

@bot.message_handler(commands=['unban'])
def handle_unban_command(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        return
        
    params = message.text.split()
    if len(params) < 2:
        bot.reply_to(message, "Foydalanish: /unban <chat_id yoki @username>")
        return

    target_info = resolve_target_arg(message, params[1], "/unban")
    if not target_info:
        return
    target_id = target_info['chat_id']

    if not is_globally_banned(target_id):
        bot.reply_to(message, "Ushbu foydalanuvchi bloklanmagan.")
        return
        
    if unban_user(target_id):
        try:
            bot.send_message(target_id, "Sizning blokirovkangiz bekor qilindi. Botdan qayta foydalanishingiz mumkin ✅")
        except Exception:
            pass
        username_display = target_info['username'] or 'No Name'
        bot.reply_to(message, f"Foydalanuvchi {target_id} ({username_display}) blokdan chiqarildi.")
    else:
        bot.reply_to(message, "Blokdan chiqarishda xatolik yuz berdi.")

@bot.message_handler(commands=['banned', 'banlist'])
def handle_banned_list_command(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        return
        
    try:
        banned_list = get_banned_users()
        if not banned_list:
            bot.reply_to(message, "Hozircha bloklangan foydalanuvchilar yo'q. ✅")
            return
            
        text = "🚫 <b>Bloklangan foydalanuvchilar ro'yxati:</b>\n\n"
        for idx, user in enumerate(banned_list, 1):
            username_val = html.escape(user['username']) if user['username'] else None
            username_part = f" (@{username_val})" if username_val else " (No Username)"
            reason_val = html.escape(user['reason']) if user['reason'] else "Belgilanmagan"
            text += f"{idx}. ID: <code>{user['chat_id']}</code>{username_part}\n   Sabab: {reason_val}\n   Vaqt: {user['timestamp']}\n\n"
            
        if len(text) > 4000:
            text = text[:4000] + "\n... (ro'yxat juda uzun)"
            
        bot.reply_to(message, text, parse_mode="HTML")
    except Exception as e:
        print(f"Error fetching banned users: {e}")
        bot.reply_to(message, "Xatolik yuz berdi.")

@bot.message_handler(commands=['addadmin'])
def handle_addadmin_command(message):
    chat_id = message.chat.id
    if not is_super_admin(chat_id):
        return
        
    params = message.text.split()
    if len(params) < 2:
        bot.reply_to(message, "Foydalanish: /addadmin <chat_id yoki @username>")
        return

    target_info = resolve_target_arg(message, params[1], "/addadmin")
    if not target_info:
        return
    target_id = target_info['chat_id']

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_admin = 1 WHERE chat_id = ?", (target_id,))
        conn.commit()
    finally:
        conn.close()
    
    try:
        bot.set_my_commands([
            telebot.types.BotCommand("start", "Botni ishga tushirish / Havola"),
            telebot.types.BotCommand("help", "Yordam va ma'lumotlar"),
            telebot.types.BotCommand("adminpage", "Admin boshqaruv paneli")
        ], scope=telebot.types.BotCommandScopeChat(target_id))
    except Exception as e:
        print(f"Error setting custom commands for new admin {target_id}: {e}")
        
    try:
        bot.send_message(
            target_id, 
            "Siz ushbu botga administrator etib tayinlandingiz! 💻\n"
            "Buyruqlar menyusini tekshiring yoki /adminpage ni yozing."
        )
    except Exception:
        pass
        
    username_display = target_info['username'] or 'No Name'
    bot.reply_to(message, f"Foydalanuvchi {target_id} ({username_display}) muvaffaqiyatli admin qilindi.")

@bot.message_handler(commands=['deladmin'])
def handle_deladmin_command(message):
    chat_id = message.chat.id
    if not is_super_admin(chat_id):
        return
        
    params = message.text.split()
    if len(params) < 2:
        bot.reply_to(message, "Foydalanish: /deladmin <chat_id yoki @username>")
        return

    target_info = resolve_target_arg(message, params[1], "/deladmin")
    if not target_info:
        return
    target_id = target_info['chat_id']

    if target_id == SUPER_ADMIN_ID:
        bot.reply_to(message, "Bosh adminni vazifasidan bo'shatib bo'lmaydi!")
        return
        
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_admin = 0 WHERE chat_id = ?", (target_id,))
        conn.commit()
    finally:
        conn.close()
    
    try:
        bot.delete_my_commands(scope=telebot.types.BotCommandScopeChat(target_id))
    except Exception as e:
        print(f"Error deleting custom commands for {target_id}: {e}")
        
    try:
        bot.send_message(target_id, "Sizning administratorlik huquqlaringiz bekor qilindi ❌")
    except Exception:
        pass
        
    username_display = target_info['username'] or 'No Name'
    bot.reply_to(message, f"Foydalanuvchi {target_id} ({username_display}) adminlikdan olib tashlandi.")

@bot.message_handler(commands=['adminpage'])
def handle_adminpage_command(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        bot.reply_to(message, "Ushbu sahifa\nfaqat administratorlar uchun ❗")
        return
        
    text = (
        "💻 ADMINISTRATOR PANEL:\n\n"
        "Quyidagi buyruqlardan foydalanishingiz mumkin:\n"
        "🚨 /reports - Shikoyatlar ro'yxati (oxirgi 10 ta)\n"
        "📊 /stats - Bot a'zolari va faoliyat statistikasi\n"
        "📢 /broadcast - Barcha a'zolarga xabar tarqatish\n"
        "🚫 /ban <chat_id yoki @username> [sabab] - Foydalanuvchini bloklash\n"
        "✅ /unban <chat_id yoki @username> - Blokdan chiqarish\n"
        "📜 /banned - Bloklanganlar ro'yxati\n"
        "📝 /userinfo <chat_id yoki @username> - Foydalanuvchi haqida ma'lumot"
    )
    if is_super_admin(chat_id):
        text += (
            "\n\n👑 SUPER ADMIN BUYRUQLARI:\n"
            "➕ /addadmin <chat_id yoki @username> - Yangi admin qo'shish\n"
            "➖ /deladmin <chat_id yoki @username> - Adminlikdan bo'shatish"
        )
    bot.reply_to(message, text)

@bot.message_handler(commands=['help'])
def handle_help_command(message):
    chat_id = message.chat.id
    help_text = (
        "🤖 **Anonim Savol-Javob Bot — Yordam**\n\n"
        "📌 **Qanday ishlaydi:**\n"
        "1️⃣ /start buyrug'i orqali shaxsiy havolangizni oling\n"
        "2️⃣ Havolangizni do'stlaringiz bilan ulashing\n"
        "3️⃣ Ular sizga anonim xabar/savol yubora oladi\n"
        "4️⃣ Kelgan xabarga \"🔄 Javob berish\" tugmasi orqali javob yozing\n\n"
        "⚠️ **Xavfsizlik qoidalari:**\n"
        "- Tahdid, haqorat, shaxsiy ma'lumot (manzil, raqam) yuborish qat'iyan taqiqlanadi\n"
        "- Lokatsiya va kontakt yuborish imkoniyati xavfsizlik nuqtai nazaridan o'chirilgan\n"
        "- Qoidabuzarlik holatida \"⚠️ Shikoyat\" tugmasidan foydalaning — bu administratorga yetkaziladi\n"
        "- Har qanday foydalanuvchini \"🚫 Taqiqlash\" tugmasi orqali bloklashingiz mumkin\n\n"
        "🔒 **Maxfiylik — nima saqlanadi, nima yo'q:**\n"
        "✅ Anonim xabarlar matni bazaga yozilmaydi\n"
        "✅ Kim kimga yozgani ham saqlanmaydi — bot suhbat jurnalini yuritmaydi\n"
        "✅ Javob berish imkoniyati xabarning o'zida saqlanadi, shuning uchun "
        "eng eski xabarga ham javob bera olasiz\n"
        "⚠️ Faqat siz o'zingiz yuborgan shikoyat va /support murojaatlaringiz matni saqlanadi — "
        "aks holda administrator ularni ko'rib chiqa olmaydi\n\n"
        "/start — shaxsiy havolangizni olish\n"
        "/support — administrator bilan bog'lanish (murojaat yuborish)\n"
    )
    if is_admin(chat_id):
        help_text += "/adminpage adminstrator oynasi\n"
        
    help_text += "/help — ushbu yordam matnini ko'rish"
    
    bot.reply_to(message, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['support'])
def handle_support_command(message):
    chat_id = message.chat.id
    if is_globally_banned(chat_id):
        return

    # Clear active anonymous target if any to avoid collision
    set_active_target(chat_id, None)
    
    # Enter support mode
    USER_SUPPORT_STATES[chat_id] = True
    
    # Send cancel markup
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_user_support"))
    
    bot.send_message(
        chat_id,
        "Murojaatingizni shuyerga yozing!",
        reply_markup=markup
    )

@bot.message_handler(func=lambda message: USER_SUPPORT_STATES.get(message.chat.id) is True,
                     content_types=['text', 'photo', 'video', 'voice', 'audio', 'document', 'sticker', 'video_note'])
def handle_user_support_message(message):
    chat_id = message.chat.id
    USER_SUPPORT_STATES.pop(chat_id, None)  # Holatni butunlay olib tashlash (xotira o'smasligi uchun)

    # Determine message text content or description
    text_content = message.text or message.caption or f"[{message.content_type}]"
    
    register_user(chat_id, message.from_user.username)
    msg_id = add_support_message(chat_id, text_content)
    
    if not msg_id:
        bot.send_message(chat_id, "Xabarni saqlashda xatolik yuz berdi. Iltimos keyinroq qayta urining.")
        return

    bot.send_message(chat_id, "✅ Xabaringiz administratorga yuborildi. Tez orada javob olasiz.")

    username = message.from_user.username or "No Name"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(
        "✍️ Javob berish", callback_data=f"supportreply_{chat_id}_{msg_id}"
    ))

    # Fetch all admins from DB and send a copy of the support request
    try:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT chat_id FROM users WHERE is_admin >= 1")
            admin_rows = cursor.fetchall()
        finally:
            conn.close()
        
        for row in admin_rows:
            admin_id = row['chat_id']
            try:
                # Send support header and copy user's message
                bot.send_message(
                    admin_id,
                    f"🆘 **Yordam so'rovi** (# {msg_id})\n"
                    f"👤 Foydalanuvchi: {chat_id} (@{username})"
                )
                bot.copy_message(admin_id, chat_id, message.message_id, reply_markup=markup)
            except Exception as e:
                print(f"Admin {admin_id}ga yuborishda xatolik: {e}")
    except Exception as e:
        print(f"Error notifying admins about support request: {e}")

@bot.message_handler(commands=['reports'])
def handle_reports_command(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        return
        
    try:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM reports WHERE status = 'pending' ORDER BY timestamp DESC LIMIT 10")
            rows = cursor.fetchall()
        finally:
            conn.close()
        
        if not rows:
            bot.reply_to(message, "Hozircha ko'rib chiqilmagan (pending) shikoyatlar mavjud emas. ✅")
            return
            
        bot.reply_to(message, f"Jami {len(rows)} ta eng so'nggi shikoyatlar:")
        for row in rows:
            report_id = row['id']
            reporter = row['reporter_id']
            reported = row['reported_id']
            content = row['message_content']
            m_type = row['message_type']
            reported_at = row['timestamp']

            # Jo'natuvchining xom chat_id si ko'rsatilmaydi — taxallus yetarli.
            # Bloklash tugmasi baribir ishlaydi, ID esa tugma ichida qoladi.
            text = (
                f"🚨 Shikoyat ID: {report_id}\n"
                f"👤 Shikoyat qilgan: {reporter}\n"
                f"🎯 Kimga (Anonim): {anon_tag(reported)}\n"
                f"📁 Turi: {m_type}\n"
                f"📝 Mazmuni: {content}\n"
                f"⏰ Vaqti: {reported_at}"
            )
            
            markup = telebot.types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                telebot.types.InlineKeyboardButton("✅ Ko'rib chiqildi", callback_data=f"resolve_{report_id}"),
                telebot.types.InlineKeyboardButton("🚫 Bloklash", callback_data=f"adminban_{reported}_{report_id}")
            )
            bot.send_message(chat_id, text, reply_markup=markup)
    except Exception as e:
        print(f"Error handling reports command: {e}")
        bot.reply_to(message, "Xatolik yuz berdi.")

@bot.message_handler(commands=['stats'])
def handle_stats_command(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        return
        
    try:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) FROM users")
            total_users = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM banned_users")
            total_banned = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM reports WHERE status = 'pending'")
            pending_reports = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM reports WHERE status = 'resolved'")
            resolved_reports = cursor.fetchone()[0]

            # Jurnal emas, oddiy hisoblagichlar yig'indisi
            cursor.execute("SELECT COALESCE(SUM(sent_count), 0) FROM users")
            total_messages = cursor.fetchone()[0]
        finally:
            conn.close()

        text = (
            "📊 BOT STATISTIKASI:\n\n"
            f"👤 Jami ro'yxatdan o'tgan foydalanuvchilar: {total_users}\n"
            f"🚫 Bloklangan (banned) foydalanuvchilar: {total_banned}\n"
            f"📨 Yuborilgan anonim xabarlar soni: {total_messages}\n"
            f"🚨 Ko'rib chiqilayotgan shikoyatlar (pending): {pending_reports}\n"
            f"✅ Hal qilingan shikoyatlar (resolved): {resolved_reports}"
        )
        bot.reply_to(message, text)
    except Exception as e:
        print(f"Error handling stats command: {e}")
        bot.reply_to(message, "Xatolik yuz berdi.")

@bot.message_handler(commands=['broadcast'])
def handle_broadcast_command(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        return
        
    ADMIN_BROADCAST_STATES[chat_id] = True
    
    # Send cancel markup
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_broadcast"))
    
    bot.send_message(
        chat_id,
        "📢 **E'lon tarqatish bo'limi**\n\n"
        "Barcha a'zolarga tarqatmoqchi bo'lgan xabaringizni yuboring. "
        "Bu matn, rasm (izohi bilan), video (izohi bilan), ovozli xabar (voice), stiker, video note yoki hujjat bo'lishi mumkin.",
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.message_handler(func=lambda message: ADMIN_BROADCAST_STATES.get(message.chat.id) is True, 
                     content_types=['text', 'photo', 'video', 'voice', 'audio', 'document', 'sticker', 'video_note'])
def handle_pending_broadcast(message):
    chat_id = message.chat.id
    ADMIN_BROADCAST_STATES.pop(chat_id, None)  # Holatni butunlay olib tashlash
    ADMIN_BROADCAST_PENDING_MSGS[chat_id] = message
    
    # Send preview of the message
    bot.send_message(chat_id, "🔍 **Siz yuborgan e'lonning ko'rinishi:**")
    bot.copy_message(chat_id, chat_id, message.message_id)
    
    # Send confirmation buttons
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("✅ Tasdiqlash", callback_data="confirm_broadcast"),
        telebot.types.InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_broadcast")
    )
    bot.send_message(
        chat_id,
        "👆 Ushbu xabarni barcha a'zolarga tarqatishni tasdiqlaysizmi?",
        reply_markup=markup
    )

@bot.message_handler(commands=['history', 'userinfo'])
def handle_history_command(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        return

    params = message.text.split()
    if len(params) < 2:
        bot.reply_to(message, "Foydalanish: /userinfo <chat_id yoki @username>")
        return

    target_info = resolve_target_arg(message, params[1], "/userinfo")
    if not target_info:
        return

    # Bu yerda ataylab "kim bilan yozishgan" ro'yxati YO'Q — bot bunday
    # ma'lumotni umuman saqlamaydi. Faqat sonli hisoblagichlar ko'rsatiladi.
    target_id = target_info['chat_id']
    ban_status = "ha" if is_globally_banned(target_id) else "yo'q"
    admin_status = "ha" if is_admin(target_id) else "yo'q"

    info_text = (
        f"👤 Foydalanuvchi: {target_id} ({target_info['username'] or 'No Name'})\n\n"
        f"📨 Jami jo'natgan anonim xabarlari: {target_info['sent_count'] or 0}\n"
        f"📩 Jami qabul qilgan xabarlari: {target_info['received_count'] or 0}\n"
        f"🚫 Global blok: {ban_status}\n"
        f"💻 Administrator: {admin_status}\n\n"
        "ℹ️ Kim bilan yozishgani saqlanmaydi — bot suhbat jurnalini yuritmaydi."
    )
    bot.reply_to(message, info_text)

@bot.message_handler(commands=['start'])
def handle_start(message):
    chat_id = message.chat.id
    if is_globally_banned(chat_id):
        bot.send_message(chat_id, "Siz xavfsizlik qoidalarini buzganligingiz sababli botdan butunlay bloklangansiz ❌")
        return
        
    username = message.from_user.username
    register_user(chat_id, username)

    # Check if a deep link code was provided
    params = message.text.split()
    if len(params) > 1:
        target_code = params[1]
        target_user = get_user_by_link_code(target_code)
        
        if target_user:
            target_id = target_user['chat_id']
            if target_id == chat_id:
                bot.send_message(
                    chat_id,
                    "O'zingizga anonim xabar yubora olmaysiz!",
                    reply_markup=telebot.types.ReplyKeyboardRemove()
                )
                return
                
            if is_blocked(target_id, chat_id):
                bot.send_message(
                    chat_id,
                    "Siz ushbu foydalanuvchi tomonidan bloklangansiz. Xabar yubora olmaysiz.",
                    reply_markup=telebot.types.ReplyKeyboardRemove()
                )
                return
                
            set_active_target(chat_id, target_id)
            bot.send_message(
                chat_id,
                "Murojaatingizni shuyerga yozing!",
                reply_markup=get_cancel_markup()
            )
            return
        else:
            bot.send_message(
                chat_id,
                "Xato havola! Bunday havola mavjud emas.",
                reply_markup=telebot.types.ReplyKeyboardRemove()
            )
            return
            
    # Regular start - send user their link
    send_user_link(chat_id, message.from_user.username)

@bot.message_handler(
    func=lambda message: message.reply_to_message is not None
                         and resolve_sender_from_reply(message.reply_to_message) is not None,
    content_types=['text', 'photo', 'video', 'voice', 'audio', 'document', 'sticker', 'video_note']
)
def handle_reply_messages(message):
    chat_id = message.chat.id
    if is_globally_banned(chat_id):
        bot.send_message(chat_id, "Siz xavfsizlik qoidalarini buzganligingiz sababli botdan butunlay bloklangansiz ❌")
        return

    # Jo'natuvchi xabarning tugmasidan aniqlanadi — bazaga murojaat yo'q,
    # shuning uchun eski xabarlarga ham javob berish ishlayveradi.
    target_sender_id = resolve_sender_from_reply(message.reply_to_message)
    register_user(chat_id, message.from_user.username)
    forward_anonymous_message(chat_id, target_sender_id, message)

@bot.message_handler(content_types=['location', 'venue', 'contact'])
def handle_unsupported_content(message):
    """Lokatsiya va kontakt xavfsizlik sababli uzatilmaydi (ikkalasi bir xil ishlaydi)."""
    chat_id = message.chat.id
    user_info = get_user(chat_id)
    target_id = user_info['active_target_id'] if user_info else None

    markup = None
    if target_id:
        markup = get_again_markup(target_id)
        remember_last_target(chat_id, target_id)

    bot.send_message(
        chat_id,
        "Xavfsizlik nuqtai nazaridan bot ushbu xabarni qo'llab quvvatlamaydi ❌",
        reply_markup=markup
    )

def _is_admin_support_reply(message):
    """Bitta DB o'qish bilan tekshiradi va anonim yozish rejimini buzmaydi."""
    user_info = get_user(message.chat.id)
    if not user_info or not user_info['active_support_target']:
        return False
    # Admin shu payt kimgadir anonim xabar yozayotgan bo'lsa, aralashmaymiz
    if user_info['active_target_id']:
        return False
    return is_admin(message.chat.id)

@bot.message_handler(func=_is_admin_support_reply)
def handle_admin_support_reply(message):
    admin_id = message.chat.id
    user_info = get_user(admin_id)
    user_id = user_info['active_support_target']

    try:
        bot.send_message(user_id, f"👨‍💼 Administrator javobi:\n\n{message.text}")
        bot.reply_to(message, "✅ Javobingiz foydalanuvchiga yuborildi.")
    except Exception as e:
        print(f"Error sending support reply to {user_id}: {e}")
        bot.reply_to(message, "❌ Yuborishda xatolik: foydalanuvchi botni bloklagan bo'lishi mumkin.")

    clear_admin_support_target(admin_id)
    mark_support_resolved_for_user(user_id)

@bot.message_handler(content_types=['text', 'photo', 'video', 'voice', 'audio', 'document', 'sticker', 'video_note'])
def handle_writing_mode(message):
    chat_id = message.chat.id
    if is_globally_banned(chat_id):
        bot.send_message(chat_id, "Siz xavfsizlik qoidalarini buzganligingiz sababli botdan butunlay bloklangansiz ❌")
        return
        
    register_user(chat_id, message.from_user.username)
    
    user_info = get_user(chat_id)
    if user_info and user_info['active_target_id']:
        target_id = user_info['active_target_id']
        forward_anonymous_message(chat_id, target_id, message)
    else:
        # If they just typed something without an active target, send their personal link
        send_user_link(chat_id, message.from_user.username)

# ---------------- Inline Keyboard Callbacks ----------------

@bot.callback_query_handler(func=lambda call: call.data == 'cancel')
def handle_cancel_callback(call):
    try:
        chat_id = call.from_user.id
        clear_target(chat_id)
        bot.answer_callback_query(call.id, "Bekor qilindi.")
        # call.from_user — haqiqiy foydalanuvchi (call.message.from_user esa bot)
        send_user_link(chat_id, call.from_user.username)
    except Exception as e:
        print(f"Error in cancel callback: {e}")
        bot.answer_callback_query(call.id, "Xatolik yuz berdi.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('again_'))
def handle_again_callback(call):
    try:
        chat_id = call.message.chat.id
        if is_globally_banned(chat_id):
            bot.answer_callback_query(call.id, "Siz botdan butunlay bloklangansiz ❌", show_alert=True)
            return
            
        target_id = int(call.data.split('_')[1])
        
        if is_blocked(target_id, chat_id):
            bot.send_message(chat_id, "Siz ushbu foydalanuvchi tomonidan bloklangansiz. Xabar yubora olmaysiz.")
            bot.answer_callback_query(call.id)
            return
            
        set_active_target(chat_id, target_id)
        bot.send_message(
            chat_id,
            "Murojaatingizni shuyerga yozing!",
            reply_markup=get_cancel_markup()
        )
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"Error in again callback: {e}")
        bot.answer_callback_query(call.id, "Xatolik yuz berdi.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('reply_'))
def handle_reply_callback(call):
    try:
        chat_id = call.message.chat.id
        if is_globally_banned(chat_id):
            bot.answer_callback_query(call.id, "Siz botdan butunlay bloklangansiz ❌", show_alert=True)
            return
            
        sender_id = int(call.data.split('_')[1])
        
        # Check if blocked
        if is_blocked(sender_id, chat_id):
            bot.send_message(chat_id, "Siz ushbu foydalanuvchi tomonidan bloklangansiz. Javob yubora olmaysiz.")
            bot.answer_callback_query(call.id)
            return
            
        set_active_target(chat_id, sender_id)
        bot.send_message(
            chat_id,
            "Murojaatingizni shuyerga yozing!",
            reply_markup=get_cancel_markup()
        )
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"Error handling reply callback: {e}")
        bot.answer_callback_query(call.id, "Xatolik yuz berdi.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('block_'))
def handle_block_callback(call):
    try:
        sender_id = int(call.data.split('_')[1])
        owner_id = call.message.chat.id
        
        if block_user(owner_id, sender_id):
            bot.answer_callback_query(call.id, "Foydalanuvchi bloklandi. Endi u sizga yozolmaydi.", show_alert=True)
            # Edit the message to show "Bloklandi" inline status
            new_markup = telebot.types.InlineKeyboardMarkup(row_width=1)
            new_markup.add(
                telebot.types.InlineKeyboardButton("🔄 Javob berish", callback_data=f"reply_{sender_id}"),
                telebot.types.InlineKeyboardButton("⚠️ Shikoyat", callback_data=f"report_{sender_id}"),
                telebot.types.InlineKeyboardButton("🚫 Bloklandi", callback_data="none")
            )
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=new_markup
            )
        else:
            bot.answer_callback_query(call.id, "Foydalanuvchi allaqachon bloklangan.", show_alert=True)
    except Exception as e:
        print(f"Error blocking user: {e}")
        bot.answer_callback_query(call.id, "Xatolik yuz berdi.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('report_'))
def handle_report_callback(call):
    try:
        reported_id = int(call.data.split('_')[1])
        reporter_id = call.message.chat.id
        
        # Determine reported message content & type
        msg_text = call.message.text or call.message.caption or f"[{call.message.content_type}]"
        msg_type = call.message.content_type
        
        # Check if already reported to avoid duplicate reports in list
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM reports WHERE reporter_id = ? AND reported_id = ? AND message_content = ? AND status = 'pending'",
                (reporter_id, reported_id, msg_text)
            )
            already_reported = cursor.fetchone() is not None
        finally:
            conn.close()
        
        if already_reported:
            bot.answer_callback_query(call.id, "Ushbu xabar bo'yicha shikoyat allaqachon yuborilgan.", show_alert=True)
            return
            
        if add_report(reporter_id, reported_id, msg_text, msg_type):
            bot.answer_callback_query(call.id, "Shikoyat qabul qilindi va administratorga yuborildi.", show_alert=True)
            
            # Edit the message markup to show "Shikoyat qilindi" inline status
            new_markup = telebot.types.InlineKeyboardMarkup(row_width=1)
            new_markup.add(
                telebot.types.InlineKeyboardButton("🔄 Javob berish", callback_data=f"reply_{reported_id}"),
                telebot.types.InlineKeyboardButton("⚠️ Shikoyat qilindi", callback_data="none"),
                telebot.types.InlineKeyboardButton("🚫 Bloklash", callback_data=f"block_{reported_id}")
            )
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=new_markup
            )
        else:
            bot.answer_callback_query(call.id, "Shikoyat yuborishda xatolik yuz berdi.", show_alert=True)
    except Exception as e:
        print(f"Error handling report callback: {e}")
        bot.answer_callback_query(call.id, "Xatolik yuz berdi.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('resolve_'))
def handle_resolve_report_callback(call):
    try:
        if not is_admin(call.message.chat.id):
            bot.answer_callback_query(call.id, "Ruxsat berilmagan.")
            return
        report_id = int(call.data.split('_')[1])
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE reports SET status = 'resolved' WHERE id = ?", (report_id,))
            conn.commit()
        finally:
            conn.close()
        bot.answer_callback_query(call.id, "Shikoyat ko'rib chiqilgan deb belgilandi ✅")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=call.message.text + "\n\n✅ [Ko'rib chiqilgan]"
        )
    except Exception as e:
        print(f"Error resolving report: {e}")
        bot.answer_callback_query(call.id, "Xatolik yuz berdi.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('adminban_'))
def handle_adminban_report_callback(call):
    try:
        if not is_admin(call.message.chat.id):
            bot.answer_callback_query(call.id, "Ruxsat berilmagan.")
            return
        parts = call.data.split('_')
        reported_id = int(parts[1])
        report_id = int(parts[2])
        
        if ban_user(reported_id, call.message.chat.id, "Shikoyat tufayli bloklandi"):
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("UPDATE reports SET status = 'resolved' WHERE id = ?", (report_id,))
                conn.commit()
            finally:
                conn.close()
            
            try:
                bot.send_message(reported_id, "Siz xavfsizlik qoidalarini buzganligingiz sababli botdan administrator tomonidan bloklandingiz ❌")
            except Exception:
                pass
                
            bot.answer_callback_query(call.id, "Foydalanuvchi bloklandi va shikoyat yopildi 🚫")
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=call.message.text + "\n\n🚫 [Bloklangan va resolved]"
            )
        else:
            bot.answer_callback_query(call.id, "Bloklashda xatolik yuz berdi.")
    except Exception as e:
        print(f"Error in adminban report callback: {e}")
        bot.answer_callback_query(call.id, "Xatolik yuz berdi.")

@bot.callback_query_handler(func=lambda call: call.data == 'none')
def handle_none_callback(call):
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == 'confirm_broadcast')
def handle_confirm_broadcast(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id, "Ruxsat berilmagan.")
        return
        
    pending_msg = ADMIN_BROADCAST_PENDING_MSGS.get(chat_id)
    if not pending_msg:
        bot.answer_callback_query(call.id, "Tarqatiladigan xabar topilmadi.")
        return
        
    bot.answer_callback_query(call.id, "Tarqatish boshlandi...")
    bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text="📢 E'lon tarqatilmoqda, iltimos kuting..."
    )
    
    # Run the broadcast loop asynchronously in a daemon thread
    def run_async_broadcast(admin_chat_id, message_to_copy):
        try:
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                # Bloklangan foydalanuvchilarga e'lon yuborilmaydi
                cursor.execute("""
                    SELECT chat_id FROM users
                    WHERE chat_id NOT IN (SELECT chat_id FROM banned_users)
                """)
                users = cursor.fetchall()
            finally:
                conn.close()

            success = 0
            fail = 0

            for user in users:
                target_chat_id = user['chat_id']
                # Telegram 429 qaytarsa, so'ralgan vaqt kutilib bir marta qayta uriniladi
                for attempt in (1, 2):
                    try:
                        bot.copy_message(target_chat_id, admin_chat_id, message_to_copy.message_id)
                        success += 1
                        break
                    except telebot.apihelper.ApiTelegramException as e:
                        params = (getattr(e, 'result_json', None) or {}).get('parameters') or {}
                        wait = params.get('retry_after')
                        if e.error_code == 429 and wait and attempt == 1:
                            time.sleep(min(wait, 30))
                            continue
                        fail += 1
                        break
                    except Exception:
                        fail += 1
                        break

                # Short delay to prevent hitting Telegram API rate limits (30 msgs/sec limit)
                time.sleep(0.05)

            bot.send_message(
                admin_chat_id,
                "📢 E'lon tarqatish yakunlandi:\n\n"
                f"✅ Muvaffaqiyatli: {success} ta foydalanuvchiga\n"
                f"❌ Muvaffaqiyatsiz: {fail} ta (botni bloklaganlar)"
            )
        except Exception as e:
            print(f"Error during async broadcast: {e}")
            bot.send_message(admin_chat_id, "E'lon tarqatishda xatolik yuz berdi.")
            
    threading.Thread(target=run_async_broadcast, args=(chat_id, pending_msg), daemon=True).start()
    
    # Clean up states immediately to unblock admin
    ADMIN_BROADCAST_PENDING_MSGS.pop(chat_id, None)
    ADMIN_BROADCAST_STATES.pop(chat_id, None)

@bot.callback_query_handler(func=lambda call: call.data == 'cancel_broadcast')
def handle_cancel_broadcast(call):
    chat_id = call.message.chat.id
    ADMIN_BROADCAST_PENDING_MSGS.pop(chat_id, None)
    ADMIN_BROADCAST_STATES.pop(chat_id, None)
    bot.answer_callback_query(call.id, "E'lon bekor qilindi.")
    bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text="❌ E'lon tarqatish bekor qilindi."
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('supportreply_'))
def handle_supportreply_callback(call):
    try:
        if not is_admin(call.message.chat.id):
            bot.answer_callback_query(call.id, "Ruxsat berilmagan.")
            return
        parts = call.data.split('_')
        user_id = int(parts[1])
        msg_id = int(parts[2])

        set_admin_support_target(call.message.chat.id, user_id)
        bot.send_message(
            call.message.chat.id,
            f"✍️ Endi #{msg_id} so'roviga javobingizni yozing (foydalanuvchi {user_id}):"
        )
        bot.answer_callback_query(call.id)

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE support_messages SET status = 'in_progress' WHERE id = ?", (msg_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"Error in supportreply callback: {e}")
        bot.answer_callback_query(call.id, "Xatolik yuz berdi.")

@bot.callback_query_handler(func=lambda call: call.data == 'cancel_user_support')
def handle_cancel_user_support(call):
    chat_id = call.from_user.id
    USER_SUPPORT_STATES.pop(chat_id, None)
    bot.answer_callback_query(call.id, "Murojaat yuborish bekor qilindi.")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="❌ Murojaat yuborish bekor qilindi."
    )
    # call.from_user — haqiqiy foydalanuvchi (call.message.from_user esa bot)
    send_user_link(chat_id, call.from_user.username)

# Eslatma: eski "periodic_cleanup" olib tashlandi — endi tozalanadigan
# xabar jurnali yo'q, chunki bot suhbat ma'lumotini umuman saqlamaydi.

# ---------------- Main Execution ----------------

# Modul sifatida import qilinganda ham jadvallar mavjud bo'lishi uchun
# init_db() shu yerda, __main__ blokidan tashqarida chaqiriladi.
init_db()

if __name__ == '__main__':
    print("Database initialized successfully.")

if __name__ == '__main__':
    # Menyu buyruqlarini sozlash
    try:
        # Barcha foydalanuvchilar uchun umumiy menyu
        bot.set_my_commands([
            telebot.types.BotCommand("start", "Botni ishga tushirish / Havola"),
            telebot.types.BotCommand("support", "Administrator bilan bog'lanish"),
            telebot.types.BotCommand("help", "Yordam va ma'lumotlar")
        ])
        
        # Query administrators from the database
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT chat_id FROM users WHERE is_admin >= 1")
            admin_rows = cursor.fetchall()
        finally:
            conn.close()
        
        # Administratorlar uchun maxsus menyu (scope orqali)
        for row in admin_rows:
            admin_id = row['chat_id']
            try:
                bot.set_my_commands([
                    telebot.types.BotCommand("start", "Botni ishga tushirish / Havola"),
                    telebot.types.BotCommand("support", "Administrator bilan bog'lanish"),
                    telebot.types.BotCommand("help", "Yordam va ma'lumotlar"),
                    telebot.types.BotCommand("adminpage", "Admin boshqaruv paneli")
                ], scope=telebot.types.BotCommandScopeChat(admin_id))
            except Exception as admin_e:
                print(f"Error setting admin commands for {admin_id}: {admin_e}")
    except Exception as e:
        print(f"Error setting bot commands: {e}")
        
    print("Anonymous Q&A Bot is starting...")
    bot.infinity_polling()
