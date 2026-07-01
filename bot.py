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

# فایل تکی که منتظر انتخاب کپشنه: user_id -> item {"chat_id", "message_id"}
pending_single_items: dict[int, dict] = {}

# آیتم‌های بچی که منتظر انتخاب کپشنه: user_id -> [items]
pending_batch_items: dict[int, list] = {}

# منتظر متن کپشن از ادمین: user_id -> {"mode": "single"|"batch"}
pending_caption_input: dict[int, dict] = {}

# اطلاعات نهایی لینک بین مرحله‌ی کپشن و مرحله‌ی حذف خودکار: user_id -> {"mode", "item"/"items", "title"}
pending_link_data: dict[int, dict] = {}

# ادمین‌هایی که منتظر عدد دلخواه (ثانیه) برای حذف خودکار هستن
pending_duration_input: set[int] = set()

# منتظر متن جدید پروفایل ربات از ادمین: user_id -> "name"|"description"|"short_description"
pending_profile_edit: dict[int, str] = {}


def is_admin_user(update: Update) -> bool:
    user = update.effective_user
    return bool(user and db.is_admin(user.username))


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} ثانیه"
    minutes, rem_sec = divmod(seconds, 60)
    if minutes < 60:
        if rem_sec == 0:
            return f"{minutes} دقیقه"
        return f"{minutes} دقیقه و {rem_sec} ثانیه"
    hours, rem_min = divmod(minutes, 60)
    if rem_min == 0:
        return f"{hours} ساعت"
    return f"{hours} ساعت و {rem_min} دقیقه"


def build_duration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 بدون حذف خودکار", callback_data="dur_0")],
        [
            InlineKeyboardButton("⏱ 30 ثانیه", callback_data="dur_30"),
            InlineKeyboardButton("⏱ 1 دقیقه", callback_data="dur_60"),
        ],
        [
            InlineKeyboardButton("⏱ 5 دقیقه", callback_data="dur_300"),
            InlineKeyboardButton("⏱ 1 ساعت", callback_data="dur_3600"),
        ],
        [InlineKeyboardButton("✏️ عدد دلخواه (ثانیه)", callback_data="dur_custom")],
    ])


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

    auto_delete_seconds = batch.get("auto_delete_seconds")
    if auto_delete_seconds is None:
        # لینک‌های قدیمی که این ستون رو نداشتن -> برو سراغ مقدار پیش‌فرض کانفیگ
        auto_delete_seconds = config.AUTO_DELETE_MINUTES * 60

    if auto_delete_seconds and auto_delete_seconds > 0 and sent_message_ids:
        notice = await context.bot.send_message(
            chat_id,
            f"⚠️ این فایل تا {format_duration(auto_delete_seconds)} دیگه پاک میشه، "
            "حتماً با «Save Message» (سیو مسیج) ذخیره‌ش کن.",
        )
        asyncio.create_task(
            auto_delete_later(context, chat_id, sent_message_ids, notice.message_id, auto_delete_seconds)
        )


async def auto_delete_later(context, chat_id: int, message_ids: list, notice_id: int, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
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
    user_id = update.effective_user.id
    state = batch_state.get(user_id)
    if not state or not state["items"]:
        await update.message.reply_text("هیچ فایلی توی این بچ نیست. اول چند فایل فوروارد کن.")
        return

    # اگه کپشن قبلاً با /setcaption تنظیم شده باشه، مستقیم لینک بساز
    if state.get("title"):
        await finalize_batch_link(context, update.effective_chat.id, user_id, state["items"], state["title"])
        batch_state.pop(user_id, None)
        return

    # وگرنه بپرس که کپشن میخواد یا نه
    pending_batch_items[user_id] = state["items"]
    batch_state.pop(user_id, None)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 بدون کپشن", callback_data="capbatch_skip")],
        [InlineKeyboardButton("✏️ کپشن دلخواه", callback_data="capbatch_custom")],
    ])
    await update.message.reply_text("می‌خوای برای این بچ کپشن بزاری؟", reply_markup=keyboard)


async def finalize_single_link(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, item: dict, title: str, auto_delete_seconds: int = 0
):
    code = db.save_file_batch([item], title, user_id, auto_delete_seconds)
    link = f"https://t.me/{config.BOT_USERNAME}?start={code}"
    extra = f"\n⏱ حذف خودکار: {format_duration(auto_delete_seconds)}" if auto_delete_seconds else ""
    await context.bot.send_message(
        chat_id,
        f"✅ لینک ساخته شد:\n{link}{extra}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔗 اشتراک‌گذاری", url=f"https://t.me/share/url?url={link}")]]
        ),
    )


