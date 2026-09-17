import asyncio
import html
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TimedOut,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from scheduler import SchedulerDB


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()

try:
    OWNER_ID = int(OWNER_ID_RAW)
except ValueError:
    OWNER_ID = 0


MAX_MEDIA = 10
MAX_RETRIES = 3

scheduler_db = SchedulerDB()


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
    handlers=[
        logging.FileHandler(
            LOG_DIR / "bot.log",
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger("telepost")


# ============================================================
# CONVERSATION STATES
# ============================================================

WAITING_MEDIA = 1
WAITING_CAPTION = 2
WAITING_CHANNELS = 3
WAITING_DELAY = 4
WAITING_SCHEDULE = 5


# ============================================================
# OWNER SECURITY
# ============================================================

def is_owner(update: Update) -> bool:
    user = update.effective_user

    if not user:
        return False

    return user.id == OWNER_ID


async def owner_only(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    if not is_owner(update):
        if update.effective_message:
            await update.effective_message.reply_text(
                "⛔ You are not authorized to use this bot."
            )

        return False

    return True


# ============================================================
# START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await owner_only(update, context):
        return

    await update.message.reply_text(
        "📡 <b>Telepost</b>\n\n"
        "Telegram scheduled posting bot.\n\n"
        "Use /newpost to create a post.",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# NEW POST
# ============================================================

async def newpost_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await owner_only(update, context):
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["media"] = []

    await update.message.reply_text(
        "📎 Send up to 10 photos/videos/documents/audio/voice/GIFs.\n\n"
        "When finished, send /done."
    )

    return WAITING_MEDIA


# ============================================================
# MEDIA RECEIVING
# ============================================================

async def receive_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await owner_only(update, context):
        return ConversationHandler.END

    media = context.user_data.setdefault("media", [])

    if len(media) >= MAX_MEDIA:
        await update.message.reply_text(
            "⚠️ Maximum 10 media files allowed."
        )
        return WAITING_MEDIA

    message = update.message

    item = None

    if message.photo:
        item = {
            "type": "photo",
            "file_id": message.photo[-1].file_id,
        }

    elif message.video:
        item = {
            "type": "video",
            "file_id": message.video.file_id,
        }

    elif message.document:
        item = {
            "type": "document",
            "file_id": message.document.file_id,
        }

    elif message.audio:
        item = {
            "type": "audio",
            "file_id": message.audio.file_id,
        }

    elif message.voice:
        item = {
            "type": "voice",
            "file_id": message.voice.file_id,
        }

    elif message.animation:
        item = {
            "type": "animation",
            "file_id": message.animation.file_id,
        }

    if item is None:
        await update.message.reply_text(
            "❌ Unsupported media type."
        )
        return WAITING_MEDIA

    media.append(item)

    await update.message.reply_text(
        f"✅ Media added ({len(media)}/{MAX_MEDIA}).\n"
        "Send more or /done."
    )

    return WAITING_MEDIA


# ============================================================
# DONE MEDIA
# ============================================================

async def done_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await owner_only(update, context):
        return ConversationHandler.END

    media = context.user_data.get("media", [])

    if not media:
        await update.message.reply_text(
            "❌ No media added yet."
        )
        return WAITING_MEDIA

    await update.message.reply_text(
        "📝 Send the caption.\n\n"
        "You can use Telegram HTML formatting."
    )

    return WAITING_CAPTION


# ============================================================
# CAPTION
# ============================================================

async def receive_caption(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await owner_only(update, context):
        return ConversationHandler.END

    context.user_data["caption"] = update.message.text or ""

    await update.message.reply_text(
        "📢 Send destination channel IDs.\n\n"
        "Example:\n"
        "<code>-1001234567890</code>\n\n"
        "For multiple channels, put each ID on a new line.",
        parse_mode=ParseMode.HTML,
    )

    return WAITING_CHANNELS


# ============================================================
# CHANNELS
# ============================================================

async def receive_channels(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await owner_only(update, context):
        return ConversationHandler.END

    raw = update.message.text or ""

    channels = []

    for line in raw.splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            channels.append(int(line))
        except ValueError:
            pass

    if not channels:
        await update.message.reply_text(
            "❌ No valid channel IDs found."
        )
        return WAITING_CHANNELS

    context.user_data["channels"] = channels

    keyboard = [
        [
            InlineKeyboardButton(
                "⚡ Now",
                callback_data="delay_0",
            ),
            InlineKeyboardButton(
                "⏱ 2 sec",
                callback_data="delay_2",
            ),
            InlineKeyboardButton(
                "⏱ 5 sec",
                callback_data="delay_5",
            ),
        ],
        [
            InlineKeyboardButton(
                "⏱ 10 sec",
                callback_data="delay_10",
            ),
            InlineKeyboardButton(
                "⏱ 30 sec",
                callback_data="delay_30",
            ),
        ],
    ]

    await update.message.reply_text(
        "⏱ Choose delay between channel posts:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    return WAITING_DELAY


# ============================================================
# DELAY
# ============================================================

async def delay_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != OWNER_ID:
        return

    delay = int(query.data.split("_")[1])

    context.user_data["delay"] = delay

    keyboard = [
        [
            InlineKeyboardButton(
                "🚀 Post Now",
                callback_data="schedule_now",
            )
        ],
        [
            InlineKeyboardButton(
                "⏰ In 1 Hour",
                callback_data="schedule_1h",
            ),
            InlineKeyboardButton(
                "⏰ In 24 Hours",
                callback_data="schedule_24h",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔁 Every Hour",
                callback_data="schedule_hourly",
            ),
            InlineKeyboardButton(
                "🔁 Every 24 Hours",
                callback_data="schedule_daily",
            ),
        ],
    ]

    await query.edit_message_text(
        f"⏱ Delay selected: {delay} seconds\n\n"
        "Choose when to publish:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
# SCHEDULE
# ============================================================

async def schedule_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != OWNER_ID:
        return

    action = query.data.replace("schedule_", "")

    media = context.user_data.get("media", [])
    caption = context.user_data.get("caption", "")
    channels = context.user_data.get("channels", [])
    delay = context.user_data.get("delay", 0)

    if not media or not channels:
        await query.edit_message_text(
            "❌ Post data is incomplete."
        )
        return ConversationHandler.END

    data = {
        "media": media,
        "caption": caption,
        "channels": channels,
        "delay": delay,
    }

    now = datetime.now()

    if action == "now":
        job_id = scheduler_db.create_job(
            job_type="one_time",
            run_at=now.timestamp(),
            interval_seconds=None,
            data=data,
        )

        await schedule_saved_job(
            context.application,
            job_id,
            run_at=now,
        )

        await query.edit_message_text(
            f"🚀 Post queued.\n\n"
            f"Job ID: <code>{job_id}</code>",
            parse_mode=ParseMode.HTML,
        )

    elif action == "1h":
        run_at = now + timedelta(hours=1)

        job_id = scheduler_db.create_job(
            job_type="one_time",
            run_at=run_at.timestamp(),
            interval_seconds=None,
            data=data,
        )

        await schedule_saved_job(
            context.application,
            job_id,
            run_at=run_at,
        )

        await query.edit_message_text(
            f"⏰ Scheduled for {run_at:%Y-%m-%d %H:%M:%S}\n\n"
            f"Job ID: <code>{job_id}</code>",
            parse_mode=ParseMode.HTML,
        )

    elif action == "24h":
        run_at = now + timedelta(hours=24)

        job_id = scheduler_db.create_job(
            job_type="one_time",
            run_at=run_at.timestamp(),
            interval_seconds=None,
            data=data,
        )

        await schedule_saved_job(
            context.application,
            job_id,
            run_at=run_at,
        )

        await query.edit_message_text(
            f"⏰ Scheduled for {run_at:%Y-%m-%d %H:%M:%S}\n\n"
            f"Job ID: <code>{job_id}</code>",
            parse_mode=ParseMode.HTML,
        )

    elif action == "hourly":
        run_at = now

        job_id = scheduler_db.create_job(
            job_type="repeating",
            run_at=run_at.timestamp(),
            interval_seconds=3600,
            data=data,
        )

        await schedule_saved_job(
            context.application,
            job_id,
            run_at=run_at,
        )

        await query.edit_message_text(
            "🔁 Repeating post created.\n\n"
            "Interval: Every hour\n"
            f"Job ID: <code>{job_id}</code>",
            parse_mode=ParseMode.HTML,
        )

    elif action == "daily":
        run_at = now

        job_id = scheduler_db.create_job(
            job_type="repeating",
            run_at=run_at.timestamp(),
            interval_seconds=86400,
            data=data,
        )

        await schedule_saved_job(
            context.application,
            job_id,
            run_at=run_at,
        )

        await query.edit_message_text(
            "🔁 Repeating post created.\n\n"
            "Interval: Every 24 hours\n"
            f"Job ID: <code>{job_id}</code>",
            parse_mode=ParseMode.HTML,
        )

    context.user_data.clear()

    return ConversationHandler.END


# ============================================================
# RETRY SYSTEM
# ============================================================

async def retry_operation(operation, description="Telegram request"):
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await operation()

        except RetryAfter as error:
            last_error = error

            wait_time = int(error.retry_after) + 1

            logger.warning(
                "%s rate limited. Waiting %s seconds.",
                description,
                wait_time,
            )

            await asyncio.sleep(wait_time)

        except (TimedOut, NetworkError) as error:
            last_error = error

            if attempt >= MAX_RETRIES:
                break

            wait_time = attempt * 2

            logger.warning(
                "%s network error. "
                "Retry %s/%s in %s seconds: %s",
                description,
                attempt,
                MAX_RETRIES,
                wait_time,
                error,
            )

            await asyncio.sleep(wait_time)

    raise last_error


# ============================================================
# SEND SINGLE MEDIA
# ============================================================

async def send_single_media(
    bot,
    channel_id,
    item,
    caption="",
):
    media_type = item["type"]
    file_id = item["file_id"]

    async def operation():
        if media_type == "photo":
            return await bot.send_photo(
                chat_id=channel_id,
                photo=file_id,
                caption=caption or None,
                parse_mode=ParseMode.HTML,
            )

        if media_type == "video":
            return await bot.send_video(
                chat_id=channel_id,
                video=file_id,
                caption=caption or None,
                parse_mode=ParseMode.HTML,
            )

        if media_type == "document":
            return await bot.send_document(
                chat_id=channel_id,
                document=file_id,
                caption=caption or None,
                parse_mode=ParseMode.HTML,
            )

        if media_type == "audio":
            return await bot.send_audio(
                chat_id=channel_id,
                audio=file_id,
                caption=caption or None,
                parse_mode=ParseMode.HTML,
            )

        if media_type == "voice":
            return await bot.send_voice(
                chat_id=channel_id,
                voice=file_id,
                caption=caption or None,
                parse_mode=ParseMode.HTML,
            )

        if media_type == "animation":
            return await bot.send_animation(
                chat_id=channel_id,
                animation=file_id,
                caption=caption or None,
                parse_mode=ParseMode.HTML,
            )

        raise ValueError(
            f"Unsupported media type: {media_type}"
        )

    return await retry_operation(
        operation,
        f"send {media_type} to {channel_id}",
    )


# ============================================================
# SEND ALBUM
# ============================================================

async def send_album(
    bot,
    channel_id,
    media,
    caption,
):
    telegram_media = []

    from telegram import InputMediaPhoto, InputMediaVideo

    for index, item in enumerate(media):
        item_type = item["type"]

        if item_type == "photo":
            telegram_media.append(
                InputMediaPhoto(
                    media=item["file_id"],
                    caption=caption if index == 0 else None,
                    parse_mode=ParseMode.HTML,
                )
            )

        elif item_type == "video":
            telegram_media.append(
                InputMediaVideo(
                    media=item["file_id"],
                    caption=caption if index == 0 else None,
                    parse_mode=ParseMode.HTML,
                )
            )

    if not telegram_media:
        return

    async def operation():
        return await bot.send_media_group(
            chat_id=channel_id,
            media=telegram_media,
        )

    await retry_operation(
        operation,
        f"send album to {channel_id}",
    )


# ============================================================
# DISPATCH POST
# ============================================================

async def dispatch_post(
    application: Application,
    data: dict,
):
    bot = application.bot

    media = data.get("media", [])
    caption = data.get("caption", "")
    channels = data.get("channels", [])
    delay = int(data.get("delay", 0))

    # Validate/sanitize caption for Telegram HTML mode.
    # Existing Telegram HTML tags are intentionally preserved.
    caption = caption.strip()

    for channel_index, channel_id in enumerate(channels):
        try:
            # Photos/videos can be grouped into an album.
            album_media = [
                item
                for item in media
                if item["type"] in {"photo", "video"}
            ]

            other_media = [
                item
                for item in media
                if item["type"] not in {"photo", "video"}
            ]

            if len(album_media) >= 2:
                # Telegram media groups support up to 10 items.
                await send_album(
                    bot,
                    channel_id,
                    album_media[:10],
                    caption,
                )

            elif len(album_media) == 1:
                await send_single_media(
                    bot,
                    channel_id,
                    album_media[0],
                    caption,
                )

            for index, item in enumerate(other_media):
                item_caption = caption if not album_media and index == 0 else ""

                await send_single_media(
                    bot,
                    channel_id,
                    item,
                    item_caption,
                )

            logger.info(
                "Post successfully sent to channel %s",
                channel_id,
            )

        except (BadRequest, Forbidden) as error:
            logger.error(
                "Permanent Telegram error for channel %s: %s",
                channel_id,
                error,
            )

        except Exception:
            logger.exception(
                "Failed to send post to channel %s",
                channel_id,
            )

        if delay > 0 and channel_index < len(channels) - 1:
            await asyncio.sleep(delay)


# ============================================================
# PERSISTENT JOB CALLBACK
# ============================================================

async def persistent_job_callback(
    context: ContextTypes.DEFAULT_TYPE,
):
    job = context.job

    if not job:
        return

    job_id = job.data

    stored_job = scheduler_db.get_job(job_id)

    if not stored_job:
        logger.warning(
            "Persistent job %s no longer exists.",
            job_id,
        )
        return

    try:
        data = json.loads(stored_job["data"])

        await dispatch_post(
            context.application,
            data,
        )

    except Exception:
        logger.exception(
            "Persistent job %s failed.",
            job_id,
        )

    finally:
        if stored_job["job_type"] == "one_time":
            scheduler_db.delete_one_time_job(job_id)

        else:
            interval = stored_job["interval_seconds"]

            if interval:
                next_run = (
                    datetime.now().timestamp()
                    + interval
                )

                scheduler_db.update_run_time(
                    job_id,
                    next_run,
                )


# ============================================================
# SCHEDULE SAVED JOB
# ============================================================

async def schedule_saved_job(
    application: Application,
    job_id: str,
    run_at: datetime,
):
    stored_job = scheduler_db.get_job(job_id)

    if not stored_job:
        return

    current_time = datetime.now()

    if run_at < current_time:
        run_at = current_time

    if stored_job["job_type"] == "one_time":
        application.job_queue.run_once(
            persistent_job_callback,
            when=run_at,
            data=job_id,
            name=f"telepost_{job_id}",
        )

    elif stored_job["job_type"] == "repeating":
        interval = stored_job["interval_seconds"]

        if not interval:
            return

        application.job_queue.run_repeating(
            persistent_job_callback,
            interval=interval,
            first=run_at,
            data=job_id,
            name=f"telepost_{job_id}",
        )


# ============================================================
# RESTORE JOBS AFTER RESTART
# ============================================================

async def restore_saved_jobs(
    application: Application,
):
    jobs = scheduler_db.get_active_jobs()

    if not jobs:
        logger.info("No saved jobs to restore.")
        return

    logger.info(
        "Restoring %s saved jobs...",
        len(jobs),
    )

    current_time = datetime.now()

    for job in jobs:
        try:
            if job["run_at"]:
                run_at = datetime.fromtimestamp(
                    job["run_at"]
                )
            else:
                run_at = current_time

            if run_at < current_time:
                run_at = current_time

            await schedule_saved_job(
                application,
                job["id"],
                run_at,
            )

            logger.info(
                "Restored job %s",
                job["id"],
            )

        except Exception:
            logger.exception(
                "Failed to restore job %s",
                job["id"],
            )


# ============================================================
# POST INIT
# ============================================================

async def post_init(
    application: Application,
):
    await restore_saved_jobs(application)


# ============================================================
# LIST JOBS
# ============================================================

async def listjobs_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await owner_only(update, context):
        return

    jobs = scheduler_db.get_active_jobs()

    if not jobs:
        await update.message.reply_text(
            "📭 No active scheduled jobs."
        )
        return

    lines = ["📋 <b>Active Jobs</b>\n"]

    for job in jobs:
        job_id = job["id"]
        job_type = job["job_type"]

        if job["run_at"]:
            run_at = datetime.fromtimestamp(
                job["run_at"]
            ).strftime("%Y-%m-%d %H:%M:%S")
        else:
            run_at = "N/A"

        if job_type == "repeating":
            interval = job["interval_seconds"]

            if interval == 3600:
                interval_text = "Every hour"
            elif interval == 86400:
                interval_text = "Every 24 hours"
            else:
                interval_text = f"Every {interval}s"

            lines.append(
                f"🔁 <code>{job_id}</code>\n"
                f"   {interval_text}\n"
                f"   Next: {run_at}\n"
            )

        else:
            lines.append(
                f"⏰ <code>{job_id}</code>\n"
                f"   One-time\n"
                f"   Run: {run_at}\n"
            )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# CANCEL JOB
# ============================================================

async def canceljob_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await owner_only(update, context):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/canceljob JOB_ID"
        )
        return

    job_id = context.args[0].strip()

    deleted = scheduler_db.delete_job(job_id)

    if not deleted:
        await update.message.reply_text(
            "❌ Job not found."
        )
        return

    for job in context.application.job_queue.jobs():
        if job.name == f"telepost_{job_id}":
            job.schedule_removal()

    await update.message.reply_text(
        f"🗑 Job <code>{html.escape(job_id)}</code> cancelled.",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# CANCEL CONVERSATION
# ============================================================

async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await owner_only(update, context):
        return ConversationHandler.END

    context.user_data.clear()

    await update.message.reply_text(
        "❌ Current post cancelled."
    )

    return ConversationHandler.END


# ============================================================
# CHANNEL ID
# ============================================================

async def channelid_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await owner_only(update, context):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/channelid @channelusername"
        )
        return

    username = context.args[0].strip()

    if not username.startswith("@"):
        username = f"@{username}"

    try:
        chat = await retry_operation(
            lambda: context.bot.get_chat(username),
            "get channel ID",
        )

        await update.message.reply_text(
            "📢 Channel information\n\n"
            f"Title: {html.escape(chat.title or 'Unknown')}\n"
            f"ID: <code>{chat.id}</code>\n"
            f"Username: {html.escape(chat.username or 'None')}",
            parse_mode=ParseMode.HTML,
        )

    except Exception as error:
        logger.exception(
            "Failed to get channel ID."
        )

        await update.message.reply_text(
            f"❌ Could not access that channel.\n\n"
            f"Error: {error}"
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.exception(
        "Unhandled bot exception",
        exc_info=context.error,
    )


# ============================================================
# BUILD APPLICATION
# ============================================================

def build_application():
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler(
                "newpost",
                newpost_command,
            )
        ],
        states={
            WAITING_MEDIA: [
                MessageHandler(
                    filters.PHOTO
                    | filters.VIDEO
                    | filters.Document.ALL
                    | filters.AUDIO
                    | filters.VOICE
                    | filters.ANIMATION,
                    receive_media,
                ),
                CommandHandler(
                    "done",
                    done_media,
                ),
            ],
            WAITING_CAPTION: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_caption,
                ),
            ],
            WAITING_CHANNELS: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_channels,
                ),
            ],
            WAITING_DELAY: [],
            WAITING_SCHEDULE: [],
        },
        fallbacks=[
            CommandHandler(
                "cancel",
                cancel_command,
            ),
        ],
        allow_reentry=True,
    )

    application.add_handler(conversation)

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "post",
            newpost_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "listjobs",
            listjobs_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "canceljob",
            canceljob_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "channelid",
            channelid_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            delay_callback,
            pattern=r"^delay_\d+$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            schedule_callback,
            pattern=r"^schedule_(now|1h|24h|hourly|daily)$",
        )
    )

    application.add_error_handler(error_handler)

    return application


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing from .env"
        )

    if not OWNER_ID:
        raise RuntimeError(
            "OWNER_ID is missing or invalid in .env"
        )

    logger.info("=" * 60)
    logger.info("Telepost starting...")
    logger.info("Owner ID: %s", OWNER_ID)
    logger.info("Database: %s", scheduler_db.database)
    logger.info("=" * 60)

    application = build_application()

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()