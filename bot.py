import os
import re
import time
import telebot
import yt_dlp
import asyncio
import traceback
import subprocess
import threading
from shazamio import Shazam
from pydub import AudioSegment
import imageio_ffmpeg
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
import html
import sqlite3
from translations import TRANSLATIONS

import queue as _queue

# Thread synchronization for concurrent downloads to prevent file lock/WinError 32 errors
DOWNLOAD_LOCKS = {}
DOWNLOAD_LOCKS_MUTEX = threading.Lock()

def get_video_lock(video_id):
    with DOWNLOAD_LOCKS_MUTEX:
        if video_id not in DOWNLOAD_LOCKS:
            DOWNLOAD_LOCKS[video_id] = threading.Lock()
        return DOWNLOAD_LOCKS[video_id]

# ── Per-user FIFO task queue ──────────────────────────────────────────────────
# Har bir foydalanuvchi (chat_id) uchun alohida navbat va worker thread.
# So'rovlar kelgan tartibda, birin-ketin bajariladi.
_USER_QUEUES  = {}   # chat_id → queue.Queue
_USER_WORKERS = {}   # chat_id → threading.Thread
_UQUEUE_LOCK  = threading.Lock()

def _make_worker(chat_id):
    """Foydalanuvchi navbatini ketma-ket ishlovchi daemon thread."""
    def _run():
        print(f"[Queue DEBUG] Worker started for chat_id={chat_id}")
        q = _USER_QUEUES[chat_id]
        while True:
            try:
                task = q.get(timeout=300)   # 5 daqiqa bo'sh tursa, tozalanadi
            except _queue.Empty:
                print(f"[Queue DEBUG] Worker exiting due to timeout for chat_id={chat_id}")
                with _UQUEUE_LOCK:
                    _USER_WORKERS.pop(chat_id, None)
                    _USER_QUEUES.pop(chat_id, None)
                return
            try:
                print(f"[Queue DEBUG] Worker executing task for chat_id={chat_id}")
                task()
                print(f"[Queue DEBUG] Worker finished task for chat_id={chat_id}")
            except Exception as exc:
                print(f"[Queue DEBUG] Worker error [{chat_id}]: {exc}")
            finally:
                q.task_done()
    t = threading.Thread(target=_run, daemon=True, name=f"worker-{chat_id}")
    t.start()
    return t

def enqueue_task(chat_id, fn):
    """Vazifani foydalanuvchining FIFO navbatiga qo'shadi."""
    print(f"[Queue DEBUG] Enqueuing task for chat_id={chat_id}")
    with _UQUEUE_LOCK:
        if chat_id not in _USER_QUEUES:
            _USER_QUEUES[chat_id] = _queue.Queue()
        worker = _USER_WORKERS.get(chat_id)
        if worker is None or not worker.is_alive():
            _USER_WORKERS[chat_id] = _make_worker(chat_id)
    _USER_QUEUES[chat_id].put(fn)
# ─────────────────────────────────────────────────────────────────────────────


# Refresh PATH from Registry on Windows to ensure winget-installed FFmpeg/FFprobe are visible
if os.name == 'nt':
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            user_path = winreg.QueryValueEx(key, "Path")[0]
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"System\CurrentControlSet\Control\Session Manager\Environment") as key:
            sys_path = winreg.QueryValueEx(key, "Path")[0]
        os.environ["PATH"] = sys_path + ";" + user_path
    except Exception as e:
        print(f"Error refreshing PATH: {e}")

# Load environment variables from .env file
load_dotenv()


def safe_print(*args, **kwargs):
    try:
        msg = " ".join(str(arg) for arg in args)
        print(msg, **kwargs)
    except Exception:
        try:
            msg = " ".join(str(arg) for arg in args)
            print(msg.encode('ascii', errors='replace').decode('ascii'), **kwargs)
        except Exception:
            pass


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Telegram Bot Token
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable topilmadi. "
        "Botni ishga tushirishdan oldin: export BOT_TOKEN=\"...\" yoki .env faylini tekshiring."
    )

telebot.apihelper.CONNECT_TIMEOUT = 120
telebot.apihelper.READ_TIMEOUT = 120

bot = telebot.TeleBot(TOKEN)

try:
    BOT_USERNAME = bot.get_me().username
except Exception as e:
    print(f"Error getting bot info: {e}")
    BOT_USERNAME = "insta_save_videoo_bot"

CAPTION_TEXT = f"❤️@{BOT_USERNAME} orqali yuklab olindi🚀"

# Rate limiting: max 5 requests per 60 seconds per user
USER_RATE_LIMITS = {}
RATE_LIMIT_LOCK = threading.Lock()

def check_rate_limit(chat_id):
    now = time.time()
    with RATE_LIMIT_LOCK:
        if chat_id not in USER_RATE_LIMITS:
            USER_RATE_LIMITS[chat_id] = []
        # Filter out requests older than 60 seconds
        USER_RATE_LIMITS[chat_id] = [t for t in USER_RATE_LIMITS[chat_id] if now - t < 60]
        if len(USER_RATE_LIMITS[chat_id]) >= 5:
            return False
        USER_RATE_LIMITS[chat_id].append(now)
        return True

USER_LANG_DB = os.path.join(BASE_DIR, "user_settings.db")

def init_user_db():
    try:
        conn = sqlite3.connect(USER_LANG_DB)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_langs (
                chat_id INTEGER PRIMARY KEY,
                lang TEXT DEFAULT 'uz_lat'
            )
        """)
        conn.commit()
        conn.close()
        print("SQLite user settings database initialized.")
    except Exception as e:
        print(f"Error initializing user settings database: {e}")

def get_user_lang_or_none(chat_id):
    try:
        conn = sqlite3.connect(USER_LANG_DB)
        cursor = conn.cursor()
        cursor.execute("SELECT lang FROM user_langs WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0]
    except Exception as e:
        print(f"Error reading user lang: {e}")
    return None

def get_user_lang(chat_id):
    lang = get_user_lang_or_none(chat_id)
    return lang if lang else 'uz_lat'

def set_user_lang(chat_id, lang):
    try:
        conn = sqlite3.connect(USER_LANG_DB)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_langs (chat_id, lang) VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET lang = EXCLUDED.lang
        """, (chat_id, lang))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error setting user lang: {e}")