async def finalize_batch_link(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, items: list, title: str, auto_delete_seconds: int = 0
):
    code = db.save_file_batch(items, title, user_id, auto_delete_seconds)
    link = f"https://t.me/{config.BOT_USERNAME}?start={code}"
    extra = f"\n⏱ حذف خودکار: {format_duration(auto_delete_seconds)}" if auto_delete_seconds else ""
    await context.bot.send_message(
        chat_id,
        f"✅ لینک بچ ساخته شد ({len(items)} فایل):\n{link}{extra}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔗 اشتراک‌گذاری", url=f"https://t.me/share/url?url={link}")]]
        ),
    )


async def finalize_pending_link(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, pending: dict, auto_delete_seconds: int):
    if pending["mode"] == "single":
        await finalize_single_link(context, chat_id, user_id, pending["item"], pending["title"], auto_delete_seconds)
    else:
        await finalize_batch_link(context, chat_id, user_id, pending["items"], pending["title"], auto_delete_seconds)


async def admin_media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return

    message = update.message
    stored = await message.copy(chat_id=config.STORAGE_CHANNEL)
    item = {"chat_id": config.STORAGE_CHANNEL, "message_id": stored.message_id}

    user_id = update.effective_user.id
    state = batch_state.get(user_id)
    if state and state.get("active"):
        state["items"].append(item)
        await message.reply_text(
            f"➕ اضافه شد ({len(state['items'])} فایل تا الان). "
            "برای فایل بعدی فوروارد کن یا /donebatch رو بزن.\n"
            "برای تنظیم کپشن این بچ: /setcaption متن دلخواه"
        )
        return

    # حالت تکی: قبل از ساخت لینک، درباره‌ی کپشن بپرس
    pending_single_items[user_id] = item
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 بدون کپشن", callback_data="capsingle_skip")],
        [InlineKeyboardButton("✏️ کپشن دلخواه", callback_data="capsingle_custom")],
    ])
    await message.reply_text("می‌خوای برای این فایل کپشن بزاری؟", reply_markup=keyboard)


async def caption_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    await query.answer()

    if data == "capsingle_skip":
        item = pending_single_items.pop(user_id, None)
        if not item:
            await query.edit_message_text("⛔️ این درخواست منقضی شده، دوباره فایل رو بفرست.")
            return
        pending_link_data[user_id] = {"mode": "single", "item": item, "title": ""}
        await query.edit_message_text(
            "این فایل بعد از چند وقت خودکار پاک بشه؟",
            reply_markup=build_duration_keyboard(),
        )

    elif data == "capsingle_custom":
        if user_id not in pending_single_items:
            await query.edit_message_text("⛔️ این درخواست منقضی شده، دوباره فایل رو بفرست.")
            return
        pending_caption_input[user_id] = {"mode": "single"}
        await query.edit_message_text("✏️ کپشن مورد نظرت رو برام بفرست:")

    elif data == "capbatch_skip":
        items = pending_batch_items.pop(user_id, None)
        if not items:
            await query.edit_message_text("⛔️ این درخواست منقضی شده.")
            return
        pending_link_data[user_id] = {"mode": "batch", "items": items, "title": ""}
        await query.edit_message_text(
            "این بچ بعد از چند وقت خودکار پاک بشه؟",
            reply_markup=build_duration_keyboard(),
        )

    elif data == "capbatch_custom":
        if user_id not in pending_batch_items:
            await query.edit_message_text("⛔️ این درخواست منقضی شده.")
            return
        pending_caption_input[user_id] = {"mode": "batch"}
        await query.edit_message_text("✏️ کپشن مورد نظرت رو برام بفرست:")


async def duration_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    await query.answer()

    pending = pending_link_data.get(user_id)
    if not pending:
        await query.edit_message_text("⛔️ این درخواست منقضی شده.")
        return

    if data == "dur_custom":
        pending_duration_input.add(user_id)
        await query.edit_message_text("✏️ چند ثانیه دیگه پاک بشه؟ فقط یه عدد بفرست (مثلاً 45):")
        return

    seconds = int(data.split("_", 1)[1])
    pending_link_data.pop(user_id, None)
    await query.edit_message_text("⏳ در حال ساخت لینک...")
    await finalize_pending_link(context, query.message.chat_id, user_id, pending, seconds)


