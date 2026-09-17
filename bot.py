import asyncio
import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
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
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from scheduler import SchedulerDB


# ============================================================
# PATHS / CONFIG
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


# ============================================================
# SETTINGS
# ============================================================

MAX_MEDIA = 50

# Telegram allows a maximum of 10 photo/video items
# in one media group.
TELEGRAM_ALBUM_SIZE = 10

MAX_RETRIES = 3


# ============================================================
# TIMEZONE
# ============================================================

UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")


def utc_now():
    return datetime.now(UTC)


def format_ist(timestamp):
    return datetime.fromtimestamp(
        timestamp,
        tz=IST,
    ).strftime(
        "%Y-%m-%d %H:%M:%S IST"
    )


# ============================================================
# DATABASE
# ============================================================

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


# ============================================================
# OWNER SECURITY
# ============================================================

def is_owner(update: Update) -> bool:
    user = update.effective_user

    if not user:
        return False

    return user.id == OWNER_ID


async def owner_only(update, context) -> bool:
    if not is_owner(update):

        if update.effective_message:
            await update.effective_message.reply_text(
                "⛔ You are not authorized to use this bot."
            )

        return False

    return True


# ============================================================
# /START
# ============================================================

async def start_command(update, context):
    if not await owner_only(update, context):
        return

    await update.message.reply_text(
        "📡 <b>Telepost</b>\n\n"
        "Create and schedule media posts for your "
        "Telegram channels/groups.\n\n"
        "Use /newpost to create a post.",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# /NEWPOST
# ============================================================

async def newpost_command(update, context):
    if not await owner_only(update, context):
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["media"] = []

    await update.message.reply_text(
        "📎 Send up to <b>50</b> photos/videos/documents/"
        "audio/voice/GIFs.\n\n"
        "Telegram albums will automatically be split "
        "into batches of 10.\n\n"
        "When finished, send /done.",
        parse_mode=ParseMode.HTML,
    )

    return WAITING_MEDIA


# ============================================================
# RECEIVE MEDIA
# ============================================================

async def receive_media(update, context):
    if not await owner_only(update, context):
        return ConversationHandler.END

    media = context.user_data.setdefault(
        "media",
        [],
    )

    if len(media) >= MAX_MEDIA:

        await update.message.reply_text(
            "⚠️ Maximum limit reached.\n\n"
            "You can send up to 50 media files "
            "in one post."
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

    count = len(media)

    await update.message.reply_text(
        f"✅ Added {count}/{MAX_MEDIA}\n\n"
        "Send more or /done."
    )

    return WAITING_MEDIA


# ============================================================
# /DONE
# ============================================================

async def done_media(update, context):
    if not await owner_only(update, context):
        return ConversationHandler.END

    media = context.user_data.get(
        "media",
        [],
    )

    if not media:

        await update.message.reply_text(
            "❌ No media added yet."
        )

        return WAITING_MEDIA

    await update.message.reply_text(
        f"✅ {len(media)} media files received.\n\n"
        "📝 Now send the caption.\n\n"
        "You can use Telegram HTML formatting."
    )

    return WAITING_CAPTION


# ============================================================
# RECEIVE CAPTION
# ============================================================

async def receive_caption(update, context):
    if not await owner_only(update, context):
        return ConversationHandler.END

    context.user_data["caption"] = (
        update.message.text or ""
    )

    await update.message.reply_text(
        "📢 Send destination channel/group IDs.\n\n"
        "You can use one per line:\n"
        "<code>-1001234567890</code>\n"
        "<code>-1009876543210</code>\n\n"
        "Or comma-separated:\n"
        "<code>@channelone,@channeltwo</code>\n\n"
        "Public usernames are also supported.",
        parse_mode=ParseMode.HTML,
    )

    return WAITING_CHANNELS


# ============================================================
# PARSE DESTINATIONS
# ============================================================

def parse_destinations(raw_text):
    """
    Accepts:

        @channel1
        @channel2

    or:

        @channel1,@channel2

    or:

        -1001234567890
        -1009876543210

    or mixed formats.
    """

    channels = []

    # Allow both commas and new lines.
    normalized = raw_text.replace(
        ",",
        "\n",
    )

    for value in normalized.splitlines():

        value = value.strip()

        if not value:
            continue

        # Numeric Telegram ID
        try:

            channels.append(
                int(value)
            )

            continue

        except ValueError:
            pass

        # Public username
        if value.startswith("@"):

            channels.append(
                value
            )

    # Remove duplicates while preserving order.
    unique_channels = []

    for channel in channels:

        if channel not in unique_channels:

            unique_channels.append(
                channel
            )

    return unique_channels


# ============================================================
# RECEIVE CHANNELS
# ============================================================

async def receive_channels(update, context):
    if not await owner_only(update, context):
        return ConversationHandler.END

    raw = update.message.text or ""

    channels = parse_destinations(
        raw
    )

    if not channels:

        await update.message.reply_text(
            "❌ No valid destinations found.\n\n"
            "Use numeric IDs or @usernames."
        )

        return WAITING_CHANNELS

    context.user_data["channels"] = channels

    destinations_text = "\n".join(
        f"• {channel}"
        for channel in channels
    )

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
        "✅ <b>Destinations:</b>\n\n"
        f"{html.escape(destinations_text)}\n\n"
        "⏱ Choose delay between destination posts:",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
        parse_mode=ParseMode.HTML,
    )

    return WAITING_DELAY


# ============================================================
# DELAY CALLBACK
# ============================================================

async def delay_callback(update, context):
    query = update.callback_query

    await query.answer()

    if query.from_user.id != OWNER_ID:
        return

    delay = int(
        query.data.split("_")[1]
    )

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
        f"⏱ Delay: {delay} seconds\n\n"
        "Choose when to publish:",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# SCHEDULE CALLBACK
# ============================================================

async def schedule_callback(update, context):
    query = update.callback_query

    await query.answer()

    if query.from_user.id != OWNER_ID:
        return

    action = query.data.replace(
        "schedule_",
        "",
    )

    media = context.user_data.get(
        "media",
        [],
    )

    caption = context.user_data.get(
        "caption",
        "",
    )

    channels = context.user_data.get(
        "channels",
        [],
    )

    delay = int(
        context.user_data.get(
            "delay",
            0,
        )
    )

    if not media:

        await query.edit_message_text(
            "❌ No media found."
        )

        return ConversationHandler.END

    if not channels:

        await query.edit_message_text(
            "❌ No destinations found."
        )

        return ConversationHandler.END

    data = {
        "media": media,
        "caption": caption,
        "channels": channels,
        "delay": delay,
    }

    now = utc_now()

    # ========================================================
    # POST NOW
    # ========================================================

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
            now,
        )

        await query.edit_message_text(
            "🚀 <b>Post queued</b>\n\n"
            f"Media: {len(media)}/50\n"
            f"Destinations: {len(channels)}\n"
            f"Job ID: <code>{job_id}</code>",
            parse_mode=ParseMode.HTML,
        )

    # ========================================================
    # IN 1 HOUR
    # ========================================================

    elif action == "1h":

        run_at = now + timedelta(
            hours=1
        )

        job_id = scheduler_db.create_job(
            job_type="one_time",
            run_at=run_at.timestamp(),
            interval_seconds=None,
            data=data,
        )

        await schedule_saved_job(
            context.application,
            job_id,
            run_at,
        )

        await query.edit_message_text(
            "⏰ <b>Scheduled</b>\n\n"
            f"Run: {run_at.astimezone(IST):%Y-%m-%d %H:%M:%S} IST\n"
            f"Media: {len(media)}/50\n"
            f"Destinations: {len(channels)}\n"
            f"Job ID: <code>{job_id}</code>",
            parse_mode=ParseMode.HTML,
        )

    # ========================================================
    # IN 24 HOURS
    # ========================================================

    elif action == "24h":

        run_at = now + timedelta(
            hours=24
        )

        job_id = scheduler_db.create_job(
            job_type="one_time",
            run_at=run_at.timestamp(),
            interval_seconds=None,
            data=data,
        )

        await schedule_saved_job(
            context.application,
            job_id,
            run_at,
        )

        await query.edit_message_text(
            "⏰ <b>Scheduled</b>\n\n"
            f"Run: {run_at.astimezone(IST):%Y-%m-%d %H:%M:%S} IST\n"
            f"Media: {len(media)}/50\n"
            f"Destinations: {len(channels)}\n"
            f"Job ID: <code>{job_id}</code>",
            parse_mode=ParseMode.HTML,
        )

    # ========================================================
    # EVERY HOUR
    # ========================================================

    elif action == "hourly":

        job_id = scheduler_db.create_job(
            job_type="repeating",
            run_at=now.timestamp(),
            interval_seconds=3600,
            data=data,
        )

        await schedule_saved_job(
            context.application,
            job_id,
            now,
        )

        await query.edit_message_text(
            "🔁 <b>Repeating post created</b>\n\n"
            "First post: Immediately\n"
            "Then: Every hour\n"
            f"Media: {len(media)}/50\n"
            f"Destinations: {len(channels)}\n"
            f"Job ID: <code>{job_id}</code>",
            parse_mode=ParseMode.HTML,
        )

    # ========================================================
    # EVERY 24 HOURS
    # ========================================================

    elif action == "daily":

        job_id = scheduler_db.create_job(
            job_type="repeating",
            run_at=now.timestamp(),
            interval_seconds=86400,
            data=data,
        )

        await schedule_saved_job(
            context.application,
            job_id,
            now,
        )

        await query.edit_message_text(
            "🔁 <b>Repeating post created</b>\n\n"
            "First post: Immediately\n"
            "Then: Every 24 hours\n"
            f"Media: {len(media)}/50\n"
            f"Destinations: {len(channels)}\n"
            f"Job ID: <code>{job_id}</code>",
            parse_mode=ParseMode.HTML,
        )

    context.user_data.clear()

    return ConversationHandler.END


