# -*- coding: utf-8 -*-
"""
KinoHub Pro Bot — to'liq funksional Telegram kino-bot (v2)
=============================================================

O'RNATISH:
    pip install python-telegram-bot --upgrade
    (Kunlik avtomatik baza zaxirasi ishlashi uchun ixtiyoriy: )
    pip install "python-telegram-bot[job-queue]" --upgrade

SOZLASH (2 usul bor):
    1) Fayl ichida pastdagi "SOZLAMALAR" bo'limini to'ldiring, YOKI
    2) (tavsiya etiladi, ayniqsa Railway kabi hostinglar uchun)
       Quyidagi Environment Variable'larni sozlang:
           BOT_TOKEN, BOT_USERNAME, SUPER_ADMIN_ID, DB_PATH

RAILWAY'DA MA'LUMOT O'CHIB KETMASLIGI UCHUN:
    Railway loyihangizga "Volume" qo'shing (Settings -> Volumes -> New Volume),
    masalan "/data" manziliga ulang, so'ng DB_PATH environment variable'ini
    "/data/kinobot.db" qilib belgilang. Shunda kod yangilansa ham (deploy),
    baza fayli volume'da saqlanib qoladi va o'chib ketmaydi.

ISHGA TUSHIRISH:
    python bot.py
"""

import os
import re
import shutil
import difflib
import asyncio
import logging
import sqlite3
import datetime
from contextlib import closing

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ChatMemberStatus, ParseMode, ChatType
from telegram.error import TelegramError, Forbidden
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    ChatJoinRequestHandler,
    ContextTypes,
    filters,
)

# ============================== SOZLAMALAR ==============================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "SIZNING_TOKEN_BU_YERGA")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "KinoHub_brobot")
SUPER_ADMIN_ID = int(os.environ.get("SUPER_ADMIN_ID", "123456789"))

# DB_PATH ustuvorligi: 1) qo'lda DB_PATH  2) Railway Volume avtomatik aniqlanadi  3) joriy papka
_railway_volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
if os.environ.get("DB_PATH"):
    DB_PATH = os.environ["DB_PATH"]
elif _railway_volume:
    DB_PATH = os.path.join(_railway_volume, "kinobot.db")
else:
    DB_PATH = "kinobot.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============================== BAZA (DATABASE) ==============================

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _safe_add_column(conn, table, coldef):
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
    except sqlite3.OperationalError:
        pass  # ustun allaqachon mavjud


def with_conn_count(query: str, params: tuple) -> int:
    """Bitta COUNT(*) natijasini qaytaruvchi qisqa yordamchi."""
    with closing(get_conn()) as conn:
        row = conn.execute(query, params).fetchone()
        return row["c"] if row else 0


def init_db():
    with closing(get_conn()) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id         INTEGER PRIMARY KEY,
                username        TEXT,
                first_name      TEXT,
                balance         REAL DEFAULT 0,
                ref_by          INTEGER,
                ref_bonus_given INTEGER DEFAULT 0,
                xp              INTEGER DEFAULT 0,
                level           INTEGER DEFAULT 1,
                joined_at       TEXT
            );

            CREATE TABLE IF NOT EXISTS mission_progress (
                user_id     INTEGER,
                mission_key TEXT,
                day         TEXT,
                completed   INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, mission_key, day)
            );

            CREATE TABLE IF NOT EXISTS movies (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                code        TEXT NOT NULL,
                episode     INTEGER DEFAULT 1,
                title       TEXT,
                genre       TEXT,
                language    TEXT,
                country     TEXT,
                file_id     TEXT,
                file_type   TEXT,
                mode        TEXT DEFAULT 'full',   -- 'full' (rasm, tolq malumot) yoki 'simple' (qisqa video)
                is_series   INTEGER DEFAULT 0,      -- 0=oddiy kino, 1=serial (kod band bolish mantigi uchun)
                downloads   INTEGER DEFAULT 0,
                created_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS downloads_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                movie_id      INTEGER,
                user_id       INTEGER,
                downloaded_at TEXT
            );

            CREATE TABLE IF NOT EXISTS join_requests (
                chat_id      INTEGER,
                user_id      INTEGER,
                requested_at TEXT,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS channels (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER,
                username    TEXT,
                invite_link TEXT,
                title       TEXT,
                ctype       TEXT,     -- 'public' yoki 'private'
                kind        TEXT      -- 'channel' yoki 'group'
            );

            CREATE TABLE IF NOT EXISTS pending_chats (
                chat_id     INTEGER PRIMARY KEY,
                title       TEXT,
                username    TEXT,
                kind        TEXT,
                detected_at TEXT
            );

            CREATE TABLE IF NOT EXISTS admins (
                user_id     INTEGER PRIMARY KEY,
                added_by    INTEGER,
                added_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS payment_requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                ptype       TEXT,
                amount      REAL,
                status      TEXT DEFAULT 'pending',
                created_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS radar_requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                query       TEXT,
                notified    INTEGER DEFAULT 0,
                created_at  TEXT,
                UNIQUE(user_id, query)
            );

            CREATE TABLE IF NOT EXISTS watch_later (
                user_id     INTEGER,
                code        TEXT,
                added_at    TEXT,
                PRIMARY KEY (user_id, code)
            );

            CREATE INDEX IF NOT EXISTS idx_users_ref_by ON users(ref_by);
            CREATE TABLE IF NOT EXISTS favorites (
                user_id     INTEGER,
                code        TEXT,
                added_at    TEXT,
                PRIMARY KEY (user_id, code)
            );

            CREATE TABLE IF NOT EXISTS ratings (
                user_id     INTEGER,
                code        TEXT,
                stars       INTEGER,
                rated_at    TEXT,
                PRIMARY KEY (user_id, code)
            );

            CREATE TABLE IF NOT EXISTS lucky_codes (
                code        TEXT PRIMARY KEY,
                reward      REAL,
                added_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS lucky_code_claims (
                user_id     INTEGER,
                code        TEXT,
                claimed_at  TEXT,
                PRIMARY KEY (user_id, code)
            );

            CREATE TABLE IF NOT EXISTS secret_code_claims (
                user_id     INTEGER,
                day         TEXT,
                claimed_at  TEXT,
                PRIMARY KEY (user_id, day)
            );

            CREATE INDEX IF NOT EXISTS idx_movies_code ON movies(code);
            CREATE INDEX IF NOT EXISTS idx_payment_status ON payment_requests(status);
            CREATE INDEX IF NOT EXISTS idx_downloads_log_at ON downloads_log(downloaded_at);
            CREATE INDEX IF NOT EXISTS idx_radar_notified ON radar_requests(notified);
            """
        )
        # eski bazalar uchun xavfsiz migratsiya
        _safe_add_column(conn, "users", "ref_bonus_given INTEGER DEFAULT 0")
        _safe_add_column(conn, "channels", "kind TEXT DEFAULT 'channel'")
        _safe_add_column(conn, "movies", "mode TEXT DEFAULT 'full'")
        _safe_add_column(conn, "movies", "is_series INTEGER DEFAULT 0")
        _safe_add_column(conn, "users", "xp INTEGER DEFAULT 0")
        _safe_add_column(conn, "users", "level INTEGER DEFAULT 1")
        _safe_add_column(conn, "movies", "shares INTEGER DEFAULT 0")
        _safe_add_column(conn, "users", "vip_forced INTEGER DEFAULT 0")
        _safe_add_column(conn, "users", "streak_count INTEGER DEFAULT 0")
        _safe_add_column(conn, "users", "last_active_date TEXT")
        _safe_add_column(conn, "users", "streak_last_reward INTEGER DEFAULT 0")
        _safe_add_column(conn, "users", "last_mystery_date TEXT")
        _safe_add_column(conn, "lucky_codes", "expires_at TEXT")
        _safe_add_column(conn, "lucky_codes", "max_uses INTEGER DEFAULT 0")

        defaults = {
            "movie_channel_username": "",
            "movie_channel_chatid": "",
            "referral_bonus": "500",
            "premium_price": "50000",
            "stars_price": "20000",
            "bot_enabled": "1",
            "xp_per_view": "5",
            "xp_per_referral": "20",
            "xp_level_step": "100",
            "vip_threshold_referrals": "10",
            "streak_reward_7": "1000",
            "streak_reward_30": "5000",
            "streak_reward_100": "20000",
            "mystery_box_min": "200",
            "mystery_box_max": "2000",
            "secret_code_today": "",
            "secret_code_reward": "1000",
            "secret_code_set_at": "",
            "secret_code_expires_at": "",
            "secret_code_max_uses": "0",
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

        conn.execute(
            "INSERT OR IGNORE INTO admins (user_id, added_by, added_at) VALUES (?, ?, ?)",
            (SUPER_ADMIN_ID, SUPER_ADMIN_ID, datetime.datetime.now().isoformat()),
        )


def get_setting(key: str) -> str:
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else ""


def set_setting(key: str, value: str):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def is_admin(user_id: int) -> bool:
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
        return row is not None


def get_admin_ids() -> list:
    with closing(get_conn()) as conn:
        return [r["user_id"] for r in conn.execute("SELECT user_id FROM admins").fetchall()]


def get_user(user_id: int):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def ref_count(user_id: int) -> int:
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM users WHERE ref_by=?", (user_id,)).fetchone()
        return row["c"] if row else 0


# ============================== RADAR (KUTILAYOTGAN KINOLAR) ==============================

def add_radar_request(user_id: int, query: str):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO radar_requests (user_id, query, notified, created_at) VALUES (?, ?, 0, ?)",
            (user_id, query.strip(), datetime.datetime.now().isoformat()),
        )


def get_pending_radar_requests():
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM radar_requests WHERE notified=0").fetchall()


def mark_radar_notified(request_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE radar_requests SET notified=1 WHERE id=?", (request_id,))


def radar_stats():
    """Har bir so'rov nechta marta so'ralganini ko'rsatadi (admin panel uchun)."""
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT query, COUNT(*) AS c FROM radar_requests WHERE notified=0 "
            "GROUP BY query ORDER BY c DESC, MAX(created_at) DESC LIMIT 30"
        ).fetchall()


async def notify_radar_waiters(context: ContextTypes.DEFAULT_TYPE, title: str, code: str):
    """Yangi kino/qism qo'shilganda, shu nomni kutayotgan foydalanuvchilarga xabar beradi."""
    if not title:
        return
    title_l = title.strip().lower()
    for req in get_pending_radar_requests():
        q = (req["query"] or "").strip().lower()
        if not q:
            continue
        matched = q in title_l or title_l in q or difflib.SequenceMatcher(None, q, title_l).ratio() >= 0.6
        if not matched:
            continue
        try:
            await context.bot.send_message(
                req["user_id"],
                f"🔔 Siz kutayotgan kino botga qo'shildi!\n\n🎬 {title}\n🎬 Kino kodi: {code}\n\n"
                f"Yuklab olish uchun kodni yuboring: {code}",
            )
        except (Forbidden, TelegramError):
            pass
        mark_radar_notified(req["id"])


# ============================== KEYIN KO'RAMAN (WATCH LATER) ==============================

def add_watch_later(user_id: int, code: str):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO watch_later (user_id, code, added_at) VALUES (?, ?, ?)",
            (user_id, code, datetime.datetime.now().isoformat()),
        )


def remove_watch_later(user_id: int, code: str):
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM watch_later WHERE user_id=? AND code=?", (user_id, code))


def is_in_watch_later(user_id: int, code: str) -> bool:
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT 1 FROM watch_later WHERE user_id=? AND code=?", (user_id, code)
        ).fetchone()
        return row is not None


def get_watch_later(user_id: int):
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT w.code, MAX(m.title) AS title FROM watch_later w "
            "LEFT JOIN movies m ON m.code = w.code "
            "WHERE w.user_id=? GROUP BY w.code ORDER BY MAX(w.added_at) DESC",
            (user_id,),
        ).fetchall()


def watch_later_count(user_id: int) -> int:
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM watch_later WHERE user_id=?", (user_id,)).fetchone()
        return row["c"] if row else 0


def change_balance(user_id: int, delta: float):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, user_id))


# ============================== SEVIMLILAR (FAVORITES) ==============================

def toggle_favorite(user_id: int, code: str) -> bool:
    """True qaytarsa — qo'shildi, False qaytarsa — olib tashlandi."""
    with closing(get_conn()) as conn, conn:
        row = conn.execute("SELECT 1 FROM favorites WHERE user_id=? AND code=?", (user_id, code)).fetchone()
        if row:
            conn.execute("DELETE FROM favorites WHERE user_id=? AND code=?", (user_id, code))
            return False
        conn.execute(
            "INSERT INTO favorites (user_id, code, added_at) VALUES (?, ?, ?)",
            (user_id, code, datetime.datetime.now().isoformat()),
        )
        return True


def is_favorite(user_id: int, code: str) -> bool:
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT 1 FROM favorites WHERE user_id=? AND code=?", (user_id, code)
        ).fetchone() is not None


def favorites_count_for_user(user_id: int) -> int:
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT COUNT(*) c FROM favorites WHERE user_id=?", (user_id,)).fetchone()
        return row["c"] if row else 0


def favorites_count_for_code(code: str) -> int:
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT COUNT(*) c FROM favorites WHERE code=?", (code,)).fetchone()
        return row["c"] if row else 0


def get_user_favorites(user_id: int):
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT f.code, MAX(m.title) AS title FROM favorites f "
            "LEFT JOIN movies m ON m.code = f.code "
            "WHERE f.user_id=? GROUP BY f.code ORDER BY MAX(f.added_at) DESC",
            (user_id,),
        ).fetchall()


# ============================== BAHOLASH (RATING) ==============================

def rate_movie(user_id: int, code: str, stars: int):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO ratings (user_id, code, stars, rated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, code) DO UPDATE SET stars=excluded.stars, rated_at=excluded.rated_at",
            (user_id, code, stars, datetime.datetime.now().isoformat()),
        )


def get_avg_rating(code: str):
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT AVG(stars) AS avg_r, COUNT(*) AS c FROM ratings WHERE code=?", (code,)
        ).fetchone()
        return (row["avg_r"], row["c"]) if row and row["c"] else (None, 0)


# ============================== KINO STATISTIKASI ==============================

def code_total_downloads(code: str) -> int:
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT COALESCE(SUM(downloads), 0) c FROM movies WHERE code=?", (code,)).fetchone()
        return row["c"] if row else 0


def code_total_views(code: str) -> int:
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM downloads_log dl JOIN movies m ON m.id = dl.movie_id WHERE m.code=?", (code,)
        ).fetchone()
        return row["c"] if row else 0


def increment_shares(code: str):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE movies SET shares = shares + 1 WHERE code=?", (code,))


def code_total_shares(code: str) -> int:
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT COALESCE(SUM(shares), 0) c FROM movies WHERE code=?", (code,)).fetchone()
        return row["c"] if row else 0


def movie_stats_text(code: str) -> str:
    avg_r, rating_c = get_avg_rating(code)
    rating_line = f"⭐ O'rtacha baho: {avg_r:.1f}/5 ({rating_c} ta baho)" if rating_c else "⭐ O'rtacha baho: hali baho yo'q"
    return (
        f"👁 Ko'rilganlar: {code_total_views(code)} ta\n"
        f"⬇️ Yuklab olinganlar: {code_total_downloads(code)} ta\n"
        f"{rating_line}\n"
        f"❤️ Sevimlilar soni: {favorites_count_for_code(code)} ta\n"
        f"📤 Ulashilganlar: {code_total_shares(code)} ta"
    )


# ============================== TARIX (WATCH HISTORY) ==============================

def get_user_history(user_id: int, limit: int = 10):
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT m.code, m.title, dl.downloaded_at FROM downloads_log dl "
            "JOIN movies m ON m.id = dl.movie_id WHERE dl.user_id=? "
            "ORDER BY dl.downloaded_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


# ============================== VIP HOLATI ==============================

def is_vip(user_row) -> bool:
    if user_row is None:
        return False
    if user_row["vip_forced"]:
        return True
    threshold = int(get_setting("vip_threshold_referrals") or 10)
    return ref_count(user_row["user_id"]) >= threshold


def set_vip_forced(user_id: int, value: bool):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE users SET vip_forced=? WHERE user_id=?", (1 if value else 0, user_id))


# ============================== STREAK (KETMA-KET KIRISH) ==============================

STREAK_MILESTONES = [(7, "streak_reward_7"), (30, "streak_reward_30"), (100, "streak_reward_100")]


