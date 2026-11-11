
import os
import re
import html
import sqlite3
import asyncio
import logging
from datetime import datetime, timezone

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler


# ==========================================
# CONFIGURATION
# ==========================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "./bot.db")

ADMIN_IDS = {
    int(item.strip())
    for item in os.getenv("ADMIN_IDS", "").split(",")
    if item.strip().isdigit()
}

MIN_INTERVAL = 5
MAX_INTERVAL = 360
DEFAULT_INTERVAL = 15

INSTAGRAM_APP_ID = "936619743392459"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


# ==========================================
# DATABASE
# ==========================================

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_user_id INTEGER PRIMARY KEY,
                target_username TEXT,
                last_known_status TEXT DEFAULT 'UNKNOWN',
                last_checked_at TEXT,
                check_interval_minutes INTEGER DEFAULT 15,
                consecutive_errors INTEGER DEFAULT 0
            )
        """)

        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }

        required_columns = {
            "target_username": "TEXT",
            "last_known_status": "TEXT DEFAULT 'UNKNOWN'",
            "last_checked_at": "TEXT",
            "check_interval_minutes": "INTEGER DEFAULT 15",
            "consecutive_errors": "INTEGER DEFAULT 0",
        }

        for column, definition in required_columns.items():
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE users ADD COLUMN {column} {definition}"
                )

        conn.commit()


def db_get_user(user_id):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE telegram_user_id = ?",
            (user_id,),
        ).fetchone()


def db_get_all_users():
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM users ORDER BY telegram_user_id"
        ).fetchall()


def db_upsert_user(user_id, **fields):
    allowed = {
        "target_username",
        "last_known_status",
        "last_checked_at",
        "check_interval_minutes",
        "consecutive_errors",
    }

    fields = {
        key: value
        for key, value in fields.items()
        if key in allowed
    }

    with db_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (telegram_user_id) VALUES (?)",
            (user_id,),
        )

        if fields:
            assignments = ", ".join(
                f"{column} = ?" for column in fields
            )
            values = list(fields.values()) + [user_id]

            conn.execute(
                f"""
                UPDATE users
                SET {assignments}
                WHERE telegram_user_id = ?
                """,
                values,
            )

        conn.commit()


def db_delete_target(user_id):
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE users
            SET target_username = NULL,
                last_known_status = 'UNKNOWN',
                last_checked_at = NULL,
                consecutive_errors = 0
            WHERE telegram_user_id = ?
            """,
            (user_id,),
        )
        conn.commit()


# ==========================================
# HELPERS
# ==========================================

def esc(value):
    return html.escape(str(value))


def valid_username(username):
    username = username.strip().lstrip("@")

    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
        return None

    return username


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def emoji_for(status):
    if status == "ACTIVE":
        return "🟢"
    if status == "DEACTIVATED":
        return "🔴"
    return "⚪"


def is_admin(user_id):
    return user_id in ADMIN_IDS


async def send_text(bot, chat_id, message):
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Could not send message to %s", chat_id)


def get_interval(row):
    value = row["check_interval_minutes"] or DEFAULT_INTERVAL
    return max(MIN_INTERVAL, min(MAX_INTERVAL, int(value)))


# ==========================================
# INSTAGRAM STATUS CHECK
# ==========================================

