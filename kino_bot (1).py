# -*- coding: utf-8 -*-
"""
KinoHub Pro Bot — to'liq funksional Telegram kino-bot
=======================================================

O'RNATISH:
    pip install -r requirements.txt

SOZLASH:
    BOT_TOKEN va SUPER_ADMIN_ID endi kodga yozilmaydi — ularni muhit
    o'zgaruvchisi (environment variable) sifatida beriladi:
        export BOT_TOKEN="123456:ABC-token"
        export SUPER_ADMIN_ID="123456789"
    Render'da bu — dashboard'dagi "Environment" bo'limi orqali qo'shiladi.

ISHGA TUSHIRISH:
    python kino_bot.py
"""

import os
import logging
import sqlite3
import datetime
from contextlib import closing
from threading import Thread

from flask import Flask
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError, Forbidden, BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================== SOZLAMALAR ==============================
# Bu qiymatlar endi kod ichiga yozilmaydi — deploy qilayotgan joyingizda
# (Render bo'lsa "Environment" bo'limida) muhit o'zgaruvchisi sifatida kiritiladi.

BOT_TOKEN = os.environ.get("BOT_TOKEN")            # @BotFather'dan olingan token
BOT_USERNAME = os.environ.get("BOT_USERNAME", "KinoHub_brobot")  # @ belgisiz
_super_admin_raw = os.environ.get("SUPER_ADMIN_ID")
DB_PATH = "kinobot.db"

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN muhit o'zgaruvchisi topilmadi! "
        "Render'da Environment bo'limiga BOT_TOKEN qo'shing (yoki lokal ishga "
        "tushirayotgan bo'lsangiz, terminalda export BOT_TOKEN=... qiling)."
    )
if not _super_admin_raw or not _super_admin_raw.strip("-").isdigit():
    raise RuntimeError(
        "SUPER_ADMIN_ID muhit o'zgaruvchisi topilmadi yoki noto'g'ri! "
        "O'zingizning Telegram ID'ingizni (masalan @userinfobot orqali bilib olasiz) "
        "SUPER_ADMIN_ID nomi bilan Environment bo'limiga qo'shing."
    )
SUPER_ADMIN_ID = int(_super_admin_raw)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============================== BAZA (DATABASE) ==============================

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with closing(get_conn()) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                balance     REAL DEFAULT 0,
                ref_by      INTEGER,
                joined_at   TEXT
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
                downloads   INTEGER DEFAULT 0,
                created_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS channels (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER,
                username    TEXT,
                invite_link TEXT,
                title       TEXT,
                ctype       TEXT   -- 'public' yoki 'private'
            );

            CREATE TABLE IF NOT EXISTS admins (
                user_id     INTEGER PRIMARY KEY,
                added_by    INTEGER,
                added_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS payment_requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                ptype       TEXT,      -- 'premium' yoki 'stars'
                amount      REAL,
                status      TEXT DEFAULT 'pending',
                created_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        # Standart sozlamalar
        defaults = {
            "movie_channel_username": "",
            "movie_channel_chatid": "",
            "referral_bonus": "500",
            "premium_price": "50000",
            "stars_price": "20000",
            "bot_enabled": "1",
        }
        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )
        # Super admin
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
        rows = conn.execute("SELECT user_id FROM admins").fetchall()
        return [r["user_id"] for r in rows]


def get_user(user_id: int):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def ref_count(user_id: int) -> int:
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE ref_by=?", (user_id,)
        ).fetchone()
        return row["c"] if row else 0


def change_balance(user_id: int, delta: float):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, user_id)
        )


def register_user_if_new(user_id, username, first_name, ref_by=None) -> bool:
    """Foydalanuvchini bazaga qo'shadi. Yangi bo'lsa True qaytaradi."""
    existing = get_user(user_id)
    if existing:
        return False
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO users (user_id, username, first_name, balance, ref_by, joined_at) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            (user_id, username, first_name, ref_by, datetime.datetime.now().isoformat()),
        )
    return True


def get_mandatory_channels():
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT * FROM channels ORDER BY id"
        ).fetchall()


def add_channel(chat_id, username, invite_link, title, ctype):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO channels (chat_id, username, invite_link, title, ctype) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, username, invite_link, title, ctype),
        )


def delete_channel(channel_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM channels WHERE id=?", (channel_id,))


def get_movies_by_code(code: str):
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT * FROM movies WHERE code=? ORDER BY episode", (code,)
        ).fetchall()


def get_movie_episode(code: str, episode: int):
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT * FROM movies WHERE code=? AND episode=?", (code, episode)
        ).fetchone()