async def setcaption_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return
    user_id = update.effective_user.id
    state = batch_state.get(user_id)

    # حالت اول: توی یه بچ فعال هستیم -> کل متن بعد از دستور، کپشن بچه
    if state and state.get("active"):
        parts = update.message.text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("فرمت درست (توی بچ فعال): /setcaption متن کپشن")
            return
        state["title"] = parts[1]
        await update.message.reply_text(f"✅ کپشن این بچ تنظیم شد:\n{parts[1]}")
        return

    # حالت دوم: ویرایش کپشن یه لینک از قبل ساخته‌شده -> /setcaption کد متن
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text(
            "برای تنظیم کپشن یه لینک ساخته‌شده:\n"
            "/setcaption کد متن_کپشن\n\n"
            "یا اول /newbatch رو بزن، فایل‌ها رو بفرست و بعد /setcaption متن رو بزن."
        )
        return

    code, caption = parts[1].strip(), parts[2]
    if "start=" in code:
        code = code.split("start=")[-1]
    ok = db.update_file_batch_title(code, caption)
    if ok:
        await update.message.reply_text(f"✅ کپشن لینک با کد {code} بروزرسانی شد:\n{caption}")
    else:
        await update.message.reply_text("❌ لینکی با این کد پیدا نشد.")


async def deletefile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("فرمت درست: /deletefile کد (یا کل لینک)\nمثال: /deletefile Ab3dEfGh")
        return
    code = parts[1].strip()
    if "start=" in code:
        code = code.split("start=")[-1]
    ok = db.delete_file_batch(code)
    if ok:
        await update.message.reply_text(f"✅ لینک با کد {code} حذف شد.")
    else:
        await update.message.reply_text("❌ لینکی با این کد پیدا نشد.")


async def setautodelete_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[2].strip().isdigit():
        await update.message.reply_text(
            "فرمت درست: /setautodelete کد ثانیه\n"
            "مثال: /setautodelete Ab3dEfGh 30\n"
            "برای غیرفعال کردن حذف خودکار، عدد 0 بفرست."
        )
        return
    code = parts[1].strip()
    if "start=" in code:
        code = code.split("start=")[-1]
    seconds = int(parts[2].strip())
    ok = db.update_file_batch_auto_delete(code, seconds)
    if ok:
        if seconds > 0:
            await update.message.reply_text(f"✅ حذف خودکار لینک {code} روی {format_duration(seconds)} تنظیم شد.")
        else:
            await update.message.reply_text(f"✅ حذف خودکار لینک {code} غیرفعال شد.")
    else:
        await update.message.reply_text("❌ لینکی با این کد پیدا نشد.")


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
# پروفایل ربات (نام / بیو / توضیح کوتاه)
# ---------------------------------------------------------------------------
async def botprofile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ تغییر نام ربات", callback_data="profile_setname")],
        [InlineKeyboardButton("✏️ تغییر بیو (توضیحات کامل)", callback_data="profile_setdesc")],
        [InlineKeyboardButton("✏️ تغییر توضیح کوتاه", callback_data="profile_setshort")],
    ])
    await update.message.reply_text(
        "🤖 تنظیمات پروفایل ربات\n\n"
        "کدوم مورد رو می‌خوای تغییر بدی؟\n\n"
        "⚠️ توجه: عکس پروفایل ربات از طریق کد قابل تغییر نیست، تلگرام برای این کار "
        "API‌ای در اختیار خود ربات نمیزاره. برای تغییر عکس باید به @BotFather مراجعه کنی.",
        reply_markup=keyboard,
    )


async def profile_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    field_map = {
        "profile_setname": ("name", "نام ربات"),
        "profile_setdesc": ("description", "بیو (توضیحات کامل)"),
        "profile_setshort": ("short_description", "توضیح کوتاه"),
    }
    field, label = field_map[query.data]
    pending_profile_edit[user_id] = field
    await query.edit_message_text(f"✏️ {label} جدید رو برام بفرست:")