DOWNLOAD_DIR = os.path.join(BASE_DIR, "temp_downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Startup cleanup: clear any leftover files from the previous run
try:
    for fname in os.listdir(DOWNLOAD_DIR):
        fpath = os.path.join(DOWNLOAD_DIR, fname)
        if os.path.isfile(fpath):
            os.remove(fpath)
    print("Startup cleanup complete: cleared temp_downloads directory.")
except Exception as e:
    print(f"Error during startup cleanup: {e}")

DOWNLOAD_SEMAPHORE = threading.BoundedSemaphore(3)

MAX_FILE_SIZE = 50 * 1024 * 1024
CACHE_LOCK = threading.Lock()
MAX_SEARCH_CACHE_SIZE = 200
MAX_MEMORY_CACHE_SIZE = 500

SEARCH_CACHE = {}

def add_to_search_cache(key, value):
    with CACHE_LOCK:
        if len(SEARCH_CACHE) >= MAX_SEARCH_CACHE_SIZE:
            # Evict the oldest key (since dict is ordered by insertion in modern Python)
            try:
                first_key = next(iter(SEARCH_CACHE))
                SEARCH_CACHE.pop(first_key, None)
            except (StopIteration, KeyError):
                pass
        SEARCH_CACHE[key] = value

try:
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    AudioSegment.converter = ffmpeg_exe
    print(f"FFmpeg successfully configured at: {ffmpeg_exe}")
except Exception as e:
    ffmpeg_exe = None
    print(f"Error configuring FFmpeg path: {e}")

DB_TYPE = os.getenv("DB_TYPE", "none")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_NAME = os.getenv("DB_NAME", "bot_cache")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_PORT = os.getenv("DB_PORT", "5432")


def get_db_connection():
    if DB_TYPE == "postgresql":
        import psycopg2
        return psycopg2.connect(
            host=DB_HOST, database=DB_NAME, user=DB_USER,
            password=DB_PASSWORD, port=DB_PORT
        )
    return None


def init_db():
    conn = get_db_connection()
    if not conn:
        print("Database caching is disabled (DB_TYPE is not set to 'postgresql').")
        return
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audio_cache (
            shortcode VARCHAR(50) PRIMARY KEY,
            file_id VARCHAR(255) NOT NULL,
            title VARCHAR(255),
            performer VARCHAR(255)
        )
    """)
    conn.commit()
    conn.close()


try:
    init_db()
except Exception as e:
    print(f"Error initializing DB: {e}")


LOCAL_MEMORY_CACHE = {}


def get_cached_audio(shortcode):
    try:
        conn = get_db_connection()
        if not conn:
            return LOCAL_MEMORY_CACHE.get(shortcode)
        cursor = conn.cursor()
        cursor.execute("SELECT file_id, title, performer FROM audio_cache WHERE shortcode = %s", (shortcode,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return {"file_id": row[0], "title": row[1], "performer": row[2]}
    except Exception as e:
        print(f"DB Read Error: {e}")
    return LOCAL_MEMORY_CACHE.get(shortcode)


def set_cached_audio(shortcode, file_id, title, performer):
    with CACHE_LOCK:
        if len(LOCAL_MEMORY_CACHE) >= MAX_MEMORY_CACHE_SIZE:
            try:
                first_key = next(iter(LOCAL_MEMORY_CACHE))
                LOCAL_MEMORY_CACHE.pop(first_key, None)
            except (StopIteration, KeyError):
                pass
        LOCAL_MEMORY_CACHE[shortcode] = {"file_id": file_id, "title": title, "performer": performer}
    try:
        conn = get_db_connection()
        if not conn:
            return
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO audio_cache (shortcode, file_id, title, performer) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (shortcode) DO UPDATE SET file_id = EXCLUDED.file_id",
            (shortcode, file_id, title, performer)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB Write Error: {e}")


def cleanup_old_downloads(max_age_seconds=3600):
    now = time.time()
    try:
        for fname in os.listdir(DOWNLOAD_DIR):
            fpath = os.path.join(DOWNLOAD_DIR, fname)
            try:
                if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > max_age_seconds:
                    os.remove(fpath)
            except Exception:
                pass
    except Exception as e:
        print(f"Cleanup error: {e}")


def extract_instagram_url(text):
    pattern = r'(https?://(?:www\.)?instagram\.com/[^\s]+)'
    match = re.search(pattern, text)
    if match:
        return match.group(1)
    return None


def extract_youtube_url(text):
    pattern = r'(https?://(?:www\.|m\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[A-Za-z0-9_-]{11})'
    match = re.search(pattern, text)
    if match:
        return match.group(1)
    return None


def extract_youtube_id(url):
    pattern = r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})'
    match = re.search(pattern, url)
    if match:
        video_id = match.group(1)
        if re.match(r'^[A-Za-z0-9_-]{11}$', video_id):
            return video_id
    return None


def get_youtube_video_info(video_id):
    if not re.match(r'^[A-Za-z0-9_-]{11}$', video_id):
        return {
            "title": "YouTube Video",
            "performer": "YouTube",
            "thumbnail": None,
            "duration": None,
            "duration_sec": 0
        }
    url = f"https://www.youtube.com/watch?v={video_id}"
    cookies_file = os.path.join(BASE_DIR, "cookies.txt")
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True
    }
    if os.path.exists(cookies_file):
        ydl_opts['cookiefile'] = cookies_file
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            duration_sec = info.get('duration') or 0
            duration = "00:00"
            if duration_sec:
                mins = int(duration_sec) // 60
                secs = int(duration_sec) % 60
                duration = f"{mins}:{secs:02d}"
            return {
                "title": info.get("title", "YouTube Video"),
                "performer": info.get("uploader", "YouTube"),
                "thumbnail": info.get("thumbnail"),
                "duration": duration,
                "duration_sec": duration_sec
            }
    except Exception as e:
        safe_print(f"Failed to fetch YouTube info for {video_id}: {e}")
        return {
            "title": "YouTube Video",
            "performer": "YouTube",
            "thumbnail": None,
            "duration": None,
            "duration_sec": 0
        }


def download_instagram_video(url):
    cookies_file = os.path.join(BASE_DIR, "cookies.txt")
    ydl_opts = {
        'format': 'best[filesize<50M]/best/bestvideo+bestaudio',
        'outtmpl': os.path.join(DOWNLOAD_DIR, 'insta_%(id)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
    }
    if os.path.exists(cookies_file):
        ydl_opts['cookiefile'] = cookies_file
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if 'requested_downloads' in info and len(info['requested_downloads']) > 0:
                filepath = info['requested_downloads'][0]['filepath']
                if os.path.exists(filepath):
                    return filepath
            filepath = ydl.prepare_filename(info)
            if os.path.exists(filepath):
                return filepath
            video_id = info.get('id')
            if video_id:
                for file in os.listdir(DOWNLOAD_DIR):
                    if video_id in file and file.startswith('insta_'):
                        full_path = os.path.join(DOWNLOAD_DIR, file)
                        if os.path.exists(full_path):
                            return full_path
            return None
    except Exception as e:
        print(f"yt-dlp download error: {e}")
        return None


def extract_instagram_shortcode(url):
    pattern = r'instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)'
    match = re.search(pattern, url)
    if match:
        return match.group(1)
    return None


def download_instagram_audio(url, for_shazam=False):
    """
    Instagram audiosini yuklab oladi.
    for_shazam=True bo'lsa, faqat Shazam uchun eng kichik (worst) sifat yuklanadi.
    """
    cookies_file = os.path.join(BASE_DIR, "cookies.txt")
    # Shazam uchun faqat 10 soniya kerak — eng kichik format yetarli (5-10x tezroq)
    fmt = 'worstaudio/worst' if for_shazam else 'bestaudio/best'
    ydl_opts = {
        'format': fmt,
        'outtmpl': os.path.join(DOWNLOAD_DIR, 'audio_%(id)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
    }
    if os.path.exists(cookies_file):
        ydl_opts['cookiefile'] = cookies_file
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if 'requested_downloads' in info and len(info['requested_downloads']) > 0:
                filepath = info['requested_downloads'][0]['filepath']
                if os.path.exists(filepath):
                    return filepath, info.get('title', 'Audio')
            filepath = ydl.prepare_filename(info)
            if os.path.exists(filepath):
                return filepath, info.get('title', 'Audio')
            video_id = info.get('id')
            if video_id:
                for file in os.listdir(DOWNLOAD_DIR):
                    if video_id in file and file.startswith('audio_'):
                        full_path = os.path.join(DOWNLOAD_DIR, file)
                        if os.path.exists(full_path):
                            return full_path, info.get('title', 'Audio')
            return None, None
    except Exception as e:
        print(f"yt-dlp audio download error: {e}")
        return None, None


def recognize_song(audio_path):
    """
    Cuts the first 10 seconds of the audio and sends it to Shazam.
    Returns (title, performer) if recognized, (None, None) otherwise.

    DEBUG VERSION: prints full traceback and raw Shazam response so we
    can see exactly why recognition is failing.
    """
    short_audio_path = os.path.join(DOWNLOAD_DIR, f"short_{os.path.basename(audio_path)}.wav")
    try:
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if not exe:
            safe_print("[SHAZAM DEBUG] FFmpeg executable not found!")
            return None, None, None

        cmd = [
            exe, '-y',
            '-i', audio_path,
            '-ss', '00:00:00',
            '-t', '10',
            '-ar', '16000',
            '-ac', '1',
            '-acodec', 'pcm_s16le',
            short_audio_path
        ]
        creation_flags = 0x08000000 if os.name == 'nt' else 0

        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=creation_flags, check=True
        )

        if not os.path.exists(short_audio_path) or os.path.getsize(short_audio_path) < 1000:
            safe_print(f"[SHAZAM DEBUG] Cut audio file missing or too small: {short_audio_path}")
            safe_print(f"[SHAZAM DEBUG] ffmpeg stderr: {result.stderr.decode(errors='ignore')[-500:]}")
            return None, None, None

        safe_print(f"[SHAZAM DEBUG] Cut audio ready: {short_audio_path} "
                   f"({os.path.getsize(short_audio_path)} bytes)")

        async def _async_recognize():
            shazam_client = Shazam()
            out = await shazam_client.recognize(short_audio_path)
            return out

        out = asyncio.run(_async_recognize())
        safe_print(f"[SHAZAM DEBUG] Raw Shazam response keys: {list(out.keys()) if out else out}")

        if out and out.get('matches'):
            safe_print(f"[SHAZAM DEBUG] {len(out['matches'])} match(es) found")
        else:
            safe_print("[SHAZAM DEBUG] No matches in response (Shazam couldn't identify the audio)")

        if out and 'track' in out:
            track = out['track']
            title = track.get('title')
            performer = track.get('subtitle')
            coverart = track.get('images', {}).get('coverart')
            safe_print(f"[SHAZAM DEBUG] Recognized: {performer} - {title}")
            return title, performer, coverart

    except subprocess.CalledProcessError as e:
        safe_print(f"[SHAZAM DEBUG] ffmpeg cut failed: {e.stderr.decode(errors='ignore')[-500:] if e.stderr else e}")
    except Exception as e:
        safe_print(f"[SHAZAM DEBUG] Song recognition error: {e}")
        traceback.print_exc()
    finally:
        if os.path.exists(short_audio_path):
            try:
                os.remove(short_audio_path)
            except Exception:
                pass
    return None, None, None


def get_youtube_spelling_suggestion(query):
    import urllib.request
    import urllib.parse
    import json
    try:
        encoded_query = urllib.parse.quote_plus(query)
        url = f"https://clients1.google.com/complete/search?client=youtube&hl=uz&q={encoded_query}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.0.0 Safari/537.36"
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as response:
            content = response.read().decode('utf-8')
            if content.startswith("window.google.ac.h("):
                content = content[len("window.google.ac.h("):-1]
            data = json.loads(content)
            suggestions = []
            for item in data[1]:
                if isinstance(item, list) and len(item) > 0:
                    suggestions.append(item[0])
                elif isinstance(item, str):
                    suggestions.append(item)
            if suggestions:
                return suggestions[0]
    except Exception as e:
        safe_print(f"Error fetching YouTube spelling suggestions: {e}")
    return None


def search_youtube_tracks(artist, title, max_results=5):
    query = f"{artist} {title}".strip()
    search_query = f"ytsearch{max_results}:{query}"
    ydl_opts = {
        'extract_flat': 'in_playlist',
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
    }
    results = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            if 'entries' in info:
                for entry in info['entries']:
                    if not entry:
                        continue
                    video_id = entry.get('id')
                    video_title = entry.get('title')
                    duration_sec = entry.get('duration')

                    # Convert duration to MM:SS
                    duration = "00:00"
                    if duration_sec:
                        mins = int(duration_sec) // 60
                        secs = int(duration_sec) % 60
                        duration = f"{mins}:{secs:02d}"

                    results.append({
                        "id": video_id,
                        "title": video_title,
                        "duration": duration
                    })
    except Exception as e:
        safe_print(f"[SHAZAM DEBUG] YouTube search error: {e}")

    # Fallback to corrected spelling suggestion if search was empty
    if not results and query:
        suggestion = get_youtube_spelling_suggestion(query)
        if suggestion and suggestion.lower().strip() != query.lower().strip():
            safe_print(f"[SHAZAM DEBUG] 0 results for '{query}'. Retrying corrected suggestion: '{suggestion}'")
            return search_youtube_tracks("", suggestion, max_results)

    return results


def download_audio_by_id(video_id):
    # Noyob fayl nomi — parallel so'rovlar bir-birining faylini o'chirmasin
    unique_name = f'song_{video_id}_{int(time.time())}'
    output_template = os.path.join(DOWNLOAD_DIR, f'{unique_name}.%(ext)s')
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'continuedl': False,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
    }
    if ffmpeg_exe:
        ydl_opts['ffmpeg_location'] = ffmpeg_exe
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
            mp3_path = os.path.join(DOWNLOAD_DIR, f"{unique_name}.mp3")
            if os.path.exists(mp3_path):
                return mp3_path
    except Exception as e:
        safe_print(f"[SHAZAM DEBUG] Audio download by ID error: {e}")
    return None


def download_video_by_id(video_id):
    # Clean up any existing or partial files for this video_id first to prevent lock and HTTP 416 errors
    for fname in os.listdir(DOWNLOAD_DIR):
        if video_id in fname:
            try:
                os.remove(os.path.join(DOWNLOAD_DIR, fname))
            except Exception:
                pass

    output_template = os.path.join(DOWNLOAD_DIR, f'video_{video_id}.%(ext)s')
    ydl_opts = {
        'format': 'best[filesize<50M]/best',
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'continuedl': False,
    }
    if ffmpeg_exe:
        ydl_opts['ffmpeg_location'] = ffmpeg_exe
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
            filepath = ydl.prepare_filename(info)
            if os.path.exists(filepath):
                return filepath
    except Exception as e:
        safe_print(f"[SHAZAM DEBUG] Video download by ID error: {e}")
    return None


def get_or_create_search_results(shortcode, url):
    if shortcode in SEARCH_CACHE:
        return SEARCH_CACHE[shortcode]

    if shortcode.startswith('rec_') or shortcode.startswith('txt_'):
        return None

    audio_path = None
    try:
        # Shazam uchun eng kichik sifat yuklanadi (tezroq)
        audio_path, original_title = download_instagram_audio(url, for_shazam=True)
        if audio_path:
            title, performer, coverart = recognize_song(audio_path)
            if title and performer:
                results = search_youtube_tracks(performer, title)
                if results:
                    add_to_search_cache(shortcode, {
                        "artist": performer,
                        "title": title,
                        "results": results
                    })
                    return SEARCH_CACHE[shortcode]
    except Exception as e:
        safe_print(f"[SHAZAM DEBUG] Reconstruct search cache error: {e}")
    finally:
        if audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except Exception:
                pass
    return None



def get_lang_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("🇺🇿 O'zbek tili", callback_data="setlang_uz_lat"),
        InlineKeyboardButton("🇷🇺 Русский", callback_data="setlang_ru")
    )
    markup.row(
        InlineKeyboardButton("🇬🇧 English", callback_data="setlang_en"),
        InlineKeyboardButton("🇺🇿 Ўзбекча", callback_data="setlang_uz_cyr")
    )
    return markup


@bot.message_handler(commands=['lang'])
def change_language(message):
    if not check_rate_limit(message.chat.id):
        return
    try:
        bot.reply_to(
            message,
            "Iltimos, tilni tanlang / Please select language / Пожалуйста, выберите язык:",
            reply_markup=get_lang_keyboard()
        )
    except Exception as e:
        print(f"Error sending lang select: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith('setlang_'))
def handle_setlang_callback(call):
    lang_code = call.data.split('_', 1)[1]
    chat_id = call.message.chat.id
    
    # Store in SQLite
    set_user_lang(chat_id, lang_code)
    
    # Answer query
    try:
        bot.answer_callback_query(call.id, TRANSLATIONS[lang_code]['lang_changed'])
    except Exception:
        pass
        
    # Edit the language selection message to show welcome message
    first_name = call.from_user.first_name if call.from_user.first_name else "Foydalanuvchi"
    welcome_text = TRANSLATIONS[lang_code]['welcome'].format(name=html.escape(first_name))
    
    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=welcome_text,
            parse_mode="HTML"
        )
    except Exception:
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=call.message.message_id,
                text=welcome_text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
            )
        except Exception:
            pass


@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not check_rate_limit(message.chat.id):
        try:
            bot.reply_to(
                message,
                "⚠️ Spamdan himoyalanish uchun cheklov! / Rate limit alert! / Ограничение спама! ❌"
            )
        except Exception:
            pass
        return

    lang = get_user_lang_or_none(message.chat.id)
    if not lang:
        try:
            bot.reply_to(
                message,
                "Iltimos, tilni tanlang / Please select language / Пожалуйста, выберите язык:",
                reply_markup=get_lang_keyboard()
            )
        except Exception as e:
            print(f"Error sending lang select: {e}")
    else:
        first_name = message.from_user.first_name if message.from_user.first_name else "Foydalanuvchi"
        welcome_text = TRANSLATIONS[lang]['welcome'].format(name=html.escape(first_name))
        try:
            bot.reply_to(message, welcome_text, parse_mode="HTML")
        except Exception:
            # Fallback to plain text if HTML parsing fails due to user input
            bot.reply_to(message, welcome_text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))


@bot.message_handler(commands=['instagram'])
def handle_instagram_command(message):
    lang = get_user_lang(message.chat.id)
    if not check_rate_limit(message.chat.id):
        try:
            bot.reply_to(message, TRANSLATIONS[lang]['rate_limit'])
        except Exception:
            pass
        return
    text = TRANSLATIONS[lang]['instagram_guide']
    bot.reply_to(message, text, parse_mode="HTML")


@bot.message_handler(commands=['youtube'])
def handle_youtube_command(message):
    lang = get_user_lang(message.chat.id)
    if not check_rate_limit(message.chat.id):
        try:
            bot.reply_to(message, TRANSLATIONS[lang]['rate_limit'])
        except Exception:
            pass
        return
    text = TRANSLATIONS[lang]['youtube_guide']
    bot.reply_to(message, text, parse_mode="HTML")


@bot.message_handler(commands=['shazam'])
def handle_shazam_command(message):
    lang = get_user_lang(message.chat.id)
    if not check_rate_limit(message.chat.id):
        try:
            bot.reply_to(message, TRANSLATIONS[lang]['rate_limit'])
        except Exception:
            pass
        return
    text = TRANSLATIONS[lang]['shazam_guide']
    bot.reply_to(message, text, parse_mode="HTML")


@bot.message_handler(content_types=['voice', 'audio', 'video', 'video_note'])
def handle_media_recognition(message):
    lang = get_user_lang(message.chat.id)
    status_msg = bot.reply_to(message, TRANSLATIONS[lang]['analyzing'])
    file_id = None
    file_ext = "wav"

    if message.voice:
        file_id = message.voice.file_id
        file_ext = "ogg"
    elif message.audio:
        file_id = message.audio.file_id
        file_ext = "mp3"
    elif message.video:
        file_id = message.video.file_id
        file_ext = "mp4"
    elif message.video_note:
        file_id = message.video_note.file_id
        file_ext = "mp4"

    if not file_id:
        bot.edit_message_text(TRANSLATIONS[lang]['media_not_found'], message.chat.id, status_msg.message_id)
        return

    import uuid
    media_id = uuid.uuid4().hex[:12]
    input_path = os.path.join(DOWNLOAD_DIR, f"media_{media_id}.{file_ext}")

    try:
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        with open(input_path, 'wb') as new_file:
            new_file.write(downloaded_file)

        title, performer, coverart = recognize_song(input_path)
        shortcode = f"rec_{media_id}"

        if title and performer:
            bot.edit_message_text(
                f"{TRANSLATIONS[lang]['identified_lbl']}: <b>{html.escape(performer)} - {html.escape(title)}</b>\n{TRANSLATIONS[lang]['searching_options']}",
                message.chat.id, status_msg.message_id, parse_mode="HTML"
            )

            results = search_youtube_tracks(performer, title)
            if results:
                add_to_search_cache(shortcode, {
                    "artist": performer,
                    "title": title,
                    "results": results
                })

                escaped_performer = html.escape(performer)
                escaped_title = html.escape(title)

                caption = (
                    f"{TRANSLATIONS[lang]['performer_lbl']}: <b>{escaped_performer}</b>\n"
                    f"{TRANSLATIONS[lang]['song_lbl']}: <b>{escaped_title}</b>\n\n"
                )
                for i, track in enumerate(results):
                    escaped_track_title = html.escape(track['title'])
                    caption += f"{i+1}. {escaped_track_title} <b>{track['duration']}</b>\n"
                caption += TRANSLATIONS[lang]['caption_footer'].format(username=BOT_USERNAME)

                reply_markup = InlineKeyboardMarkup()

                num_buttons = []
                for i in range(len(results)):
                    num_buttons.append(InlineKeyboardButton(str(i+1), callback_data=f"dla_{shortcode}_{i}"))
                reply_markup.row(*num_buttons)

                if coverart:
                    try:
                        bot.send_photo(
                            chat_id=message.chat.id,
                            photo=coverart,
                            caption=caption,
                            reply_markup=reply_markup,
                            parse_mode="HTML",
                            reply_to_message_id=message.message_id
                        )
                        bot.delete_message(message.chat.id, status_msg.message_id)
                        return
                    except Exception as pe:
                        safe_print(f"Error sending cover photo: {pe}")

                bot.send_message(
                    chat_id=message.chat.id,
                    text=caption,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                    reply_to_message_id=message.message_id
                )
                bot.delete_message(message.chat.id, status_msg.message_id)
            else:
                bot.edit_message_text(TRANSLATIONS[lang]['no_match'], message.chat.id, status_msg.message_id)
        else:
            bot.edit_message_text(TRANSLATIONS[lang]['not_found'], message.chat.id, status_msg.message_id)

    except Exception as e:
        safe_print(f"Media recognition error: {e}")
        traceback.print_exc()
        try:
            bot.edit_message_text(TRANSLATIONS[lang]['general_error'], message.chat.id, status_msg.message_id)
        except Exception:
            pass
    finally:
        if os.path.exists(input_path):
            try:
                os.remove(input_path)
            except Exception:
                pass


@bot.message_handler(func=lambda message: True)
def handle_message(message):
    lang = get_user_lang(message.chat.id)
    if not check_rate_limit(message.chat.id):
        try:
            bot.reply_to(message, TRANSLATIONS[lang]['rate_limit'])
        except Exception:
            pass
        return

    text = message.text
    if not text:
        return
    insta_url = extract_instagram_url(text)
    yt_url = extract_youtube_url(text)

    if yt_url:
        video_id = extract_youtube_id(yt_url)
        if not video_id:
            lang = get_user_lang(message.chat.id)
            bot.reply_to(message, TRANSLATIONS[lang]['invalid_yt_link'])
            return

        # Fetch video info to show preview
        info = get_youtube_video_info(video_id)
        title = info["title"]
        duration = info["duration"]
        thumbnail = info["thumbnail"]

        # Build markup
        reply_markup = InlineKeyboardMarkup()
        btn_video = InlineKeyboardButton("🎬 Video", callback_data=f"ytv_{video_id}")
        # Shorts: Shazam orqali asl qo'shiqni topamiz; oddiy video: o'sha videoning audiosini beramiz
        is_shorts = '/shorts/' in yt_url
        audio_callback = f"ytas_{video_id}" if is_shorts else f"yta_{video_id}"
        btn_audio = InlineKeyboardButton("🎧 Audio", callback_data=audio_callback)
        btn_group = InlineKeyboardButton(TRANSLATIONS[lang]['btn_add_group'], url=f"https://t.me/{BOT_USERNAME}?startgroup=true")
        reply_markup.row(btn_video, btn_audio)
        reply_markup.row(btn_group)

        caption_text = (
            f"🎬 <b>{html.escape(title)}</b>\n"
            f"⏱ Davomiyligi: <b>{duration}</b>\n\n"
            f"Yuklab olish formatlari ↓"
        )

        if thumbnail:
            try:
                bot.send_photo(
                    chat_id=message.chat.id,
                    photo=thumbnail,
                    caption=caption_text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                    reply_to_message_id=message.message_id
                )
                return
            except Exception as e:
                safe_print(f"Failed to send YouTube preview photo: {e}")

        # Fallback to text message if send_photo fails or thumbnail is None
        bot.send_message(
            chat_id=message.chat.id,
            text=caption_text,
            reply_markup=reply_markup,
            parse_mode="HTML",
            reply_to_message_id=message.message_id
        )
        return

    url = insta_url
    if not url:
        lang = get_user_lang(message.chat.id)
        status_msg = bot.reply_to(
            message,
            TRANSLATIONS[lang]['searching'].format(query=html.escape(text)),
            parse_mode="HTML"
        )
        import uuid
        shortcode = f"txt_{uuid.uuid4().hex[:12]}"
        # Search 30 results for pagination
        results = search_youtube_tracks("", text, max_results=30)
        if results:
            add_to_search_cache(shortcode, {
                "artist": "",
                "title": text,
                "results": results
            })
            
            page_size = 10
            start_idx = 0
            end_idx = min(start_idx + page_size, len(results))
            
            caption = f"🔍 <b>{html.escape(text)}</b>\n\n"
            for i in range(start_idx, end_idx):
                escaped_track_title = html.escape(results[i]['title'])
                caption += f"{i+1}. {escaped_track_title} <b>{results[i]['duration']}</b>\n"
            caption += TRANSLATIONS[lang]['caption_footer'].format(username=BOT_USERNAME)

            reply_markup = InlineKeyboardMarkup()
            
            # Row 1: 1 to 5
            row1 = []
            for i in range(start_idx, min(start_idx + 5, end_idx)):
                row1.append(InlineKeyboardButton(str(i+1), callback_data=f"dla_{shortcode}_{i}"))
            if row1:
                reply_markup.row(*row1)
                
            # Row 2: 6 to 10
            row2 = []
            for i in range(start_idx + 5, min(start_idx + 10, end_idx)):
                row2.append(InlineKeyboardButton(str(i+1), callback_data=f"dla_{shortcode}_{i}"))
            if row2:
                reply_markup.row(*row2)
                
            # Row 3: Nav: ⬅️, ❌, ➡️
            nav_row = []
            nav_row.append(InlineKeyboardButton(" ", callback_data="noop"))
            nav_row.append(InlineKeyboardButton("❌", callback_data=f"del_{shortcode}"))
            
            total_pages = (len(results) + page_size - 1) // page_size
            if total_pages > 1:
                nav_row.append(InlineKeyboardButton("➡️", callback_data=f"pg_{shortcode}_1"))
            else:
                nav_row.append(InlineKeyboardButton(" ", callback_data="noop"))
            reply_markup.row(*nav_row)

            bot.send_message(
                chat_id=message.chat.id,
                text=caption,
                reply_markup=reply_markup,
                parse_mode="HTML",
                reply_to_message_id=message.message_id
            )
            bot.delete_message(message.chat.id, status_msg.message_id)
        else:
            bot.edit_message_text(TRANSLATIONS[lang]['nothing_found'], message.chat.id, status_msg.message_id)
        return

    shortcode = extract_instagram_shortcode(url)
    
    # Check cache first for silent instant delivery
    if shortcode:
        cached = get_cached_audio(shortcode)
        if cached:
            reply_markup = InlineKeyboardMarkup()
            btn_audio = InlineKeyboardButton(TRANSLATIONS[lang]['btn_download'], callback_data=f"audio_{shortcode}")
            btn_group = InlineKeyboardButton(TRANSLATIONS[lang]['btn_add_group'], url=f"https://t.me/{BOT_USERNAME}?startgroup=true")
            reply_markup.row(btn_audio)
            reply_markup.row(btn_group)
            
            try:
                bot.send_video(
                    message.chat.id,
                    cached["file_id"],
                    caption=CAPTION_TEXT,
                    reply_markup=reply_markup,
                    reply_to_message_id=message.message_id,
                    supports_streaming=True
                )
                return
            except Exception as e:
                safe_print(f"Failed to send cached Reel video: {e}")

    lang = get_user_lang(message.chat.id)
    status_msg = bot.reply_to(message, TRANSLATIONS[lang]['downloading_video'])
    video_path = None
    try:
        video_path = download_instagram_video(url)
        if video_path and os.path.exists(video_path):
            file_size = os.path.getsize(video_path)
            if file_size > MAX_FILE_SIZE:
                bot.edit_message_text(
                    TRANSLATIONS[lang]['size_error'],
                    message.chat.id, status_msg.message_id
                )
            else:
                bot.edit_message_text(TRANSLATIONS[lang]['sending_to_tg'], message.chat.id, status_msg.message_id)
                reply_markup = None
                if shortcode:
                    reply_markup = InlineKeyboardMarkup()
                    btn_audio = InlineKeyboardButton(TRANSLATIONS[lang]['btn_download'], callback_data=f"audio_{shortcode}")
                    btn_group = InlineKeyboardButton(TRANSLATIONS[lang]['btn_add_group'], url=f"https://t.me/{BOT_USERNAME}?startgroup=true")
                    reply_markup.row(btn_audio)
                    reply_markup.row(btn_group)
                with open(video_path, 'rb') as video_file:
                    sent_msg = bot.send_video(
                        message.chat.id, video_file,
                        caption=CAPTION_TEXT,
                        reply_markup=reply_markup, timeout=120,
                        reply_to_message_id=message.message_id,
                        supports_streaming=True
                    )
                if sent_msg and sent_msg.video and shortcode:
                    set_cached_audio(shortcode, sent_msg.video.file_id, "Video", "Instagram")
                bot.delete_message(message.chat.id, status_msg.message_id)
            os.remove(video_path)
        else:
            bot.edit_message_text(
                TRANSLATIONS[lang]['insta_download_error'],
                message.chat.id, status_msg.message_id
            )
    except Exception as e:
        print(f"Message handling error: {e}")
        try:
            bot.edit_message_text(
                TRANSLATIONS[lang]['try_again'],
                message.chat.id, status_msg.message_id
            )
        except Exception:
            pass
        if video_path and os.path.exists(video_path):
            try:
                os.remove(video_path)
            except Exception:
                pass




@bot.callback_query_handler(func=lambda call: call.data.startswith('audio_'))
def handle_audio_callback(call):
    if not check_rate_limit(call.message.chat.id):
        try:
            bot.answer_callback_query(
                call.id,
                TRANSLATIONS[lang]['rate_limit_cb'],
                show_alert=True
            )
        except Exception:
            pass
        return

    shortcode = call.data.split('_', 1)[1]
    
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    def run_audio():
        lang = get_user_lang(call.message.chat.id)
        url = f"https://www.instagram.com/reel/{shortcode}/"

        status_msg = bot.send_message(
            chat_id=call.message.chat.id,
            text=TRANSLATIONS[lang]['analyzing'],
            reply_to_message_id=call.message.message_id
        )

        audio_path = None
        try:
            # Shazam uchun eng kichik sifat (worstaudio) — 5-10x tezroq yuklanadi
            audio_path, original_title = download_instagram_audio(url, for_shazam=True)
            if not audio_path or not os.path.exists(audio_path):
                bot.edit_message_text(TRANSLATIONS[lang]['download_error'], call.message.chat.id, status_msg.message_id)
                return

            title, performer, coverart = recognize_song(audio_path)

            if title and performer:
                bot.edit_message_text(
                    f"{TRANSLATIONS[lang]['identified_lbl']}: <b>{html.escape(performer)} - {html.escape(title)}</b>\n{TRANSLATIONS[lang]['searching_options']}",
                    call.message.chat.id, status_msg.message_id, parse_mode="HTML"
                )

                results = search_youtube_tracks(performer, title)
                if results:
                    # Save in memory cache
                    add_to_search_cache(shortcode, {
                        "artist": performer,
                        "title": title,
                        "results": results
                    })

                    escaped_performer = html.escape(performer)
                    escaped_title = html.escape(title)

                    caption = (
                        f"Ijrochi: <b>{escaped_performer}</b>\n"
                        f"Qo'shiq nomi: <b>{escaped_title}</b>\n\n"
                    )
                    for i, track in enumerate(results):
                        escaped_track_title = html.escape(track['title'])
                        caption += f"{i+1}. {escaped_track_title} <b>{track['duration']}</b>\n"
                    caption += TRANSLATIONS[lang]['caption_footer'].format(username=BOT_USERNAME)

                    # Keyboard
                    reply_markup = InlineKeyboardMarkup()

                    # Video button (downloads option 1 video - which is the official one)
                    btn_video = InlineKeyboardButton(TRANSLATIONS[lang]['btn_video'], callback_data=f"dlv_{shortcode}_0")
                    reply_markup.row(btn_video)

                    # Number buttons (1 to len(results))
                    num_buttons = []
                    for i in range(len(results)):
                        num_buttons.append(InlineKeyboardButton(str(i+1), callback_data=f"dla_{shortcode}_{i}"))
                    reply_markup.row(*num_buttons)

                    if coverart:
                        try:
                            bot.send_photo(
                                chat_id=call.message.chat.id,
                                photo=coverart,
                                caption=caption,
                                reply_markup=reply_markup,
                                parse_mode="HTML",
                                reply_to_message_id=call.message.message_id
                            )
                            bot.delete_message(call.message.chat.id, status_msg.message_id)
                            return
                        except Exception as pe:
                            safe_print(f"Error sending cover photo: {pe}")

                    bot.send_message(
                        chat_id=call.message.chat.id,
                        text=caption,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                        reply_to_message_id=call.message.message_id
                    )
                    bot.delete_message(call.message.chat.id, status_msg.message_id)
                else:
                    bot.edit_message_text(TRANSLATIONS[lang]['no_match'], call.message.chat.id, status_msg.message_id)
            else:
                bot.edit_message_text(TRANSLATIONS[lang]['not_found'], call.message.chat.id, status_msg.message_id)

        except Exception as e:
            safe_print(f"Audio callback error: {e}")
            traceback.print_exc()
            try:
                bot.edit_message_text(TRANSLATIONS[lang]['general_error'], call.message.chat.id, status_msg.message_id)
            except Exception:
                pass
        finally:
            if audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except Exception:
                    pass

    enqueue_task(call.message.chat.id, run_audio)





@bot.callback_query_handler(func=lambda call: call.data.startswith('ytv_'))
def handle_youtube_video_callback(call):
    if not check_rate_limit(call.message.chat.id):
        try:
            bot.answer_callback_query(
                call.id,
                TRANSLATIONS[lang]['rate_limit_cb'],
                show_alert=True
            )
        except Exception:
            pass
        return

    video_id = call.data.split('_', 1)[1]
    
    # Strictly validate video_id using regex
    if not re.match(r'^[A-Za-z0-9_-]{11}$', video_id):
        lang = get_user_lang(call.message.chat.id)
        bot.answer_callback_query(call.id, TRANSLATIONS[lang]['invalid_id'], show_alert=True)
        return

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    def run_ytv():
        lang = get_user_lang(call.message.chat.id)
        cache_key = f"yt_{video_id}"
        
        # 1. Check cache first
        cached = get_cached_audio(cache_key)
        if cached:
            try:
                bot.send_video(
                    chat_id=call.message.chat.id,
                    video=cached["file_id"],
                    caption=CAPTION_TEXT,
                    reply_to_message_id=call.message.message_id,
                    supports_streaming=True
                )
                return
            except Exception as e:
                # Self-healing cache: if the cached file_id is invalid/expired, delete it from cache
                safe_print(f"Failed to send cached video, clearing cache: {e}")
                try:
                    # Remove from memory cache and database
                    LOCAL_MEMORY_CACHE.pop(cache_key, None)
                    conn = get_db_connection()
                    if conn:
                        cursor = conn.cursor()
                        cursor.execute("DELETE FROM audio_cache WHERE shortcode = %s", (cache_key,))
                        conn.commit()
                        conn.close()
                except Exception as ce:
                    safe_print(f"Failed to clear cache entry: {ce}")

        # Fetch initial video info to get title and duration
        info = get_youtube_video_info(video_id)
        title = info["title"]
        duration_sec = info.get("duration_sec", 0)

        # Decide format based on duration heuristic to stay under 50MB
        if duration_sec > 1200:  # > 20 minutes
            fmt_rule = 'worstvideo+worstaudio/worst'
        elif duration_sec > 600:  # 10 to 20 minutes
            fmt_rule = 'bestvideo[height<=360]+bestaudio/best[height<=360]/worst'
        elif duration_sec > 300:  # 5 to 10 minutes
            fmt_rule = 'bestvideo[height<=480]+bestaudio/best[height<=480]/worst'
        else:  # < 5 minutes
            fmt_rule = 'bestvideo[filesize<=40M]+bestaudio[filesize<=10M]/best[filesize<=50M]/bestvideo[filesize_approx<=40M]+bestaudio[filesize_approx<=10M]/best[filesize_approx<=50M]/worst'

        status_msg = bot.send_message(
            chat_id=call.message.chat.id,
            text=TRANSLATIONS[lang]['downloading_video'],
            reply_to_message_id=call.message.message_id
        )

        temp_filename = f"video_{video_id}_{int(time.time())}"
        video_path_template = os.path.join(DOWNLOAD_DIR, f"{temp_filename}.%(ext)s")

        last_update_time = [0.0]
        last_percent = [0]

        def progress_hook(d):
            if d['status'] == 'downloading':
                total_bytes = d.get('total_bytes') or d.get('total_bytes_approx')
                downloaded_bytes = d.get('downloaded_bytes', 0)
                if total_bytes:
                    percent = int(downloaded_bytes / total_bytes * 100)
                    now = time.time()
                    # Limit message edits to at most once per 2 seconds, and only if percent changes by >= 15%
                    if (percent - last_percent[0] >= 15 or now - last_update_time[0] >= 2.0) and percent <= 100:
                        last_percent[0] = percent
                        last_update_time[0] = now
                        try:
                            bot.edit_message_text(
                                chat_id=call.message.chat.id,
                                message_id=status_msg.message_id,
                                text=TRANSLATIONS[lang]['downloading_video_percent'].format(percent=percent)
                            )
                        except Exception:
                            pass

        # Enforce quality sizing under 50MB and safe concurrent downloading using semaphore
        ydl_opts = {
            'format': fmt_rule,
            'format_sort': ['res:720', 'filesize'],
            'outtmpl': video_path_template,
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'progress_hooks': [progress_hook]
        }
        
        cookies_file = os.path.join(BASE_DIR, "cookies.txt")
        if os.path.exists(cookies_file):
            ydl_opts['cookiefile'] = cookies_file

        downloaded_file = None
        try:
            with DOWNLOAD_SEMAPHORE:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
                    
            # Locate the downloaded file
            for f in os.listdir(DOWNLOAD_DIR):
                if f.startswith(temp_filename):
                    downloaded_file = os.path.join(DOWNLOAD_DIR, f)
                    break

            if downloaded_file and os.path.exists(downloaded_file):
                file_size = os.path.getsize(downloaded_file)
                if file_size > MAX_FILE_SIZE:
                    bot.edit_message_text(
                        TRANSLATIONS[lang]['size_error'],
                        call.message.chat.id, status_msg.message_id
                    )
                else:
                    with open(downloaded_file, 'rb') as video_file:
                        sent_msg = bot.send_video(
                            chat_id=call.message.chat.id,
                            video=video_file,
                            caption=CAPTION_TEXT,
                            reply_to_message_id=call.message.message_id,
                            supports_streaming=True
                        )
                    if sent_msg and sent_msg.video:
                        set_cached_audio(cache_key, sent_msg.video.file_id, title, "Video")
                    bot.delete_message(call.message.chat.id, status_msg.message_id)
            else:
                bot.edit_message_text(TRANSLATIONS[lang]['download_error'], call.message.chat.id, status_msg.message_id)

        except Exception as e:
            safe_print(f"Download YouTube video callback error: {e}")
            traceback.print_exc()
            try:
                bot.edit_message_text(TRANSLATIONS[lang]['general_error'], call.message.chat.id, status_msg.message_id)
            except Exception:
                pass
        finally:
            if downloaded_file and os.path.exists(downloaded_file):
                try:
                    os.remove(downloaded_file)
                except Exception:
                    pass

    enqueue_task(call.message.chat.id, run_ytv)


@bot.callback_query_handler(func=lambda call: call.data.startswith('yta_'))
def handle_youtube_audio_callback(call):
    if not check_rate_limit(call.message.chat.id):
        try:
            bot.answer_callback_query(
                call.id,
                TRANSLATIONS[lang]['rate_limit_cb'],
                show_alert=True
            )
        except Exception:
            pass
        return

    video_id = call.data.split('_', 1)[1]

    # Strictly validate video_id using regex
    if not re.match(r'^[A-Za-z0-9_-]{11}$', video_id):
        lang = get_user_lang(call.message.chat.id)
        bot.answer_callback_query(call.id, TRANSLATIONS[lang]['invalid_id'], show_alert=True)
        return

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    def run_yta():
        lang = get_user_lang(call.message.chat.id)
        cache_key = f"yta_cached_{video_id}"

        # Check cache first (self-healing)
        cached = get_cached_audio(cache_key)
        if cached:
            try:
                bot.send_audio(
                    chat_id=call.message.chat.id,
                    audio=cached["file_id"],
                    title=cached["title"],
                    performer=cached["performer"],
                    caption=CAPTION_TEXT,
                    reply_to_message_id=call.message.message_id
                )
                return
            except Exception as e:
                safe_print(f"Failed to send cached YouTube audio, clearing cache: {e}")
                try:
                    LOCAL_MEMORY_CACHE.pop(cache_key, None)
                    conn = get_db_connection()
                    if conn:
                        cursor = conn.cursor()
                        cursor.execute("DELETE FROM audio_cache WHERE shortcode = %s", (cache_key,))
                        conn.commit()
                        conn.close()
                except Exception as ce:
                    safe_print(f"Failed to clear cache entry: {ce}")

        # Status xabarini darhol yuboramiz — info olish uchun kutilmaydi
        status_msg = bot.send_message(
            chat_id=call.message.chat.id,
            text=TRANSLATIONS[lang]['downloading_audio'],
            reply_to_message_id=call.message.message_id
        )

        temp_audio_name = f"audio_{video_id}_{int(time.time())}"
        temp_audio_template = os.path.join(DOWNLOAD_DIR, f"{temp_audio_name}.%(ext)s")

        cookies_file = os.path.join(BASE_DIR, "cookies.txt")
        # Odatda 192kbps — 20 daqiqalik kontent ~28MB. Agar hajm oshsa, keyin xabar beramiz.
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': temp_audio_template,
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }
        if ffmpeg_exe:
            ydl_opts['ffmpeg_location'] = ffmpeg_exe
        if os.path.exists(cookies_file):
            ydl_opts['cookiefile'] = cookies_file

        audio_path = None
        video_title = 'YouTube Audio'
        video_performer = 'YouTube'
        try:
            # extract_info(download=True) — bitta so'rovda ham ma'lumot, ham yuklab olish
            with DOWNLOAD_SEMAPHORE:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    dl_info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
                    if dl_info:
                        video_title = dl_info.get('title', 'YouTube Audio')
                        video_performer = dl_info.get('uploader', 'YouTube')
                        duration_sec = dl_info.get('duration', 0) or 0
                        # Agar davomiylik uzun bo'lsa va bitreyt yuqori bo'lsa, ogohlantirish
                        safe_print(f"[YTA] {video_title} | {duration_sec}s | 192kbps")

            # Locate the downloaded MP3
            for f in os.listdir(DOWNLOAD_DIR):
                if f.startswith(temp_audio_name) and f.endswith(".mp3"):
                    audio_path = os.path.join(DOWNLOAD_DIR, f)
                    break

            if not audio_path or not os.path.exists(audio_path):
                bot.edit_message_text("Audio yuklab bo'lmadi. ❌", call.message.chat.id, status_msg.message_id)
                return

            file_size = os.path.getsize(audio_path)
            if file_size > MAX_FILE_SIZE:
                bot.edit_message_text(
                    f"Audio hajmi {file_size // (1024*1024)}MB — 50MB dan katta, yuborib bo'lmaydi. ❌",
                    call.message.chat.id, status_msg.message_id
                )
                return

            with open(audio_path, 'rb') as audio_file:
                sent_msg = bot.send_audio(
                    chat_id=call.message.chat.id,
                    audio=audio_file,
                    title=video_title,
                    performer=video_performer,
                    caption=CAPTION_TEXT,
                    reply_to_message_id=call.message.message_id
                )
            if sent_msg and sent_msg.audio:
                set_cached_audio(cache_key, sent_msg.audio.file_id, video_title, video_performer)
            bot.delete_message(call.message.chat.id, status_msg.message_id)

        except Exception as e:
            safe_print(f"YouTube audio callback error: {e}")
            traceback.print_exc()
            try:
                bot.edit_message_text(TRANSLATIONS[lang]['general_error'], call.message.chat.id, status_msg.message_id)
            except Exception:
                pass
        finally:
            if audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except Exception:
                    pass

    enqueue_task(call.message.chat.id, run_yta)


@bot.callback_query_handler(func=lambda call: call.data.startswith('ytas_'))
def handle_youtube_shorts_audio_callback(call):
    """YouTube Shorts uchun: Shazam orqali asl qo'shiqni aniqlab yuklab beradi."""
    if not check_rate_limit(call.message.chat.id):
        try:
            bot.answer_callback_query(
                call.id,
                TRANSLATIONS[lang]['rate_limit_cb'],
                show_alert=True
            )
        except Exception:
            pass
        return

    video_id = call.data.split('_', 1)[1]

    if not re.match(r'^[A-Za-z0-9_-]{11}$', video_id):
        lang = get_user_lang(call.message.chat.id)
        bot.answer_callback_query(call.id, TRANSLATIONS[lang]['invalid_id'], show_alert=True)
        return

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    def run_ytas():
        lang = get_user_lang(call.message.chat.id)
        cache_key = f"ytas_cached_{video_id}"

        cached = get_cached_audio(cache_key)
        if cached:
            try:
                bot.send_audio(
                    chat_id=call.message.chat.id,
                    audio=cached["file_id"],
                    title=cached["title"],
                    performer=cached["performer"],
                    caption=CAPTION_TEXT,
                    reply_to_message_id=call.message.message_id
                )
                return
            except Exception as e:
                safe_print(f"Failed to send cached Shorts audio, clearing cache: {e}")
                try:
                    LOCAL_MEMORY_CACHE.pop(cache_key, None)
                    conn = get_db_connection()
                    if conn:
                        cursor = conn.cursor()
                        cursor.execute("DELETE FROM audio_cache WHERE shortcode = %s", (cache_key,))
                        conn.commit()
                        conn.close()
                except Exception as ce:
                    safe_print(f"Failed to clear cache entry: {ce}")

        # Status xabarini darhol yuboramiz
        status_msg = bot.send_message(
            chat_id=call.message.chat.id,
            text=TRANSLATIONS[lang]['analyzing'],
            reply_to_message_id=call.message.message_id
        )

        temp_audio_name = f"shorts_{video_id}_{int(time.time())}"
        temp_audio_template = os.path.join(DOWNLOAD_DIR, f"{temp_audio_name}.%(ext)s")

        cookies_file = os.path.join(BASE_DIR, "cookies.txt")
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': temp_audio_template,
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }
        if ffmpeg_exe:
            ydl_opts['ffmpeg_location'] = ffmpeg_exe
        if os.path.exists(cookies_file):
            ydl_opts['cookiefile'] = cookies_file

        full_audio_path = None
        official_audio_path = None
        video_title = 'YouTube Shorts'
        try:
            # extract_info(download=True) — bitta so'rovda ma'lumot va yuklab olish
            with DOWNLOAD_SEMAPHORE:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    dl_info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
                    if dl_info:
                        video_title = dl_info.get('title', 'YouTube Shorts')

            for f in os.listdir(DOWNLOAD_DIR):
                if f.startswith(temp_audio_name) and f.endswith(".mp3"):
                    full_audio_path = os.path.join(DOWNLOAD_DIR, f)
                    break

            if not full_audio_path or not os.path.exists(full_audio_path):
                bot.edit_message_text(TRANSLATIONS[lang]['not_found'], call.message.chat.id, status_msg.message_id)
                return

            # Shazam orqali aniqlaymiz
            title, performer, coverart = recognize_song(full_audio_path)

            # Agar Shazam ishlamasa, video nomidan parse qilamiz
            if not title or not performer:
                if " - " in video_title:
                    parts = video_title.split(" - ", 1)
                    performer_candidate = re.sub(r'[\(\[\{].*?[\)\]\}]', '', parts[0]).strip()
                    title_candidate = re.sub(r'[\(\[\{].*?[\)\]\}]', '', parts[1]).strip()
                    if performer_candidate and title_candidate:
                        performer, title = performer_candidate, title_candidate

            if title and performer:
                bot.edit_message_text(
                    TRANSLATIONS[lang]['identified_downloading'].format(performer=html.escape(performer), title=html.escape(title)),
                    call.message.chat.id, status_msg.message_id, parse_mode="HTML"
                )
                results = search_youtube_tracks(performer, title)
                if results:
                    official_id = results[0]["id"]
                    temp_official_name = f"official_{official_id}_{int(time.time())}"
                    temp_official_template = os.path.join(DOWNLOAD_DIR, f"{temp_official_name}.%(ext)s")

                    ydl_opts_official = {
                        'format': 'bestaudio/best',
                        'outtmpl': temp_official_template,
                        'quiet': True,
                        'no_warnings': True,
                        'nocheckcertificate': True,
                        'postprocessors': [{
                            'key': 'FFmpegExtractAudio',
                            'preferredcodec': 'mp3',
                            'preferredquality': '192', # Fixed NameError from audio_bitrate
                        }],
                    }
                    if ffmpeg_exe:
                        ydl_opts_official['ffmpeg_location'] = ffmpeg_exe
                    if os.path.exists(cookies_file):
                        ydl_opts_official['cookiefile'] = cookies_file

                    with DOWNLOAD_SEMAPHORE:
                        with yt_dlp.YoutubeDL(ydl_opts_official) as ydl:
                            ydl.download([f"https://www.youtube.com/watch?v={official_id}"])

                    for f in os.listdir(DOWNLOAD_DIR):
                        if f.startswith(temp_official_name) and f.endswith(".mp3"):
                            official_audio_path = os.path.join(DOWNLOAD_DIR, f)
                            break

                    if official_audio_path and os.path.exists(official_audio_path):
                        file_size = os.path.getsize(official_audio_path)
                        if file_size > MAX_FILE_SIZE:
                            bot.edit_message_text(
                                TRANSLATIONS[lang]['not_found_size'].format(size=file_size // (1024*1024)),
                                call.message.chat.id, status_msg.message_id
                            )
                            return

                        with open(official_audio_path, 'rb') as audio_file:
                            sent_msg = bot.send_audio(
                                chat_id=call.message.chat.id,
                                audio=audio_file,
                                title=title,
                                performer=performer,
                                caption=CAPTION_TEXT,
                                reply_to_message_id=call.message.message_id
                            )
                        if sent_msg and sent_msg.audio:
                            set_cached_audio(cache_key, sent_msg.audio.file_id, title, performer)
                        bot.delete_message(call.message.chat.id, status_msg.message_id)
                        return

            bot.edit_message_text(TRANSLATIONS[lang]['not_found'], call.message.chat.id, status_msg.message_id)

        except Exception as e:
            safe_print(f"YouTube Shorts audio callback error: {e}")
            traceback.print_exc()
            try:
                bot.edit_message_text(TRANSLATIONS[lang]['general_error'], call.message.chat.id, status_msg.message_id)
            except Exception:
                pass
        finally:
            if full_audio_path and os.path.exists(full_audio_path):
                try:
                    os.remove(full_audio_path)
                except Exception:
                    pass
            if official_audio_path and os.path.exists(official_audio_path):
                try:
                    os.remove(official_audio_path)
                except Exception:
                    pass

    enqueue_task(call.message.chat.id, run_ytas)


@bot.callback_query_handler(func=lambda call: call.data.startswith('pg_'))
def handle_pagination_callback(call):
    lang = get_user_lang(call.message.chat.id)
    prefix, rest = call.data.split('_', 1)
    shortcode, page_str = rest.rsplit('_', 1)
    page = int(page_str)

    if shortcode not in SEARCH_CACHE:
        bot.answer_callback_query(call.id, TRANSLATIONS[lang]['search_expired'], show_alert=True)
        return

    cache_data = SEARCH_CACHE[shortcode]
    results = cache_data["results"]
    title_query = cache_data["title"]

    page_size = 10
    total_pages = (len(results) + page_size - 1) // page_size

    if page < 0 or page >= total_pages:
        bot.answer_callback_query(call.id)
        return

    caption = f"🔍 <b>{html.escape(title_query)}</b>\n\n"

    start_idx = page * page_size
    end_idx = min(start_idx + page_size, len(results))

    page_results = results[start_idx:end_idx]
    for i, track in enumerate(page_results):
        idx = i + 1
        escaped_track_title = html.escape(track['title'])
        caption += f"{idx}. {escaped_track_title} <b>{track['duration']}</b>\n"
    caption += TRANSLATIONS[lang]['caption_footer'].format(username=BOT_USERNAME)

    reply_markup = InlineKeyboardMarkup()

    # Row 1: 1 to 5
    row1 = []
    for i in range(start_idx, min(start_idx + 5, end_idx)):
        row1.append(InlineKeyboardButton(str(i - start_idx + 1), callback_data=f"dla_{shortcode}_{i}"))
    if row1:
        reply_markup.row(*row1)

    # Row 2: 6 to 10
    row2 = []
    for i in range(start_idx + 5, min(start_idx + 10, end_idx)):
        row2.append(InlineKeyboardButton(str(i - start_idx + 1), callback_data=f"dla_{shortcode}_{i}"))
    if row2:
        reply_markup.row(*row2)

    # Row 3: Nav: ⬅️, ❌, ➡️
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"pg_{shortcode}_{page - 1}"))
    else:
        nav_row.append(InlineKeyboardButton(" ", callback_data="noop"))

    nav_row.append(InlineKeyboardButton("❌", callback_data=f"del_{shortcode}"))

    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"pg_{shortcode}_{page + 1}"))
    else:
        nav_row.append(InlineKeyboardButton(" ", callback_data="noop"))

    reply_markup.row(*nav_row)

    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=caption,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
    except Exception as e:
        safe_print(f"Pagination edit message error: {e}")

    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('del_'))