def add_movie(code, episode, title, genre, language, country, file_id, file_type):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO movies (code, episode, title, genre, language, country, "
            "file_id, file_type, downloads, created_at) VALUES (?,?,?,?,?,?,?,?,0,?)",
            (code, episode, title, genre, language, country, file_id, file_type,
             datetime.datetime.now().isoformat()),
        )


def increment_downloads(movie_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE movies SET downloads = downloads + 1 WHERE id=?", (movie_id,))


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
        return conn.execute(
            "SELECT * FROM payment_requests WHERE id=?", (req_id,)
        ).fetchone()


def set_payment_status(req_id: int, status: str):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE payment_requests SET status=? WHERE id=?", (status, req_id))


def get_pending_payments():
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT * FROM payment_requests WHERE status='pending' ORDER BY id DESC"
        ).fetchall()


def all_user_ids():
    with closing(get_conn()) as conn:
        return [r["user_id"] for r in conn.execute("SELECT user_id FROM users").fetchall()]


def stats_for_period(days: int):
    since = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
    with closing(get_conn()) as conn:
        users_c = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE joined_at >= ?", (since,)
        ).fetchone()["c"]
        movies_c = conn.execute(
            "SELECT COUNT(*) c FROM movies WHERE created_at >= ?", (since,)
        ).fetchone()["c"]
        downloads_c = conn.execute(
            "SELECT COALESCE(SUM(downloads),0) c FROM movies"
        ).fetchone()["c"]
    return users_c, movies_c, downloads_c


# ============================== KLAVIATURALAR ==============================

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


def kb_referal_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Havolani nusxalash", callback_data=f"refcopy_{user_id}")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="earn")],
    ])


def kb_withdraw_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Telegram premium", callback_data="withdraw_premium")],
        [InlineKeyboardButton("✨ Telegram stars", callback_data="withdraw_stars")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="earn")],
    ])


def kb_admin_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 Kanallar", callback_data="adm_channels"),
         InlineKeyboardButton("🎬 Kino yuklash", callback_data="adm_upload")],
        [InlineKeyboardButton("📢 Xabarnoma", callback_data="adm_broadcast"),
         InlineKeyboardButton("📊 Statistika", callback_data="adm_stats")],
        [InlineKeyboardButton("⚙️ Bot holati", callback_data="adm_status"),
         InlineKeyboardButton("👮 Adminlar", callback_data="adm_admins")],
        [InlineKeyboardButton("💳 To'lovlar", callback_data="adm_payments"),
         InlineKeyboardButton("🔧 Sozlamalar", callback_data="adm_settings")],
        [InlineKeyboardButton("🔎 Foydalanuvchini tekshirish", callback_data="adm_checkuser")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="back_start")],
    ])


def kb_channels_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔒 Majburiy obunalar", callback_data="adm_mandatory")],
        [InlineKeyboardButton("🎥 Kino kanal", callback_data="adm_moviechannel")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_panel")],
    ])


def kb_mandatory_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Ommaviy", callback_data="adm_mpublic")],
        [InlineKeyboardButton("🔐 Maxfiy", callback_data="adm_mprivate")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="adm_channels")],
    ])


def kb_channel_type_menu(ctype: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Qo'shish", callback_data=f"chadd_{ctype}")],
        [InlineKeyboardButton("📋 Ro'yxat", callback_data=f"chlist_{ctype}")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="adm_mandatory")],
    ])


