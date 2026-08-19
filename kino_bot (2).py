# -*- coding: utf-8 -*-
"""
KinoHub Pro Bot — to'liq funksional Telegram kino-bot (v2)
=============================================================

O'RNATISH:
    pip install python-telegram-bot --upgrade

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
                joined_at       TEXT
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
                downloads   INTEGER DEFAULT 0,
                created_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS downloads_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                movie_id      INTEGER,
                user_id       INTEGER,
                downloaded_at TEXT
            );

            CREATE TABLE IF NOT EXISTS channels (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER,
                username    TEXT,
                invite_link TEXT,
                title       TEXT,
                ctype       TEXT,     -- 'public' yoki 'private'
                kind        TEXT,     -- 'channel' yoki 'group'
                chat_type   TEXT      -- telegram xom turi: 'channel' | 'supergroup' | 'group'
            );

            CREATE TABLE IF NOT EXISTS pending_chats (
                chat_id     INTEGER PRIMARY KEY,
                title       TEXT,
                username    TEXT,
                kind        TEXT,
                chat_type   TEXT,
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

            CREATE INDEX IF NOT EXISTS idx_users_ref_by ON users(ref_by);
            CREATE INDEX IF NOT EXISTS idx_movies_code ON movies(code);
            CREATE INDEX IF NOT EXISTS idx_payment_status ON payment_requests(status);
            CREATE INDEX IF NOT EXISTS idx_downloads_log_at ON downloads_log(downloaded_at);
            """
        )
        # eski bazalar uchun xavfsiz migratsiya
        _safe_add_column(conn, "users", "ref_bonus_given INTEGER DEFAULT 0")
        _safe_add_column(conn, "channels", "kind TEXT DEFAULT 'channel'")
        _safe_add_column(conn, "movies", "mode TEXT DEFAULT 'full'")
        _safe_add_column(conn, "channels", "chat_type TEXT")
        _safe_add_column(conn, "pending_chats", "chat_type TEXT")

        defaults = {
            "movie_channel_username": "",
            "movie_channel_chatid": "",
            "referral_bonus": "500",
            "premium_price": "50000",
            "stars_price": "20000",
            "bot_enabled": "1",
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


def change_balance(user_id: int, delta: float):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, user_id))