def handle_delete_callback(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == 'noop')
def handle_noop_callback(call):
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('dla_'))
def handle_download_audio_callback(call):
    if not check_rate_limit(call.message.chat.id):
        try:
            bot.answer_callback_query(
                call.id,
                TRANSLATIONS[lang]['rate_limit_cb'],
                show_alert=True
            )
        except Exception:
            pass
        return

    prefix, rest = call.data.split('_', 1)
    shortcode, index_str = rest.rsplit('_', 1)
    index = int(index_str)

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    def run_dla():
        lang = get_user_lang(call.message.chat.id)
        cache_key = f"{shortcode}_a_{index}"
        cached = get_cached_audio(cache_key)
        if cached:
            try:
                bot.send_audio(
                    chat_id=call.message.chat.id,
                    audio=cached["file_id"],
                    title=cached["title"],
                    performer=cached["performer"],
                    caption=CAPTION_TEXT,
                    reply_to_message_id=call.message.message_id
                )
                return
            except Exception as e:
                safe_print(f"Failed to send cached file: {e}")

        url = f"https://www.instagram.com/reel/{shortcode}/"
        cache_data = get_or_create_search_results(shortcode, url)
        if not cache_data or index >= len(cache_data["results"]):
            lang = get_user_lang(call.message.chat.id)
            bot.send_message(call.message.chat.id, TRANSLATIONS[lang]['search_expired'])
            return

        video_info = cache_data["results"][index]
        video_id = video_info["id"]
        title = video_info["title"]
        performer = cache_data["artist"]

        lock = get_video_lock(video_id)
        with lock:
            # Double check cache inside the lock
            cached = get_cached_audio(cache_key)
            if cached:
                try:
                    bot.send_audio(
                        chat_id=call.message.chat.id,
                        audio=cached["file_id"],
                        title=cached["title"],
                        performer=cached["performer"],
                        caption=CAPTION_TEXT,
                        reply_to_message_id=call.message.message_id
                    )
                    return
                except Exception as e:
                    safe_print(f"Failed to send cached file in lock: {e}")

            status_msg = bot.send_message(
                chat_id=call.message.chat.id,
                text=TRANSLATIONS[lang]['downloading_audio'],
                reply_to_message_id=call.message.message_id
            )

            full_audio_path = None
            try:
                full_audio_path = download_audio_by_id(video_id)
                if full_audio_path and os.path.exists(full_audio_path):
                    with open(full_audio_path, 'rb') as audio_file:
                        sent_msg = bot.send_audio(
                            chat_id=call.message.chat.id,
                            audio=audio_file,
                            title=title,
                            performer=performer,
                            caption=CAPTION_TEXT,
                            reply_to_message_id=call.message.message_id
                        )
                    if sent_msg and sent_msg.audio:
                        set_cached_audio(cache_key, sent_msg.audio.file_id, title, performer)
                    os.remove(full_audio_path)
                    bot.delete_message(call.message.chat.id, status_msg.message_id)
                else:
                    bot.edit_message_text(TRANSLATIONS[lang]['download_error'], call.message.chat.id, status_msg.message_id)
            except Exception as e:
                safe_print(f"Download audio callback error: {e}")
                traceback.print_exc()
                try:
                    bot.edit_message_text(TRANSLATIONS[lang]['general_error'], call.message.chat.id, status_msg.message_id)
                except Exception:
                    pass
            finally:
                if full_audio_path and os.path.exists(full_audio_path):
                    try:
                        os.remove(full_audio_path)
                    except Exception:
                        pass

    enqueue_task(call.message.chat.id, run_dla)