async def update_streak_and_reward(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Har kuni FAQAT bir marta (foydalanuvchi /start bosganda) chaqiriladi:
    ketma-ket kirish kunini hisoblaydi va 7/30/100 kunlarda bonus beradi."""
    u = get_user(user_id)
    if u is None:
        return
    today = datetime.date.today()
    last = u["last_active_date"]
    if last == today.isoformat():
        return  # bugun allaqachon hisoblangan
    if last:
        try:
            last_date = datetime.date.fromisoformat(last)
        except ValueError:
            last_date = None
    else:
        last_date = None
    if last_date and (today - last_date).days == 1:
        new_streak = (u["streak_count"] or 0) + 1
    else:
        new_streak = 1
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "UPDATE users SET streak_count=?, last_active_date=? WHERE user_id=?",
            (new_streak, today.isoformat(), user_id),
        )
    last_reward = u["streak_last_reward"] or 0
    for milestone, setting_key in STREAK_MILESTONES:
        if new_streak >= milestone > last_reward:
            bonus = float(get_setting(setting_key) or 0)
            change_balance(user_id, bonus)
            with closing(get_conn()) as conn, conn:
                conn.execute("UPDATE users SET streak_last_reward=? WHERE user_id=?", (milestone, user_id))
            try:
                await context.bot.send_message(
                    user_id,
                    f"🔥 {milestone} kunlik streak uchun tabriklaymiz!\n💰 Bonus: {bonus:.0f} so'm hisobingizga qo'shildi.",
                )
            except (Forbidden, TelegramError):
                pass


# ============================== SIRLI QUTI ==============================

def can_open_mystery_box(user_id: int) -> bool:
    u = get_user(user_id)
    if u is None:
        return False
    return u["last_mystery_date"] != datetime.date.today().isoformat()


def open_mystery_box(user_id: int) -> float:
    import random
    lo = float(get_setting("mystery_box_min") or 200)
    hi = float(get_setting("mystery_box_max") or 2000)
    reward = round(random.uniform(lo, hi), -2) or lo  # 100 ga yaqinlashtirish
    change_balance(user_id, reward)
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "UPDATE users SET last_mystery_date=? WHERE user_id=?",
            (datetime.date.today().isoformat(), user_id),
        )
    return reward


# ============================== BUGUNGI MAXFIY KOD ==============================

def has_claimed_secret_code_today(user_id: int) -> bool:
    today = datetime.date.today().isoformat()
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT 1 FROM secret_code_claims WHERE user_id=? AND day=?", (user_id, today)
        ).fetchone() is not None


def claim_secret_code(user_id: int):
    today = datetime.date.today().isoformat()
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO secret_code_claims (user_id, day, claimed_at) VALUES (?, ?, ?)",
            (user_id, today, datetime.datetime.now().isoformat()),
        )


def secret_code_claims_count_since(since_iso: str) -> int:
    """Joriy maxfiy kod o'rnatilganidan beri nechta odam uni ishlatganini sanaydi."""
    if not since_iso:
        return 0
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM secret_code_claims WHERE claimed_at >= ?", (since_iso,)
        ).fetchone()
        return row["c"] if row else 0


def is_secret_code_active() -> bool:
    """Maxfiy kod hali muddati o'tmaganmi va foydalanuvchilar chegarasiga yetmaganmi."""
    code = get_setting("secret_code_today")
    if not code:
        return False
    expires_at = get_setting("secret_code_expires_at")
    if expires_at:
        try:
            if datetime.datetime.now() > datetime.datetime.fromisoformat(expires_at):
                return False
        except ValueError:
            pass
    max_uses = int(get_setting("secret_code_max_uses") or 0)
    if max_uses:
        set_at = get_setting("secret_code_set_at")
        if secret_code_claims_count_since(set_at) >= max_uses:
            return False
    return True


# ============================== OMADLI KODLAR ==============================

def set_lucky_code(code: str, reward: float, expires_at: str = "", max_uses: int = 0):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO lucky_codes (code, reward, added_at, expires_at, max_uses) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(code) DO UPDATE SET reward=excluded.reward, added_at=excluded.added_at, "
            "expires_at=excluded.expires_at, max_uses=excluded.max_uses",
            (code, reward, datetime.datetime.now().isoformat(), expires_at, max_uses),
        )


def get_lucky_code(code: str):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM lucky_codes WHERE code=?", (code,)).fetchone()


def remove_lucky_code(code: str):
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM lucky_codes WHERE code=?", (code,))


def lucky_code_claims_count(code: str) -> int:
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT COUNT(*) c FROM lucky_code_claims WHERE code=?", (code,)).fetchone()
        return row["c"] if row else 0


async def claim_lucky_code_if_needed(context: ContextTypes.DEFAULT_TYPE, user_id: int, code: str):
    lucky = get_lucky_code(code)
    if not lucky:
        return

    # Muddati tugaganmi?
    if lucky["expires_at"]:
        try:
            if datetime.datetime.now() > datetime.datetime.fromisoformat(lucky["expires_at"]):
                remove_lucky_code(code)  # muddati o'tgan omadli kodni avtomatik tozalaymiz
                return
        except ValueError:
            pass

    # Foydalanuvchilar soni chegarasiga yetganmi?
    max_uses = lucky["max_uses"] or 0
    if max_uses and lucky_code_claims_count(code) >= max_uses:
        return

    with closing(get_conn()) as conn:
        already = conn.execute(
            "SELECT 1 FROM lucky_code_claims WHERE user_id=? AND code=?", (user_id, code)
        ).fetchone()
    if already:
        return
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO lucky_code_claims (user_id, code, claimed_at) VALUES (?, ?, ?)",
            (user_id, code, datetime.datetime.now().isoformat()),
        )
    change_balance(user_id, lucky["reward"])
    try:
        await context.bot.send_message(
            user_id,
            f"🍀 Tabriklaymiz! Siz OMADLI KODni topdingiz!\n💰 Bonus: {lucky['reward']:.0f} so'm hisobingizga qo'shildi.",
        )
    except (Forbidden, TelegramError):
        pass


def mark_ref_bonus_given(user_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE users SET ref_bonus_given=1 WHERE user_id=?", (user_id,))


# ============================== XP / DARAJA (LEVEL) ==============================

def level_for_xp(xp: int) -> int:
    step = int(get_setting("xp_level_step") or 100)
    if step <= 0:
        step = 100
    return xp // step + 1


async def add_xp(context: ContextTypes.DEFAULT_TYPE, user_id: int, amount: int):
    """Foydalanuvchiga XP qo'shadi, darajani qayta hisoblaydi va daraja
    ko'tarilsa foydalanuvchiga xabar yuboradi."""
    u = get_user(user_id)
    if not u:
        return
    old_level = u["level"] or 1
    new_xp = (u["xp"] or 0) + amount
    new_level = level_for_xp(new_xp)
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE users SET xp=?, level=? WHERE user_id=?", (new_xp, new_level, user_id))
    if new_level > old_level:
        try:
            await context.bot.send_message(
                user_id,
                f"🎉 Tabriklaymiz! Siz {new_level}-darajaga chiqdingiz!\n✨ Jami XP: {new_xp}"
            )
        except TelegramError:
            pass


def xp_progress_text(xp: int, level: int) -> str:
    step = int(get_setting("xp_level_step") or 100)
    if step <= 0:
        step = 100
    current_floor = (level - 1) * step
    next_floor = level * step
    have = xp - current_floor
    need = next_floor - current_floor
    return f"{have}/{need} XP (keyingi daraja: {level + 1})"


# ============================== KUNLIK MISSIYALAR ==============================

def _today_str() -> str:
    return datetime.date.today().isoformat()


DAILY_MISSIONS = [
    {"key": "watch3", "text": "🎬 Bugun 3 ta kino ko'rish", "target": 3, "xp": 30, "bonus": 500},
    {"key": "refer1", "text": "👥 Bugun 1 ta do'st taklif qilish", "target": 1, "xp": 50, "bonus": 1000},
]


def _mission_progress_value(user_id: int, key: str) -> int:
    today = _today_str()
    with closing(get_conn()) as conn:
        if key == "watch3":
            row = conn.execute(
                "SELECT COUNT(*) c FROM downloads_log WHERE user_id=? AND downloaded_at >= ?",
                (user_id, today),
            ).fetchone()
            return row["c"]
        if key == "refer1":
            row = conn.execute(
                "SELECT COUNT(*) c FROM users WHERE ref_by=? AND ref_bonus_given=1 AND joined_at >= ?",
                (user_id, today),
            ).fetchone()
            return row["c"]
    return 0


def _is_mission_completed(user_id: int, key: str, day: str) -> bool:
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT completed FROM mission_progress WHERE user_id=? AND mission_key=? AND day=?",
            (user_id, key, day),
        ).fetchone()
        return bool(row and row["completed"])


def _mark_mission_completed(user_id: int, key: str, day: str):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO mission_progress (user_id, mission_key, day, completed) VALUES (?,?,?,1) "
            "ON CONFLICT(user_id, mission_key, day) DO UPDATE SET completed=1",
            (user_id, key, day),
        )


async def check_and_complete_missions(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Har bir kunlik missiyani tekshiradi; yangi bajarilgan bo'lsa mukofot beradi."""
    today = _today_str()
    for m in DAILY_MISSIONS:
        if _is_mission_completed(user_id, m["key"], today):
            continue
        progress = _mission_progress_value(user_id, m["key"])
        if progress >= m["target"]:
            _mark_mission_completed(user_id, m["key"], today)
            change_balance(user_id, m["bonus"])
            await add_xp(context, user_id, m["xp"])
            try:
                await context.bot.send_message(
                    user_id,
                    f"✅ Missiya bajarildi: {m['text']}\n"
                    f"🎁 Mukofot: +{m['bonus']:.0f} so'm, +{m['xp']} XP"
                )
            except TelegramError:
                pass


def get_missions_status(user_id: int):
    """Har bir missiya uchun (matn, progress, target, bajarilganmi) ro'yxati."""
    today = _today_str()
    result = []
    for m in DAILY_MISSIONS:
        progress = min(_mission_progress_value(user_id, m["key"]), m["target"])
        completed = _is_mission_completed(user_id, m["key"], today)
        result.append((m["text"], progress, m["target"], completed))
    return result


# ============================== LIGA (HAFTALIK REYTING) ==============================

def _week_start_iso() -> str:
    now = datetime.datetime.now()
    monday = (now - datetime.timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday.isoformat()


def liga_leaderboard(limit: int = 10):
    """Bu haftaning reytingi: faollik (kino ko'rish/yuklab olish, x1) + referal (x5) ballari bo'yicha."""
    week_start = _week_start_iso()
    with closing(get_conn()) as conn:
        rows = conn.execute(
            """
            SELECT u.user_id, u.first_name,
                COALESCE(v.views, 0) AS views,
                COALESCE(r.refs, 0) AS refs,
                (COALESCE(v.views,0)*1 + COALESCE(r.refs,0)*5) AS score
            FROM users u
            LEFT JOIN (
                SELECT user_id, COUNT(*) views FROM downloads_log
                WHERE downloaded_at >= ? GROUP BY user_id
            ) v ON v.user_id = u.user_id
            LEFT JOIN (
                SELECT ref_by AS user_id, COUNT(*) refs FROM users
                WHERE ref_bonus_given=1 AND joined_at >= ? GROUP BY ref_by
            ) r ON r.user_id = u.user_id
            WHERE score > 0
            ORDER BY score DESC
            LIMIT ?
            """,
            (week_start, week_start, limit),
        ).fetchall()
    return rows


def liga_user_rank(user_id: int):
    """Foydalanuvchining bu haftadagi reytingdagi o'rni va balli. Topilmasa None."""
    board = liga_leaderboard(limit=100000)
    for i, row in enumerate(board, start=1):
        if row["user_id"] == user_id:
            return i, row["score"]
    return None, 0


def register_user_if_new(user_id, username, first_name, ref_by=None) -> bool:
    with closing(get_conn()) as conn:
        existing = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
    if existing:
        return False
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO users (user_id, username, first_name, balance, ref_by, ref_bonus_given, joined_at) "
            "VALUES (?, ?, ?, 0, ?, 0, ?)",
            (user_id, username, first_name, ref_by, datetime.datetime.now().isoformat()),
        )
    return True


# ---- Kanallar / guruhlar ----

def get_mandatory_channels():
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM channels ORDER BY id").fetchall()


def add_channel(chat_id, username, invite_link, title, ctype, kind):
    with closing(get_conn()) as conn, conn:
        existing = conn.execute("SELECT id FROM channels WHERE chat_id=?", (chat_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE channels SET username=?, invite_link=?, title=?, ctype=?, kind=? WHERE chat_id=?",
                (username, invite_link, title, ctype, kind, chat_id),
            )
        else:
            conn.execute(
                "INSERT INTO channels (chat_id, username, invite_link, title, ctype, kind) "
                "VALUES (?,?,?,?,?,?)",
                (chat_id, username, invite_link, title, ctype, kind),
            )


def delete_channel(channel_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM channels WHERE id=?", (channel_id,))


def get_channel_by_id(channel_id: int):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()


# ---- Aniqlangan (pending) chatlar ----

def upsert_pending_chat(chat_id, title, username, kind):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO pending_chats (chat_id, title, username, kind, detected_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, username=excluded.username, "
            "kind=excluded.kind, detected_at=excluded.detected_at",
            (chat_id, title, username, kind, datetime.datetime.now().isoformat()),
        )


def get_pending_chat(chat_id: int):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM pending_chats WHERE chat_id=?", (chat_id,)).fetchone()


def delete_pending_chat(chat_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM pending_chats WHERE chat_id=?", (chat_id,))


# ---- Maxfiy kanal/guruh sorovlari (faqat QAYD qilinadi, avtomatik tasdiqlanmaydi) ----

def record_join_request(chat_id: int, user_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO join_requests (chat_id, user_id, requested_at) VALUES (?, ?, ?)",
            (chat_id, user_id, datetime.datetime.now().isoformat()),
        )


def has_join_request(chat_id: int, user_id: int) -> bool:
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT 1 FROM join_requests WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ).fetchone()
        return row is not None


# ---- Kinolar ----

def get_movies_by_code(code: str):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM movies WHERE code=? ORDER BY episode", (code,)).fetchall()


def get_movie_episode(code: str, episode: int):
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT * FROM movies WHERE code=? AND episode=?", (code, episode)
        ).fetchone()


def get_random_movie():
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM movies ORDER BY RANDOM() LIMIT 1").fetchone()


def get_all_movie_titles():
    """Smart Search uchun: nomi bor barcha (kod, nom) juftliklari, har kod bittadan.
    'simple' (qisqa video) rejimidagi nomlar bu yerga KIRMAYDI — ular faqat bazada
    saqlanadi, qidiruvda ishtirok etmaydi."""
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT DISTINCT code, title FROM movies WHERE title IS NOT NULL AND title != '' "
            "AND mode != 'simple' GROUP BY code"
        ).fetchall()


def add_movie(code, episode, title, genre, language, country, file_id, file_type, mode="full", is_series=0):
    with closing(get_conn()) as conn, conn:
        existing = conn.execute(
            "SELECT id FROM movies WHERE code=? AND episode=?", (code, episode)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE movies SET title=?, genre=?, language=?, country=?, file_id=?, "
                "file_type=?, mode=?, is_series=? WHERE id=?",
                (title, genre, language, country, file_id, file_type, mode, is_series, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO movies (code, episode, title, genre, language, country, file_id, "
                "file_type, mode, is_series, downloads, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,?)",
                (code, episode, title, genre, language, country, file_id, file_type, mode, is_series,
                 datetime.datetime.now().isoformat()),
            )


def delete_movie(movie_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM movies WHERE id=?", (movie_id,))


def get_movie_by_id(movie_id: int):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()


_EDITABLE_MOVIE_FIELDS = {"title", "genre", "language", "country", "episode"}


def update_movie_field(movie_id: int, field: str, value):
    if field not in _EDITABLE_MOVIE_FIELDS:
        return
    with closing(get_conn()) as conn, conn:
        conn.execute(f"UPDATE movies SET {field}=? WHERE id=?", (value, movie_id))


def delete_movies_by_code(code: str):
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM movies WHERE code=?", (code,))


def increment_downloads(movie_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE movies SET downloads = downloads + 1 WHERE id=?", (movie_id,))


def record_download(movie_id: int, user_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO downloads_log (movie_id, user_id, downloaded_at) VALUES (?, ?, ?)",
            (movie_id, user_id, datetime.datetime.now().isoformat()),
        )


# ---- To'lovlar ----

def add_payment_request(user_id, ptype, amount) -> int:
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO payment_requests (user_id, ptype, amount, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (user_id, ptype, amount, datetime.datetime.now().isoformat()),
        )
        return cur.lastrowid


def get_payment_request(req_id: int):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM payment_requests WHERE id=?", (req_id,)).fetchone()


def set_payment_status(req_id: int, status: str):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE payment_requests SET status=? WHERE id=?", (status, req_id))


def get_pending_payments():
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT * FROM payment_requests WHERE status='pending' ORDER BY id DESC"
        ).fetchall()


# ---- Umumiy ----

def all_user_ids():
    with closing(get_conn()) as conn:
        return [r["user_id"] for r in conn.execute("SELECT user_id FROM users").fetchall()]


def _day_boundaries():
    now = datetime.datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    labels = ["Bugun", "Kecha", "2 kun oldin"]
    return [
        (labels[i], today_start - datetime.timedelta(days=i), today_start - datetime.timedelta(days=i - 1))
        for i in range(3)
    ]


def _week_boundaries():
    now = datetime.datetime.now()
    this_monday = (now - datetime.timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    labels = ["Bu hafta", "O'tgan hafta", "2 hafta oldin"]
    return [
        (labels[i], this_monday - datetime.timedelta(weeks=i), this_monday - datetime.timedelta(weeks=i - 1))
        for i in range(3)
    ]


def _add_months(dt, n):
    idx = dt.year * 12 + (dt.month - 1) + n
    return datetime.datetime(idx // 12, idx % 12 + 1, 1)


def _month_boundaries():
    now = datetime.datetime.now()
    this_month = _add_months(now, 0)
    labels = ["Bu oy", "O'tgan oy", "2 oy oldin"]
    return [(labels[i], _add_months(this_month, -i), _add_months(this_month, -i + 1)) for i in range(3)]


def stats_breakdown(kind: str):
    """kind: 'day' | 'week' | 'month' -> [(label, yangi_foydalanuvchi, yuklab_olish), ...] eng yangisi birinchi"""
    if kind == "day":
        periods = _day_boundaries()
    elif kind == "week":
        periods = _week_boundaries()
    else:
        periods = _month_boundaries()

    results = []
    with closing(get_conn()) as conn:
        for label, start, end in periods:
            s, e = start.isoformat(), end.isoformat()
            users_c = conn.execute(
                "SELECT COUNT(*) c FROM users WHERE joined_at >= ? AND joined_at < ?", (s, e)
            ).fetchone()["c"]
            downloads_c = conn.execute(
                "SELECT COUNT(*) c FROM downloads_log WHERE downloaded_at >= ? AND downloaded_at < ?", (s, e)
            ).fetchone()["c"]
            results.append((label, users_c, downloads_c))
    return results


def general_stats():
    """Umumiy (butun davr) statistika: jami sonlar + eng ko'p yuklab olingan top-5 kino."""
    with closing(get_conn()) as conn:
        total_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        total_codes = conn.execute("SELECT COUNT(DISTINCT code) c FROM movies").fetchone()["c"]
        total_episodes = conn.execute("SELECT COUNT(*) c FROM movies").fetchone()["c"]
        total_downloads = conn.execute("SELECT COALESCE(SUM(downloads), 0) c FROM movies").fetchone()["c"]
        top = conn.execute(
            "SELECT code, MAX(title) AS title, SUM(downloads) AS total_dl "
            "FROM movies GROUP BY code ORDER BY total_dl DESC LIMIT 5"
        ).fetchall()
    return {
        "total_users": total_users,
        "total_codes": total_codes,
        "total_episodes": total_episodes,
        "total_downloads": total_downloads,
        "top": top,
    }


# ============================== INLINE KLAVIATURALAR (oddiy foydalanuvchi) ==============================

def kb_main_menu(user_id: int) -> InlineKeyboardMarkup:
    movie_channel = get_setting("movie_channel_username")
    rows = []
    if movie_channel:
        rows.append([InlineKeyboardButton("🎬 Kino kodlari", url=f"https://t.me/{movie_channel}")])
    else:
        rows.append([InlineKeyboardButton("🎬 Kino kodlari", callback_data="no_movie_channel")])
    rows.append([InlineKeyboardButton("🎲 Tasodifiy kino", callback_data="random_movie"),
                 InlineKeyboardButton("🎁 Sirli quti", callback_data="mystery_box")])
    rows.append([InlineKeyboardButton("👤 Profil", callback_data="profile"),
                 InlineKeyboardButton("💰 Pul ishlash", callback_data="earn")])
    if is_admin(user_id):
        rows.append([InlineKeyboardButton("🛠 Boshqaruv paneli", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def kb_earn_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Referalim", callback_data="earn_ref")],
        [InlineKeyboardButton("💵 Pul yechish", callback_data="earn_withdraw")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="back_start")],
    ])


def kb_referal_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="earn")],
    ])


def kb_withdraw_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Telegram premium", callback_data="withdraw_premium")],
        [InlineKeyboardButton("✨ Telegram stars", callback_data="withdraw_stars")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="earn")],
    ])


