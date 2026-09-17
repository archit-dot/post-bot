# Telepost

Telepost is a Telegram bot for creating, scheduling and repeating media posts
to one or multiple Telegram channels.

## Features

- Owner-only access
- Photo posting
- Video posting
- Document posting
- Audio posting
- Voice message posting
- GIF/animation posting
- Media albums
- HTML captions
- Multiple destination channels
- Configurable delay between channels
- Post immediately
- Schedule for later
- Repeat every hour
- Repeat every 24 hours
- Persistent SQLite database
- Automatic job restoration after restart
- Retry handling for temporary Telegram/network errors
- Telegram rate-limit handling
- Logging
- `/listjobs`
- `/canceljob`
- `/channelid`

## Requirements

- Python 3.11+
- Telegram bot token
- Telegram user ID
- Bot must have permission to post in destination channels

## Installation

Clone the repository:

```bash
git clone YOUR_REPOSITORY_URL
cd telepost

Create a virtual environment:

``bash
 python -m venv .venv

 Activate it.

Windows PowerShell:

.venv\Scripts\Activate.ps1

LINUX/VPS:

source .venv/bin/activate

Install dependencies:

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Configuration

Create .env:

BOT_TOKEN=YOUR_BOT_TOKEN
OWNER_ID=YOUR_TELEGRAM_USER_ID

Never commit .env.

Running
python bot.py
Commands
Start
/start
Create a post
/newpost

Send media, then:

/done

Enter the caption.

Enter destination channel IDs.

Select the delay.

Then select:

Post Now
In 1 Hour
In 24 Hours
Every Hour
Every 24 Hours
View scheduled jobs
/listjobs
Cancel a job
/canceljob JOB_ID

Example:

/canceljob a12bc345
Get public channel ID
/channelid @channelusername
Database

Telepost uses SQLite.

The database is automatically created at:

data/telepost.db

The database is intentionally excluded from Git.

Scheduled jobs survive bot restarts because job information is stored in SQLite.

Logging

Logs are stored in:

logs/bot.log

Logs are excluded from Git.

Telegram permissions

The bot must be able to post in destination channels.

For scheduled posts to work, keep the bot as an administrator in the destination channel.

Security:

The bot accepts administrative commands only from the configured OWNER_ID.

The bot token is stored in .env and should never be committed to source control.

Production:

For 24/7 operation on Linux/VPS, run Telepost using systemd.