@bot.callback_query_handler(func=lambda call: call.data.startswith('dlv_'))
def handle_download_video_callback(call):
    if not check_rate_limit(call.message.chat.id):
        try:
            bot.answer_callback_query(
                call.id,
                TRANSLATIONS[lang]['rate_limit_cb'],
                show_alert=True
            )
        except Exception:
            pass
        return

    prefix, rest = call.data.split('_', 1)
    shortcode, index_str = rest.rsplit('_', 1)
    index = int(index_str)

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    def run_dlv():
        lang = get_user_lang(call.message.chat.id)
        cache_key = f"{shortcode}_v_{index}"
        cached = get_cached_audio(cache_key)
        if cached:
            try:
                bot.send_video(
                    chat_id=call.message.chat.id,
                    video=cached["file_id"],
                    caption=CAPTION_TEXT,
                    reply_to_message_id=call.message.message_id,
                    supports_streaming=True
                )
                return
            except Exception as e:
                safe_print(f"Failed to send cached video: {e}")

        url = f"https://www.instagram.com/reel/{shortcode}/"
        cache_data = get_or_create_search_results(shortcode, url)
        if not cache_data or index >= len(cache_data["results"]):
            lang = get_user_lang(call.message.chat.id)
            bot.send_message(call.message.chat.id, TRANSLATIONS[lang]['search_expired'])
            return

        video_info = cache_data["results"][index]
        video_id = video_info["id"]
        title = video_info["title"]

        lock = get_video_lock(video_id)
        with lock:
            # Double check cache inside the lock
            cached = get_cached_audio(cache_key)
            if cached:
                try:
                    bot.send_video(
                        chat_id=call.message.chat.id,
                        video=cached["file_id"],
                        caption=CAPTION_TEXT,
                        reply_to_message_id=call.message.message_id,
                        supports_streaming=True
                    )
                    return
                except Exception as e:
                    safe_print(f"Failed to send cached video in lock: {e}")

            status_msg = bot.send_message(
                chat_id=call.message.chat.id,
                text=TRANSLATIONS[lang]['downloading_video'],
                reply_to_message_id=call.message.message_id
            )

            full_video_path = None
            try:
                full_video_path = download_video_by_id(video_id)
                if full_video_path and os.path.exists(full_video_path):
                    file_size = os.path.getsize(full_video_path)
                    if file_size > MAX_FILE_SIZE:
                        bot.edit_message_text(TRANSLATIONS[lang]['size_error'], call.message.chat.id, status_msg.message_id)
                    else:
                        with open(full_video_path, 'rb') as video_file:
                            sent_msg = bot.send_video(
                                chat_id=call.message.chat.id,
                                video=video_file,
                                caption=CAPTION_TEXT,
                                reply_to_message_id=call.message.message_id,
                                supports_streaming=True
                            )
                        if sent_msg and sent_msg.video:
                            set_cached_audio(cache_key, sent_msg.video.file_id, title, "Video")
                        bot.delete_message(call.message.chat.id, status_msg.message_id)
                    os.remove(full_video_path)
                else:
                    bot.edit_message_text(TRANSLATIONS[lang]['download_error'], call.message.chat.id, status_msg.message_id)
            except Exception as e:
                safe_print(f"Download video callback error: {e}")
                traceback.print_exc()
                try:
                    bot.edit_message_text(TRANSLATIONS[lang]['general_error'], call.message.chat.id, status_msg.message_id)
                except Exception:
                    pass
            finally:
                if full_video_path and os.path.exists(full_video_path):
                    try:
                        os.remove(full_video_path)
                    except Exception:
                        pass

    enqueue_task(call.message.chat.id, run_dlv)