def kb_back(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Orqaga", callback_data=callback_data)]])


# ============================== MAJBURIY OBUNA TEKSHIRUVI ==============================

async def check_subscription(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> list:
    """Obuna bo'lmagan kanallar ro'yxatini qaytaradi (bo'sh bo'lsa — hammasiga obuna)."""
    not_subscribed = []
    channels = get_mandatory_channels()
    for ch in channels:
        target = ch["chat_id"] if ch["chat_id"] else f"@{ch['username']}"
        try:
            member = await context.bot.get_chat_member(target, user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                not_subscribed.append(ch)
        except TelegramError:
            # Bot kanalda emas yoki kanal topilmadi — xavfsizlik uchun obuna bo'lmagan deb hisoblaymiz
            not_subscribed.append(ch)
    return not_subscribed


def kb_subscription(channels) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        if ch["ctype"] == "public" and ch["username"]:
            url = f"https://t.me/{ch['username']}"
        else:
            url = ch["invite_link"] or "https://t.me"
        rows.append([InlineKeyboardButton(f"📢 {ch['title'] or ch['username'] or 'Kanal'}", url=url)])
    rows.append([InlineKeyboardButton("✅ Tekshirdim", callback_data="check_sub")])
    return InlineKeyboardMarkup(rows)


async def notify_admins_new_user(context: ContextTypes.DEFAULT_TYPE, user):
    now = datetime.datetime.now()
    text = (
        "👤 Yangi obunachi qo'shildi!\n\n"
        f"👤 Ism: {user.first_name or '-'}\n"
        f"🆔 ID: {user.id}\n"
        f"🔗 Telegram: {'@' + user.username if user.username else 'yo`q'}\n"
        f"🕒 Vaqt: {now.strftime('%d.%m.%Y')} | {now.strftime('%H:%M:%S')}"
    )
    for admin_id in get_admin_ids():
        try:
            await context.bot.send_message(admin_id, text)
        except TelegramError:
            pass


async def send_start_message(update_or_query, context: ContextTypes.DEFAULT_TYPE, user):
    """Har doim foydalanuvchining o'z ismi bilan start xabarini yuboradi."""
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

    # bot o'chirilgan bo'lsa (faqat oddiy foydalanuvchilar uchun)
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
        if ref_by and get_user(ref_by):
            bonus = float(get_setting("referral_bonus") or 0)
            change_balance(ref_by, bonus)
            try:
                await context.bot.send_message(
                    ref_by,
                    f"🎉 Sizning referal havolangiz orqali yangi foydalanuvchi qo'shildi!\n"
                    f"💰 Hisobingizga {bonus:.0f} so'm qo'shildi."
                )
            except TelegramError:
                pass

    # majburiy obunani tekshirish
    not_subs = await check_subscription(context, user.id)
    if not_subs:
        context.user_data["pending_code"] = pending_code
        await update.message.reply_text(
            "⚠️ Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling, so'ng "
            "\"✅ Tekshirdim\" tugmasini bosing:",
            reply_markup=kb_subscription(not_subs),
        )
        return

    await send_start_message(update, context, user)

    if pending_code:
        await process_movie_code(update.message, context, pending_code)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "☎️ Yordam:\n\n"
        "Kino kodini yuborsangiz, tegishli film sizga yuboriladi.\n"
        "Muammo yuzaga kelsa, adminlarga murojaat qiling."
    )


# ============================== KINO QIDIRUV ==============================

def kb_movie_card(code: str, downloads: int) -> InlineKeyboardMarkup:
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
    rows = []
    row = []
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


async def process_movie_code(message, context: ContextTypes.DEFAULT_TYPE, code: str):
    episodes = get_movies_by_code(code)
    if not episodes:
        await message.reply_text("❌ Bunday kodli kino topilmadi. Kodni tekshirib qayta yuboring.")
        return

    if len(episodes) > 1:
        await message.reply_text(
            f"🎬 \"{episodes[0]['title']}\" — {len(episodes)} qism topildi.\n"
            f"Kerakli qismni tanlang:",
            reply_markup=kb_episodes(code, list(episodes)),
        )
        return

    await send_movie_episode(message, context, episodes[0])


async def send_movie_episode(message, context: ContextTypes.DEFAULT_TYPE, movie):
    increment_downloads(movie["id"])
    movie_channel = get_setting("movie_channel_username")
    caption = (
        f"🎬 {movie['title']}\n"
        f"🎞 {movie['genre']}\n"
        f"🌍 {movie['country']} | 🗣 {movie['language']}\n"
        f"🎬 Kino kodi: {movie['code']}\n"
        f"📡 Kanal: @{movie_channel}\n"
        f"⬇️ Yuklanishlar soni: {movie['downloads'] + 1}"
    ) if movie_channel else (
        f"🎬 {movie['title']}\n"
        f"🎞 {movie['genre']}\n"
        f"🌍 {movie['country']} | 🗣 {movie['language']}\n"
        f"🎬 Kino kodi: {movie['code']}\n"
        f"⬇️ Yuklanishlar soni: {movie['downloads'] + 1}"
    )
    kb = kb_movie_card(movie["code"], movie["downloads"] + 1)
    if movie["file_type"] == "video":
        await context.bot.send_video(message.chat_id, movie["file_id"], caption=caption, reply_markup=kb)
    else:
        await context.bot.send_document(message.chat_id, movie["file_id"], caption=caption, reply_markup=kb)


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
            await query.answer("❌ Siz hali barcha kanallarga obuna bo'lmagansiz!", show_alert=True)
            return
        await query.message.delete()
        pending_code = context.user_data.pop("pending_code", None)
        await send_start_message(query, context, user)
        if pending_code:
            await process_movie_code(query.message, context, pending_code)
        return

    # ---------- Pul ishlash ----------
    if data == "earn":
        u = get_user(user.id)
        rc = ref_count(user.id)
        await query.message.edit_text(
            "💰 Pul ishlash bo'limi\n\n"
            f"💳 Sizning hisobingiz: {u['balance']:.0f} so'm\n"
            f"👥 Siz taklif qilgan foydalanuvchilar: {rc} ta",
            reply_markup=kb_earn_menu(),
        )
        return

    if data == "earn_ref":
        rc = ref_count(user.id)
        link = f"https://t.me/{BOT_USERNAME}?start=ref{user.id}"
        await query.message.edit_text(
            "👥 Referal tizimi\n\n"
            f"🔗 Sizning referal havolangiz:\n{link}\n\n"
            f"✅ Siz taklif qilganlar: {rc} ta",
            reply_markup=kb_referal_menu(user.id),
        )
        return

    if data.startswith("refcopy_"):
        link = f"https://t.me/{BOT_USERNAME}?start=ref{user.id}"
        await context.bot.send_message(user.id, f"`{link}`", parse_mode=ParseMode.MARKDOWN)
        await query.answer("Havola yuborildi, uni nusxalashingiz mumkin ✅")
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
        price_key = "premium_price" if ptype == "premium" else "stars_price"
        price = float(get_setting(price_key))
        u = get_user(user.id)
        label = "Telegram premium" if ptype == "premium" else "Telegram stars"

        if u["balance"] < price:
            await query.answer("❌ Hisobingizda mablag' yetarli emas", show_alert=True)
            return

        change_balance(user.id, -price)
        req_id = add_payment_request(user.id, ptype, price)
        await query.message.edit_text(
            f"✅ Tabriklaymiz! Sizning {label} olish bo'yicha arizangiz adminga yuborildi.",
        )

        uname = f"@{user.username}" if user.username else "yo'q"
        admin_text = (
            f"💳 Foydalanuvchi {label} sotib olmoqchi va uning hisobidan {price:.0f} so'm "
            f"yechib olindi.\n\n"
            f"👤 Foydalanuvchi: {user.first_name} ({uname})\n"
            f"🆔 ID: {user.id}\n\n"
            f"So'rovni tasdiqlaysizmi?"
        )
        admin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👁 Ko'rish", url=f"tg://user?id={user.id}")],
            [InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"payconf_{req_id}")],
        ])
        for admin_id in get_admin_ids():
            try:
                await context.bot.send_message(admin_id, admin_text, reply_markup=admin_kb)
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
                f"tasdiqlandi, admin javobini kuting!\n\n"
                f"🔎 Kino qidirish uchun qayta /start bosing.",
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
            await send_movie_episode(query.message, context, movie)
        else:
            await query.answer("Topilmadi", show_alert=True)
        return

    if data.startswith("eppage_"):
        _, code, page = data.split("_", 2)
        episodes = list(get_movies_by_code(code))
        await query.message.edit_reply_markup(reply_markup=kb_episodes(code, episodes, int(page)))
        return

    # ---------- Admin panel ----------
    # DIQQAT: bu yerda "adm_" (pastki chiziq bilan) tekshirilgani uchun aynan
    # "admin_panel", "admdel_..." kabi callback'lar o'tkazib yuborilib, admin
    # panel umuman ochilmas edi. Shuningdek "setedit_", "balchg_", "promo_"
    # ham bu shartga kirmagani uchun mos tugmalar hech narsa qilmas edi.
    if (
        data.startswith("adm")
        or data.startswith("ch")
        or data.startswith("promo_")
        or data.startswith("setedit_")
        or data.startswith("balchg_")
    ):
        if not is_admin(user.id):
            await query.answer("⛔️ Sizda ruxsat yo'q.", show_alert=True)
            return
        await admin_callback_router(query, context, data, user)
        return