def kb_back(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Orqaga", callback_data=callback_data)]])


# ============================== ADMIN PANEL — PASTKI MENYU (ReplyKeyboard) ==============================

RK_MAIN = ReplyKeyboardMarkup([
    ["📡 Kanallar", "🎬 Kino yuklash"],
    ["✏️ Tahrirlash", "🗑 Kino o'chirish"],
    ["📢 Xabarnoma", "📊 Statistika"],
    ["👮 Adminlar", "💳 To'lovlar"],
    ["🔧 Sozlamalar", "🔎 Foydalanuvchini tekshirish"],
    ["⚙️ Bot holati", "💾 Baza zaxirasi"],
    ["📥 Kutilayotgan kinolar", "🎮 O'yin funksiyalari"],
    ["⬅️ Chiqish"],
], resize_keyboard=True)

RK_GAME = ReplyKeyboardMarkup([
    ["🔑 Bugungi maxfiy kod", "🍀 Omadli kod belgilash"],
    ["🍀 Omadli kodni bekor qilish", "👑 VIP berish/olish"],
    ["⬅️ Orqaga"],
], resize_keyboard=True)

RK_CHANNELS = ReplyKeyboardMarkup([
    ["🔒 Majburiy obunalar", "🎥 Kino kanal"],
    ["⬅️ Orqaga"],
], resize_keyboard=True)

RK_ADMINS = ReplyKeyboardMarkup([
    ["➕ Admin qo'shish", "📋 Adminlar ro'yxati"],
    ["⬅️ Orqaga"],
], resize_keyboard=True)

RK_SETTINGS = ReplyKeyboardMarkup([
    ["👥 Referal narxi", "⭐ Premium narxi"],
    ["✨ Stars narxi", "🎬 XP (kino ko'rish)"],
    ["👥 XP (referal)", "📶 XP (daraja bosqichi)"],
    ["⬅️ Orqaga"],
], resize_keyboard=True)

RK_STATS = ReplyKeyboardMarkup([
    ["📅 Kunlik", "🗓 Haftalik", "📆 Oylik"],
    ["📈 Umumiy"],
    ["⬅️ Orqaga"],
], resize_keyboard=True)


def rk_status() -> ReplyKeyboardMarkup:
    current = get_setting("bot_enabled")
    toggle_label = "🔴 O'chirish" if current == "1" else "🟢 Yoqish"
    return ReplyKeyboardMarkup([[toggle_label], ["⬅️ Orqaga"]], resize_keyboard=True)


def current_admin_keyboard(context: ContextTypes.DEFAULT_TYPE) -> ReplyKeyboardMarkup:
    level = context.user_data.get("admin_level", "main")
    mapping = {
        "main": RK_MAIN, "channels": RK_CHANNELS, "admins": RK_ADMINS,
        "settings": RK_SETTINGS, "stats": RK_STATS, "status": rk_status(),
    }
    return mapping.get(level, RK_MAIN)


# ============================== MAJBURIY OBUNA ==============================

MAX_MANDATORY_CHANNELS = 10


async def check_subscription(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> list:
    """Obuna bo'lmagan kanal/guruhlar ro'yxatini qaytaradi.
    Ommaviy: haqiqiy a'zolik tekshiriladi (get_chat_member).
    Maxfiy: bot HECH KIMNI tasdiqlamaydi — faqat foydalanuvchi so'rov
    yuborganmi (join_requests jadvalida yozuv bormi) shu tekshiriladi.
    ADMINLARDAN majburiy obuna umuman so'ralmaydi."""
    if is_admin(user_id):
        return []
    not_subscribed = []
    for ch in get_mandatory_channels():
        if ch["ctype"] == "private":
            if not has_join_request(ch["chat_id"], user_id):
                not_subscribed.append(ch)
        else:
            try:
                member = await context.bot.get_chat_member(ch["chat_id"], user_id)
                if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                    not_subscribed.append(ch)
            except TelegramError:
                not_subscribed.append(ch)
    return not_subscribed


def kb_subscription(channels) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        icon = "📢" if ch["kind"] == "channel" else "👥"
        if ch["ctype"] == "public" and ch["username"]:
            url = f"https://t.me/{ch['username']}"
        else:
            url = ch["invite_link"] or "https://t.me"
        rows.append([InlineKeyboardButton(f"{icon} {ch['title'] or 'Obuna'}", url=url)])
    rows.append([InlineKeyboardButton("✅ Tekshirdim", callback_data="check_sub")])
    return InlineKeyboardMarkup(rows)


async def grant_referral_bonus_if_needed(context: ContextTypes.DEFAULT_TYPE, user_row):
    """Referal bonusi FAQAT taklif qilingan foydalanuvchi majburiy kanallarga
    a'zo bo'lgandan (tasdiqlangandan) keyin beriladi — oldindan emas."""
    if not user_row or user_row["ref_by"] is None or user_row["ref_bonus_given"]:
        return
    referrer = get_user(user_row["ref_by"])
    if not referrer:
        return
    bonus = float(get_setting("referral_bonus") or 0)
    change_balance(referrer["user_id"], bonus)
    mark_ref_bonus_given(user_row["user_id"])
    xp_amount = int(get_setting("xp_per_referral") or 20)
    await add_xp(context, referrer["user_id"], xp_amount)
    try:
        await context.bot.send_message(
            referrer["user_id"],
            "🎉 Sizning referal havolangiz orqali qo'shilgan foydalanuvchi majburiy "
            "kanal(lar)ga a'zo bo'ldi!\n"
            f"💰 Hisobingizga {bonus:.0f} so'm qo'shildi. (+{xp_amount} XP)"
        )
    except TelegramError:
        pass


async def notify_admins_new_user(context: ContextTypes.DEFAULT_TYPE, user):
    now = datetime.datetime.now()
    if user.username:
        tg_line = f"🔗 Telegram: @{user.username}\n"
    else:
        tg_line = "🔗 Telegram: yo'q\n"
    text = (
        "👤 Yangi obunachi qo'shildi!\n\n"
        f"👤 Ism: {user.first_name or '-'}\n"
        f"🆔 ID: `{user.id}`\n"
        + tg_line +
        f"🕒 Vaqt: {now.strftime('%d.%m.%Y')} | {now.strftime('%H:%M:%S')}"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("👁 Ko'rish", url=f"tg://user?id={user.id}")]])
    for admin_id in get_admin_ids():
        try:
            await context.bot.send_message(admin_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except TelegramError:
            pass


async def notify_admins_movie_not_found(context: ContextTypes.DEFAULT_TYPE, user_id: int, query_text: str):
    """Foydalanuvchi qidirgan kino (kod yoki nom) topilmasa, adminlarga darhol xabar beriladi —
    shunda talab yuqori kinolarni tezroq qo'shish mumkin bo'ladi."""
    u = get_user(user_id)
    name = u["first_name"] if u and u["first_name"] else "-"
    username = u["username"] if u else None
    tg_line = f"🔗 @{username}\n" if username else ""
    text = (
        "🔎 Foydalanuvchi kino qidirdi, lekin topilmadi!\n\n"
        f"👤 {name}\n"
        f"🆔 ID: `{user_id}`\n"
        + tg_line +
        f"📝 So'rov: \"{query_text}\""
    )
    for admin_id in get_admin_ids():
        try:
            await context.bot.send_message(admin_id, text, parse_mode=ParseMode.MARKDOWN)
        except TelegramError:
            pass


async def send_start_message(update_or_query, context: ContextTypes.DEFAULT_TYPE, user):
    text = (
        f"🖐 Assalomu alaykum, {user.first_name}!\n\n"
        "📊 Bot buyruqlari:\n"
        "/start - ♻️ Botni qayta ishga tushirish\n"
        "/help - ☎️ Qo'llab-quvvatlash\n\n"
        "🔎 Film kodini yuboring:"
    )
    kb = kb_main_menu(user.id)
    if hasattr(update_or_query, "message") and update_or_query.message:
        await update_or_query.message.reply_text(text, reply_markup=kb)
    else:
        await context.bot.send_message(user.id, text, reply_markup=kb)


# ============================== /start va /help ==============================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data.pop("state", None)
    context.user_data.pop("admin_level", None)

    if get_setting("bot_enabled") != "1" and not is_admin(user.id):
        await update.message.reply_text("🛠 Botda ta'mirlash ishlari olib borilyapti. Iltimos keyinroq urinib ko'ring.")
        return

    args = context.args
    pending_code = None
    ref_by = None
    if args:
        payload = args[0]
        if payload.startswith("ref") and "_" in payload:
            # Challenge orqali ulashilgan havola: ref<id>_<kino_kodi>
            ref_part, _, code_part = payload.partition("_")
            if ref_part[3:].isdigit():
                candidate = int(ref_part[3:])
                if candidate != user.id:
                    ref_by = candidate
            if code_part:
                pending_code = code_part
        elif payload.startswith("ref") and payload[3:].isdigit():
            candidate = int(payload[3:])
            if candidate != user.id:
                ref_by = candidate
        elif payload.isdigit():
            pending_code = payload

    is_new = register_user_if_new(user.id, user.username, user.first_name, ref_by)
    if is_new:
        await notify_admins_new_user(context, user)

    not_subs = await check_subscription(context, user.id)
    if not_subs:
        context.user_data["pending_code"] = pending_code
        await update.message.reply_text(
            "⚠️ Botdan foydalanish uchun quyidagi kanalllarga azo bo'lib \"✅ Tekshirdim\" tugmasini bosing:",
            reply_markup=kb_subscription(not_subs),
        )
        return

    await grant_referral_bonus_if_needed(context, get_user(user.id))
    await update_streak_and_reward(context, user.id)
    await send_start_message(update, context, user)

    if pending_code:
        await process_movie_code(update.message, context, pending_code, user.id)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "☎️ Yordam:\n\n"
        "Kino kodini yuborsangiz, tegishli film sizga yuboriladi.\n"
        "Muammo yuzaga kelsa, adminlarga murojaat qiling."
    )


# ============================== KINO QIDIRUV ==============================

def kb_movie_card(code: str, user_id: int = None) -> InlineKeyboardMarkup:
    movie_channel = get_setting("movie_channel_username")
    rows = []
    if movie_channel:
        rows.append([InlineKeyboardButton("🎬 Ko'proq kinolar", url=f"https://t.me/{movie_channel}")])

    in_wl = user_id is not None and is_in_watch_later(user_id, code)
    wl_label = "✅ Ro'yxatda" if in_wl else "🔖 Keyin ko'raman"
    in_fav = user_id is not None and is_favorite(user_id, code)
    fav_label = "💔 Sevimlidan olish" if in_fav else "❤️ Sevimli"

    rows.append([
        InlineKeyboardButton(fav_label, callback_data=f"fav_{code}"),
        InlineKeyboardButton(wl_label, callback_data=f"wl_{code}"),
    ])
    rows.append([
        InlineKeyboardButton("🌟 Baholash", callback_data=f"rate_{code}"),
        InlineKeyboardButton("📊 Statistika", callback_data=f"mstat_{code}"),
    ])
    rows.append([InlineKeyboardButton("📤 Challenge yuborish", callback_data=f"chal_{code}")])
    return InlineKeyboardMarkup(rows)


def kb_rating(code: str) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton("⭐" * n, callback_data=f"ratepick_{code}_{n}") for n in range(1, 6)]
    return InlineKeyboardMarkup([row])


