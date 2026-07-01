import sqlite3
import json
import time
import random
import string
from config import DB_PATH, DEFAULT_ADMIN_USERNAMES, DEFAULT_FORCE_JOIN_CHANNELS

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row


def init_db():
    cur = _conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            joined_at INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS files (
            code TEXT PRIMARY KEY,
            items TEXT,
            title TEXT,
            auto_delete_seconds INTEGER,
            created_by INTEGER,
            created_at INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            username TEXT PRIMARY KEY
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS force_channels (
            username TEXT PRIMARY KEY,
            title TEXT
        )
    """)
    _conn.commit()

    # migration: اگه دیتابیس قبلاً بدون ستون auto_delete_seconds ساخته شده باشه
    cur.execute("PRAGMA table_info(files)")
    existing_cols = {row["name"] for row in cur.fetchall()}
    if "auto_delete_seconds" not in existing_cols:
        cur.execute("ALTER TABLE files ADD COLUMN auto_delete_seconds INTEGER")
        _conn.commit()
    cur.execute("SELECT COUNT(*) as c FROM admins")
    if cur.fetchone()["c"] == 0:
        for uname in DEFAULT_ADMIN_USERNAMES:
            cur.execute("INSERT OR IGNORE INTO admins (username) VALUES (?)", (uname.lower(),))
    cur.execute("SELECT COUNT(*) as c FROM force_channels")
    if cur.fetchone()["c"] == 0:
        for uname, title in DEFAULT_FORCE_JOIN_CHANNELS:
            cur.execute("INSERT OR IGNORE INTO force_channels (username, title) VALUES (?, ?)", (uname, title))
    _conn.commit()


# ---------- کاربران ----------
def add_user(user_id: int, username: str | None):
    cur = _conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, username, joined_at) VALUES (?, ?, ?)",
        (user_id, username or "", int(time.time())),
    )
    _conn.commit()


def get_all_user_ids():
    cur = _conn.cursor()
    cur.execute("SELECT user_id FROM users")
    return [row["user_id"] for row in cur.fetchall()]


def get_user_count():
    cur = _conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM users")
    return cur.fetchone()["c"]


# ---------- ادمین‌ها ----------
def is_admin(username: str | None) -> bool:
    if not username:
        return False
    cur = _conn.cursor()
    cur.execute("SELECT 1 FROM admins WHERE username = ?", (username.lower(),))
    return cur.fetchone() is not None


def add_admin(username: str):
    cur = _conn.cursor()
    cur.execute("INSERT OR IGNORE INTO admins (username) VALUES (?)", (username.lower(),))
    _conn.commit()


def remove_admin(username: str):
    cur = _conn.cursor()
    cur.execute("DELETE FROM admins WHERE username = ?", (username.lower(),))
    _conn.commit()


def list_admins():
    cur = _conn.cursor()
    cur.execute("SELECT username FROM admins")
    return [row["username"] for row in cur.fetchall()]


# ---------- کانال‌های جوین اجباری ----------
def list_force_channels():
    cur = _conn.cursor()
    cur.execute("SELECT username, title FROM force_channels")
    return [(row["username"], row["title"]) for row in cur.fetchall()]


def add_force_channel(username: str, title: str):
    cur = _conn.cursor()
    cur.execute("INSERT OR REPLACE INTO force_channels (username, title) VALUES (?, ?)", (username, title))
    _conn.commit()


def remove_force_channel(username: str):
    cur = _conn.cursor()
    cur.execute("DELETE FROM force_channels WHERE username = ?", (username,))
    _conn.commit()


# ---------- فایل‌ها / لینک‌ها ----------
def _gen_code(length=8):
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def save_file_batch(items: list, title: str, created_by: int, auto_delete_seconds: int | None = None) -> str:
    """items: لیستی از دیکشنری {chat_id, message_id}"""
    code = _gen_code()
    cur = _conn.cursor()
    while True:
        cur.execute("SELECT 1 FROM files WHERE code = ?", (code,))
        if cur.fetchone() is None:
            break
        code = _gen_code()
    cur.execute(
        "INSERT INTO files (code, items, title, auto_delete_seconds, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (code, json.dumps(items), title, auto_delete_seconds, created_by, int(time.time())),
    )
    _conn.commit()
    return code


def get_file_batch(code: str):
    cur = _conn.cursor()
    cur.execute("SELECT items, title, auto_delete_seconds FROM files WHERE code = ?", (code,))
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "items": json.loads(row["items"]),
        "title": row["title"],
        "auto_delete_seconds": row["auto_delete_seconds"],
    }


def get_file_count():
    cur = _conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM files")
    return cur.fetchone()["c"]


def delete_file_batch(code: str) -> bool:
    """حذف یه لینک/بچ با کد مشخص. True برمیگردونه اگه واقعاً چیزی حذف شده باشه."""
    cur = _conn.cursor()
    cur.execute("DELETE FROM files WHERE code = ?", (code,))
    _conn.commit()
    return cur.rowcount > 0


def update_file_batch_title(code: str, title: str) -> bool:
    """آپدیت کپشن/تایتل یه لینک از قبل ساخته‌شده. True برمیگردونه اگه لینک وجود داشته باشه."""
    cur = _conn.cursor()
    cur.execute("UPDATE files SET title = ? WHERE code = ?", (title, code))
    _conn.commit()
    return cur.rowcount > 0


def update_file_batch_auto_delete(code: str, auto_delete_seconds: int) -> bool:
    """آپدیت مدت حذف خودکار یه لینک از قبل ساخته‌شده. True برمیگردونه اگه لینک وجود داشته باشه."""
    cur = _conn.cursor()
    cur.execute("UPDATE files SET auto_delete_seconds = ? WHERE code = ?", (auto_delete_seconds, code))
    _conn.commit()
    return cur.rowcount > 0