# ============================== ADMIN PANEL ROUTER ==============================

async def admin_callback_router(query, context: ContextTypes.DEFAULT_TYPE, data: str, user):
    context.user_data.pop("state", None)

    if data == "admin_panel":
        await query.message.edit_text("🛠 Boshqaruv paneli", reply_markup=kb_admin_panel())
        return

    if data == "adm_channels":
        await query.message.edit_text("📡 Kanallarni sozlash bo'limi", reply_markup=kb_channels_menu())
        return

    if data == "adm_mandatory":
        await query.message.edit_text(
            "🔒 Majburiy obunalar\n\nQaysi turdagi kanal bilan ishlaysiz?",
            reply_markup=kb_mandatory_menu(),
        )
        return

    if data == "adm_mpublic":
        await query.message.edit_text("🌐 Ommaviy kanallarni sozlash bo'limidasiz",
                                       reply_markup=kb_channel_type_menu("public"))
        return

    if data == "adm_mprivate":
        await query.message.edit_text("🔐 Maxfiy kanallarni sozlash bo'limidasiz",
                                       reply_markup=kb_channel_type_menu("private"))
        return

    if data == "adm_moviechannel":
        context.user_data["state"] = "await_movie_channel"
        await query.message.edit_text(
            "⚠️ Kanal manzilini yuborishdan avval botni kanalga **admin** qilib qo'ying!\n\n"
            "📢 Kino kanali username'ini yuboring (masalan: @KinoHubKanal):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_back("adm_channels"),
        )
        return

    # ---- Ommaviy: qo'shish ----
    if data == "chadd_public":
        context.user_data["state"] = "await_add_public"
        await query.message.edit_text(
            "⚠️ Kanalingiz manzilini yuborishdan avval botni kanalingizga **admin** qilib olishingiz kerak!\n\n"
            "📢 Kerakli kanal manzilini yuboring:\n\n"
            "📄 Namuna: `@KinoHubKanal`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_back("adm_mpublic"),
        )
        return

    # ---- Maxfiy: qo'shish (forward orqali) ----
    if data == "chadd_private":
        context.user_data["state"] = "await_add_private"
        await query.message.edit_text(
            "⚠️ Botni maxfiy kanalingizga **admin** (taklif havolasi yaratish huquqi bilan) qilib qo'ying.\n\n"
            "📨 So'ng o'sha kanaldan istalgan xabarni shu yerga **forward** qiling:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_back("adm_mprivate"),
        )
        return

    # ---- Ro'yxat / O'chirish (public va private uchun umumiy) ----
    if data.startswith("chlist_"):
        ctype = data.split("_", 1)[1]
        await show_channel_list(query, ctype)
        return

    if data.startswith("chdel_"):
        _, ctype, ch_id = data.split("_", 2)
        delete_channel(int(ch_id))
        await query.answer("✅ Kanal o'chirildi")
        await show_channel_list(query, ctype)
        return

    # ---- Kino yuklash ----
    if data == "adm_upload":
        context.user_data["state"] = "await_movie_code"
        context.user_data["new_movie"] = {}
        await query.message.edit_text("🎬 Kino kodini kiriting (masalan: 1):",
                                       reply_markup=kb_back("admin_panel"))
        return

    if data.startswith("promo_"):
        choice = data.split("_", 1)[1]  # photo / video / skip
        movie = context.user_data.get("last_movie")
        if not movie:
            return
        if choice == "skip":
            await query.message.edit_text("✅ Kino muvaffaqiyatli yuklandi (kanalga joylanmadi).")
            context.user_data.pop("last_movie", None)
            return
        context.user_data["state"] = f"await_promo_{choice}"
        label = "poster rasmini" if choice == "photo" else "qisqa video (teaser)ni"
        await query.message.edit_text(f"🖼 Endi kinoning {label} yuboring:")
        return

    # ---- Xabarnoma ----
    if data == "adm_broadcast":
        context.user_data["state"] = "await_broadcast"
        await query.message.edit_text(
            "📢 Barcha foydalanuvchilarga yuboriladigan xabar matnini kiriting:",
            reply_markup=kb_back("admin_panel"),
        )
        return

    # ---- Statistika ----
    if data == "adm_stats":
        await query.message.edit_text(
            "📊 Statistika davrini tanlang:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 Kunlik", callback_data="adm_stats_1"),
                 InlineKeyboardButton("🗓 Haftalik", callback_data="adm_stats_7"),
                 InlineKeyboardButton("📆 Oylik", callback_data="adm_stats_30")],
                [InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_panel")],
            ]),
        )
        return

    if data.startswith("adm_stats_"):
        days = int(data.rsplit("_", 1)[1])
        users_c, movies_c, downloads_c = stats_for_period(days)
        period_name = {1: "kunlik", 7: "haftalik", 30: "oylik"}[days]
        await query.message.edit_text(
            f"📊 Statistika ({period_name}):\n\n"
            f"👥 Yangi foydalanuvchilar: {users_c}\n"
            f"🎬 Yangi yuklangan kinolar: {movies_c}\n"
            f"⬇️ Jami yuklab olishlar: {downloads_c}",
            reply_markup=kb_back("adm_stats"),
        )
        return

    # ---- Bot holati ----
    if data == "adm_status":
        current = get_setting("bot_enabled")
        status_text = "🟢 Yoqilgan" if current == "1" else "🔴 O'chirilgan"
        await query.message.edit_text(
            f"⚙️ Bot holati: {status_text}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔴 O'chirish" if current == "1" else "🟢 Yoqish",
                                       callback_data="adm_status_toggle")],
                [InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_panel")],
            ]),
        )
        return

    if data == "adm_status_toggle":
        current = get_setting("bot_enabled")
        set_setting("bot_enabled", "0" if current == "1" else "1")
        await admin_callback_router(query, context, "adm_status", user)
        return

    # ---- Adminlar ----
    if data == "adm_admins":
        await query.message.edit_text(
            "👮 Adminlar bo'limi",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Admin qo'shish", callback_data="adm_admin_add")],
                [InlineKeyboardButton("📋 Ro'yxat / O'chirish", callback_data="adm_admin_list")],
                [InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_panel")],
            ]),
        )
        return

    if data == "adm_admin_add":
        context.user_data["state"] = "await_admin_add"
        await query.message.edit_text("🆔 Yangi admin qilinadigan foydalanuvchi ID sini yuboring:",
                                       reply_markup=kb_back("adm_admins"))
        return

    if data == "adm_admin_list":
        await show_admin_list(query)
        return

    if data.startswith("admdel_"):
        target_id = int(data.split("_", 1)[1])
        if target_id == SUPER_ADMIN_ID:
            await query.answer("⛔️ Asosiy adminni o'chirib bo'lmaydi!", show_alert=True)
            return
        with closing(get_conn()) as conn, conn:
            conn.execute("DELETE FROM admins WHERE user_id=?", (target_id,))
        await query.answer("✅ Admin o'chirildi")
        await show_admin_list(query)
        return

    # ---- To'lovlar ----
    if data == "adm_payments":
        await show_payments(query)
        return

    # ---- Sozlamalar ----
    if data == "adm_settings":
        await query.message.edit_text(
            "🔧 Sozlamalar",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 Referal narxi", callback_data="setedit_referral_bonus")],
                [InlineKeyboardButton("⭐ Premium narxi", callback_data="setedit_premium_price")],
                [InlineKeyboardButton("✨ Stars narxi", callback_data="setedit_stars_price")],
                [InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_panel")],
            ]),
        )
        return

    if data.startswith("setedit_"):
        key = data.split("_", 1)[1]
        context.user_data["state"] = f"await_setting_{key}"
        await query.message.edit_text(f"✏️ Yangi qiymatni kiriting (raqam):", reply_markup=kb_back("adm_settings"))
        return

    # ---- Foydalanuvchini tekshirish ----
    if data == "adm_checkuser":
        context.user_data["state"] = "await_check_user"
        await query.message.edit_text("🆔 Tekshirmoqchi bo'lgan foydalanuvchi ID sini kiriting:",
                                       reply_markup=kb_back("admin_panel"))
        return

    if data.startswith("balchg_"):
        _, sign, target_id = data.split("_", 2)
        context.user_data["state"] = f"await_balance_{sign}_{target_id}"
        word = "qo'shmoqchi" if sign == "add" else "ayirmoqchi"
        await query.message.edit_text(f"💰 Hisobga {word} bo'lgan summani kiriting:")
        return


