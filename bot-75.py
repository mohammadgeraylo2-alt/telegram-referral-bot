import asyncio
import logging

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import config
import db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("movie-bot")

# حالت بچ برای هر ادمین: user_id -> {"active": bool, "items": [...], "title": str|None}
batch_state: dict[int, dict] = {}


def is_admin_user(update: Update) -> bool:
    user = update.effective_user
    return bool(user and db.is_admin(user.username))


# ---------------------------------------------------------------------------
# جوین اجباری
# ---------------------------------------------------------------------------
async def get_not_joined_channels(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    not_joined = []
    for username, title in db.list_force_channels():
        try:
            member = await context.bot.get_chat_member(f"@{username}", user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                not_joined.append((username, title))
        except (BadRequest, Forbidden) as e:
            log.warning(f"خطا در بررسی عضویت کانال {username}: {e}")
            not_joined.append((username, title))
    return not_joined


def build_join_keyboard(not_joined, payload: str = ""):
    rows = []
    for username, title in not_joined:
        rows.append([InlineKeyboardButton(f"عضویت در {title}", url=f"https://t.me/{username}")])
    rows.append([InlineKeyboardButton("✅ عضو شدم", callback_data=f"check_{payload}")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_user(user.id, user.username)

    payload = context.args[0] if context.args else ""

    not_joined = await get_not_joined_channels(context, user.id)
    if not_joined:
        await update.message.reply_text(
            f"{config.BOT_TITLE}\n\n"
            "برای استفاده از ربات، اول باید توی کانال‌های زیر عضو بشی 👇",
            reply_markup=build_join_keyboard(not_joined, payload),
        )
        return

    if payload:
        await deliver_content(context, update.effective_chat.id, payload)
        return

    await update.message.reply_text(
        f"{config.BOT_TITLE}\n\n"
        "به ربات فیلم و سریال خوش اومدی 🍿\n"
        "کافیه لینکی که برات فرستاده شده رو باز کنی تا محتوا برات ارسال بشه."
    )


async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    payload = query.data[len("check_"):]
    not_joined = await get_not_joined_channels(context, query.from_user.id)

    if not_joined:
        await query.answer("هنوز توی همه کانال‌ها عضو نشدی!", show_alert=True)
        return

    await query.answer("عضویت تایید شد ✅")
    try:
        await query.message.delete()
    except Exception:
        pass

    if payload:
        await deliver_content(context, query.from_user.id, payload)
    else:
        await context.bot.send_message(query.from_user.id, f"{config.BOT_TITLE}\n\nخوش اومدی! 🍿")


async def deliver_content(context: ContextTypes.DEFAULT_TYPE, chat_id: int, code: str):
    batch = db.get_file_batch(code)
    if batch is None:
        await context.bot.send_message(chat_id, "❌ این لینک نامعتبره یا منقضی شده.")
        return

    if batch["title"]:
        await context.bot.send_message(chat_id, f"🎬 {batch['title']}")

    sent_message_ids = []
    for item in batch["items"]:
        try:
            sent = await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=item["chat_id"],
                message_id=item["message_id"],
            )
            sent_message_ids.append(sent.message_id)
        except Exception as e:
            log.warning(f"خطا در ارسال فایل: {e}")

    if config.AUTO_DELETE_MINUTES > 0 and sent_message_ids:
        notice = await context.bot.send_message(
            chat_id,
            f"⚠️ این فایل‌ها تا {config.AUTO_DELETE_MINUTES} دقیقه دیگه پاک میشن، ذخیره‌شون کن.",
        )
        asyncio.create_task(
            auto_delete_later(context, chat_id, sent_message_ids, notice.message_id)
        )


async def auto_delete_later(context, chat_id: int, message_ids: list, notice_id: int):
    await asyncio.sleep(config.AUTO_DELETE_MINUTES * 60)
    for mid in message_ids:
        try:
            await context.bot.delete_message(chat_id, mid)
        except Exception:
            pass
    try:
        await context.bot.delete_message(chat_id, notice_id)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# پنل ادمین - ساخت لینک (تکی یا بچ)
# ---------------------------------------------------------------------------
async def newbatch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return
    batch_state[update.effective_user.id] = {"active": True, "items": [], "title": None}
    await update.message.reply_text(
        "📦 حالت بچ شروع شد.\n"
        "حالا فیلم/سریال‌هاتو یکی‌یکی برام فوروارد کن.\n"
        "وقتی تموم شد بنویس: /donebatch\n"
        "برای لغو: /cancelbatch"
    )


async def cancelbatch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return
    batch_state.pop(update.effective_user.id, None)
    await update.message.reply_text("❌ حالت بچ لغو شد.")


async def donebatch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return
    state = batch_state.get(update.effective_user.id)
    if not state or not state["items"]:
        await update.message.reply_text("هیچ فایلی توی این بچ نیست. اول چند فایل فوروارد کن.")
        return

    code = db.save_file_batch(state["items"], state.get("title") or "", update.effective_user.id)
    link = f"https://t.me/{config.BOT_USERNAME}?start={code}"
    batch_state.pop(update.effective_user.id, None)

    await update.message.reply_text(
        f"✅ لینک بچ ساخته شد ({len(state['items'])} فایل):\n{link}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔗 اشتراک‌گذاری", url=f"https://t.me/share/url?url={link}")]]
        ),
    )


