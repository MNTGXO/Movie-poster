### File: `README.md` ```markdown
# Movie Poster Bot

A Telegram bot that automatically posts movie posters and metadata from TMDB when files are shared in source channels.

## Features

- Auto-detects movie names from filenames
- Fetches metadata from TMDB API
- Posts posters with formatted captions to destination channel
- Supports multiple source channels
- Quality detection and filtering

## Setup

1. Clone this repository
2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Configure `config.py` with your:
   - Telegram API credentials
   - TMDB API key
   - MongoDB URI
   - Admin user IDs
   - Source/Destination channels

4. Run the bot:
```bash
python bot.py
```

## Docker

Build and run with Docker:
```bash
docker build -t moviebot .
docker run -p 8000:8000 moviebot
```

## Environment Variables

Configure via `config.py`:
- `API_ID` - Telegram API ID
- `API_HASH` - Telegram API hash
- `BOT_TOKEN` - Telegram bot token
- `MONGO_URI` - MongoDB connection string
- `TMDB_API_KEY` - The Movie Database API key
- `ADMINS` - List of admin user IDs
- `POST_CHANNEL` - Destination channel ID
- `SOURCE_CHANNELS` - List of source channel IDs

## Requirements

- Python 3.11+
- Pyrofork
- Motor (async MongoDB driver)
- aiohttp
- mediainfo (system package)