async def show_channel_list(query, ctype: str):
    channels = [c for c in get_mandatory_channels() if c["ctype"] == ctype]
    if not channels:
        text = "📋 Ro'yxat bo'sh."
        kb = kb_back(f"adm_m{ctype}")
    else:
        text = "📋 Kanallar ro'yxati:\n\n" + "\n".join(
            f"• {c['title'] or c['username'] or c['chat_id']}" for c in channels
        )
        rows = [[InlineKeyboardButton(f"❌ {c['title'] or c['username']}", callback_data=f"chdel_{ctype}_{c['id']}")]
                for c in channels]
        rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data=f"adm_m{ctype}")])
        kb = InlineKeyboardMarkup(rows)
    await query.message.edit_text(text, reply_markup=kb)


async def show_admin_list(query):
    with closing(get_conn()) as conn:
        rows = conn.execute("SELECT a.user_id, u.first_name, u.username FROM admins a "
                             "LEFT JOIN users u ON u.user_id=a.user_id").fetchall()
    lines = []
    kb_rows = []
    for r in rows:
        name = r["first_name"] or str(r["user_id"])
        lines.append(f"• {name} (ID: {r['user_id']})")
        if r["user_id"] != SUPER_ADMIN_ID:
            kb_rows.append([InlineKeyboardButton(f"❌ {name}", callback_data=f"admdel_{r['user_id']}")])
    kb_rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="adm_admins")])
    await query.message.edit_text("👮 Adminlar ro'yxati:\n\n" + "\n".join(lines),
                                   reply_markup=InlineKeyboardMarkup(kb_rows))