def kb_episodes(code: str, episodes: list, page: int = 0, per_page: int = 10) -> InlineKeyboardMarkup:
    start = page * per_page
    chunk = episodes[start:start + per_page]
    rows, row = [], []
    for m in chunk:
        row.append(InlineKeyboardButton(f"{m['episode']}-qism", callback_data=f"ep_{code}_{m['episode']}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"eppage_{code}_{page-1}"))
    if start + per_page < len(episodes):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"eppage_{code}_{page+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)



# ============================== SMART SEARCH ==============================

_CASUAL_PHRASES = {
    "salom", "assalomu alaykum", "assalomu alekum", "alaykum assalom", "salomlar",
    "hi", "hello", "hey", "qalaysan", "qalesan", "qandaysan", "qanaqasan",
    "rahmat", "raxmat", "tashakkur", "xayr", "xayrli kun", "xayrli tun",
    "ha", "yoq", "yo'q", "ok", "okay", "mayli", "bo'ldi", "boldi", "yaxshi",
    "zor", "zo'r", "super", "admin", "bot", "botmisan", "kimsan", "kim san",
    "test", "salom bot", "привет", "спасибо",
}


def looks_like_casual_text(text: str) -> bool:
    """Salomlashish, minnatdorchilik, emoji va hokazo -- kino qidiruvi EMAS deb topadi.
    Raqamli matn (kino kodi bo'lishi mumkin, 1-2 xonali bo'lsa ham) HECH QACHON bu yerda
    "tushunarsiz" deb topilmaydi."""
    t = text.strip().lower()
    if not t:
        return True
    if t.isdigit():
        return False
    if not re.search(r"[a-zA-Zа-яА-ЯёЁ0-9\u0400-\u04FF]", t):
        return True  # faqat emoji/tinish belgilari
    if t in _CASUAL_PHRASES:
        return True
    if len(t) <= 2:
        return True
    return False


def smart_search_title(query: str):
    """Kino nomi bo'yicha qidiradi (xato yozilgan nomlarni ham imkon qadar topadi).
    Topilsa (kod, nom) qaytaradi, aks holda (None, None)."""
    query_l = query.strip().lower()
    rows = get_all_movie_titles()
    if not rows:
        return None, None

    title_to_code = {r["title"]: r["code"] for r in rows}
    titles = list(title_to_code.keys())

    contains = [t for t in titles if query_l in t.lower()]
    if contains:
        contains.sort(key=len)
        best = contains[0]
        return title_to_code[best], best

    lower_titles = [t.lower() for t in titles]
    close = difflib.get_close_matches(query_l, lower_titles, n=1, cutoff=0.6)
    if close:
        for t in titles:
            if t.lower() == close[0]:
                return title_to_code[t], t

    # 3) Ko'p so'zli nomlarda alohida so'zlar bo'yicha ham qidiramiz
    #    (masalan "avenjers" -> "Avengers: Endgame" ichidagi "Avengers" so'ziga mos keladi)
    best_match, best_ratio = None, 0.0
    for t in titles:
        for word in t.lower().replace(":", " ").replace(",", " ").split():
            ratio = difflib.SequenceMatcher(None, query_l, word).ratio()
            if ratio > best_ratio:
                best_ratio, best_match = ratio, t
    if best_match and best_ratio >= 0.72:
        return title_to_code[best_match], best_match

    return None, None


async def process_movie_code(message, context: ContextTypes.DEFAULT_TYPE, code: str, user_id: int):
    episodes = get_movies_by_code(code)
    if not episodes:
        await message.reply_text("❌ Bunday kodli kino topilmadi. Kodni tekshirib qayta yuboring.")
        await notify_admins_movie_not_found(context, user_id, code)
        return

    if len(episodes) > 1:
        title_display = episodes[0]["title"] or f"Kod {code}"
        await message.reply_text(
            f"🎬 \"{title_display}\" — {len(episodes)} qism topildi.\nKerakli qismni tanlang:",
            reply_markup=kb_episodes(code, list(episodes)),
        )
        return

    await send_movie_episode(message, context, episodes[0], user_id)


async def send_movie_episode(message, context: ContextTypes.DEFAULT_TYPE, movie, user_id: int):
    record_download(movie["id"], user_id)
    increment_downloads(movie["id"])
    xp_amount = int(get_setting("xp_per_view") or 5)
    await add_xp(context, user_id, xp_amount)
    await claim_lucky_code_if_needed(context, user_id, movie["code"])

    mode = movie["mode"] if movie["mode"] else "full"
    if mode == "simple":
        # Qisqa video rejimida: faqat bot va yuklab olishlar soni
        caption = (
            f"👉 @{BOT_USERNAME}\n"
            f"⬇️ Yuklanishlar soni: {movie['downloads'] + 1}"
        )
    else:
        movie_channel = get_setting("movie_channel_username")
        channel_line = f"📡 Kanal: @{movie_channel}\n" if movie_channel else ""
        caption = (
            f"🎬 {movie['title']}\n"
            f"🎞 {movie['genre']}\n"
            f"🌍 {movie['country']} | 🗣 {movie['language']}\n"
            f"🎬 Kino kodi: {movie['code']}\n"
            + channel_line +
            f"⬇️ Yuklanishlar soni: {movie['downloads'] + 1}"
        )

    kb = kb_movie_card(movie["code"], user_id)
    protect = not is_admin(user_id)  # adminlar forward qila oladi, oddiy foydalanuvchilar yo'q
    if movie["file_type"] == "video":
        await context.bot.send_video(message.chat_id, movie["file_id"], caption=caption,
                                      reply_markup=kb, protect_content=protect)
    else:
        await context.bot.send_document(message.chat_id, movie["file_id"], caption=caption,
                                         reply_markup=kb, protect_content=protect)


# ============================== AVTOMATIK KANAL/GURUH ANIQLASH ==============================

async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot biror kanal yoki guruhga ADMIN qilib qo'shilganda avtomatik ishga tushadi.
    Bu — 'forward yuborish' usuli o'rniga ishlatiladigan, kanal HAM guruh uchun
    bab-baravar ishlaydigan yagona va ishonchli usul."""
    result = update.my_chat_member
    chat = result.chat
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status

    if new_status != ChatMemberStatus.ADMINISTRATOR or old_status == ChatMemberStatus.ADMINISTRATOR:
        return  # faqat "hozirgina admin qilingan" holatini ushlaymiz

    kind = "channel" if chat.type == ChatType.CHANNEL else "group"
    upsert_pending_chat(chat.id, chat.title or chat.username or str(chat.id), chat.username, kind)

    label = "Kanal" if kind == "channel" else "Guruh"
    uname_line = f"🔗 Username: @{chat.username}\n" if chat.username else "🔗 Username: yo'q (maxfiy)\n"
    text = (
        f"✅ Men yangi {label.lower()}da ADMIN qilib tayinlandim!\n\n"
        f"📌 Nomi: {chat.title or '-'}\n"
        + uname_line +
        f"🆔 Chat ID: `{chat.id}`\n\n"
        "Bu chatni qanday ishlatamiz?\n\n"
        "⚠️ Eslatma: agar bu chatda username bo'lmasa (maxfiy), foydalanuvchilar "
        "qo'shilish so'rovi yuboradi. Men bu so'rovlarni AVTOMATIK TASDIQLAMAYMAN — "
        "faqat so'rov yuborilganini tekshiraman. Haqiqiy a'zolikni siz (yoki shu "
        "chatning boshqa adminlari) o'zingiz tasdiqlaysiz."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Majburiy obunaga qo'shish", callback_data=f"padd_add_{chat.id}")],
        [InlineKeyboardButton("🎬 Kino kanali qilish", callback_data=f"padd_movie_{chat.id}")],
        [InlineKeyboardButton("❌ Bekor qilish", callback_data=f"padd_cancel_{chat.id}")],
    ])
    for admin_id in get_admin_ids():
        try:
            await context.bot.send_message(admin_id, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except TelegramError:
            pass


async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maxfiy kanal/guruhga kelgan so'rovni AVTOMATIK TASDIQLAMAYDI —
    faqat foydalanuvchi so'rov yuborganini bazaga qayd qiladi. Haqiqiy
    tasdiqlash o'sha kanal/guruhning o'z egasi/adminlari tomonidan amalga
    oshiriladi."""
    req = update.chat_join_request
    record_join_request(req.chat.id, req.from_user.id)
    try:
        await context.bot.send_message(
            req.from_user.id,
            "✅ So'rovingiz qabul qilindi!\n\n🔎 Kino qidirish uchun qayta /start bosing."
        )
    except TelegramError:
        pass


# ============================== CALLBACK QUERY ROUTER ==============================

async def safe_answer(query, *args, **kwargs):
    """CallbackQuery.answer() Telegram tomonidan FAQAT BIR MARTA chaqirilishi mumkin —
    bir xil tugma bosilishiga ikkinchi marta javob berishga urinish xatolik chiqaradi
    va shu sababli tugma "ishlamay qolgandek" ko'rinadi. Bu funksiya shu xatoni xavfsiz yutadi."""
    try:
        await query.answer(*args, **kwargs)
    except TelegramError:
        pass


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    await safe_answer(query)

    # ---------- Umumiy ----------
    if data == "back_start":
        context.user_data.pop("state", None)
        await query.message.edit_text(
            f"🖐 Assalomu alaykum, {user.first_name}!\n\n"
            "📊 Bot buyruqlari:\n"
            "/start - ♻️ Botni qayta ishga tushirish\n"
            "/help - ☎️ Qo'llab-quvvatlash\n\n"
            "🔎 Film kodini yuboring:",
            reply_markup=kb_main_menu(user.id),
        )
        return

    if data == "no_movie_channel":
        await safe_answer(query, "Hali kino kanali ulanmagan.", show_alert=True)
        return

    if data == "random_movie":
        movie = get_random_movie()
        if not movie:
            await safe_answer(query, "😔 Hozircha botda kino yo'q.", show_alert=True)
            return
        not_subs = await check_subscription(context, user.id)
        if not_subs:
            await safe_answer(query, "⚠️ Avval majburiy kanal/guruhlarga obuna bo'ling.", show_alert=True)
            return
        await grant_referral_bonus_if_needed(context, get_user(user.id))
        await send_movie_episode(query.message, context, movie, user.id)
        return

    if data == "mystery_box":
        if not can_open_mystery_box(user.id):
            await safe_answer(query, "📦 Siz bugun sirli qutini allaqachon ochingiz. Ertaga qayta urinib ko'ring!",
                                show_alert=True)
            return
        reward = open_mystery_box(user.id)
        await query.message.reply_text(
            f"🎁 Sirli quti ochildi!\n💰 Siz {reward:.0f} so'm yutib oldingiz! Ertaga yana urinib ko'ring."
        )
        return

    if data == "profile":
        u = get_user(user.id)
        views_c = with_conn_count("SELECT COUNT(*) c FROM downloads_log WHERE user_id=?", (user.id,))
        rank, score = liga_user_rank(user.id)
        rank_text = f"#{rank} (bu hafta {score} ball)" if rank else "hali reytingda yo'q"
        vip_line = "👑 VIP: Ha" if is_vip(u) else "👑 VIP: Yo'q"
        streak_line = f"🔥 Streak: {u['streak_count'] or 0} kun"
        await query.message.edit_text(
            "👤 Profil\n\n"
            f"🆔 ID: `{user.id}`\n"
            f"🏅 Daraja: {u['level']}\n"
            f"✨ XP: {xp_progress_text(u['xp'], u['level'])}\n"
            f"{vip_line}\n"
            f"{streak_line}\n"
            f"🏆 Liga o'rni: {rank_text}\n"
            f"👁 Ko'rilgan kinolar: {views_c} ta\n"
            f"❤️ Sevimlilar: {favorites_count_for_user(user.id)} ta\n"
            f"👥 Referallar: {ref_count(user.id)} ta\n"
            f"💳 Balans: {u['balance']:.0f} so'm",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏆 Liga (TOP-10)", callback_data="liga")],
                [InlineKeyboardButton(f"🔖 Keyin ko'raman ({watch_later_count(user.id)})",
                                       callback_data="watch_later_list"),
                 InlineKeyboardButton(f"❤️ Sevimlilar ({favorites_count_for_user(user.id)})",
                                       callback_data="favorites_list")],
                [InlineKeyboardButton("🕘 Tarix", callback_data="history_list")],
                [InlineKeyboardButton("⬅️ Orqaga", callback_data="back_start")],
            ]),
        )
        return

    if data == "favorites_list":
        items = get_user_favorites(user.id)
        if not items:
            await safe_answer(query, "💔 Sevimlilar ro'yxati bo'sh.", show_alert=True)
            return
        rows = [
            [InlineKeyboardButton(f"🎬 {r['title'] or r['code']}", callback_data=f"wlpick_{r['code']}")]
            for r in items
        ]
        rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="profile")])
        await query.message.edit_text("❤️ Sevimlilar ro'yxati:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "history_list":
        items = get_user_history(user.id, 15)
        if not items:
            await safe_answer(query, "🕘 Tarixingiz hozircha bo'sh.", show_alert=True)
            return
        lines = ["🕘 Ko'rilgan kinolar tarixi (oxirgi 15 ta):\n"]
        for r in items:
            when = (r["downloaded_at"] or "")[:16].replace("T", " ")
            lines.append(f"🎬 {r['title'] or r['code']} — {when}")
        await query.message.edit_text("\n".join(lines), reply_markup=kb_back("profile"))
        return

    if data == "missions":
        await query.message.edit_text(
            "🎯 Kunlik missiyalar funksiyasi hozircha o'chirilgan.", reply_markup=kb_back("profile")
        )
        return

    if data == "liga":
        board = liga_leaderboard(10)
        if not board:
            text = "🏆 Liga\n\nBu hafta hali hech kim ball to'plamagan."
        else:
            lines = ["🏆 Liga — bu haftaning TOP-10 si:\n"]
            medals = ["🥇", "🥈", "🥉"]
            for i, row in enumerate(board):
                medal = medals[i] if i < 3 else f"{i+1}."
                lines.append(f"{medal} {row['first_name'] or row['user_id']} — {row['score']} ball")
            rank, score = liga_user_rank(user.id)
            if rank and rank > 10:
                lines.append(f"\n... sizning o'rningiz: #{rank} ({score} ball)")
            text = "\n".join(lines)
        await query.message.edit_text(text, reply_markup=kb_back("profile"))
        return

    if data == "check_sub":
        not_subs = await check_subscription(context, user.id)
        if not_subs:
            await safe_answer(query, "❌ Siz hali barcha kanal/guruhlarga obuna bo'lmagansiz yoki "
                                "maxfiy chatlarga so'rov yubormagansiz!", show_alert=True)
            return
        await grant_referral_bonus_if_needed(context, get_user(user.id))
        await query.message.delete()
        pending_code = context.user_data.pop("pending_code", None)
        pending_search = context.user_data.pop("pending_search", None)
        await send_start_message(query, context, user)
        if pending_code:
            await process_movie_code(query.message, context, pending_code, user.id)
        elif pending_search:
            code, matched_title = smart_search_title(pending_search)
            if code:
                await query.message.reply_text(f"🔎 Topildi: \"{matched_title}\"")
                await process_movie_code(query.message, context, code, user.id)
            else:
                context.user_data["last_search_query"] = pending_search
                await notify_admins_movie_not_found(context, user.id, pending_search)
                await query.message.reply_text(
                    "❌ Bunday nomli kino topilmadi.\n\n🔎 Boshqa nom bilan urinib ko'ring.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🔔 Qo'shilganda xabar berish", callback_data="radar_add")]]
                    ),
                )
        return

    # ---------- Pul ishlash ----------
    if data == "earn":
        u = get_user(user.id)
        rc = ref_count(user.id)
        await query.message.edit_text(
            "💰 Pul ishlash bo'limi\n\n"
            f"🆔 ID: `{user.id}`\n"
            f"💳 Sizning hisobingiz: {u['balance']:.0f} so'm\n"
            f"👥 Siz taklif qilgan foydalanuvchilar: {rc} ta",
            reply_markup=kb_earn_menu(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if data == "earn_ref":
        rc = ref_count(user.id)
        link = f"https://t.me/{BOT_USERNAME}?start=ref{user.id}"
        await query.message.edit_text(
            "👥 Referal tizimi\n\n"
            f"🔗 Sizning referal havolangiz:\n`{link}`\n\n"
            f"✅ Siz taklif qilganlar: {rc} ta\n\n"
            "💡 Havolani nusxalash uchun uni bosib turing (long-press).",
            reply_markup=kb_referal_menu(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if data == "earn_withdraw":
        u = get_user(user.id)
        premium_price = float(get_setting("premium_price"))
        stars_price = float(get_setting("stars_price"))
        await query.message.edit_text(
            "💵 Pul yechish\n\n"
            f"💳 Sizning hisobingiz: {u['balance']:.0f} so'm\n\n"
            "📌 Narxlar:\n"
            f"⭐ Telegram premium: {premium_price:.0f} so'm\n"
            f"✨ Telegram stars: {stars_price:.0f} so'm",
            reply_markup=kb_withdraw_menu(),
        )
        return

    if data in ("withdraw_premium", "withdraw_stars"):
        ptype = "premium" if data == "withdraw_premium" else "stars"
        price = float(get_setting("premium_price" if ptype == "premium" else "stars_price"))
        u = get_user(user.id)
        label = "Telegram premium" if ptype == "premium" else "Telegram stars"

        if u["balance"] < price:
            await safe_answer(query, "❌ Hisobingizda mablag' yetarli emas", show_alert=True)
            return

        change_balance(user.id, -price)
        req_id = add_payment_request(user.id, ptype, price)
        await query.message.edit_text(f"✅ Tabriklaymiz! Sizning {label} olish bo'yicha arizangiz adminga yuborildi.")

        uname = f"@{user.username}" if user.username else "yo'q"
        admin_text = (
            f"💳 Foydalanuvchi {label} sotib olmoqchi va uning hisobidan {price:.0f} so'm "
            f"yechib olindi.\n\n"
            f"👤 Foydalanuvchi: {user.first_name} ({uname})\n"
            f"🆔 ID: `{user.id}`\n\n"
            f"So'rovni tasdiqlaysizmi?"
        )
        admin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👁 Ko'rish", url=f"tg://user?id={user.id}")],
            [InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"payconf_{req_id}")],
        ])
        for admin_id in get_admin_ids():
            try:
                await context.bot.send_message(admin_id, admin_text, reply_markup=admin_kb, parse_mode=ParseMode.MARKDOWN)
            except TelegramError:
                pass
        return

    if data.startswith("payconf_"):
        if not is_admin(user.id):
            return
        req_id = int(data.split("_", 1)[1])
        req = get_payment_request(req_id)
        if not req or req["status"] != "pending":
            await safe_answer(query, "Bu so'rov allaqachon ko'rib chiqilgan.", show_alert=True)
            return
        set_payment_status(req_id, "confirmed")
        label = "Telegram premium" if req["ptype"] == "premium" else "Telegram stars"
        target_user = get_user(req["user_id"])
        fname = target_user["first_name"] if target_user else str(req["user_id"])
        try:
            await context.bot.send_message(
                req["user_id"],
                f"🎉 Tabriklaymiz, {fname}! Sizning {label} olish haqidagi so'rovingiz "
                f"tasdiqlandi, admin javobini kuting!\n\n🔎 Kino qidirish uchun qayta /start bosing.",
            )
        except TelegramError:
            pass
        await query.edit_message_text(
            query.message.text + "\n\n✅ Foydalanuvchiga to'lov tasdiqlanganligi haqida xabar yuborildi."
        )
        return

    # ---------- Kino qismlari ----------
    if data.startswith("ep_"):
        _, code, ep = data.split("_", 2)
        movie = get_movie_episode(code, int(ep))
        if movie:
            await send_movie_episode(query.message, context, movie, user.id)
        else:
            await safe_answer(query, "Topilmadi", show_alert=True)
        return

    if data.startswith("eppage_"):
        _, code, page = data.split("_", 2)
        episodes = list(get_movies_by_code(code))
        await query.message.edit_reply_markup(reply_markup=kb_episodes(code, episodes, int(page)))
        return

    # ---------- Keyin ko'raman ----------
    if data.startswith("wl_"):
        code = data.split("_", 1)[1]
        if is_in_watch_later(user.id, code):
            remove_watch_later(user.id, code)
            await safe_answer(query, "❌ Ro'yxatdan olib tashlandi.")
        else:
            add_watch_later(user.id, code)
            await safe_answer(query, "🔖 'Keyin ko'raman' ro'yxatiga qo'shildi!")
        try:
            await query.message.edit_reply_markup(reply_markup=kb_movie_card(code, user.id))
        except TelegramError:
            pass
        return

    if data == "watch_later_list":
        items = get_watch_later(user.id)
        if not items:
            await safe_answer(query, "📭 Ro'yxatingiz hozircha bo'sh.", show_alert=True)
            return
        rows = [
            [InlineKeyboardButton(f"🎬 {r['title'] or r['code']}", callback_data=f"wlpick_{r['code']}")]
            for r in items
        ]
        rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="profile")])
        await query.message.edit_text("🔖 Keyin ko'raman ro'yxati:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("wlpick_"):
        code = data.split("_", 1)[1]
        await process_movie_code(query.message, context, code, user.id)
        return

    # ---------- Radar: kutilayotgan kino ----------
    if data == "radar_add":
        query_text = context.user_data.pop("last_search_query", None)
        if not query_text:
            await safe_answer(query, "⚠️ Amal muddati tugagan, qaytadan qidiring.", show_alert=True)
            return
        add_radar_request(user.id, query_text)
        await query.message.edit_text(
            f"🔔 Ajoyib! \"{query_text}\" nomli kino botga qo'shilganda sizga avtomatik xabar beramiz."
        )
        return

    # ---------- Sevimlilar ----------
    if data.startswith("fav_"):
        code = data.split("_", 1)[1]
        added = toggle_favorite(user.id, code)
        await safe_answer(query, "❤️ Sevimlilarga qo'shildi!" if added else "💔 Sevimlilardan olib tashlandi.")
        try:
            await query.message.edit_reply_markup(reply_markup=kb_movie_card(code, user.id))
        except TelegramError:
            pass
        return

    # ---------- Baholash ----------
    if data.startswith("ratepick_"):
        _, code, stars = data.split("_", 2)
        stars = int(stars)
        rate_movie(user.id, code, stars)
        movie_title = None
        eps = get_movies_by_code(code)
        if eps:
            movie_title = eps[0]["title"]
        title_line = f" \"{movie_title}\"" if movie_title else ""
        await safe_answer(query, f"✅ Siz {stars} ⭐ baho berdingiz. Rahmat!")
        try:
            await query.message.edit_text(f"⭐ Siz{title_line} kinosiga {stars}/5 baho berdingiz. Rahmat!")
        except TelegramError:
            pass
        return

    if data.startswith("rate_"):
        code = data.split("_", 1)[1]
        await query.message.reply_text("🌟 Kinoga baho bering:", reply_markup=kb_rating(code))
        return

    # ---------- Kino statistikasi ----------
    if data.startswith("mstat_"):
        code = data.split("_", 1)[1]
        await safe_answer(query)
        await query.message.reply_text(f"📊 Statistika:\n\n{movie_stats_text(code)}")
        return

    # ---------- Kino Challenge (ulashish) ----------
    if data.startswith("chal_"):
        code = data.split("_", 1)[1]
        increment_shares(code)
        share_text = urllib.parse.quote("Men bu kinoni ko'rdim. Sen ham ko'rib baho ber! 🎬")
        share_url = (
            f"https://t.me/share/url?url=https://t.me/{BOT_USERNAME}?start=ref{user.id}_{code}"
            f"&text={share_text}"
        )
        await safe_answer(query)
        await query.message.reply_text(
            "📤 Do'stlaringizga ulashing:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↗️ Ulashish", url=share_url)]]),
        )
        return

    # ---------- Quyidagilar faqat adminlar uchun ----------
    if data.startswith(("admin_panel", "padd_", "uptype_", "upmode_", "chdel_", "admdel_",
                         "balchg_", "codeconf_", "movdel_", "movdelall_", "series_more_",
                         "medit_", "mfield_", "fileoverwrite_", "skiptitle")):
        if not is_admin(user.id):
            await safe_answer(query, "⛔️ Sizda ruxsat yo'q.", show_alert=True)
            return
        await admin_callback_router(query, context, data, user)
        return


