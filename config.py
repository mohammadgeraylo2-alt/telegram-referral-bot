import os
from dotenv import load_dotenv

load_dotenv()

# توکن ربات (فقط از BotFather بگیر - نیازی به my.telegram.org نیست)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# آیدی عددی یا یوزرنیم کانالی که فایل‌ها توش ذخیره میشن (باید ربات ادمینش باشه)
# میتونه یوزرنیم باشه مثل "my_storage_channel" یا آیدی عددی منفی مثل -1001234567890
STORAGE_CHANNEL = os.getenv("STORAGE_CHANNEL", "")

# یوزرنیم ربات بدون @ (برای ساخت لینک دیپ‌لینک)
BOT_USERNAME = os.getenv("BOT_USERNAME", "")

# ادمین‌های اولیه (یوزرنیم بدون @) - بعدا از طریق دیتابیس هم قابل مدیریت هستن
DEFAULT_ADMIN_USERNAMES = ["AsAo04", "Justt_mmd"]

# کانال‌های جوین اجباری پیش‌فرض (یوزرنیم بدون @, عنوان نمایشی)
DEFAULT_FORCE_JOIN_CHANNELS = [
    ("InstaSaveXX", "📥 دانلودر اینستاگرام"),
    ("filmie_i", "🎬 فیلم و سریال"),
    ("yes_coine", "🎬  سریال"),
]

# لینک پیج اینستاگرام (برای دکمه فالو افتخاری توی جوین اجباری)
INSTAGRAM_PAGE_URL = "https://instagram.com/instasavexx_bot"

# نام و امضای ربات
BOT_TITLE = "🎬 سینما ولت | CinemaVault"
DB_PATH = os.getenv("DB_PATH", "bot.db")

# دقیقه‌های پاک شدن خودکار فایل ارسالی (0 = غیرفعال)
AUTO_DELETE_MINUTES = int(os.getenv("AUTO_DELETE_MINUTE
S", "0"))