async def show_payments(query):
    pending = get_pending_payments()
    if not pending:
        await query.message.edit_text("💳 Hozircha kutilayotgan to'lovlar yo'q.", reply_markup=kb_back("admin_panel"))
        return
    lines = ["💳 Kutilayotgan to'lovlar:\n"]
    for p in pending:
        u = get_user(p["user_id"])
        name = u["first_name"] if u else str(p["user_id"])
        lines.append(f"#{p['id']} — {name} — {p['ptype']} — {p['amount']:.0f} so'm")
    await query.message.edit_text("\n".join(lines), reply_markup=kb_back("admin_panel"))


# ============================== MATN HANDLER (STATE MACHINE) ==============================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()
    state = context.user_data.get("state")

    # ---------- Kino kanal manzilini o'rnatish ----------
    if state == "await_movie_channel":
        username = text.lstrip("@")
        try:
            chat = await context.bot.get_chat(f"@{username}")
            member = await context.bot.get_chat_member(chat.id, context.bot.id)
            if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                await update.message.reply_text("❌ Bot kanalda admin emas. Botni admin qilib, qayta yuboring.")
                return
        except TelegramError:
            await update.message.reply_text("❌ Kanal topilmadi. Manzilni tekshirib qayta yuboring.")
            return
        set_setting("movie_channel_username", username)
        set_setting("movie_channel_chatid", str(chat.id))
        context.user_data.pop("state", None)
        await update.message.reply_text(f"✅ Kino kanali muvaffaqiyatli ulandi: @{username}",
                                         reply_markup=kb_channels_menu())
        return

    # ---------- Ommaviy majburiy kanal qo'shish ----------
    if state == "await_add_public":
        username = text.lstrip("@")
        try:
            chat = await context.bot.get_chat(f"@{username}")
            member = await context.bot.get_chat_member(chat.id, context.bot.id)
            if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                await update.message.reply_text("❌ Bot kanalda admin emas!")
                return
        except TelegramError:
            await update.message.reply_text("❌ Kanal topilmadi. Manzilni tekshiring.")
            return
        add_channel(chat.id, username, None, chat.title, "public")
        context.user_data.pop("state", None)
        await update.message.reply_text(f"✅ Kanal muvaffaqiyatli ulandi: @{username}",
                                         reply_markup=kb_channel_type_menu("public"))
        return

    # ---------- Sozlamalarni o'zgartirish ----------
    if state and state.startswith("await_setting_"):
        key = state.replace("await_setting_", "")
        if not text.replace(".", "", 1).isdigit():
            await update.message.reply_text("❌ Faqat raqam kiriting.")
            return
        set_setting(key, text)
        context.user_data.pop("state", None)
        await update.message.reply_text("✅ Sozlama yangilandi.", reply_markup=kb_back("adm_settings"))
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
                                         reply_markup=kb_back("adm_admins"))
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
                                         reply_markup=kb_back("admin_panel"))
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
            f"🆔 ID: {target['user_id']}\n"
            f"💳 Balans: {target['balance']:.0f} so'm\n"
            f"👥 Taklif qilganlar: {rc} ta\n"
            f"🔗 Kim tomonidan taklif qilingan: {referrer_text}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Pul qo'shish", callback_data=f"balchg_add_{target_id}"),
                 InlineKeyboardButton("➖ Pul ayirish", callback_data=f"balchg_sub_{target_id}")],
                [InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_panel")],
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
        await update.message.reply_text(admin_msg, reply_markup=kb_back("admin_panel"))
        try:
            await context.bot.send_message(target_id, user_msg)
        except TelegramError:
            pass
        return

    # ---------- Kino yuklash: matnli qadamlar ----------
    if state == "await_movie_code":
        context.user_data["new_movie"]["code"] = text
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
        context.user_data["state"] = "await_movie_episode"
        await update.message.reply_text("🔢 Nechanchi qism ekanini kiriting (oddiy kino bo'lsa 1 deb yozing):")
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
            "⚠️ Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:",
            reply_markup=kb_subscription(not_subs),
        )
        return
    await process_movie_code(update.message, context, text)