# ============================== ADMIN CALLBACK ROUTER ==============================

async def admin_callback_router(query, context: ContextTypes.DEFAULT_TYPE, data: str, user):

    # ---- Boshqaruv paneliga kirish (Reply Keyboard yuboriladi) ----
    if data == "admin_panel":
        context.user_data["admin_level"] = "main"
        await context.bot.send_message(user.id, "🛠 Boshqaruv paneliga xush kelibsiz!", reply_markup=RK_MAIN)
        return

    # ---- Aniqlangan kanal/guruhni tasdiqlash ----
    if data.startswith("padd_"):
        _, action, chat_id_s = data.split("_", 2)
        chat_id = int(chat_id_s)
        pending = get_pending_chat(chat_id)
        if not pending:
            await safe_answer(query, "⚠️ Bu so'rov eskirgan yoki allaqachon ishlov berilgan.", show_alert=True)
            return

        if action == "cancel":
            delete_pending_chat(chat_id)
            await query.message.edit_text("❌ Bekor qilindi.")
            return

        if action == "movie":
            set_setting("movie_channel_username", pending["username"] or "")
            set_setting("movie_channel_chatid", str(chat_id))
            delete_pending_chat(chat_id)
            await query.message.edit_text(f"✅ Kino kanali sifatida ulandi: {pending['title']}")
            return

        if action == "add":
            if len(get_mandatory_channels()) >= MAX_MANDATORY_CHANNELS:
                delete_pending_chat(chat_id)
                await query.message.edit_text(
                    f"⚠️ Siz allaqachon {MAX_MANDATORY_CHANNELS} ta kanal/guruh ulagansiz "
                    f"(maksimal chegara). Yangisini qo'shish uchun avval birortasini "
                    f"o'chiring (🔒 Majburiy obunalar bo'limidan)."
                )
                return
            if pending["username"]:
                # Ommaviy — oddiy username havolasi yetarli
                add_channel(chat_id, pending["username"], None, pending["title"], "public", pending["kind"])
                delete_pending_chat(chat_id)
                await query.message.edit_text(
                    f"✅ Ommaviy majburiy obunaga qo'shildi: {pending['title']}\n"
                    f"(Foydalanuvchilar to'g'ridan-to'g'ri qo'shiladi)"
                )
            else:
                # Maxfiy — join-request havolasi yaratamiz; BOT TASDIQLAMAYDI,
                # faqat so'rov yuborilganini tekshiradi (check_subscription orqali)
                invite_link = None
                try:
                    link_obj = await context.bot.create_chat_invite_link(chat_id, creates_join_request=True)
                    invite_link = link_obj.invite_link
                except TelegramError as e:
                    await query.message.edit_text(
                        f"⚠️ Taklif havolasi yaratib bo'lmadi: {e}\n\n"
                        "Bot bu chatda 'Foydalanuvchilarni taklif qilish' huquqiga ega ekanligini tekshiring."
                    )
                    return
                add_channel(chat_id, None, invite_link, pending["title"], "private", pending["kind"])
                delete_pending_chat(chat_id)
                await query.message.edit_text(
                    f"✅ Maxfiy majburiy obunaga qo'shildi: {pending['title']}\n"
                    f"Foydalanuvchilar so'rov yuboradi — men buni QAYD qilaman "
                    f"(o'zim tasdiqlamayman, haqiqiy tasdiqlashni siz o'zingiz qilasiz)."
                )
            return

    # ---- Kino yuklash: rasm bilan (to'liq) yoki qisqa video bilan (tezkor) ----
    if data.startswith("upmode_"):
        mode = data.split("_", 1)[1]  # full / simple
        context.user_data["new_movie"] = {"mode": mode}
        context.user_data["state"] = "await_movie_code"
        await query.message.edit_text("🎬 Kino kodini kiriting (masalan: 1):")
        return

    # ---- Kino yuklash: kino yoki serial ----
    if data.startswith("uptype_"):
        if data == "uptype_movie":
            context.user_data["new_movie"]["episode"] = 1
            context.user_data["new_movie"]["is_series"] = 0
            context.user_data["state"] = "await_movie_file"
            await query.message.edit_text("🎥 Endi kino faylini (video yoki hujjat) yuboring:")
        else:
            context.user_data["new_movie"]["is_series"] = 1
            context.user_data["state"] = "await_movie_episode"
            await query.message.edit_text("🔢 Nechanchi qism ekanini kiriting (masalan: 1):")
        return

    # ---- Mavjud serial kodiga yana qism qoshishni tasdiqlash ----
    if data == "codeconf_yes":
        context.user_data["new_movie"]["is_series"] = 1
        mode = context.user_data["new_movie"].get("mode", "full")
        if mode == "full":
            context.user_data["state"] = "await_movie_title"
            await query.message.edit_text("📌 Kino nomini kiriting:")
        else:
            context.user_data["state"] = "await_movie_episode"
            await query.message.edit_text("🔢 Nechanchi qism ekanini kiriting:")
        return

    if data == "codeconf_no":
        context.user_data["state"] = "await_movie_code"
        await query.message.edit_text("🎬 Boshqa kino kodini kiriting:")
        return

    # ---- Serial joylangandan keyin: yana qism qo'shish yoki tugatish ----
    if data == "series_more_yes":
        nm = context.user_data.get("new_movie") or {}
        # Kod/sarlavha/janr/til/davlat/mode saqlanib qoladi — faqat yangi qism raqami va fayli so'raladi
        context.user_data["new_movie"] = {
            "code": nm.get("code"),
            "title": nm.get("title", ""),
            "genre": nm.get("genre", ""),
            "language": nm.get("language", ""),
            "country": nm.get("country", ""),
            "mode": nm.get("mode", "full"),
            "is_series": 1,
        }
        context.user_data["state"] = "await_movie_episode"
        await query.message.edit_text("🔢 Nechanchi qism ekanini kiriting:")
        return

    if data == "series_more_no":
        context.user_data.pop("new_movie", None)
        context.user_data.pop("state", None)
        await query.message.edit_text("🏁 Serial yuklash yakunlandi.")
        return

    # ---- Qisqa video: nom kiritilmadi ----
    if data == "skiptitle":
        context.user_data["new_movie"]["title"] = ""
        context.user_data.pop("state", None)
        await query.message.edit_text(
            "📺 Bu qanday kontent?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎬 Oddiy kino", callback_data="uptype_movie")],
                [InlineKeyboardButton("📺 Serial", callback_data="uptype_series")],
            ]),
        )
        return

    # ---- Bir xil kod+qism ustidan qayta yozishni tasdiqlash ----
    if data == "fileoverwrite_yes":
        pending = context.user_data.pop("pending_file", None)
        if not pending or "new_movie" not in context.user_data:
            await safe_answer(query, "⚠️ Sessiya eskirgan, qaytadan yuklang.", show_alert=True)
            return
        await query.message.edit_text("♻️ Ustidan yozilmoqda...")
        await save_movie_file_and_continue(query.message, context, pending["file_id"], pending["file_type"])
        return

    if data == "fileoverwrite_no":
        context.user_data.pop("pending_file", None)
        context.user_data["state"] = "await_movie_file"
        await query.message.edit_text("❌ Bekor qilindi. Boshqa faylni yuboring yoki /start bilan qayta boshlang.")
        return

    # ---- Kinoni o'chirish ----
    if data.startswith("movdelall_"):
        code = data.split("_", 1)[1]
        delete_movies_by_code(code)
        await query.message.edit_text(f"✅ '{code}' kodidagi barcha qismlar o'chirildi.")
        return

    if data.startswith("movdel_"):
        movie_id = int(data.split("_", 1)[1])
        delete_movie(movie_id)
        await query.message.edit_text("✅ Kino o'chirildi.")
        return

    # ---- Kinoni tahrirlash: qismni tanlash ----
    if data.startswith("medit_"):
        movie_id = int(data.split("_", 1)[1])
        movie = get_movie_by_id(movie_id)
        if not movie:
            await safe_answer(query, "❌ Topilmadi", show_alert=True)
            return
        await query.message.edit_text(movie_edit_text(movie), reply_markup=kb_edit_movie(movie))
        return

    # ---- Kinoni tahrirlash: maydonni tanlash ----
    if data.startswith("mfield_"):
        _, field, movie_id_s = data.split("_", 2)
        movie_id = int(movie_id_s)
        context.user_data["state"] = "await_edit_value"
        context.user_data["edit_target"] = {"id": movie_id, "field": field}
        prompt = "🔢 Yangi qism raqamini kiriting:" if field == "episode" else "✏️ Yangi qiymatni kiriting:"
        await query.message.edit_text(prompt)
        return

    # ---- Kanal/guruhni ro'yxatdan o'chirish ----
    if data.startswith("chdel_"):
        ch_id = int(data.split("_", 1)[1])
        delete_channel(ch_id)
        await safe_answer(query, "✅ O'chirildi")
        await query.message.edit_text("✅ Ro'yxatdan o'chirildi.")
        return

    # ---- Adminni o'chirish ----
    if data.startswith("admdel_"):
        target_id = int(data.split("_", 1)[1])
        if target_id == SUPER_ADMIN_ID:
            await safe_answer(query, "⛔️ Asosiy adminni o'chirib bo'lmaydi!", show_alert=True)
            return
        with closing(get_conn()) as conn, conn:
            conn.execute("DELETE FROM admins WHERE user_id=?", (target_id,))
        await safe_answer(query, "✅ Admin o'chirildi")
        await query.message.edit_text("✅ Admin ro'yxatdan o'chirildi.")
        return

    # ---- Balans o'zgartirish so'ralishi ----
    if data.startswith("balchg_"):
        _, sign, target_id = data.split("_", 2)
        context.user_data["state"] = f"await_balance_{sign}_{target_id}"
        word = "qo'shmoqchi" if sign == "add" else "ayirmoqchi"
        await query.message.edit_text(f"💰 Hisobga {word} bo'lgan summani kiriting:")
        return




# ============================== RO'YXATLARNI KO'RSATISH ==============================

async def show_mandatory_list_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channels = get_mandatory_channels()
    if not channels:
        await update.message.reply_text(
            "📋 Hozircha majburiy obuna kanal/guruhlari yo'q.\n\n"
            "➕ Qo'shish uchun: botni kerakli kanal yoki guruhga ADMIN qilib qo'shing — "
            "men avtomatik aniqlab, tasdiqlash uchun xabar yuboraman."
        )
        return
    lines = ["📋 Majburiy obuna ro'yxati:\n"]
    kb_rows = []
    for ch in channels:
        icon = "📢" if ch["kind"] == "channel" else "👥"
        type_label = "Ommaviy" if ch["ctype"] == "public" else "Maxfiy"
        lines.append(f"{icon} {ch['title']} — {type_label}")
        kb_rows.append([InlineKeyboardButton(f"❌ {ch['title']}", callback_data=f"chdel_{ch['id']}")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb_rows))