async def admin_media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return

    message = update.message
    stored = await message.copy(chat_id=config.STORAGE_CHANNEL)
    item = {"chat_id": config.STORAGE_CHANNEL, "message_id": stored.message_id}

    state = batch_state.get(update.effective_user.id)
    if state and state.get("active"):
        state["items"].append(item)
        await message.reply_text(
            f"➕ اضافه شد ({len(state['items'])} فایل تا الان). "
            "برای فایل بعدی فوروارد کن یا /donebatch رو بزن."
        )
        return

    # حالت تکی: بلافاصله لینک بساز
    code = db.save_file_batch([item], "", update.effective_user.id)
    link = f"https://t.me/{config.BOT_USERNAME}?start={code}"
    await message.reply_text(
        f"✅ لینک ساخته شد:\n{link}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔗 اشتراک‌گذاری", url=f"https://t.me/share/url?url={link}")]]
        ),
    )


# ---------------------------------------------------------------------------
# مدیریت کانال‌های جوین اجباری
# ---------------------------------------------------------------------------
async def addchannel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("فرمت درست: /addchannel یوزرنیم عنوان\nمثال: /addchannel MyChannel کانال من")
        return
    username, title = parts[1].lstrip("@"), parts[2]
    db.add_force_channel(username, title)
    await update.message.reply_text(f"✅ کانال {username} به لیست جوین اجباری اضافه شد.")


async def delchannel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("فرمت درست: /delchannel یوزرنیم")
        return
    username = parts[1].lstrip("@")
    db.remove_force_channel(username)
    await update.message.reply_text(f"✅ کانال {username} حذف شد.")


async def channels_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return
    chans = db.list_force_channels()
    if not chans:
        text = "هیچ کانال جوین اجباری‌ای تنظیم نشده."
    else:
        text = "📋 کانال‌های جوین اجباری:\n" + "\n".join(f"• @{u} - {t}" for u, t in chans)
    await update.message.reply_text(text)


# ---------------------------------------------------------------------------
# مدیریت ادمین‌ها
# ---------------------------------------------------------------------------
async def addadmin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("فرمت درست: /addadmin یوزرنیم")
        return
    username = parts[1].lstrip("@")
    db.add_admin(username)
    await update.message.reply_text(f"✅ @{username} به ادمین‌ها اضافه شد.")


async def deladmin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("فرمت درست: /deladmin یوزرنیم")
        return
    username = parts[1].lstrip("@")
    db.remove_admin(username)
    await update.message.reply_text(f"✅ @{username} از ادمین‌ها حذف شد.")


async def admins_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return
    admins = db.list_admins()
    await update.message.reply_text("👤 ادمین‌ها:\n" + "\n".join(f"@{a}" for a in admins))


# ---------------------------------------------------------------------------
# آمار و همگانی
# ---------------------------------------------------------------------------
async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return
    await update.message.reply_text(
        f"📊 آمار ربات\n\n"
        f"👤 کاربران: {db.get_user_count()}\n"
        f"🎬 لینک‌های ساخته‌شده: {db.get_file_count()}"
    )


async def broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("روی پیامی که میخوای همگانی بشه ریپلای کن و بنویس /broadcast")
        return

    users = db.get_all_user_ids()
    status = await update.message.reply_text(f"در حال ارسال به {len(users)} کاربر...")
    sent, failed = 0, 0
    for uid in users:
        try:
            await update.message.reply_to_message.copy(chat_id=uid)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # جلوگیری از فلود
    await status.edit_text(f"✅ ارسال شد: {sent}\n❌ ناموفق: {failed}")


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return
    await update.message.reply_text(
        "🛠 راهنمای پنل ادمین\n\n"
        "• فوروارد یه فیلم = ساخت لینک تکی فوری\n"
        "• /newbatch - شروع ساخت لینک برای چند فایل با هم\n"
        "• /donebatch - پایان بچ و ساخت لینک\n"
        "• /cancelbatch - لغو بچ\n"
        "• /addchannel یوزرنیم عنوان - افزودن کانال جوین اجباری\n"
        "• /delchannel یوزرنیم - حذف کانال جوین اجباری\n"
        "• /channels - لیست کانال‌های جوین اجباری\n"
        "• /addadmin یوزرنیم - افزودن ادمین\n"
        "• /deladmin یوزرنیم - حذف ادمین\n"
        "• /admins - لیست ادمین‌ها\n"
        "• /stats - آمار ربات\n"
        "• /broadcast - (ریپلای روی یه پیام) ارسال همگانی"
    )


def main():
    db.init_db()

    app: Application = ApplicationBuilder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("newbatch", newbatch_handler))
    app.add_handler(CommandHandler("cancelbatch", cancelbatch_handler))
    app.add_handler(CommandHandler("donebatch", donebatch_handler))
    app.add_handler(CommandHandler("addchannel", addchannel_handler))
    app.add_handler(CommandHandler("delchannel", delchannel_handler))
    app.add_handler(CommandHandler("channels", channels_handler))
    app.add_handler(CommandHandler("addadmin", addadmin_handler))
    app.add_handler(CommandHandler("deladmin", deladmin_handler))
    app.add_handler(CommandHandler("admins", admins_handler))
    app.add_handler(CommandHandler("stats", stats_handler))
    app.add_handler(CommandHandler("broadcast", broadcast_handler))
    app.add_handler(CommandHandler("help", help_handler))

    app.add_handler(CallbackQueryHandler(check_join_callback, pattern=r"^check_"))

    media_filter = (
        filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.ANIMATION
    )
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & media_filter, admin_media_handler))

    log.info("ربات در حال اجراست...")
    app.run_polling()


if __name__ == "__main__":
    main()