# ============================== MEDIA HANDLER ==============================

async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    state = context.user_data.get("state")
    msg = update.message

    # ---------- Maxfiy kanal qo'shish: forward orqali ----------
    if state == "await_add_private" and msg.forward_from_chat:
        chat = msg.forward_from_chat
        try:
            member = await context.bot.get_chat_member(chat.id, context.bot.id)
            if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                await msg.reply_text("❌ Bot bu kanalda admin emas!")
                return
            invite_link = None
            try:
                link_obj = await context.bot.create_chat_invite_link(chat.id)
                invite_link = link_obj.invite_link
            except TelegramError:
                pass
        except TelegramError:
            await msg.reply_text("❌ Kanalni tekshirib bo'lmadi.")
            return
        add_channel(chat.id, None, invite_link, chat.title, "private")
        context.user_data.pop("state", None)
        await msg.reply_text(f"✅ Maxfiy kanal ulandi: {chat.title}", reply_markup=kb_channel_type_menu("private"))
        return

    # ---------- Kino faylini qabul qilish ----------
    if state == "await_movie_file" and (msg.video or msg.document):
        file_id = msg.video.file_id if msg.video else msg.document.file_id
        file_type = "video" if msg.video else "document"
        nm = context.user_data["new_movie"]
        add_movie(nm["code"], nm["episode"], nm["title"], nm["genre"], nm["language"],
                   nm["country"], file_id, file_type)
        movie = get_movie_episode(nm["code"], nm["episode"])
        context.user_data["last_movie"] = dict(movie)
        context.user_data.pop("state", None)
        await msg.reply_text(
            "✅ Kino saqlandi!\n\n📡 Kanalga qanday post qilamiz?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🖼 Rasm bilan (to'liq post)", callback_data="promo_photo")],
                [InlineKeyboardButton("🎞 Qisqa video bilan", callback_data="promo_video")],
                [InlineKeyboardButton("❌ Kanalga joylamaslik", callback_data="promo_skip")],
            ]),
        )
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
                await msg.reply_text("⚠️ Kanalga yuborishda xatolik yuz berdi (bot admin ekanligini tekshiring).")
        context.user_data.pop("state", None)
        context.user_data.pop("last_movie", None)
        await msg.reply_text("✅ Kino kanalga muvaffaqiyatli joylandi!", reply_markup=kb_back("admin_panel"))
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
                await msg.reply_text("⚠️ Kanalga yuborishda xatolik yuz berdi (bot admin ekanligini tekshiring).")
        context.user_data.pop("state", None)
        context.user_data.pop("last_movie", None)
        await msg.reply_text("✅ Kino kanalga muvaffaqiyatli joylandi!", reply_markup=kb_back("admin_panel"))
        return