async def show_admin_list_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT a.user_id, u.first_name FROM admins a LEFT JOIN users u ON u.user_id=a.user_id"
        ).fetchall()
    lines = ["👮 Adminlar ro'yxati:\n"]
    kb_rows = []
    for r in rows:
        name = r["first_name"] or str(r["user_id"])
        lines.append(f"• {name} (ID: {r['user_id']})")
        if r["user_id"] != SUPER_ADMIN_ID:
            kb_rows.append([InlineKeyboardButton(f"❌ {name}", callback_data=f"admdel_{r['user_id']}")])
    kb = InlineKeyboardMarkup(kb_rows) if kb_rows else None
    await update.message.reply_text("\n".join(lines), reply_markup=kb)


async def show_payments_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = get_pending_payments()
    if not pending:
        await update.message.reply_text("💳 Hozircha kutilayotgan to'lovlar yo'q.")
        return
    lines = ["💳 Kutilayotgan to'lovlar:\n"]
    kb_rows = []
    for p in pending:
        u = get_user(p["user_id"])
        name = u["first_name"] if u else str(p["user_id"])
        lines.append(f"#{p['id']} — {name} — {p['ptype']} — {p['amount']:.0f} so'm")
        kb_rows.append([InlineKeyboardButton(f"✅ Tasdiqlash #{p['id']}", callback_data=f"payconf_{p['id']}")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb_rows))


# ============================== KINO TAHRIRLASH ==============================

def kb_edit_movie(movie) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📌 Nomi", callback_data=f"mfield_title_{movie['id']}")],
        [InlineKeyboardButton("🎞 Janr", callback_data=f"mfield_genre_{movie['id']}")],
        [InlineKeyboardButton("🗣 Til", callback_data=f"mfield_language_{movie['id']}")],
        [InlineKeyboardButton("🌍 Davlat", callback_data=f"mfield_country_{movie['id']}")],
    ]
    if movie["is_series"]:
        rows.append([InlineKeyboardButton("🔢 Qism raqami", callback_data=f"mfield_episode_{movie['id']}")])
    return InlineKeyboardMarkup(rows)


def movie_edit_text(movie) -> str:
    header = f"✏️ Tahrirlash — kod: {movie['code']}"
    if movie["is_series"]:
        header += f" ({movie['episode']}-qism)"
    return (
        f"{header}\n\n"
        f"📌 Nomi: {movie['title'] or '-'}\n"
        f"🎞 Janr: {movie['genre'] or '-'}\n"
        f"🗣 Til: {movie['language'] or '-'}\n"
        f"🌍 Davlat: {movie['country'] or '-'}\n\n"
        "Qaysi maydonni o'zgartirmoqchisiz?"
    )


# ============================== BAZA ZAXIRASI ==============================

async def send_db_backup(msg, context: ContextTypes.DEFAULT_TYPE):
    try:
        with closing(get_conn()) as conn:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
    except sqlite3.Error:
        pass
    now = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    try:
        with open(DB_PATH, "rb") as f:
            await msg.reply_document(
                document=f,
                filename=f"kinobot_backup_{now}.db",
                caption=f"💾 Baza zaxirasi — {now}",
            )
    except FileNotFoundError:
        await msg.reply_text("❌ Baza fayli topilmadi.")
    except TelegramError as e:
        await msg.reply_text(f"⚠️ Zaxirani yuborishda xatolik: {e}")


async def restore_db_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Faqat SUPER_ADMIN uchun: '/restore' izohi bilan yuborilgan .db faylni
    joriy bazaning o'rniga qo'yadi. Avval faylning sog'ligi (integrity) tekshiriladi,
    joriy baza esa almashtirishdan oldin ehtiyot nusxa sifatida saqlab qo'yiladi."""
    msg = update.message
    logger.info(
        "RESTORE: handler chaqirildi. from_user_id=%s SUPER_ADMIN_ID=%s has_doc=%s caption=%r",
        update.effective_user.id if update.effective_user else None,
        SUPER_ADMIN_ID,
        bool(msg.document),
        msg.caption,
    )
    if update.effective_user is None or update.effective_user.id != SUPER_ADMIN_ID:
        logger.info("RESTORE: rad etildi — admin ID mos kelmadi.")
        return
    doc = msg.document
    if doc is None:
        logger.info("RESTORE: hujjat topilmadi (doc is None).")
        await msg.reply_text("❌ Fayl topilmadi. Backup (.db) faylni '/restore' izohi bilan yuboring.")
        return

    logger.info("RESTORE: fayl yuklab olinmoqda... file_id=%s file_name=%s size=%s", doc.file_id, doc.file_name, doc.file_size)
    tmp_path = f"{DB_PATH}.restore_tmp"
    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(tmp_path)

        logger.info("RESTORE: fayl yuklandi, integrity_check tekshirilmoqda...")
        with closing(sqlite3.connect(tmp_path)) as conn:
            result = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        logger.info("RESTORE: integrity_check natijasi = %s", result)
        if result != "ok":
            os.remove(tmp_path)
            await msg.reply_text(
                f"❌ Yuklangan fayl SQLite bazasi sifatida buzilgan yoki noto'g'ri "
                f"(integrity_check: {result}). Baza ALMASHTIRILMADI, hech narsa o'zgarmadi."
            )
            return

        if os.path.exists(DB_PATH):
            shutil.copy(DB_PATH, f"{DB_PATH}.before_restore")

        os.replace(tmp_path, DB_PATH)

        # Eski WAL/SHM fayllarni tozalash — aks holda ular yangi bazaga
        # mos kelmay, keyingi ulanishlarda nomuvofiqlik keltirib chiqarishi mumkin.
        for suffix in ("-wal", "-shm"):
            stale = f"{DB_PATH}{suffix}"
            if os.path.exists(stale):
                os.remove(stale)

        logger.info("RESTORE: muvaffaqiyatli yakunlandi, DB_PATH=%s ustiga yozildi.", DB_PATH)
        await msg.reply_text(
            "✅ Baza muvaffaqiyatli tiklandi!\n"
            "Eski baza ehtiyot nusxa sifatida '.before_restore' nomi bilan saqlandi.\n"
            "Botni /start bilan tekshirib ko'ring."
        )
    except TelegramError as e:
        logger.exception("RESTORE: TelegramError yuz berdi")
        await msg.reply_text(f"⚠️ Faylni yuklab olishda xatolik: {e}")
    except Exception as e:
        logger.exception("RESTORE: kutilmagan xatolik yuz berdi")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        await msg.reply_text(f"⚠️ Tiklashda xatolik: {e}")


async def scheduled_backup_job(context: ContextTypes.DEFAULT_TYPE):
    """Har kuni avtomatik ishga tushib, bazani barcha adminlarga yuboradi."""
    try:
        with closing(get_conn()) as conn:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
    except sqlite3.Error:
        pass
    now = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    for admin_id in get_admin_ids():
        try:
            with open(DB_PATH, "rb") as f:
                await context.bot.send_document(
                    admin_id,
                    document=f,
                    filename=f"kinobot_backup_{now}.db",
                    caption=f"💾 Kunlik avtomatik baza zaxirasi — {now}",
                )
        except (TelegramError, FileNotFoundError):
            pass


# ============================== XABARNOMA (BROADCAST) ==============================