# ============================================================
# RETRY TEMPORARY ERRORS ONLY
# ============================================================

async def retry_operation(
    operation,
    description="Telegram request",
):
    """
    Retry only temporary Telegram/network errors.

    BadRequest and Forbidden are NOT retried because they
    represent permanent request/access problems.
    """

    last_error = None

    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):

        try:

            return await operation()

        # ----------------------------------------------------
        # Telegram rate limit
        # ----------------------------------------------------

        except RetryAfter as error:

            last_error = error

            wait_time = (
                int(error.retry_after)
                + 1
            )

            logger.warning(
                "%s rate limited. "
                "Waiting %s seconds.",
                description,
                wait_time,
            )

            await asyncio.sleep(
                wait_time
            )

        # ----------------------------------------------------
        # Temporary network errors
        # ----------------------------------------------------

        except (
            TimedOut,
            NetworkError,
        ) as error:

            last_error = error

            if attempt >= MAX_RETRIES:
                break

            wait_time = attempt * 2

            logger.warning(
                "%s temporary network error. "
                "Retry %s/%s in %s seconds.",
                description,
                attempt,
                MAX_RETRIES,
                wait_time,
            )

            await asyncio.sleep(
                wait_time
            )

    if last_error:

        raise last_error

    raise RuntimeError(
        f"{description} failed"
    )


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
    caption="",
):
    telegram_media = []

    for index, item in enumerate(media):

        if item["type"] == "photo":

            telegram_media.append(
                InputMediaPhoto(
                    media=item["file_id"],
                    caption=(
                        caption
                        if index == 0
                        else None
                    ),
                    parse_mode=ParseMode.HTML,
                )
            )

        elif item["type"] == "video":

            telegram_media.append(
                InputMediaVideo(
                    media=item["file_id"],
                    caption=(
                        caption
                        if index == 0
                        else None
                    ),
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
# SPLIT INTO TELEGRAM ALBUMS
# ============================================================

def split_into_chunks(
    items,
    chunk_size=TELEGRAM_ALBUM_SIZE,
):
    return [
        items[index:index + chunk_size]
        for index in range(
            0,
            len(items),
            chunk_size,
        )
    ]


# ============================================================
# DISPATCH POST
# ============================================================

async def dispatch_post(
    application,
    data,
):
    bot = application.bot

    media = data.get(
        "media",
        [],
    )

    caption = data.get(
        "caption",
        "",
    ).strip()

    channels = data.get(
        "channels",
        [],
    )

    delay = int(
        data.get(
            "delay",
            0,
        )
    )

    if not media:

        logger.error(
            "Post contains no media."
        )

        return

    if not channels:

        logger.error(
            "Post contains no destinations."
        )

        return

    # Photo/video media can be sent as albums.
    album_media = [
        item
        for item in media
        if item["type"] in {
            "photo",
            "video",
        }
    ]

    # Other media types are sent individually.
    other_media = [
        item
        for item in media
        if item["type"] not in {
            "photo",
            "video",
        }
    ]

    album_chunks = split_into_chunks(
        album_media,
        TELEGRAM_ALBUM_SIZE,
    )

    total_albums = len(
        album_chunks
    )

    logger.info(
        "Dispatching post: %s media, "
        "%s album(s), %s other media, "
        "%s destination(s)",
        len(media),
        total_albums,
        len(other_media),
        len(channels),
    )

    # ========================================================
    # DESTINATIONS
    # ========================================================

    for channel_index, channel_id in enumerate(
        channels
    ):

        try:

            logger.info(
                "Posting to destination %s/%s: %s",
                channel_index + 1,
                len(channels),
                channel_id,
            )

            # ------------------------------------------------
            # ALBUMS
            # ------------------------------------------------

            for album_index, chunk in enumerate(
                album_chunks
            ):

                logger.info(
                    "Sending album %s/%s to %s "
                    "(%s files)",
                    album_index + 1,
                    total_albums,
                    channel_id,
                    len(chunk),
                )

                album_caption = (
                    caption
                    if album_index == 0
                    else ""
                )

                if len(chunk) == 1:

                    await send_single_media(
                        bot,
                        channel_id,
                        chunk[0],
                        album_caption,
                    )

                else:

                    await send_album(
                        bot,
                        channel_id,
                        chunk,
                        album_caption,
                    )

                # Small gap between split albums.
                if album_index < total_albums - 1:

                    await asyncio.sleep(1)

            # ------------------------------------------------
            # INDIVIDUAL MEDIA
            # ------------------------------------------------

            for index, item in enumerate(
                other_media
            ):

                item_caption = ""

                if (
                    not album_media
                    and index == 0
                ):

                    item_caption = caption

                await send_single_media(
                    bot,
                    channel_id,
                    item,
                    item_caption,
                )

            logger.info(
                "Post successfully sent to %s",
                channel_id,
            )

        except BadRequest as error:

            # Permanent Telegram request error.
            logger.error(
                "Telegram rejected destination %s: %s",
                channel_id,
                error,
            )

        except Forbidden as error:

            # Bot doesn't have access/permission.
            logger.error(
                "Telegram permission/access error "
                "for %s: %s",
                channel_id,
                error,
            )

        except Exception:

            logger.exception(
                "Failed to send post to %s",
                channel_id,
            )

        # ----------------------------------------------------
        # DELAY BETWEEN DESTINATIONS
        # ----------------------------------------------------

        if (
            delay > 0
            and channel_index < len(channels) - 1
        ):

            logger.info(
                "Waiting %s seconds "
                "before next destination.",
                delay,
            )

            await asyncio.sleep(
                delay
            )


# ============================================================
# PERSISTENT JOB CALLBACK
# ============================================================

async def persistent_job_callback(context):

    job = context.job

    if not job:
        return

    job_id = job.data

    logger.info(
        "Executing persistent job %s",
        job_id,
    )

    stored_job = scheduler_db.get_job(
        job_id
    )

    if not stored_job:

        logger.warning(
            "Job %s no longer exists.",
            job_id,
        )

        return

    try:

        data = json.loads(
            stored_job["data"]
        )

        await dispatch_post(
            context.application,
            data,
        )

        logger.info(
            "Persistent job %s finished.",
            job_id,
        )

    except Exception:

        logger.exception(
            "Persistent job %s failed.",
            job_id,
        )

    finally:

        # ----------------------------------------------------
        # ONE-TIME JOB
        # ----------------------------------------------------

        if stored_job["job_type"] == "one_time":

            scheduler_db.delete_one_time_job(
                job_id
            )

        # ----------------------------------------------------
        # REPEATING JOB
        # ----------------------------------------------------

        else:

            interval = stored_job[
                "interval_seconds"
            ]

            if interval:

                next_run = (
                    utc_now()
                    + timedelta(
                        seconds=interval
                    )
                )

                scheduler_db.update_run_time(
                    job_id,
                    next_run.timestamp(),
                )

                logger.info(
                    "Next execution for "
                    "repeating job %s: %s",
                    job_id,
                    next_run.astimezone(IST),
                )


# ============================================================
# SCHEDULE SAVED JOB
# ============================================================

async def schedule_saved_job(
    application,
    job_id,
    run_at,
):
    stored_job = scheduler_db.get_job(
        job_id
    )

    if not stored_job:
        return

    # ========================================================
    # ONE-TIME JOB
    # ========================================================

    if stored_job["job_type"] == "one_time":

        if run_at.tzinfo is None:

            # Old/legacy naive datetime:
            # interpret it as IST.
            run_at = run_at.replace(
                tzinfo=IST
            )

        run_at = run_at.astimezone(
            UTC
        )

        current_time = utc_now()

        if run_at < current_time:

            run_at = current_time

        application.job_queue.run_once(
            persistent_job_callback,
            when=run_at,
            data=job_id,
            name=f"telepost_{job_id}",
        )

        logger.info(
            "Scheduled one-time job %s "
            "for %s",
            job_id,
            run_at.astimezone(IST),
        )

    # ========================================================
    # REPEATING JOB
    # ========================================================

    elif stored_job["job_type"] == "repeating":

        interval = stored_job[
            "interval_seconds"
        ]

        if not interval:

            logger.error(
                "Repeating job %s has no interval.",
                job_id,
            )

            return

        # First execution happens approximately
        # 2 seconds from now.
        first_run = (
            utc_now()
            + timedelta(
                seconds=2
            )
        )

        application.job_queue.run_repeating(
            persistent_job_callback,
            interval=interval,
            first=first_run,
            data=job_id,
            name=f"telepost_{job_id}",
        )

        # Store next execution time.
        scheduler_db.update_run_time(
            job_id,
            first_run.timestamp(),
        )

        logger.info(
            "Scheduled repeating job %s "
            "to run first at %s "
            "and repeat every %s seconds.",
            job_id,
            first_run.astimezone(IST),
            interval,
        )


# ============================================================
# RESTORE SAVED JOBS
# ============================================================

async def restore_saved_jobs(application):

    jobs = scheduler_db.get_active_jobs()

    if not jobs:

        logger.info(
            "No saved jobs to restore."
        )

        return

    logger.info(
        "Restoring %s saved jobs...",
        len(jobs),
    )

    current_time = utc_now()

    for job in jobs:

        try:

            if job["run_at"]:

                run_at = datetime.fromtimestamp(
                    job["run_at"],
                    tz=UTC,
                )

            else:

                run_at = current_time

            # ------------------------------------------------
            # ONE-TIME
            # ------------------------------------------------

            if job["job_type"] == "one_time":

                if run_at < current_time:

                    run_at = current_time

            # ------------------------------------------------
            # REPEATING
            # ------------------------------------------------

            elif job["job_type"] == "repeating":

                if run_at < current_time:

                    run_at = (
                        current_time
                        + timedelta(
                            seconds=2
                        )
                    )

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

async def post_init(application):

    await restore_saved_jobs(
        application
    )


# ============================================================
# /LISTJOBS
# ============================================================

async def listjobs_command(update, context):

    if not await owner_only(update, context):
        return

    jobs = scheduler_db.get_active_jobs()

    if not jobs:

        await update.message.reply_text(
            "📭 No active scheduled jobs."
        )

        return

    lines = [
        "📋 <b>Active Jobs</b>\n"
    ]

    for job in jobs:

        job_id = job["id"]

        job_type = job["job_type"]

        if job["run_at"]:

            run_at_text = format_ist(
                job["run_at"]
            )

        else:

            run_at_text = "N/A"

        try:

            job_data = json.loads(
                job["data"]
            )

            media_count = len(
                job_data.get(
                    "media",
                    [],
                )
            )

            destination_count = len(
                job_data.get(
                    "channels",
                    [],
                )
            )

        except Exception:

            media_count = "?"
            destination_count = "?"

        # ----------------------------------------------------
        # REPEATING
        # ----------------------------------------------------

        if job_type == "repeating":

            interval = job[
                "interval_seconds"
            ]

            if interval == 3600:

                interval_text = (
                    "Every hour"
                )

            elif interval == 86400:

                interval_text = (
                    "Every 24 hours"
                )

            else:

                interval_text = (
                    f"Every {interval}s"
                )

            lines.append(
                f"🔁 <code>{job_id}</code>\n"
                f"   {interval_text}\n"
                f"   Media: {media_count}/50\n"
                f"   Destinations: "
                f"{destination_count}\n"
                f"   Next: {run_at_text}\n"
            )

        # ----------------------------------------------------
        # ONE-TIME
        # ----------------------------------------------------

        else:

            lines.append(
                f"⏰ <code>{job_id}</code>\n"
                f"   One-time\n"
                f"   Media: {media_count}/50\n"
                f"   Destinations: "
                f"{destination_count}\n"
                f"   Run: {run_at_text}\n"
            )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# /CANCELJOB
# ============================================================

async def canceljob_command(update, context):

    if not await owner_only(update, context):
        return

    if not context.args:

        await update.message.reply_text(
            "Usage:\n"
            "/canceljob JOB_ID"
        )

        return

    job_id = context.args[0].strip()

    deleted = scheduler_db.delete_job(
        job_id
    )

    if not deleted:

        await update.message.reply_text(
            "❌ Job not found."
        )

        return

    for job in context.application.job_queue.jobs():

        if job.name == f"telepost_{job_id}":

            job.schedule_removal()

    await update.message.reply_text(
        f"🗑 Job <code>{html.escape(job_id)}</code> "
        "cancelled.",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# /CANCEL
# ============================================================

async def cancel_command(update, context):

    if not await owner_only(update, context):
        return ConversationHandler.END

    context.user_data.clear()

    await update.message.reply_text(
        "❌ Current post cancelled."
    )

    return ConversationHandler.END


# ============================================================
# /CHANNELID
# ============================================================

async def channelid_command(update, context):

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

        username = "@" + username

    try:

        chat = await retry_operation(
            lambda: context.bot.get_chat(
                username
            ),
            "get channel ID",
        )

        await update.message.reply_text(
            "📢 <b>Chat information</b>\n\n"
            f"Title: "
            f"{html.escape(chat.title or 'Unknown')}\n"
            f"ID: <code>{chat.id}</code>\n"
            f"Username: "
            f"{html.escape(chat.username or 'None')}",
            parse_mode=ParseMode.HTML,
        )

    except BadRequest as error:

        await update.message.reply_text(
            "❌ Telegram could not find or access "
            "that chat.\n\n"
            f"Error: {error}"
        )

    except Forbidden as error:

        await update.message.reply_text(
            "❌ The bot does not have access "
            "to that chat.\n\n"
            f"Error: {error}"
        )

    except Exception as error:

        logger.exception(
            "Failed to get channel ID."
        )

        await update.message.reply_text(
            "❌ Could not access that chat.\n\n"
            f"Error: {error}"
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context,
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

    # ========================================================
    # CONVERSATION
    # ========================================================

    conversation = ConversationHandler(

        entry_points=[
            CommandHandler(
                "newpost",
                newpost_command,
            )
        ],

        states={

            # ------------------------------------------------
            # MEDIA
            # ------------------------------------------------

            WAITING_MEDIA: [

                MessageHandler(
                    (
                        filters.PHOTO
                        | filters.VIDEO
                        | filters.Document.ALL
                        | filters.AUDIO
                        | filters.VOICE
                        | filters.ANIMATION
                    ),
                    receive_media,
                ),

                CommandHandler(
                    "done",
                    done_media,
                ),
            ],

            # ------------------------------------------------
            # CAPTION
            # ------------------------------------------------

            WAITING_CAPTION: [

                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_caption,
                ),
            ],

            # ------------------------------------------------
            # DESTINATIONS
            # ------------------------------------------------

            WAITING_CHANNELS: [

                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_channels,
                ),
            ],

            # ------------------------------------------------
            # DELAY
            # ------------------------------------------------

            WAITING_DELAY: [],
        },

        fallbacks=[
            CommandHandler(
                "cancel",
                cancel_command,
            )
        ],

        allow_reentry=True,
    )

    application.add_handler(
        conversation
    )

    # ========================================================
    # COMMANDS
    # ========================================================

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

    # ========================================================
    # DELAY CALLBACK
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            delay_callback,
            pattern=r"^delay_\d+$",
        )
    )

    # ========================================================
    # SCHEDULE CALLBACK
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            schedule_callback,
            pattern=(
                r"^schedule_"
                r"(now|1h|24h|hourly|daily)$"
            ),
        )
    )

    # ========================================================
    # ERROR HANDLER
    # ========================================================

    application.add_error_handler(
        error_handler
    )

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
            "OWNER_ID is missing or invalid "
            "in .env"
        )

    logger.info("=" * 60)

    logger.info(
        "Telepost starting..."
    )

    logger.info(
        "Owner ID: %s",
        OWNER_ID,
    )

    logger.info(
        "Maximum media per post: %s",
        MAX_MEDIA,
    )

    logger.info(
        "Telegram album size: %s",
        TELEGRAM_ALBUM_SIZE,
    )

    logger.info(
        "Scheduler timezone: UTC"
    )

    logger.info(
        "Display timezone: Asia/Kolkata"
    )

    logger.info(
        "Database: %s",
        scheduler_db.database,
    )

    logger.info("=" * 60)

    application = build_application()

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()