# ============================== RENDER UCHUN KEEP-ALIVE SERVER ==============================
# Render'ning bepul tarifi faqat HTTP so'rovlarga javob beradigan "Web Service"
# turidagi xizmatlarni bepul beradi. Bot o'zi Telegram bilan polling orqali
# ishlashda davom etadi — bu qism shunchaki Render'ga "xizmat tirik" degan
# signal berish uchun kerak. Boshqa (masalan VPS) muhitda ishga tushirilsa ham
# hech qanday zarari yo'q — shunchaki fon rejimida kichik veb-server ishlaydi.

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "🤖 KinoHub Pro Bot ishlayapti!"


def _run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)


def keep_alive():
    Thread(target=_run_web, daemon=True).start()


# ============================== MAIN ==============================

# --- YANGI QO'SHILGAN FUNKSIYALAR ---
async def forward_handler(update, context):
    forward_origin = update.message.forward_origin
    if forward_origin and forward_origin.type == 'channel':
        channel_id = forward_origin.chat.id
        channel_title = forward_origin.chat.title
        await update.message.reply_text(
            f"✅ Kanal muvaffaqiyatli tanindi!\n\n📌 Kanal nomi: {channel_title}\n🆔 Kanal ID: `{channel_id}`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Bu xabar kanaldan forward qilinmagan!")

async def unknown_text_handler(update, context):
    await update.message.reply_text("⚠️ Iltimos, kino qidirish uchun faqat kino KODINI (raqamlarda) yuboring.")

# --- ASOSIY MAIN FUNKSIYASI ---
def main():
    init_db()
    keep_alive()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))
    
    # Yangi qatorlar shu yerda
    app.add_handler(MessageHandler(filters.FORWARDED, forward_handler))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL, media_handler))
    app.add_handler(MessageHandler(filters.Regex(r'^\d+$'), text_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text_handler))

    print("🤖 Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()