async def run_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Adminning matn/rasm/video/hujjat xabarini BARCHA foydalanuvchilarga yuboradi.
    copy_message ishlatiladi — shu sababli xabar turi muhim emas."""
    context.user_data.pop("state", None)
    src = update.message
    ids = all_user_ids()
    status_msg = await src.reply_text(
        f"📤 Yuborilmoqda... (0/{len(ids)})", reply_markup=current_admin_keyboard(context)
    )
    sent, failed = 0, 0
    for i, uid in enumerate(ids, 1):
        try:
            await context.bot.copy_message(chat_id=uid, from_chat_id=src.chat_id, message_id=src.message_id)
            sent += 1
        except (Forbidden, TelegramError):
            failed += 1
        if i % 25 == 0:
            try:
                await status_msg.edit_text(f"📤 Yuborilmoqda... ({i}/{len(ids)})")
            except TelegramError:
                pass
        await asyncio.sleep(0.05)  # Telegram flood-limitiga tushib qolmaslik uchun
    await src.reply_text(f"✅ Xabar {sent}/{len(ids)} foydalanuvchiga yuborildi ({failed} ta yetib bormadi).")


# ============================== ADMIN MENYU (Reply Keyboard) ROUTERI ==============================

async def admin_menu_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Reply-keyboard menyu bosilganda ishlaydi. True qaytarsa — xabar shu yerda ishlov olindi.

    MUHIM: bu funksiya har bir tugma matnini GLOBAL (admin_level'dan MUSTAQIL) tarzda
    taniydi. Ilgari faqat 'joriy daraja'ga mos matnlarni tekshirar edik — agar
    admin_level biror sababdan (masalan, bot qayta ishga tushsa va xotiradagi
    user_data tozalansa) yo'qolib qolsa, tugmalar umuman ishlamay qolar va matn
    kino-kodi qidiruviga tushib ketardi. Endi har bir tugma o'zi mustaqil ishlaydi —
    admin_level faqat QAYSI KLAVIATURA ko'rsatilishini aniqlash uchun ishlatiladi,
    tugmani TANISH uchun emas.
    """
    user = update.effective_user
    if not is_admin(user.id):
        return False
    text = update.message.text.strip()

    if text == "⬅️ Chiqish":
        context.user_data.pop("admin_level", None)
        await update.message.reply_text("🔚 Boshqaruv panelidan chiqdingiz.", reply_markup=ReplyKeyboardRemove())
        await send_start_message(update, context, user)
        return True

    if text == "⬅️ Orqaga":
        context.user_data["admin_level"] = "main"
        await update.message.reply_text("🛠 Boshqaruv paneli", reply_markup=RK_MAIN)
        return True

    if text == "📡 Kanallar":
        context.user_data["admin_level"] = "channels"
        await update.message.reply_text("📡 Kanallar / guruhlarni sozlash bo'limi", reply_markup=RK_CHANNELS)
        return True

    if text == "🎬 Kino yuklash":
        context.user_data["admin_level"] = "main"
        context.user_data["new_movie"] = {}
        await update.message.reply_text(
            "📤 Qanday usulda yuklaymiz?\n\n"
            "🖼 Rasm bilan — to'liq ma'lumot (nomi, janri, tili, davlati) so'raladi\n"
            "🎞 Qisqa video bilan — tezkor, qo'shimcha ma'lumot so'ralmaydi",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🖼 Rasm bilan (to'liq)", callback_data="upmode_full")],
                [InlineKeyboardButton("🎞 Qisqa video bilan (tezkor)", callback_data="upmode_simple")],
            ]),
        )
        return True

    if text == "✏️ Tahrirlash":
        context.user_data["admin_level"] = "main"
        context.user_data["state"] = "await_edit_code"
        await update.message.reply_text("✏️ Tahrirlamoqchi bo'lgan kino kodini kiriting:")
        return True

    if text == "🗑 Kino o'chirish":
        context.user_data["admin_level"] = "main"
        context.user_data["state"] = "await_delete_code"
        await update.message.reply_text("🗑 O'chirmoqchi bo'lgan kino kodini kiriting:")
        return True

    if text == "📢 Xabarnoma":
        context.user_data["admin_level"] = "main"
        context.user_data["state"] = "await_broadcast"
        await update.message.reply_text(
            "📢 Barcha foydalanuvchilarga yuboriladigan xabarni kiriting.\n"
            "Matn, rasm, video yoki hujjat — istalgan turda yuborishingiz mumkin:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return True

    if text == "📊 Statistika":
        context.user_data["admin_level"] = "stats"
        await update.message.reply_text("📊 Davrni tanlang:", reply_markup=RK_STATS)
        return True

    if text == "💾 Baza zaxirasi":
        context.user_data["admin_level"] = "main"
        await send_db_backup(update.message, context)
        return True

    if text == "📥 Kutilayotgan kinolar":
        context.user_data["admin_level"] = "main"
        rows = radar_stats()
        if not rows:
            await update.message.reply_text("📥 Hozircha hech kim kino kutmayapti.")
        else:
            lines = ["📥 Kutilayotgan kinolar (nechta foydalanuvchi so'ragan):\n"]
            for r in rows:
                lines.append(f"• \"{r['query']}\" — {r['c']} kishi")
            await update.message.reply_text("\n".join(lines))
        return True

    if text == "🎮 O'yin funksiyalari":
        context.user_data["admin_level"] = "game"
        current_secret = get_setting("secret_code_today") or "(o'rnatilmagan)"
        status_line = ""
        if get_setting("secret_code_today"):
            active = "🟢 faol" if is_secret_code_active() else "🔴 tugagan"
            expires = get_setting("secret_code_expires_at")
            max_u = get_setting("secret_code_max_uses") or "0"
            exp_text = expires[:16].replace("T", " ") if expires else "muddatsiz"
            limit_text = max_u if max_u != "0" else "cheksiz"
            status_line = f"\nHolati: {active} | Tugash: {exp_text} | Limit: {limit_text}"
        await update.message.reply_text(
            f"🎮 O'yin funksiyalari\n\n🔑 Joriy maxfiy kod: {current_secret}{status_line}",
            reply_markup=RK_GAME,
        )
        return True

    if text == "🔑 Bugungi maxfiy kod":
        context.user_data["admin_level"] = "game"
        context.user_data["state"] = "await_secret_code_input"
        await update.message.reply_text("🔑 Bugungi yangi maxfiy kodni kiriting (masalan: KINOBOT2026):")
        return True

    if text == "🍀 Omadli kod belgilash":
        context.user_data["admin_level"] = "game"
        context.user_data["state"] = "await_lucky_code_input"
        await update.message.reply_text("🍀 Omadli kod qilib belgilamoqchi bo'lgan kino kodini kiriting:")
        return True

    if text == "🍀 Omadli kodni bekor qilish":
        context.user_data["admin_level"] = "game"
        context.user_data["state"] = "await_lucky_code_remove"
        await update.message.reply_text("🍀 Omadli kod ro'yxatidan olib tashlanadigan kino kodini kiriting:")
        return True

    if text == "👑 VIP berish/olish":
        context.user_data["admin_level"] = "game"
        context.user_data["state"] = "await_vip_toggle_userid"
        await update.message.reply_text("👑 VIP holatini o'zgartirmoqchi bo'lgan foydalanuvchi ID sini kiriting:")
        return True

    if text == "⚙️ Bot holati":
        current = get_setting("bot_enabled")
        status_text = "🟢 Yoqilgan" if current == "1" else "🔴 O'chirilgan"
        context.user_data["admin_level"] = "status"
        await update.message.reply_text(f"⚙️ Bot holati: {status_text}", reply_markup=rk_status())
        return True

    if text == "👮 Adminlar":
        context.user_data["admin_level"] = "admins"
        await update.message.reply_text("👮 Adminlar bo'limi", reply_markup=RK_ADMINS)
        return True

    if text == "💳 To'lovlar":
        context.user_data["admin_level"] = "main"
        await show_payments_text(update, context)
        return True

    if text == "🔧 Sozlamalar":
        context.user_data["admin_level"] = "settings"
        await update.message.reply_text("🔧 Sozlamalar", reply_markup=RK_SETTINGS)
        return True

    if text == "🔎 Foydalanuvchini tekshirish":
        context.user_data["admin_level"] = "main"
        context.user_data["state"] = "await_check_user"
        await update.message.reply_text("🆔 Tekshirmoqchi bo'lgan foydalanuvchi ID sini kiriting:")
        return True

    if text == "🔒 Majburiy obunalar":
        context.user_data["admin_level"] = "channels"
        await show_mandatory_list_text(update, context)
        return True

    if text == "🎥 Kino kanal":
        context.user_data["admin_level"] = "channels"
        movie_ch = get_setting("movie_channel_username")
        cur = f"@{movie_ch}" if movie_ch else "ulanmagan ❌"
        await update.message.reply_text(
            f"🎥 Joriy kino kanali: {cur}\n\n"
            "Yangi kanal ulash uchun: botni kerakli kanalga ADMIN qilib qo'shing — "
            "men avtomatik aniqlab, tasdiqlash uchun xabar yuboraman "
            "('🎬 Kino kanali qilish' tugmasi orqali)."
        )
        return True

    if text == "➕ Admin qo'shish":
        context.user_data["admin_level"] = "admins"
        context.user_data["state"] = "await_admin_add"
        await update.message.reply_text("🆔 Yangi admin qilinadigan foydalanuvchi ID sini yuboring:")
        return True

    if text == "📋 Adminlar ro'yxati":
        context.user_data["admin_level"] = "admins"
        await show_admin_list_text(update, context)
        return True

    settings_map = {"👥 Referal narxi": "referral_bonus", "⭐ Premium narxi": "premium_price",
                     "✨ Stars narxi": "stars_price", "🎬 XP (kino ko'rish)": "xp_per_view",
                     "👥 XP (referal)": "xp_per_referral", "📶 XP (daraja bosqichi)": "xp_level_step"}
    if text in settings_map:
        context.user_data["admin_level"] = "settings"
        context.user_data["state"] = f"await_setting_{settings_map[text]}"
        await update.message.reply_text("✏️ Yangi qiymatni kiriting (raqam):")
        return True

    stats_map = {"📅 Kunlik": "day", "🗓 Haftalik": "week", "📆 Oylik": "month"}
    stats_titles = {"day": "Kunlik", "week": "Haftalik", "month": "Oylik"}
    if text in stats_map:
        context.user_data["admin_level"] = "stats"
        kind = stats_map[text]
        breakdown = stats_breakdown(kind)
        lines = [f"📊 {stats_titles[kind]} statistika:\n"]
        for label, users_c, downloads_c in breakdown:
            lines.append(f"📅 {label}:\n   👥 Yangi foydalanuvchi: {users_c} ta\n   ⬇️ Yuklab olishlar: {downloads_c} ta\n")
        await update.message.reply_text("\n".join(lines))
        return True

    if text == "📈 Umumiy":
        context.user_data["admin_level"] = "stats"
        gs = general_stats()
        lines = [
            "📈 Umumiy statistika:\n",
            f"👥 Jami foydalanuvchilar: {gs['total_users']} ta",
            f"🎬 Jami kinolar (kodlar): {gs['total_codes']} ta",
            f"🎞 Jami qismlar (fayllar): {gs['total_episodes']} ta",
            f"⬇️ Jami yuklab olishlar: {gs['total_downloads']} ta",
        ]
        if gs["top"]:
            lines.append("\n🏆 Top-5 eng ko'p yuklab olingan kino:")
            for i, row in enumerate(gs["top"], 1):
                title_display = row["title"] or f"Kod {row['code']}"
                lines.append(f"{i}. {title_display} (kod: {row['code']}) — {row['total_dl'] or 0} ta")
        await update.message.reply_text("\n".join(lines))
        return True

    if text in ("🔴 O'chirish", "🟢 Yoqish"):
        context.user_data["admin_level"] = "status"
        current = get_setting("bot_enabled")
        set_setting("bot_enabled", "0" if current == "1" else "1")
        new_current = get_setting("bot_enabled")
        status_text = "🟢 Yoqilgan" if new_current == "1" else "🔴 O'chirilgan"
        await update.message.reply_text(f"✅ Holat o'zgartirildi: {status_text}", reply_markup=rk_status())
        return True

    return False


# ============================== MATN HANDLER (STATE MACHINE) ==============================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()
    state = context.user_data.get("state")

    # ---------- Avval: admin pastki menyusi (faqat "state" band bo'lmasa) ----------
    if not state:
        handled = await admin_menu_text_handler(update, context)
        if handled:
            return
        if is_admin(user.id) and context.user_data.get("admin_level"):
            # Admin "Boshqaruv paneli" ichida — bu yerda KINO QIDIRUV ISHLAMAYDI.
            # Faqat menyu tugmalari yoki ⬅️ Chiqish orqali chiqish ishlaydi.
            await update.message.reply_text(
                "❓ Noma'lum buyruq. Pastdagi menyudan tanlang, yoki kino qidirish "
                "uchun avval ⬅️ Chiqish tugmasini bosing."
            )
            return

    # ---------- Sozlamalarni o'zgartirish ----------
    if state and state.startswith("await_setting_"):
        key = state.replace("await_setting_", "")
        if not text.replace(".", "", 1).isdigit():
            await update.message.reply_text("❌ Faqat raqam kiriting.")
            return
        set_setting(key, text)
        context.user_data.pop("state", None)
        await update.message.reply_text("✅ Sozlama yangilandi.", reply_markup=current_admin_keyboard(context))
        return

    # ---------- Admin qo'shish ----------
    if state == "await_admin_add":
        if not text.isdigit():
            await update.message.reply_text("❌ Faqat raqamli ID kiriting.")
            return
        new_admin_id = int(text)
        with closing(get_conn()) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO admins (user_id, added_by, added_at) VALUES (?, ?, ?)",
                (new_admin_id, user.id, datetime.datetime.now().isoformat()),
            )
        context.user_data.pop("state", None)
        await update.message.reply_text(f"✅ {new_admin_id} admin sifatida qo'shildi.",
                                         reply_markup=current_admin_keyboard(context))
        try:
            await context.bot.send_message(new_admin_id, "🎉 Siz botga admin etib tayinlandingiz!")
        except TelegramError:
            pass
        return

    # ---------- Xabarnoma ----------
    if state == "await_broadcast":
        await run_broadcast(update, context)
        return

    # ---------- Foydalanuvchini tekshirish ----------
    if state == "await_check_user":
        if not text.isdigit():
            await update.message.reply_text("❌ Faqat raqamli ID kiriting.")
            return
        target_id = int(text)
        target = get_user(target_id)
        if not target:
            await update.message.reply_text("❌ Bunday foydalanuvchi topilmadi.")
            return
        rc = ref_count(target_id)
        referrer = get_user(target["ref_by"]) if target["ref_by"] else None
        referrer_text = f"{referrer['first_name']} (ID: {referrer['user_id']})" if referrer else "Hech kim (to'g'ridan-to'g'ri)"
        context.user_data.pop("state", None)
        await update.message.reply_text(
            f"🔎 Foydalanuvchi ma'lumotlari:\n\n"
            f"👤 Ism: {target['first_name']}\n"
            f"🆔 ID: `{target['user_id']}`\n"
            f"💳 Balans: {target['balance']:.0f} so'm\n"
            f"👥 Taklif qilganlar: {rc} ta\n"
            f"🔗 Kim tomonidan taklif qilingan: {referrer_text}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Pul qo'shish", callback_data=f"balchg_add_{target_id}"),
                 InlineKeyboardButton("➖ Pul ayirish", callback_data=f"balchg_sub_{target_id}")],
            ]),
        )
        return

    # ---------- Balansni o'zgartirish ----------
    if state and state.startswith("await_balance_"):
        _, _, sign, target_id = state.split("_", 3)
        target_id = int(target_id)
        try:
            amount = float(text)
        except ValueError:
            await update.message.reply_text("❌ Faqat raqam kiriting.")
            return
        context.user_data.pop("state", None)
        if sign == "add":
            change_balance(target_id, amount)
            admin_msg = f"✅ Foydalanuvchi hisobiga {amount:.0f} so'm qo'shildi."
            user_msg = f"💰 Adminlar tomonidan hisobingizga {amount:.0f} so'm pul to'ldirildi."
        else:
            change_balance(target_id, -amount)
            admin_msg = f"✅ Foydalanuvchi hisobidan {amount:.0f} so'm ayirildi."
            user_msg = f"⚠️ Adminlar tomonidan hisobingizdan {amount:.0f} so'm pul ayirildi."
        await update.message.reply_text(admin_msg, reply_markup=current_admin_keyboard(context))
        try:
            await context.bot.send_message(target_id, user_msg)
        except TelegramError:
            pass
        return

    # ---------- Kino tahrirlash ----------
    if state == "await_edit_code":
        context.user_data.pop("state", None)
        episodes = get_movies_by_code(text)
        if not episodes:
            await update.message.reply_text("❌ Bunday kodli kino topilmadi.",
                                              reply_markup=current_admin_keyboard(context))
            return
        if len(episodes) == 1:
            movie = episodes[0]
            await update.message.reply_text(movie_edit_text(movie), reply_markup=kb_edit_movie(movie))
        else:
            rows = [
                [InlineKeyboardButton(f"✏️ {m['episode']}-qism", callback_data=f"medit_{m['id']}")]
                for m in episodes
            ]
            await update.message.reply_text(
                f"Bu kodga {len(episodes)} ta qism bor. Qaysi birini tahrirlaymiz?",
                reply_markup=InlineKeyboardMarkup(rows),
            )
        return

    if state == "await_edit_value":
        target = context.user_data.get("edit_target")
        if not target:
            context.user_data.pop("state", None)
            return
        field = target["field"]
        if field == "episode":
            if not text.isdigit():
                await update.message.reply_text("❌ Faqat raqam kiriting.")
                return
            value = int(text)
        else:
            value = text
        update_movie_field(target["id"], field, value)
        context.user_data.pop("state", None)
        context.user_data.pop("edit_target", None)
        movie = get_movie_by_id(target["id"])
        if not movie:
            await update.message.reply_text("✅ Yangilandi.", reply_markup=current_admin_keyboard(context))
            return
        await update.message.reply_text(
            "✅ Yangilandi!\n\n" + movie_edit_text(movie), reply_markup=kb_edit_movie(movie)
        )
        return

    # ---------- O'yin funksiyalari (maxfiy kod / omadli kod / VIP) ----------
    if state == "await_secret_code_input":
        context.user_data["secret_code_pending"] = text.strip()
        context.user_data["state"] = "await_secret_code_duration"
        await update.message.reply_text(
            "⏳ Necha DAQIQA davom etsin? (masalan: 60)\n\n"
            "Muddatsiz bo'lishi uchun 0 deb yozing."
        )
        return

    if state == "await_secret_code_duration":
        if not text.strip().isdigit():
            await update.message.reply_text("❌ Faqat raqam kiriting (daqiqa, yoki 0 = muddatsiz).")
            return
        minutes = int(text.strip())
        context.user_data["secret_code_duration_minutes"] = minutes
        context.user_data["state"] = "await_secret_code_maxuses"
        await update.message.reply_text(
            "👥 Nechta odam ishlata olsin? (masalan: 100)\n\n"
            "Cheksiz bo'lishi uchun 0 deb yozing."
        )
        return

    if state == "await_secret_code_maxuses":
        if not text.strip().isdigit():
            await update.message.reply_text("❌ Faqat raqam kiriting (yoki 0 = cheksiz).")
            return
        max_uses = int(text.strip())
        minutes = context.user_data.pop("secret_code_duration_minutes", 0)
        code = context.user_data.pop("secret_code_pending", "")
        context.user_data.pop("state", None)

        now = datetime.datetime.now()
        expires_at = (now + datetime.timedelta(minutes=minutes)).isoformat() if minutes else ""

        set_setting("secret_code_today", code)
        set_setting("secret_code_set_at", now.isoformat())
        set_setting("secret_code_expires_at", expires_at)
        set_setting("secret_code_max_uses", str(max_uses))

        duration_text = f"{minutes} daqiqa" if minutes else "muddatsiz"
        limit_text = f"{max_uses} kishi" if max_uses else "cheksiz"
        await update.message.reply_text(
            f"✅ Bugungi maxfiy kod o'rnatildi: {code}\n"
            f"⏳ Davomiyligi: {duration_text}\n"
            f"👥 Limit: {limit_text}",
            reply_markup=RK_GAME,
        )
        return

    if state == "await_lucky_code_input":
        episodes = get_movies_by_code(text.strip())
        if not episodes:
            await update.message.reply_text("❌ Bunday kodli kino topilmadi. Qaytadan urinib ko'ring yoki /orqaga.")
            return
        context.user_data["lucky_code_pending"] = text.strip()
        context.user_data["state"] = "await_lucky_code_reward"
        await update.message.reply_text("💰 Bu kodni topganlarga qancha bonus berilsin (so'm)?")
        return

    if state == "await_lucky_code_reward":
        if not text.replace(".", "", 1).isdigit():
            await update.message.reply_text("❌ Faqat raqam kiriting.")
            return
        context.user_data["lucky_code_reward_pending"] = float(text)
        context.user_data["state"] = "await_lucky_code_duration"
        await update.message.reply_text(
            "⏳ Necha DAQIQA davom etsin? (masalan: 120)\n\n"
            "Muddatsiz bo'lishi uchun 0 deb yozing."
        )
        return

    if state == "await_lucky_code_duration":
        if not text.strip().isdigit():
            await update.message.reply_text("❌ Faqat raqam kiriting (daqiqa, yoki 0 = muddatsiz).")
            return
        context.user_data["lucky_code_duration_minutes"] = int(text.strip())
        context.user_data["state"] = "await_lucky_code_maxuses"
        await update.message.reply_text(
            "👥 Nechta odam ishlata olsin? (masalan: 50)\n\n"
            "Cheksiz bo'lishi uchun 0 deb yozing."
        )
        return

    if state == "await_lucky_code_maxuses":
        if not text.strip().isdigit():
            await update.message.reply_text("❌ Faqat raqam kiriting (yoki 0 = cheksiz).")
            return
        max_uses = int(text.strip())
        minutes = context.user_data.pop("lucky_code_duration_minutes", 0)
        reward = context.user_data.pop("lucky_code_reward_pending", 0.0)
        code = context.user_data.pop("lucky_code_pending", None)
        context.user_data.pop("state", None)
        if code:
            expires_at = ""
            if minutes:
                expires_at = (datetime.datetime.now() + datetime.timedelta(minutes=minutes)).isoformat()
            set_lucky_code(code, reward, expires_at, max_uses)
            duration_text = f"{minutes} daqiqa" if minutes else "muddatsiz"
            limit_text = f"{max_uses} kishi" if max_uses else "cheksiz"
            await update.message.reply_text(
                f"🍀 \"{code}\" endi OMADLI KOD!\n"
                f"💰 Mukofot: {reward:.0f} so'm\n"
                f"⏳ Davomiyligi: {duration_text}\n"
                f"👥 Limit: {limit_text}",
                reply_markup=RK_GAME,
            )
        return

    if state == "await_lucky_code_remove":
        context.user_data.pop("state", None)
        remove_lucky_code(text.strip())
        await update.message.reply_text(f"✅ \"{text.strip()}\" omadli kodlar ro'yxatidan olib tashlandi.",
                                         reply_markup=RK_GAME)
        return

    if state == "await_vip_toggle_userid":
        context.user_data.pop("state", None)
        if not text.strip().isdigit():
            await update.message.reply_text("❌ Faqat foydalanuvchi ID (raqam) kiriting.", reply_markup=RK_GAME)
            return
        target_id = int(text.strip())
        target = get_user(target_id)
        if not target:
            await update.message.reply_text("❌ Bunday foydalanuvchi topilmadi.", reply_markup=RK_GAME)
            return
        new_state = not bool(target["vip_forced"])
        set_vip_forced(target_id, new_state)
        status = "✅ VIP berildi" if new_state else "❌ VIP olib tashlandi"
        await update.message.reply_text(f"{status}: foydalanuvchi {target_id}", reply_markup=RK_GAME)
        return

    # ---------- Kino o'chirish ----------
    if state == "await_delete_code":
        context.user_data.pop("state", None)
        episodes = get_movies_by_code(text)
        if not episodes:
            await update.message.reply_text("❌ Bunday kodli kino topilmadi.",
                                              reply_markup=current_admin_keyboard(context))
            return
        if len(episodes) == 1:
            m = episodes[0]
            title_display = m["title"] or f"Kod {text}"
            await update.message.reply_text(
                f"🎬 \"{title_display}\" (kod: {text}) — o'chirilsinmi?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗑 Ha, o'chirish", callback_data=f"movdel_{m['id']}")],
                ]),
            )
        else:
            rows = [
                [InlineKeyboardButton(f"🗑 {m['episode']}-qism", callback_data=f"movdel_{m['id']}")]
                for m in episodes
            ]
            rows.append([InlineKeyboardButton("🗑 Barcha qismlarni o'chirish", callback_data=f"movdelall_{text}")])
            await update.message.reply_text(
                f"Bu kodga {len(episodes)} ta qism bor. Qaysi birini o'chiramiz?",
                reply_markup=InlineKeyboardMarkup(rows),
            )
        return

    # ---------- Kino yuklash: matnli qadamlar ----------
    if state == "await_movie_code":
        existing = get_movies_by_code(text)
        if existing and not existing[0]["is_series"]:
            # Bu kod allaqachon ODDIY KINO uchun band — qayta ishlatib bo'lmaydi
            await update.message.reply_text(
                "❌ Bunday kodli kino allaqachon mavjud. Boshqa kod kiriting:"
            )
            return  # holat o'zgarmaydi, admin qaytadan kod kiritadi

        context.user_data["new_movie"]["code"] = text

        if existing and existing[0]["is_series"]:
            # Bu kodga serial qoyilgan — davom ettirishni tasdiqlash kerak
            context.user_data.pop("state", None)
            await update.message.reply_text(
                f"⚠️ Bu kodga serial qo'yilgan (hozircha {len(existing)} qism bor).\n"
                f"Shu kodga yana qism qo'shmoqchimisiz?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Ha, qo'shish", callback_data="codeconf_yes")],
                    [InlineKeyboardButton("❌ Yo'q, boshqa kod", callback_data="codeconf_no")],
                ]),
            )
            return

        if context.user_data["new_movie"].get("mode") == "simple":
            # Qisqa video rejimi: nom ixtiyoriy (kiritish shart emas)
            context.user_data["state"] = "await_movie_title_optional"
            await update.message.reply_text(
                "📌 Kino nomini kiriting (bu nom faqat bazada saqlanadi, kanalga chiqmaydi va "
                "qidiruvda ishlatilmaydi). Agar nomi bo'lmasa, pastdagi tugmani bosing:",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⏭ Nomi yo'q, o'tkazib yuborish", callback_data="skiptitle")]]
                ),
            )
        else:
            context.user_data["state"] = "await_movie_title"
            await update.message.reply_text("📌 Kino nomini kiriting:")
        return

    if state == "await_movie_title_optional":
        context.user_data["new_movie"]["title"] = text.strip()
        context.user_data.pop("state", None)
        await update.message.reply_text(
            "📺 Bu qanday kontent?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎬 Oddiy kino", callback_data="uptype_movie")],
                [InlineKeyboardButton("📺 Serial", callback_data="uptype_series")],
            ]),
        )
        return

    if state == "await_movie_title":
        context.user_data["new_movie"]["title"] = text
        context.user_data["state"] = "await_movie_genre"
        await update.message.reply_text("🎞 Kino qismi yoki janrini kiriting:")
        return

    if state == "await_movie_genre":
        context.user_data["new_movie"]["genre"] = text
        context.user_data["state"] = "await_movie_lang"
        await update.message.reply_text("🗣 Kino tilini kiriting:")
        return

    if state == "await_movie_lang":
        context.user_data["new_movie"]["language"] = text
        context.user_data["state"] = "await_movie_country"
        await update.message.reply_text("🌍 Kino qaysi davlatga tegishli ekanini kiriting:")
        return

    if state == "await_movie_country":
        context.user_data["new_movie"]["country"] = text
        context.user_data.pop("state", None)
        if "is_series" in context.user_data["new_movie"]:
            # Bu allaqachon malum edi (mavjud serialga qism qoshilyapti) — savol berilmaydi
            context.user_data["state"] = "await_movie_episode"
            await update.message.reply_text("🔢 Nechanchi qism ekanini kiriting:")
            return
        await update.message.reply_text(
            "📺 Bu qanday kontent?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎬 Oddiy kino", callback_data="uptype_movie")],
                [InlineKeyboardButton("📺 Serial", callback_data="uptype_series")],
            ]),
        )
        return

    if state == "await_movie_episode":
        if not text.isdigit():
            await update.message.reply_text("❌ Faqat raqam kiriting.")
            return
        context.user_data["new_movie"]["episode"] = int(text)
        context.user_data["state"] = "await_movie_file"
        await update.message.reply_text("🎥 Endi kino faylini (video yoki hujjat) yuboring:")
        return

    # ---------- Hech qanday holat mos kelmasa: KOD / MAXFIY KOD / SMART SEARCH ----------
    text_q = text.strip()

    # 0) Raqamli matn — ENG AVVALO kino kodi sifatida tekshiriladi (hech qachon
    #    "tushunarsiz matn" yoki boshqa tekshiruvlarga chalg'imaydi)
    if text_q.isdigit():
        not_subs = await check_subscription(context, user.id)
        if not_subs:
            context.user_data["pending_code"] = text_q
            await update.message.reply_text(
                "⚠️ Botdan foydalanish uchun quyidagi kanal/guruhlarga obuna bo'ling:",
                reply_markup=kb_subscription(not_subs),
            )
            return
        await grant_referral_bonus_if_needed(context, get_user(user.id))
        await process_movie_code(update.message, context, text_q, user.id)
        return

    # 1) Bugungi maxfiy kod (agar admin belgilagan, faol va hali bugun olmagan bo'lsa)
    secret_today = get_setting("secret_code_today")
    if secret_today and text_q.lower() == secret_today.strip().lower():
        if not is_secret_code_active():
            await update.message.reply_text("⌛ Bu maxfiy kodning muddati tugagan yoki limiti to'lgan.")
        elif has_claimed_secret_code_today(user.id):
            await update.message.reply_text("✅ Siz bugungi maxfiy kod bonusini allaqachon oldingiz.")
        else:
            reward = float(get_setting("secret_code_reward") or 0)
            change_balance(user.id, reward)
            claim_secret_code(user.id)
            await update.message.reply_text(
                f"🎁 Tabriklaymiz! Bu bugungi MAXFIY KOD edi!\n💰 Bonus: {reward:.0f} so'm hisobingizga qo'shildi."
            )
        return

    # 2) Salomlashish/minnatdorchilik/emoji kabi "tasodifiy" matnlarni kino deb hisoblamaymiz
    if looks_like_casual_text(text_q):
        await update.message.reply_text("Kino kodi yoki kino nomini yuboring.")
        return

    not_subs = await check_subscription(context, user.id)
    if not_subs:
        context.user_data["pending_search"] = text_q
        await update.message.reply_text(
            "⚠️ Botdan foydalanish uchun quyidagi kanal/guruhlarga obuna bo'ling:",
            reply_markup=kb_subscription(not_subs),
        )
        return
    await grant_referral_bonus_if_needed(context, get_user(user.id))

    # 3) Raqam emas -> Smart Search (kino nomi bo'yicha, xato yozilgan bo'lsa ham)
    code, matched_title = smart_search_title(text_q)
    if code:
        await update.message.reply_text(f"🔎 Topildi: \"{matched_title}\"")
        await process_movie_code(update.message, context, code, user.id)
    else:
        context.user_data["last_search_query"] = text_q
        await notify_admins_movie_not_found(context, user.id, text_q)
        await update.message.reply_text(
            "❌ Bunday nomli kino topilmadi.\n\n"
            "🔎 Boshqa nom bilan urinib ko'ring yoki aniq kino kodini yuboring.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔔 Qo'shilganda xabar berish", callback_data="radar_add")]]
            ),
        )