def mark_ref_bonus_given(user_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE users SET ref_bonus_given=1 WHERE user_id=?", (user_id,))


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


def add_channel(chat_id, username, invite_link, title, ctype, kind, chat_type=None):
    with closing(get_conn()) as conn, conn:
        existing = conn.execute("SELECT id FROM channels WHERE chat_id=?", (chat_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE channels SET username=?, invite_link=?, title=?, ctype=?, kind=?, chat_type=? WHERE chat_id=?",
                (username, invite_link, title, ctype, kind, chat_type, chat_id),
            )
        else:
            conn.execute(
                "INSERT INTO channels (chat_id, username, invite_link, title, ctype, kind, chat_type) "
                "VALUES (?,?,?,?,?,?,?)",
                (chat_id, username, invite_link, title, ctype, kind, chat_type),
            )


def delete_channel(channel_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM channels WHERE id=?", (channel_id,))


def get_channel_by_id(channel_id: int):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()


def get_channel_by_chat_id(chat_id: int):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM channels WHERE chat_id=?", (chat_id,)).fetchone()


def update_channel_chat_id(old_chat_id: int, new_chat_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE channels SET chat_id=? WHERE chat_id=?", (new_chat_id, old_chat_id))
        conn.execute("UPDATE pending_chats SET chat_id=? WHERE chat_id=?", (new_chat_id, old_chat_id))


# ---- Aniqlangan (pending) chatlar ----

def upsert_pending_chat(chat_id, title, username, kind, chat_type=None):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO pending_chats (chat_id, title, username, kind, chat_type, detected_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, username=excluded.username, "
            "kind=excluded.kind, chat_type=excluded.chat_type, detected_at=excluded.detected_at",
            (chat_id, title, username, kind, chat_type, datetime.datetime.now().isoformat()),
        )


def get_pending_chat(chat_id: int):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM pending_chats WHERE chat_id=?", (chat_id,)).fetchone()


def delete_pending_chat(chat_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM pending_chats WHERE chat_id=?", (chat_id,))


# ---- Kinolar ----

def get_movies_by_code(code: str):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM movies WHERE code=? ORDER BY episode", (code,)).fetchall()


def get_movie_episode(code: str, episode: int):
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT * FROM movies WHERE code=? AND episode=?", (code, episode)
        ).fetchone()


def add_movie(code, episode, title, genre, language, country, file_id, file_type, mode="full"):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO movies (code, episode, title, genre, language, country, "
            "file_id, file_type, mode, downloads, created_at) VALUES (?,?,?,?,?,?,?,?,?,0,?)",
            (code, episode, title, genre, language, country, file_id, file_type, mode,
             datetime.datetime.now().isoformat()),
        )


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


# ============================== INLINE KLAVIATURALAR (oddiy foydalanuvchi) ==============================

def kb_main_menu(user_id: int) -> InlineKeyboardMarkup:
    movie_channel = get_setting("movie_channel_username")
    rows = []
    if movie_channel:
        rows.append([InlineKeyboardButton("🎬 Kino kodlari", url=f"https://t.me/{movie_channel}")])
    else:
        rows.append([InlineKeyboardButton("🎬 Kino kodlari", callback_data="no_movie_channel")])
    rows.append([InlineKeyboardButton("💰 Pul ishlash", callback_data="earn")])
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
    ["📢 Xabarnoma", "📊 Statistika"],
    ["⚙️ Bot holati", "👮 Adminlar"],
    ["💳 To'lovlar", "🔧 Sozlamalar"],
    ["🔎 Foydalanuvchini tekshirish"],
    ["⬅️ Chiqish"],
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
    ["✨ Stars narxi"],
    ["⬅️ Orqaga"],
], resize_keyboard=True)

RK_STATS = ReplyKeyboardMarkup([
    ["📅 Kunlik", "🗓 Haftalik", "📆 Oylik"],
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

async def check_subscription(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> list:
    """Obuna bo'lmagan kanal/guruhlar ro'yxatini qaytaradi."""
    not_subscribed = []
    for ch in get_mandatory_channels():
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
    try:
        await context.bot.send_message(
            referrer["user_id"],
            "🎉 Sizning referal havolangiz orqali qo'shilgan foydalanuvchi majburiy "
            "kanal(lar)ga a'zo bo'ldi!\n"
            f"💰 Hisobingizga {bonus:.0f} so'm qo'shildi."
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
        if payload.startswith("ref") and payload[3:].isdigit():
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
            "⚠️ Botdan foydalanish uchun quyidagi kanal/guruhlarga obuna bo'ling (yopiq bo'lsa — so'rov "
            "yuboring), so'ng \"✅ Tekshirdim\" tugmasini bosing:",
            reply_markup=kb_subscription(not_subs),
        )
        return

    await grant_referral_bonus_if_needed(context, get_user(user.id))
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

def kb_movie_card(code: str) -> InlineKeyboardMarkup:
    movie_channel = get_setting("movie_channel_username")
    share_url = f"https://t.me/share/url?url=https://t.me/{BOT_USERNAME}?start={code}"
    rows = []
    if movie_channel:
        rows.append([InlineKeyboardButton("🎬 Ko'proq kinolar", url=f"https://t.me/{movie_channel}")])
    rows.append([InlineKeyboardButton("↗️ Ulashish", url=share_url)])
    return InlineKeyboardMarkup(rows)


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


async def process_movie_code(message, context: ContextTypes.DEFAULT_TYPE, code: str, user_id: int):
    episodes = get_movies_by_code(code)
    if not episodes:
        await message.reply_text("❌ Bunday kodli kino topilmadi. Kodni tekshirib qayta yuboring.")
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

    kb = kb_movie_card(movie["code"])
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
    upsert_pending_chat(chat.id, chat.title or chat.username or str(chat.id), chat.username, kind, chat.type)

    label = "Kanal" if kind == "channel" else "Guruh"
    uname_line = f"🔗 Username: @{chat.username}\n" if chat.username else "🔗 Username: yo'q (maxfiy)\n"
    warn_line = ""
    if chat.type == ChatType.GROUP:
        warn_line = (
            "\n⚠️ Bu — oddiy (eski turdagi) guruh. Uni maxfiy majburiy obunaga qo'shish uchun "
            "avval *supergroup*ga aylantirish kerak: guruh sozlamalarida istalgan supergroup-only "
            "funksiyani yoqing (masalan \"Yangi a'zolarni tasdiqlash\" yoki \"Chat tarixi\"), guruh "
            "avtomatik supergroupga aylanadi. Shundan keyin pastdagi tugmani qayta bosing.\n"
        )
    text = (
        f"✅ Men yangi {label.lower()}da ADMIN qilib tayinlandim!\n\n"
        f"📌 Nomi: {chat.title or '-'}\n"
        + uname_line +
        f"🆔 Chat ID: `{chat.id}`\n"
        + warn_line +
        "\nBu chatni qanday ishlatamiz?\n\n"
        "⚠️ Eslatma: agar 'Majburiy obunaga qo'shish'ni tanlasangiz va bu chatda username "
        "bo'lmasa (maxfiy), foydalanuvchilar so'rov yuborishi va men uni avtomatik "
        "tasdiqlashim uchun menda \"Foydalanuvchilarni taklif qilish\" (Invite Users) "
        "huquqi borligiga ishonch hosil qiling."
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
    """Maxfiy kanal/guruhga kelgan qo'shilish so'rovlarini tekshirib, faqat bot
    ro'yxatidan o'tgan (majburiy obuna yoki kino kanali sifatida ulangan) chatlar
    uchun avtomatik tasdiqlaydi. Ro'yxatda yo'q, tanish bo'lmagan chatdagi
    so'rovlarga tegilmaydi — ularni chat administratorlari o'zi ko'radi."""
    req = update.chat_join_request
    channel = get_channel_by_chat_id(req.chat.id)
    is_movie_channel = str(req.chat.id) == (get_setting("movie_channel_chatid") or "")
    if not channel and not is_movie_channel:
        return

    try:
        await context.bot.approve_chat_join_request(req.chat.id, req.from_user.id)
    except TelegramError:
        return
    try:
        await context.bot.send_message(
            req.from_user.id,
            "✅ So'rovingiz tekshirildi va tasdiqlandi!\n\n🔎 Kino qidirish uchun qayta /start bosing."
        )
    except TelegramError:
        pass


# ============================== GURUH -> SUPERGROUP MIGRATSIYASI ==============================

async def migration_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Oddiy guruh supergroupga aylanganda Telegram uning chat_id sini butunlay
    o'zgartiradi. Bu holatda ro'yxatdagi (channels/pending_chats) eski ID ni
    yangisiga avtomatik ko'chiramiz, aks holda guruh 'yo'qolib qoladi'."""
    msg = update.message
    if not msg:
        return
    old_id, new_id = None, None
    if msg.migrate_to_chat_id:
        old_id, new_id = msg.chat_id, msg.migrate_to_chat_id
    elif msg.migrate_from_chat_id:
        old_id, new_id = msg.migrate_from_chat_id, msg.chat_id
    if not (old_id and new_id):
        return

    update_channel_chat_id(old_id, new_id)
    if (get_setting("movie_channel_chatid") or "") == str(old_id):
        set_setting("movie_channel_chatid", str(new_id))

    for admin_id in get_admin_ids():
        try:
            await context.bot.send_message(
                admin_id,
                f"ℹ️ Guruh supergroupga aylandi, ID avtomatik yangilandi:\n"
                f"`{old_id}` → `{new_id}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError:
            pass


# ============================== CALLBACK QUERY ROUTER ==============================

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    await query.answer()

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
        await query.answer("Hali kino kanali ulanmagan.", show_alert=True)
        return

    if data == "check_sub":
        not_subs = await check_subscription(context, user.id)
        if not_subs:
            await query.answer("❌ Siz hali barcha kanal/guruhlarga obuna bo'lmagansiz yoki so'rovingiz "
                                "hali tasdiqlanmagan!", show_alert=True)
            return
        await grant_referral_bonus_if_needed(context, get_user(user.id))
        await query.message.delete()
        pending_code = context.user_data.pop("pending_code", None)
        await send_start_message(query, context, user)
        if pending_code:
            await process_movie_code(query.message, context, pending_code, user.id)
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
            await query.answer("❌ Hisobingizda mablag' yetarli emas", show_alert=True)
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
            await query.answer("Bu so'rov allaqachon ko'rib chiqilgan.", show_alert=True)
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
            await query.answer("Topilmadi", show_alert=True)
        return

    if data.startswith("eppage_"):
        _, code, page = data.split("_", 2)
        episodes = list(get_movies_by_code(code))
        await query.message.edit_reply_markup(reply_markup=kb_episodes(code, episodes, int(page)))
        return

    # ---------- Quyidagilar faqat adminlar uchun ----------
    if data.startswith(("admin_panel", "padd_", "uptype_", "upmode_", "chdel_", "admdel_", "balchg_")):
        if not is_admin(user.id):
            await query.answer("⛔️ Sizda ruxsat yo'q.", show_alert=True)
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
            await query.answer("⚠️ Bu so'rov eskirgan yoki allaqachon ishlov berilgan.", show_alert=True)
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
            if pending["username"]:
                # Ommaviy — oddiy username havolasi yetarli
                add_channel(chat_id, pending["username"], None, pending["title"], "public", pending["kind"], pending["chat_type"])
                delete_pending_chat(chat_id)
                await query.message.edit_text(
                    f"✅ Ommaviy majburiy obunaga qo'shildi: {pending['title']}\n"
                    f"(Foydalanuvchilar to'g'ridan-to'g'ri qo'shiladi)"
                )
            else:
                # Maxfiy — join-request havolasi yaratamiz, so'rovlar avtomatik tasdiqlanadi
                invite_link = None
                try:
                    link_obj = await context.bot.create_chat_invite_link(chat_id, creates_join_request=True)
                    invite_link = link_obj.invite_link
                except TelegramError as e:
                    retry_kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 Qayta urinish", callback_data=f"padd_add_{chat_id}")],
                        [InlineKeyboardButton("❌ Bekor qilish", callback_data=f"padd_cancel_{chat_id}")],
                    ])
                    err_text = str(e).lower()
                    if pending["kind"] == "group" and pending["chat_type"] == ChatType.GROUP:
                        await query.message.edit_text(
                            "⚠️ Taklif havolasi yaratib bo'lmadi, chunki bu — oddiy (eski turdagi) guruh.\n\n"
                            "So'rov orqali qo'shilish (join request) faqat *supergroup* va kanallarda ishlaydi.\n\n"
                            "✅ Yechim: guruh sozlamalaridan istalgan supergroup-only funksiyani yoqing "
                            "(masalan \"Yangi a'zolarni tasdiqlash\"), guruh avtomatik supergroupga "
                            "aylanadi, so'ng pastdagi tugmani qayta bosing.",
                            reply_markup=retry_kb, parse_mode=ParseMode.MARKDOWN,
                        )
                    elif "administrator" in err_text or "right" in err_text or "not enough rights" in err_text:
                        await query.message.edit_text(
                            f"⚠️ Taklif havolasi yaratib bo'lmadi: {e}\n\n"
                            "Botga shu chatda \"Foydalanuvchilarni taklif qilish\" (Invite Users via Link) "
                            "admin huquqini bering, so'ng qayta urinib ko'ring.",
                            reply_markup=retry_kb,
                        )
                    else:
                        await query.message.edit_text(
                            f"⚠️ Taklif havolasi yaratib bo'lmadi: {e}\n\n"
                            "Bot bu chatda 'Foydalanuvchilarni taklif qilish' huquqiga ega ekanligini tekshiring.",
                            reply_markup=retry_kb,
                        )
                    return
                add_channel(chat_id, None, invite_link, pending["title"], "private", pending["kind"], pending["chat_type"])
                delete_pending_chat(chat_id)
                await query.message.edit_text(
                    f"✅ Maxfiy majburiy obunaga qo'shildi: {pending['title']}\n"
                    f"Foydalanuvchilar so'rov yuboradi, men tekshirib avtomatik tasdiqlayman."
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
            context.user_data["state"] = "await_movie_file"
            await query.message.edit_text("🎥 Endi kino faylini (video yoki hujjat) yuboring:")
        else:
            context.user_data["state"] = "await_movie_episode"
            await query.message.edit_text("🔢 Nechanchi qism ekanini kiriting (masalan: 1):")
        return

    # ---- Kanal/guruhni ro'yxatdan o'chirish ----
    if data.startswith("chdel_"):
        ch_id = int(data.split("_", 1)[1])
        delete_channel(ch_id)
        await query.answer("✅ O'chirildi")
        await query.message.edit_text("✅ Ro'yxatdan o'chirildi.")
        return

    # ---- Adminni o'chirish ----
    if data.startswith("admdel_"):
        target_id = int(data.split("_", 1)[1])
        if target_id == SUPER_ADMIN_ID:
            await query.answer("⛔️ Asosiy adminni o'chirib bo'lmaydi!", show_alert=True)
            return
        with closing(get_conn()) as conn, conn:
            conn.execute("DELETE FROM admins WHERE user_id=?", (target_id,))
        await query.answer("✅ Admin o'chirildi")
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


# ============================== ADMIN MENYU (Reply Keyboard) ROUTERI ==============================

async def admin_menu_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Reply-keyboard menyu bosilganda ishlaydi. True qaytarsa — xabar shu yerda ishlov olindi."""
    user = update.effective_user
    if not is_admin(user.id):
        return False
    level = context.user_data.get("admin_level")
    if not level:
        return False
    text = update.message.text.strip()

    if text == "⬅️ Chiqish":
        context.user_data.pop("admin_level", None)
        await update.message.reply_text("🔚 Boshqaruv panelidan chiqdingiz.", reply_markup=ReplyKeyboardRemove())
        return True

    if text == "⬅️ Orqaga":
        context.user_data["admin_level"] = "main"
        await update.message.reply_text("🛠 Boshqaruv paneli", reply_markup=RK_MAIN)
        return True

    if level == "main":
        if text == "📡 Kanallar":
            context.user_data["admin_level"] = "channels"
            await update.message.reply_text("📡 Kanallar / guruhlarni sozlash bo'limi", reply_markup=RK_CHANNELS)
            return True
        if text == "🎬 Kino yuklash":
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
        if text == "📢 Xabarnoma":
            context.user_data["state"] = "await_broadcast"
            await update.message.reply_text("📢 Barcha foydalanuvchilarga yuboriladigan xabar matnini kiriting:",
                                             reply_markup=ReplyKeyboardRemove())
            return True
        if text == "📊 Statistika":
            context.user_data["admin_level"] = "stats"
            await update.message.reply_text("📊 Davrni tanlang:", reply_markup=RK_STATS)
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
            await show_payments_text(update, context)
            return True
        if text == "🔧 Sozlamalar":
            context.user_data["admin_level"] = "settings"
            await update.message.reply_text("🔧 Sozlamalar", reply_markup=RK_SETTINGS)
            return True
        if text == "🔎 Foydalanuvchini tekshirish":
            context.user_data["state"] = "await_check_user"
            await update.message.reply_text("🆔 Tekshirmoqchi bo'lgan foydalanuvchi ID sini kiriting:")
            return True

    elif level == "channels":
        if text == "🔒 Majburiy obunalar":
            await show_mandatory_list_text(update, context)
            return True
        if text == "🎥 Kino kanal":
            movie_ch = get_setting("movie_channel_username")
            cur = f"@{movie_ch}" if movie_ch else "ulanmagan ❌"
            await update.message.reply_text(
                f"🎥 Joriy kino kanali: {cur}\n\n"
                "Yangi kanal ulash uchun: botni kerakli kanalga ADMIN qilib qo'shing — "
                "men avtomatik aniqlab, tasdiqlash uchun xabar yuboraman "
                "('🎬 Kino kanali qilish' tugmasi orqali)."
            )
            return True

    elif level == "admins":
        if text == "➕ Admin qo'shish":
            context.user_data["state"] = "await_admin_add"
            await update.message.reply_text("🆔 Yangi admin qilinadigan foydalanuvchi ID sini yuboring:")
            return True
        if text == "📋 Adminlar ro'yxati":
            await show_admin_list_text(update, context)
            return True

    elif level == "settings":
        mapping = {"👥 Referal narxi": "referral_bonus", "⭐ Premium narxi": "premium_price",
                   "✨ Stars narxi": "stars_price"}
        if text in mapping:
            context.user_data["state"] = f"await_setting_{mapping[text]}"
            await update.message.reply_text("✏️ Yangi qiymatni kiriting (raqam):")
            return True

    elif level == "stats":
        mapping = {"📅 Kunlik": "day", "🗓 Haftalik": "week", "📆 Oylik": "month"}
        titles = {"day": "Kunlik", "week": "Haftalik", "month": "Oylik"}
        if text in mapping:
            kind = mapping[text]
            breakdown = stats_breakdown(kind)
            lines = [f"📊 {titles[kind]} statistika:\n"]
            for label, users_c, downloads_c in breakdown:
                lines.append(f"📅 {label}:\n   👥 Yangi foydalanuvchi: {users_c} ta\n   ⬇️ Yuklab olishlar: {downloads_c} ta\n")
            await update.message.reply_text("\n".join(lines))
            return True

    elif level == "status":
        if text in ("🔴 O'chirish", "🟢 Yoqish"):
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
        context.user_data.pop("state", None)
        ids = all_user_ids()
        sent = 0
        for uid in ids:
            try:
                await context.bot.send_message(uid, text)
                sent += 1
            except (Forbidden, TelegramError):
                pass
        await update.message.reply_text(f"✅ Xabar {sent}/{len(ids)} foydalanuvchiga yuborildi.",
                                         reply_markup=current_admin_keyboard(context))
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

    # ---------- Kino yuklash: matnli qadamlar ----------
    if state == "await_movie_code":
        context.user_data["new_movie"]["code"] = text
        if context.user_data["new_movie"].get("mode") == "simple":
            # Qisqa video rejimi: qo'shimcha ma'lumot so'ralmaydi
            context.user_data.pop("state", None)
            await update.message.reply_text(
                "📺 Bu qanday kontent?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎬 Oddiy kino", callback_data="uptype_movie")],
                    [InlineKeyboardButton("📺 Serial", callback_data="uptype_series")],
                ]),
            )
        else:
            context.user_data["state"] = "await_movie_title"
            await update.message.reply_text("📌 Kino nomini kiriting:")
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

    # ---------- Hech qanday holat mos kelmasa — bu kino kodi deb qaraladi ----------
    not_subs = await check_subscription(context, user.id)
    if not_subs:
        context.user_data["pending_code"] = text
        await update.message.reply_text(
            "⚠️ Botdan foydalanish uchun quyidagi kanal/guruhlarga obuna bo'ling:",
            reply_markup=kb_subscription(not_subs),
        )
        return
    await grant_referral_bonus_if_needed(context, get_user(user.id))
    await process_movie_code(update.message, context, text, user.id)


# ============================== MEDIA HANDLER ==============================

async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    msg = update.message

    # ---------- Kino faylini qabul qilish ----------
    if state == "await_movie_file" and (msg.video or msg.document):
        file_id = msg.video.file_id if msg.video else msg.document.file_id
        file_type = "video" if msg.video else "document"
        nm = context.user_data["new_movie"]
        mode = nm.get("mode", "full")
        add_movie(nm["code"], nm["episode"], nm.get("title", ""), nm.get("genre", ""),
                   nm.get("language", ""), nm.get("country", ""), file_id, file_type, mode)
        movie = get_movie_episode(nm["code"], nm["episode"])
        context.user_data["last_movie"] = dict(movie)

        if mode == "full":
            context.user_data["state"] = "await_promo_photo"
            await msg.reply_text("✅ Kino saqlandi!\n\n🖼 Endi kinoning poster rasmini yuboring (kanalga joylash uchun):")
        else:
            context.user_data["state"] = "await_promo_video"
            await msg.reply_text("✅ Kino saqlandi!\n\n🎞 Endi qisqa video (teaser)ni yuboring (kanalga joylash uchun):")
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
        await msg.reply_text("✅ Kino kanalga muvaffaqiyatli joylandi!", reply_markup=current_admin_keyboard(context))
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
        await msg.reply_text("✅ Kino kanalga muvaffaqiyatli joylandi!", reply_markup=current_admin_keyboard(context))
        return


# ============================== MAIN ==============================

def main():
    if not BOT_TOKEN or BOT_TOKEN == "SIZNING_TOKEN_BU_YERGA":
        raise SystemExit(
            "❌ BOT_TOKEN sozlanmagan! Fayl ichida yoki BOT_TOKEN environment variable orqali kiriting."
        )

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
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, migration_handler))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.PHOTO | filters.VIDEO | filters.Document.ALL), media_handler
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, text_handler
    ))

    print("🤖 Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