def periodic_cleanup():
    while True:
        try:
            cleanup_old_downloads()
        except Exception as e:
            print(f"Error during periodic cleanup: {e}")
        time.sleep(1800)  # run every 30 minutes

def start_dummy_server():
    import http.server
    import socketserver
    
    class DummyHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot is alive!")
            
    port = int(os.environ.get("PORT", 8080))
    socketserver.TCPServer.allow_reuse_address = True
    try:
        with socketserver.TCPServer(("", port), DummyHandler) as httpd:
            print(f"Dummy HTTP server started on port {port}")
            httpd.serve_forever()
    except Exception as e:
        print(f"Error starting dummy HTTP server: {e}")

if __name__ == '__main__':
    init_user_db()
    # Start dummy HTTP server for Render free tier compatibility
    threading.Thread(target=start_dummy_server, daemon=True, name="web-server").start()
    cleanup_old_downloads()
    try:
        bot.set_my_commands([
            telebot.types.BotCommand("start", "Start / Restart Bot"),
            telebot.types.BotCommand("lang", "Change language"),
            telebot.types.BotCommand("instagram", "Instagram Downloader"),
            telebot.types.BotCommand("youtube", "YouTube Downloader"),
            telebot.types.BotCommand("shazam", "Shazam Music Finder")
        ])
        print("Bot menyu buyruqlari sozlandi.")
    except Exception as e:
        print(f"Error setting bot commands: {e}")

    threading.Thread(target=periodic_cleanup, daemon=True, name="cleanup-thread").start()
    print("Bot ishga tushdi...")
    bot.infinity_polling()