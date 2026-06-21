import os
import re
import sqlite3
import logging
from datetime import datetime

TRC20_ADDRESS_RE = re.compile(r"^T[A-Za-z1-9]{33}$")

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ===================== CONFIG =====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@airdrops_refral")  # e.g. -1001234567890 or @username
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "airdrops_refral")  # without @
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
REFERRAL_REWARD = 1.0   # USDT per confirmed referral
MIN_WITHDRAW = 10.0     # minimum USDT to withdraw
DB_PATH = os.environ.get("DB_PATH", "referrals.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ===================== DATABASE =====================
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance REAL DEFAULT 0,
            referred_by INTEGER,
            joined_channel INTEGER DEFAULT 0,
            wallet_address TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER UNIQUE,
            confirmed INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            wallet_address TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def create_user(user_id, username, referred_by=None):
    conn = db_connect()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO users (user_id, username, referred_by, created_at) VALUES (?, ?, ?, ?)",
        (user_id, username, referred_by, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

def set_joined(user_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("UPDATE users SET joined_channel = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def set_wallet(user_id, address):
    conn = db_connect()
    c = conn.cursor()
    c.execute("UPDATE users SET wallet_address = ? WHERE user_id = ?", (address, user_id))
    conn.commit()
    conn.close()

def add_balance(user_id, amount):
    conn = db_connect()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def deduct_balance(user_id, amount):
    conn = db_connect()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def referral_exists(referred_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT * FROM referrals WHERE referred_id = ?", (referred_id,))
    row = c.fetchone()
    conn.close()
    return row

def create_referral(referrer_id, referred_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO referrals (referrer_id, referred_id, created_at) VALUES (?, ?, ?)",
        (referrer_id, referred_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

def confirm_referral(referred_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("UPDATE referrals SET confirmed = 1 WHERE referred_id = ? AND confirmed = 0", (referred_id,))
    changed = c.rowcount
    conn.commit()
    conn.close()
    return changed > 0

def count_confirmed_referrals(user_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as cnt FROM referrals WHERE referrer_id = ? AND confirmed = 1", (user_id,))
    row = c.fetchone()
    conn.close()
    return row["cnt"] if row else 0

def create_withdrawal(user_id, amount, wallet_address):
    conn = db_connect()
    c = conn.cursor()
    c.execute(
        "INSERT INTO withdrawals (user_id, amount, wallet_address, created_at) VALUES (?, ?, ?, ?)",
        (user_id, amount, wallet_address, datetime.utcnow().isoformat()),
    )
    conn.commit()
    wid = c.lastrowid
    conn.close()
    return wid

def mark_withdrawal_paid(withdrawal_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("UPDATE withdrawals SET status = 'paid' WHERE id = ?", (withdrawal_id,))
    conn.commit()
    conn.close()

def get_pending_withdrawals():
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT * FROM withdrawals WHERE status = 'pending' ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    return rows

def get_stats():
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as cnt FROM users")
    total_users = c.fetchone()["cnt"]
    c.execute("SELECT COUNT(*) as cnt FROM referrals WHERE confirmed = 1")
    total_confirmed = c.fetchone()["cnt"]
    c.execute("SELECT SUM(balance) as s FROM users")
    total_balance = c.fetchone()["s"] or 0
    conn.close()
    return total_users, total_confirmed, total_balance

# ===================== HELPERS =====================
async def is_channel_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning(f"membership check failed for {user_id}: {e}")
        return False

def join_keyboard():
    keyboard = [
        [InlineKeyboardButton("📢 عضویت در کانال", url=f"https://t.me/{CHANNEL_USERNAME}")],
        [InlineKeyboardButton("✅ عضو شدم", callback_data="check_membership")],
    ]
    return InlineKeyboardMarkup(keyboard)

def main_keyboard():
    keyboard = [
        [InlineKeyboardButton("💰 موجودی من", callback_data="balance")],
        [InlineKeyboardButton("🔗 لینک دعوت من", callback_data="reflink")],
        [InlineKeyboardButton("💸 برداشت موجودی", callback_data="withdraw_info")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ===================== HANDLERS =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    existing = get_user(user.id)

    referred_by = None
    if not existing and args:
        try:
            ref_id = int(args[0])
            if ref_id != user.id and get_user(ref_id):
                referred_by = ref_id
        except ValueError:
            pass

    if not existing:
        create_user(user.id, user.username or user.first_name, referred_by)
        if referred_by:
            create_referral(referred_by, user.id)

    member = await is_channel_member(context, user.id)
    if member:
        set_joined(user.id)
        if confirm_referral(user.id):
            row = referral_exists(user.id)
            if row:
                add_balance(row["referrer_id"], REFERRAL_REWARD)
                try:
                    await context.bot.send_message(
                        chat_id=row["referrer_id"],
                        text=f"🎉 یک نفر با لینک شما عضو شد و {REFERRAL_REWARD} تتر به موجودی شما اضافه شد!",
                    )
                except Exception as e:
                    logger.warning(f"could not notify referrer: {e}")

        await update.message.reply_text(
            "خوش اومدی! 👋\nبا دعوت دوستات به کانال، به ازای هر عضو واقعی ۱ تتر دریافت می‌کنی.\n"
            f"حداقل مبلغ برداشت {int(MIN_WITHDRAW)} تتر هست.",
            reply_markup=main_keyboard(),
        )
    else:
        await update.message.reply_text(
            "برای استفاده از ربات و فعال شدن دعوتت، اول باید عضو کانال ما بشی 👇",
            reply_markup=join_keyboard(),
        )

async def check_membership_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    member = await is_channel_member(context, user_id)
    if member:
        set_joined(user_id)
        if confirm_referral(user_id):
            row = referral_exists(user_id)
            if row:
                add_balance(row["referrer_id"], REFERRAL_REWARD)
                try:
                    await context.bot.send_message(
                        chat_id=row["referrer_id"],
                        text=f"🎉 یک نفر با لینک شما عضو شد و {REFERRAL_REWARD} تتر به موجودی شما اضافه شد!",
                    )
                except Exception as e:
                    logger.warning(f"could not notify referrer: {e}")

        await query.edit_message_text(
            "عضویت شما تایید شد ✅\nحالا می‌تونی با لینک دعوتت برای هر عضو واقعی ۱ تتر بگیری.",
        )
        await context.bot.send_message(chat_id=user_id, text="منوی اصلی:", reply_markup=main_keyboard())
    else:
        await query.answer("هنوز عضو کانال نشدی!", show_alert=True)

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u = get_user(user_id)
    bal = u["balance"] if u else 0
    refs = count_confirmed_referrals(user_id)
    await update.effective_message.reply_text(
        f"💰 موجودی شما: {bal:.2f} تتر\n👥 تعداد دعوت‌های موفق: {refs}\n"
        f"حداقل برداشت: {int(MIN_WITHDRAW)} تتر"
    )

async def balance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await balance_cmd(update, context)

async def reflink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={user_id}"
    await update.effective_message.reply_text(
        f"🔗 لینک دعوت اختصاصی شما:\n{link}\n\nاین لینک رو برای دوستات بفرست و به ازای هر عضو واقعی {int(REFERRAL_REWARD)} تتر بگیر."
    )

async def reflink_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await reflink_cmd(update, context)

async def setwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("آدرس کیف پول USDT (شبکه TRC20) رو این‌طوری بفرست:\n/setwallet ADDRESS")
        return
    address = context.args[0].strip()
    if not TRC20_ADDRESS_RE.match(address):
        await update.message.reply_text(
            "❌ این آدرس معتبر نیست.\nآدرس TRC20 باید با T شروع بشه و ۳۴ کاراکتر باشه.\n"
            "آدرس رو از کیف پولت (مثل Trust Wallet یا Binance، شبکه TRC20/TRON) کپی کن و دوباره بفرست."
        )
        return
    set_wallet(user_id, address)
    await update.message.reply_text(f"✅ آدرس کیف پول TRC20 شما ذخیره شد:\n{address}")

async def withdraw_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "برای برداشت موجودی:\n"
        "1️⃣ اول آدرس کیف پول USDT (TRC20) خودت رو ثبت کن:\n/setwallet ADDRESS\n"
        f"2️⃣ بعد دستور /withdraw رو بفرست (حداقل {int(MIN_WITHDRAW)} تتر)."
    )

async def withdraw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u = get_user(user_id)
    if not u:
        await update.message.reply_text("لطفا اول دستور /start رو بزن.")
        return
    if not u["wallet_address"]:
        await update.message.reply_text("اول آدرس کیف پولت رو ثبت کن:\n/setwallet ADDRESS")
        return
    if u["balance"] < MIN_WITHDRAW:
        await update.message.reply_text(
            f"موجودی شما {u['balance']:.2f} تتر هست. حداقل مبلغ برداشت {int(MIN_WITHDRAW)} تتر می‌باشد."
        )
        return

    amount = u["balance"]
    deduct_balance(user_id, amount)
    wid = create_withdrawal(user_id, amount, u["wallet_address"])
    await update.message.reply_text(
        f"✅ درخواست برداشت شما ثبت شد (#{wid})\nمبلغ: {amount:.2f} تتر\nبه‌زودی پرداخت انجام می‌شود."
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"💸 درخواست برداشت جدید #{wid}\n"
                    f"کاربر: {user_id} (@{u['username']})\n"
                    f"مبلغ: {amount:.2f} تتر\n"
                    f"کیف پول (TRC20): {u['wallet_address']}\n\n"
                    f"بعد از پرداخت دستی، بزن: /paid {wid}"
                ),
            )
        except Exception as e:
            logger.warning(f"could not notify admin {admin_id}: {e}")

# ---------- Admin commands ----------
async def admin_paid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("استفاده: /paid WITHDRAWAL_ID")
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("شناسه نامعتبر است.")
        return
    mark_withdrawal_paid(wid)
    await update.message.reply_text(f"✅ برداشت #{wid} به عنوان پرداخت‌شده ثبت شد.")

async def admin_pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    rows = get_pending_withdrawals()
    if not rows:
        await update.message.reply_text("درخواست برداشت در انتظاری وجود ندارد.")
        return
    lines = [
        f"#{r['id']} | کاربر {r['user_id']} | {r['amount']:.2f} تتر | {r['wallet_address']}"
        for r in rows
    ]
    await update.message.reply_text("\n".join(lines))

async def admin_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    total_users, total_confirmed, total_balance = get_stats()
    await update.message.reply_text(
        f"👥 کل کاربران: {total_users}\n"
        f"✅ دعوت‌های تاییدشده: {total_confirmed}\n"
        f"💰 مجموع موجودی پرداخت‌نشده: {total_balance:.2f} تتر"
    )

# ===================== MAIN =====================
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("reflink", reflink_cmd))
    app.add_handler(CommandHandler("setwallet", setwallet_cmd))
    app.add_handler(CommandHandler("withdraw", withdraw_cmd))

    app.add_handler(CommandHandler("paid", admin_paid_cmd))
    app.add_handler(CommandHandler("pending", admin_pending_cmd))
    app.add_handler(CommandHandler("stats", admin_stats_cmd))

    app.add_handler(CallbackQueryHandler(check_membership_callback, pattern="^check_membership$"))
    app.add_handler(CallbackQueryHandler(balance_callback, pattern="^balance$"))
    app.add_handler(CallbackQueryHandler(reflink_callback, pattern="^reflink$"))
    app.add_handler(CallbackQueryHandler(withdraw_info_callback, pattern="^withdraw_info$"))

    logger.info("Bot started.")
    app.run_polling()

if __name__ == "__main__":
    main()