async def get_instagram_status(username):
    """
    Returns:
        ("ACTIVE", reason)
        ("DEACTIVATED", reason)
        ("UNKNOWN", reason)

    UNKNOWN is used whenever Instagram does not provide
    enough reliable evidence to determine the account status.
    """

    username = username.strip().lstrip("@")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/json;q=0.9,*/*;q=0.8"
        ),
        "X-IG-App-ID": INSTAGRAM_APP_ID,
        "Referer": "https://www.instagram.com/",
    }

    timeout = httpx.Timeout(15.0, connect=10.0)

    async with httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
    ) as client:

        # First method: Instagram public JSON endpoint
        try:
            response = await client.get(
                "https://www.instagram.com/api/v1/users/web_profile_info/",
                params={"username": username},
            )

            if response.status_code == 200:
                try:
                    data = response.json()
                    user = (
                        data.get("data", {}).get("user")
                        if isinstance(data, dict)
                        else None
                    )

                    if isinstance(user, dict):
                        returned_username = (
                            user.get("username", "") or ""
                        ).lower()

                        if returned_username == username.lower():
                            return (
                                "ACTIVE",
                                "Instagram JSON endpoint found the profile",
                            )

                        if returned_username:
                            return (
                                "UNKNOWN",
                                "Instagram returned a different username",
                            )

                except Exception:
                    pass

            elif response.status_code in (401, 403, 429):
                logger.info(
                    "Instagram JSON endpoint restricted: HTTP %s",
                    response.status_code,
                )

        except Exception as error:
            logger.info("Instagram JSON request failed: %s", error)

        # Second method: public profile HTML
        try:
            response = await client.get(
                f"https://www.instagram.com/{username}/"
            )

            page = response.text.lower()

            unavailable_phrases = [
                "sorry, this page isn't available",
                "the link you followed may be broken",
                "page may have been removed",
                "sorry, this page is not available",
            ]

            if (
                response.status_code == 404
                and any(phrase in page for phrase in unavailable_phrases)
            ):
                return (
                    "DEACTIVATED",
                    "Instagram returned a profile-not-found page",
                )

            if response.status_code in (401, 403, 429, 503):
                return (
                    "UNKNOWN",
                    f"Instagram restricted the request: HTTP {response.status_code}",
                )

            if response.status_code == 200:
                escaped_username = username.lower()

                profile_markers = [
                    f'"username":"{escaped_username}"',
                    f'"username": "{escaped_username}"',
                    f'instagram.com/{escaped_username}/',
                    f'content="https://www.instagram.com/{escaped_username}/"',
                ]

                if any(marker in page for marker in profile_markers):
                    return (
                        "ACTIVE",
                        "Profile markers found in Instagram HTML",
                    )

                if any(phrase in page for phrase in unavailable_phrases):
                    return (
                        "UNKNOWN",
                        "Page contains an unavailable message but status is ambiguous",
                    )

            return (
                "UNKNOWN",
                f"Could not verify profile; HTML HTTP {response.status_code}",
            )

        except Exception as error:
            return (
                "UNKNOWN",
                f"Instagram HTML request failed: {error}",
            )


# ==========================================
# SCHEDULED CHECKING
# ==========================================

def schedule_user(app, user_id, interval):
    job_id = f"instagram_{user_id}"

    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    scheduler.add_job(
        scheduled_check,
        trigger="interval",
        minutes=interval,
        id=job_id,
        args=[app, user_id],
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        replace_existing=True,
    )


def remove_user_schedule(user_id):
    job_id = f"instagram_{user_id}"

    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


async def scheduled_check(app, user_id):
    row = db_get_user(user_id)

    if row is None or not row["target_username"]:
        remove_user_schedule(user_id)
        return

    username = row["target_username"]
    old_status = row["last_known_status"] or "UNKNOWN"

    new_status, reason = await get_instagram_status(username)

    logger.info(
        "[Instagram Check] @%s: %s | Reason: %s",
        username,
        new_status,
        reason,
    )

    if new_status == "UNKNOWN":
        db_upsert_user(
            user_id,
            last_checked_at=now_utc(),
            consecutive_errors=(row["consecutive_errors"] or 0) + 1,
        )
        return

    db_upsert_user(
        user_id,
        last_known_status=new_status,
        last_checked_at=now_utc(),
        consecutive_errors=0,
    )

    # Do not notify on the first successful check.
    # Save it as the initial baseline instead.
    if old_status == "UNKNOWN":
        return

    # Notify only when a confirmed status changes.
    if new_status != old_status:
        message = (
            "🔔 <b>Instagram Status Changed</b>\n\n"
            f"👤 Profile: <b>@{esc(username)}</b>\n"
            f"Previous: {emoji_for(old_status)} {esc(old_status)}\n"
            f"Current: {emoji_for(new_status)} {esc(new_status)}\n\n"
            f"🕒 {esc(now_utc())}"
        )

        await send_text(app.bot, user_id, message)


# ==========================================
# USER COMMANDS
# ==========================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    db_upsert_user(user_id)

    message = (
        "👋 <b>Welcome to InstaMonitorBot!</b>\n\n"
        "I monitor an Instagram profile and notify you "
        "when its confirmed status changes.\n\n"
        "<b>Commands:</b>\n"
        "/target username - Set Instagram profile\n"
        "/check - Check now\n"
        "/current - Show current information\n"
        "/delay minutes - Change check interval\n"
        "/reset - Remove your target\n"
        "/help - Show help\n\n"
        f"⏱ Default interval: {DEFAULT_INTERVAL} minutes\n"
        f"Minimum: {MIN_INTERVAL} | Maximum: {MAX_INTERVAL} minutes"
    )

    await update.message.reply_text(
        message,
        parse_mode=ParseMode.HTML,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)


async def target_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "Usage: /target instagram_username"
        )
        return

    username = valid_username(context.args[0])

    if not username:
        await update.message.reply_text(
            "❌ Invalid Instagram username."
        )
        return

    await update.message.reply_text(
        f"🔎 Checking <b>@{esc(username)}</b>...",
        parse_mode=ParseMode.HTML,
    )

    new_status, reason = await get_instagram_status(username)

    logger.info(
        "[Instagram Target] @%s: %s | Reason: %s",
        username,
        new_status,
        reason,
    )

    old_row = db_get_user(user_id)
    interval = (
        get_interval(old_row)
        if old_row is not None
        else DEFAULT_INTERVAL
    )

    db_upsert_user(
        user_id,
        target_username=username,
        last_known_status=new_status,
        last_checked_at=now_utc(),
        consecutive_errors=0 if new_status != "UNKNOWN" else 1,
    )

    schedule_user(context.application, user_id, interval)

    await update.message.reply_text(
        f"✅ Target saved: <b>@{esc(username)}</b>\n\n"
        f"Status: {emoji_for(new_status)} <b>{esc(new_status)}</b>\n"
        f"⏱ Check interval: {interval} minutes\n\n"
        "I will notify you when a confirmed status change occurs.",
        parse_mode=ParseMode.HTML,
    )


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    row = db_get_user(user_id)

    if row is None or not row["target_username"]:
        await update.message.reply_text(
            "❗ No target yet. Use /target username first."
        )
        return

    username = row["target_username"]

    await update.message.reply_text(
        f"🔎 Checking <b>@{esc(username)}</b>...",
        parse_mode=ParseMode.HTML,
    )

    new_status, reason = await get_instagram_status(username)

    logger.info(
        "[Instagram Check] @%s: %s | Reason: %s",
        username,
        new_status,
        reason,
    )

    old_status = row["last_known_status"] or "UNKNOWN"

    if new_status != "UNKNOWN":
        db_upsert_user(
            user_id,
            last_known_status=new_status,
            last_checked_at=now_utc(),
            consecutive_errors=0,
        )
    else:
        db_upsert_user(
            user_id,
            last_checked_at=now_utc(),
            consecutive_errors=(row["consecutive_errors"] or 0) + 1,
        )

    changed = (
        "\n🔔 Status changed!"
        if new_status != "UNKNOWN"
        and old_status != "UNKNOWN"
        and new_status != old_status
        else ""
    )

    await update.message.reply_text(
        f"{emoji_for(new_status)} <b>@{esc(username)}</b>\n"
        f"Status: <b>{esc(new_status)}</b>"
        f"{changed}\n\n"
        f"Reason: <code>{esc(reason)}</code>",
        parse_mode=ParseMode.HTML,
    )


async def current_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    row = db_get_user(user_id)

    if row is None or not row["target_username"]:
        await update.message.reply_text(
            "❗ You have not selected a target.\n"
            "Use /target username"
        )
        return

    username = row["target_username"]
    status = row["last_known_status"] or "UNKNOWN"
    interval = get_interval(row)
    checked_at = row["last_checked_at"] or "Not checked yet"
    errors = row["consecutive_errors"] or 0

    await update.message.reply_text(
        "📊 <b>Your Monitoring Information</b>\n\n"
        f"👤 Target: <b>@{esc(username)}</b>\n"
        f"Status: {emoji_for(status)} <b>{esc(status)}</b>\n"
        f"⏱ Interval: <b>{interval} minutes</b>\n"
        f"🕒 Last check: <code>{esc(checked_at)}</code>\n"
        f"⚠️ Consecutive errors: <b>{errors}</b>",
        parse_mode=ParseMode.HTML,
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    db_delete_target(user_id)
    remove_user_schedule(user_id)

    await update.message.reply_text(
        "✅ Your Instagram target has been removed."
    )


async def delay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    row = db_get_user(user_id)

    if row is None or not row["target_username"]:
        await update.message.reply_text(
            "❗ Set a target first using /target username"
        )
        return

    if not context.args:
        await update.message.reply_text(
            f"Current interval: {get_interval(row)} minutes\n"
            f"Usage: /delay 15\n"
            f"Allowed range: {MIN_INTERVAL}-{MAX_INTERVAL} minutes"
        )
        return

    try:
        interval = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ Please enter a number of minutes."
        )
        return

    if not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        await update.message.reply_text(
            f"❌ Interval must be between "
            f"{MIN_INTERVAL} and {MAX_INTERVAL} minutes."
        )
        return

    db_upsert_user(
        user_id,
        check_interval_minutes=interval,
    )

    schedule_user(context.application, user_id, interval)

    await update.message.reply_text(
        f"✅ Check interval updated to {interval} minutes."
    )


# ==========================================
# ADMIN COMMANDS
# ==========================================

async def admin_list_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update.effective_user.id):
        return

    users = db_get_all_users()

    if not users:
        await update.message.reply_text("No users found.")
        return

    lines = ["👥 <b>Bot Users</b>\n"]

    for row in users:
        username = row["target_username"] or "No target"
        interval = get_interval(row)

        lines.append(
            f"ID: <code>{row['telegram_user_id']}</code>\n"
            f"Target: @{esc(username)}\n"
            f"Interval: {interval} min\n"
        )

    message = "\n".join(lines)

    # Telegram messages have a length limit.
    for start in range(0, len(message), 3500):
        await update.message.reply_text(
            message[start:start + 3500],
            parse_mode=ParseMode.HTML,
        )


async def admin_settarget_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update.effective_user.id):
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /admin_settarget USER_ID username"
        )
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return

    username = valid_username(context.args[1])

    if not username:
        await update.message.reply_text("Invalid Instagram username.")
        return

    new_status, reason = await get_instagram_status(username)

    logger.info(
        "[Admin Target] @%s: %s | Reason: %s",
        username,
        new_status,
        reason,
    )

    row = db_get_user(target_user_id)
    interval = (
        get_interval(row)
        if row is not None
        else DEFAULT_INTERVAL
    )

    db_upsert_user(
        target_user_id,
        target_username=username,
        last_known_status=new_status,
        last_checked_at=now_utc(),
        consecutive_errors=0 if new_status != "UNKNOWN" else 1,
    )

    schedule_user(context.application, target_user_id, interval)

    await update.message.reply_text(
        f"✅ Target for user {target_user_id} set to "
        f"@{esc(username)}.\n"
        f"Status: {esc(new_status)}"
    )


async def admin_check_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /admin_check USER_ID"
        )
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return

    row = db_get_user(target_user_id)

    if row is None or not row["target_username"]:
        await update.message.reply_text(
            "This user has no target."
        )
        return

    username = row["target_username"]
    status, reason = await get_instagram_status(username)

    logger.info(
        "[Admin Check] @%s: %s | Reason: %s",
        username,
        status,
        reason,
    )

    if status != "UNKNOWN":
        db_upsert_user(
            target_user_id,
            last_known_status=status,
            last_checked_at=now_utc(),
            consecutive_errors=0,
        )
    else:
        db_upsert_user(
            target_user_id,
            last_checked_at=now_utc(),
            consecutive_errors=(row["consecutive_errors"] or 0) + 1,
        )

    await update.message.reply_text(
        f"👤 @{esc(username)}\n"
        f"Status: {emoji_for(status)} <b>{esc(status)}</b>\n"
        f"Reason: <code>{esc(reason)}</code>",
        parse_mode=ParseMode.HTML,
    )


async def admin_delay_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update.effective_user.id):
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /admin_delay USER_ID MINUTES"
        )
        return

    try:
        target_user_id = int(context.args[0])
        interval = int(context.args[1])
    except ValueError:
        await update.message.reply_text(
            "User ID and interval must be numbers."
        )
        return

    if not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        await update.message.reply_text(
            f"Interval must be {MIN_INTERVAL}-{MAX_INTERVAL} minutes."
        )
        return

    row = db_get_user(target_user_id)

    if row is None or not row["target_username"]:
        await update.message.reply_text(
            "This user has no target."
        )
        return

    db_upsert_user(
        target_user_id,
        check_interval_minutes=interval,
    )

    schedule_user(context.application, target_user_id, interval)

    await update.message.reply_text(
        f"✅ User {target_user_id} interval set to "
        f"{interval} minutes."
    )


async def admin_broadcast_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update.effective_user.id):
        return

    message = update.message.text.partition(" ")[2].strip()

    if not message:
        await update.message.reply_text(
            "Usage: /admin_broadcast Your message"
        )
        return

    users = db_get_all_users()
    sent = 0
    failed = 0

    for row in users:
        try:
            await context.bot.send_message(
                chat_id=row["telegram_user_id"],
                text=message,
            )
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"📢 Broadcast finished.\n"
        f"Sent: {sent}\n"
        f"Failed: {failed}"
    )


# ==========================================
# APPLICATION STARTUP / SHUTDOWN
# ==========================================

async def post_init(app: Application):
    db_init()

    if not scheduler.running:
        scheduler.start()

    # Restore schedules after a restart.
    for row in db_get_all_users():
        if row["target_username"]:
            schedule_user(
                app,
                row["telegram_user_id"],
                get_interval(row),
            )

    logger.info("InstaMonitorBot started successfully.")


async def post_shutdown(app: Application):
    if scheduler.running:
        scheduler.shutdown(wait=False)

    logger.info("InstaMonitorBot stopped.")


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    db_init()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # User commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("target", target_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("current", current_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("delay", delay_cmd))

    # Admin commands
    app.add_handler(CommandHandler("admin_list", admin_list_cmd))
    app.add_handler(CommandHandler("admin_settarget", admin_settarget_cmd))
    app.add_handler(CommandHandler("admin_check", admin_check_cmd))
    app.add_handler(CommandHandler("admin_delay", admin_delay_cmd))
    app.add_handler(CommandHandler("admin_broadcast", admin_broadcast_cmd))

    logger.info("Starting Telegram polling...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