async def admin_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """متن‌های ساده‌ای که ادمین بعد از یه درخواست معلق (کپشن، مدت حذف یا پروفایل) میفرسته."""
    if not is_admin_user(update):
        return
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if user_id in pending_duration_input:
        pending_duration_input.discard(user_id)
        pending = pending_link_data.pop(user_id, None)
        if not pending:
            await update.message.reply_text("⛔️ این درخواست منقضی شده.")
            return
        if not text.isdigit():
            await update.message.reply_text(
                "❌ باید فقط یه عدد (ثانیه) بفرستی. دوباره امتحان کن:",
            )
            pending_link_data[user_id] = pending
            pending_duration_input.add(user_id)
            return
        await finalize_pending_link(context, update.effective_chat.id, user_id, pending, int(text))
        return

    if user_id in pending_caption_input:
        mode = pending_caption_input.pop(user_id)["mode"]
        if mode == "single":
            item = pending_single_items.pop(user_id, None)
            if item:
                pending_link_data[user_id] = {"mode": "single", "item": item, "title": text}
                await update.message.reply_text(
                    "این فایل بعد از چند وقت خودکار پاک بشه؟",
                    reply_markup=build_duration_keyboard(),
                )
            else:
                await update.message.reply_text("⛔️ این درخواست منقضی شده، دوباره فایل رو بفرست.")
        elif mode == "batch":
            items = pending_batch_items.pop(user_id, None)
            if items:
                pending_link_data[user_id] = {"mode": "batch", "items": items, "title": text}
                await update.message.reply_text(
                    "این بچ بعد از چند وقت خودکار پاک بشه؟",
                    reply_markup=build_duration_keyboard(),
                )
            else:
                await update.message.reply_text("⛔️ این درخواست منقضی شده.")
        return

    if user_id in pending_profile_edit:
        field = pending_profile_edit.pop(user_id)
        try:
            if field == "name":
                await context.bot.set_my_name(text)
                await update.message.reply_text(f"✅ نام ربات به «{text}» تغییر کرد.")
            elif field == "description":
                await context.bot.set_my_description(text)
                await update.message.reply_text("✅ بیوی ربات بروزرسانی شد.")
            elif field == "short_description":
                await context.bot.set_my_short_description(text)
                await update.message.reply_text("✅ توضیح کوتاه ربات بروزرسانی شد.")
        except Exception as e:
            log.warning(f"خطا در تغییر پروفایل ربات: {e}")
            await update.message.reply_text(f"❌ خطا در اعمال تغییر: {e}")
        return


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
        "• فوروارد یه فیلم = ساخت لینک تکی (کپشن و حذف خودکار رو باهات چک میکنه)\n"
        "• /newbatch - شروع ساخت لینک برای چند فایل با هم\n"
        "• /donebatch - پایان بچ و ساخت لینک\n"
        "• /cancelbatch - لغو بچ\n"
        "• /setcaption متن - تنظیم کپشن بچ فعال\n"
        "• /setcaption کد متن - ویرایش کپشن یه لینک از قبل ساخته‌شده\n"
        "• /setautodelete کد ثانیه - ویرایش زمان حذف خودکار یه لینک (0 = غیرفعال)\n"
        "• /deletefile کد - حذف یه لینک ساخته‌شده\n"
        "• /addchannel یوزرنیم عنوان - افزودن کانال جوین اجباری\n"
        "• /delchannel یوزرنیم - حذف کانال جوین اجباری\n"
        "• /channels - لیست کانال‌های جوین اجباری\n"
        "• /addadmin یوزرنیم - افزودن ادمین\n"
        "• /deladmin یوزرنیم - حذف ادمین\n"
        "• /admins - لیست ادمین‌ها\n"
        "• /botprofile - تغییر نام/بیو/توضیح کوتاه ربات\n"
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
    app.add_handler(CommandHandler("setcaption", setcaption_handler))
    app.add_handler(CommandHandler("setautodelete", setautodelete_handler))
    app.add_handler(CommandHandler("deletefile", deletefile_handler))
    app.add_handler(CommandHandler("addchannel", addchannel_handler))
    app.add_handler(CommandHandler("delchannel", delchannel_handler))
    app.add_handler(CommandHandler("channels", channels_handler))
    app.add_handler(CommandHandler("addadmin", addadmin_handler))
    app.add_handler(CommandHandler("deladmin", deladmin_handler))
    app.add_handler(CommandHandler("admins", admins_handler))
    app.add_handler(CommandHandler("botprofile", botprofile_handler))
    app.add_handler(CommandHandler("stats", stats_handler))
    app.add_handler(CommandHandler("broadcast", broadcast_handler))
    app.add_handler(CommandHandler("help", help_handler))

    app.add_handler(CallbackQueryHandler(check_join_callback, pattern=r"^check_"))
    app.add_handler(CallbackQueryHandler(caption_choice_callback, pattern=r"^cap(single|batch)_"))
    app.add_handler(CallbackQueryHandler(duration_choice_callback, pattern=r"^dur_"))
    app.add_handler(CallbackQueryHandler(profile_choice_callback, pattern=r"^profile_"))

    media_filter = (
        filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.ANIMATION
    )
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & media_filter, admin_media_handler))
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, admin_text_handler)
    )

    log.info("ربات در حال اجراست...")
    app.run_polling()


if __name__ == "__main__":
    main()