async def save_movie_file_and_continue(msg, context: ContextTypes.DEFAULT_TYPE, file_id: str, file_type: str):
    """Kino faylini bazaga yozadi va davomini so'raydi (promo rasm/video yoki serial davomi)."""
    nm = context.user_data["new_movie"]
    mode = nm.get("mode", "full")
    add_movie(nm["code"], nm["episode"], nm.get("title", ""), nm.get("genre", ""),
               nm.get("language", ""), nm.get("country", ""), file_id, file_type, mode,
               nm.get("is_series", 0))
    await notify_radar_waiters(context, nm.get("title", ""), nm["code"])

    # Serial bo'lsa, kanalga FAQAT 1-qismda xabar yuboriladi (poster/teaser bilan).
    # Keyingi qismlar (2, 3, 4...) uchun kanalga qayta post qilinmaydi — foydalanuvchi
    # o'sha bitta kod orqali botdan barcha qismlarni ko'ra oladi.
    if nm.get("is_series") and nm.get("episode", 1) != 1:
        context.user_data.pop("state", None)
        await finish_or_continue_series(msg, context, posted_to_channel=False)
        return

    movie = get_movie_episode(nm["code"], nm["episode"])
    context.user_data["last_movie"] = dict(movie)

    if mode == "full":
        context.user_data["state"] = "await_promo_photo"
        await msg.reply_text("✅ Kino saqlandi!\n\n🖼 Endi kinoning poster rasmini yuboring (kanalga joylash uchun):")
    else:
        context.user_data["state"] = "await_promo_video"
        await msg.reply_text("✅ Kino saqlandi!\n\n🎞 Endi qisqa video (teaser)ni yuboring (kanalga joylash uchun):")


async def finish_or_continue_series(msg, context: ContextTypes.DEFAULT_TYPE, posted_to_channel: bool = True):
    """Kino/serial qismi saqlangandan keyin chaqiriladi.
    Faqat SERIAL bo'lsa ("yana qism qo'shasizmi?" deb so'raydi), oddiy kino bo'lsa
    darhol tugaydi va hech narsa so'ramaydi.
    posted_to_channel=False bo'lsa — bu safar kanalga xabar YUBORILMAGAN
    (chunki serialning 1-qismidan boshqasi uchun kanalga qayta post qilinmaydi)."""
    nm = context.user_data.get("new_movie") or {}
    base_text = "✅ Kino kanalga muvaffaqiyatli joylandi!" if posted_to_channel else "✅ Qism saqlandi!"
    if nm.get("is_series"):
        context.user_data["state"] = "await_series_more"
        await msg.reply_text(
            f"{base_text}\n\n➕ Shu serialga yana qism qo'shasizmi?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Ha, yana qism qo'shish", callback_data="series_more_yes")],
                [InlineKeyboardButton("🏁 Yo'q, tugatish", callback_data="series_more_no")],
            ]),
        )
    else:
        context.user_data.pop("new_movie", None)
        await msg.reply_text(base_text, reply_markup=current_admin_keyboard(context))


# ============================== MEDIA HANDLER ==============================

async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    msg = update.message

    # ---------- Xabarnoma (rasm/video/hujjat bilan) ----------
    if state == "await_broadcast":
        await run_broadcast(update, context)
        return

    # ---------- Kino faylini qabul qilish ----------
    if state == "await_movie_file" and (msg.video or msg.document):
        file_id = msg.video.file_id if msg.video else msg.document.file_id
        file_type = "video" if msg.video else "document"
        nm = context.user_data["new_movie"]

        existing = get_movie_episode(nm["code"], nm["episode"])
        if existing:
            # Bir xil kino/qism 2-marta (tasodifan) qayta yuklanmasligi uchun bloklaymiz —
            # faqat admin ataylab tasdiqlasa, ustidan yoziladi.
            context.user_data["pending_file"] = {"file_id": file_id, "file_type": file_type}
            context.user_data.pop("state", None)
            await msg.reply_text(
                f"⚠️ \"{nm['code']}\" kodining {nm['episode']}-qismi ALLAQACHON yuklangan!\n\n"
                "Bitta kino/qism ikki marta yuklanmasligi uchun bloklandi. Mavjudini "
                "o'zgartirish uchun \"✏️ Tahrirlash\" bo'limidan foydalaning, yoki agar "
                "ataylab ustidan qayta yozmoqchi bo'lsangiz tasdiqlang:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("♻️ Ha, ustidan yozilsin", callback_data="fileoverwrite_yes")],
                    [InlineKeyboardButton("❌ Yo'q, bekor qilish", callback_data="fileoverwrite_no")],
                ]),
            )
            return

        await save_movie_file_and_continue(msg, context, file_id, file_type)
        return

    # ---------- Promo rasm ----------
    if state == "await_promo_photo" and msg.photo:
        movie = context.user_data.get("last_movie")
        if not movie:
            return
        movie_chatid = get_setting("movie_channel_chatid")
        deep_link = f"https://t.me/{BOT_USERNAME}?start={movie['code']}"
        caption = (
            f"🎬 ➺ {movie['title']}\n"
            f"🎞 ➺ {movie['genre']}\n"
            f"🗣 ➺ {movie['language']}\n"
            f"🌍 ➺ {movie['country']}\n"
            f"🎬 Kino kodi: {movie['code']}\n\n"
            f"👉 @{BOT_USERNAME} orqali yuklab oling"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Kinoni ko'rish", url=deep_link)]])
        if movie_chatid:
            try:
                await context.bot.send_photo(int(movie_chatid), msg.photo[-1].file_id, caption=caption, reply_markup=kb)
            except TelegramError:
                await msg.reply_text("⚠️ Kanalga yuborishda xatolik (bot admin ekanligini tekshiring).")
        context.user_data.pop("state", None)
        context.user_data.pop("last_movie", None)
        await finish_or_continue_series(msg, context)
        return

    # ---------- Promo video (teaser) ----------
    if state == "await_promo_video" and msg.video:
        movie = context.user_data.get("last_movie")
        if not movie:
            return
        movie_chatid = get_setting("movie_channel_chatid")
        deep_link = f"https://t.me/{BOT_USERNAME}?start={movie['code']}"
        caption = (
            f"🎬 Aynan shu kinoni to'liq holatda botimizga joyladik😢🎬\n\n"
            f"🍿 Kino kodi: {movie['code']}🍿\n\n"
            f"To'liq kinoni bot orqali ko'rishingiz mumkin!\n"
            f"👉 @{BOT_USERNAME}\n"
            f"🎬 Ko'rish uchun pastdagi tugmani bosing"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Kinoni ko'rish", url=deep_link)]])
        if movie_chatid:
            try:
                await context.bot.send_video(int(movie_chatid), msg.video.file_id, caption=caption, reply_markup=kb)
            except TelegramError:
                await msg.reply_text("⚠️ Kanalga yuborishda xatolik (bot admin ekanligini tekshiring).")
        context.user_data.pop("state", None)
        context.user_data.pop("last_movie", None)
        await finish_or_continue_series(msg, context)
        return


# ============================== MAIN ==============================

def main():
    if not BOT_TOKEN or BOT_TOKEN == "SIZNING_TOKEN_BU_YERGA":
        raise SystemExit(
            "❌ BOT_TOKEN sozlanmagan! Fayl ichida yoki BOT_TOKEN environment variable orqali kiriting."
        )

    # ---- Railway/baza diagnostikasi: bu loglar orqali muammoni darhol korish mumkin ----
    on_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    if _railway_volume:
        print(f"✅ Railway Volume aniqlandi: {_railway_volume}")
        print(f"✅ (doimiy):Bazaga {DB_PATH}")
    elif os.environ.get("DB_PATH"):
        print(f"✅ DB_PATH qo'lda belgilangan: {DB_PATH}")
    elif on_railway:
        print("⚠️⚠️⚠️  DIQQAT — MUAMMO TOPILDI  ⚠️⚠️⚠️")
        print("⚠️ Siz Railway'da ishlayapsiz, LEKIN hech qanday Volume ulanmagan!")
        print(f"⚠️ Baza vaqtinchalik joyda saqlanmoqda: {os.path.abspath(DB_PATH)}")
        print("⚠️ HAR YANGI DEPLOY'DA bu fayl butunlay o'chib ketadi (kinolar, foydalanuvchilar — hammasi).")
        print("⚠️ TUZATISH: Railway loyihangizda -> service -> Settings -> Volumes -> ")
        print("⚠️            'New Volume' -> istalgan mount path (masalan /data) -> shu servisga ulang.")
        print("⚠️ Volume ulangach, bu bot uni AVTOMATIK topadi (qo'shimcha sozlash shart emas).")
    else:
        print(f"📁 Baza fayli (lokal): {os.path.abspath(DB_PATH)}")

    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # DIQQAT: quyidagi handlerlar faqat SHAXSIY chatda ishlaydi (filters.ChatType.PRIVATE).
    # Bot biror guruh/kanalga admin qilib qo'shilsa ham, u yerda oddiy xabarlarga
    # umuman javob bermaydi — faqat botning o'zi bilan (private) muloqotda ishlaydi.
    # ChatMemberHandler va ChatJoinRequestHandler bundan mustasno — ular aynan
    # guruh/kanaldagi hodisalarni ushlash uchun kerak.
    app.add_handler(CommandHandler("start", start_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("help", help_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(join_request_handler))
    async def global_error_handler(update, context):
        logger.exception("GLOBAL XATOLIK: update=%s", update, exc_info=context.error)

    app.add_error_handler(global_error_handler)

    # '/restore' izohi bilan .db fayl yuborilsa — bazani tiklaydi (faqat SUPER_ADMIN uchun).
    # media_handler'dan OLDIN turishi shart, aks holda oddiy hujjat sifatida ushlanib qoladi.
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE
        & filters.User(user_id=SUPER_ADMIN_ID)
        & filters.Document.ALL
        & filters.CaptionRegex(re.compile(r"^/restore", re.IGNORECASE)),
        restore_db_handler
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.PHOTO | filters.VIDEO | filters.Document.ALL), media_handler
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, text_handler
    ))

    # ---- Kunlik avtomatik baza zaxirasi (har kuni soat 03:00 da barcha adminlarga yuboriladi) ----
    if app.job_queue is not None:
        app.job_queue.run_daily(scheduled_backup_job, time=datetime.time(hour=3, minute=0), name="daily_db_backup")
        print("✅ Kunlik avtomatik baza zaxirasi yoqildi (har kuni 03:00 da adminlarga yuboriladi).")
    else:
        print(
            "ℹ️ JobQueue o'rnatilmagan — avtomatik kunlik zaxira ISHLAMAYDI "
            "('💾 Baza zaxirasi' tugmasi orqali qo'lda olish esa ishlayveradi).\n"
            "   Yoqish uchun: pip install \"python-telegram-bot[job-queue]\" --upgrade"
        )

    print("🤖 Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()