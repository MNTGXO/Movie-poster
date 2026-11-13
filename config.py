import os


def _ids(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


# ── Telegram ──
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMINS = _ids(os.environ.get("ADMINS") or os.environ.get("ADMIN_ID", ""))
SOURCE_CHANNELS = _ids(os.environ.get("SOURCE_CHANNELS", ""))
POST_CHANNEL = int(os.environ["POST_CHANNEL"]) if os.environ.get("POST_CHANNEL") else None

# ── Database / TMDB ──
MONGO_URI = os.environ["MONGO_URI"]
DB_NAME = os.environ.get("DB_NAME", "moviebot")
TMDB_API_KEY = os.environ["TMDB_API_KEY"]  # v3 key

# ── Misc ──
DEFAULT_QUALITY = os.environ.get("DEFAULT_QUALITY", "HDRip")
PORT = int(os.environ.get("PORT", 8000))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# ── Auto caption (MediaInfo) ──
AUTO_CAPTION = os.environ.get("AUTO_CAPTION", "true").lower() == "true"
FULL_DOWNLOAD_LIMIT_MB = int(os.environ.get("FULL_DOWNLOAD_LIMIT_MB", 1024))  # fallback if header scan fails
FULL_DOWNLOAD_TIMEOUT = int(os.environ.get("FULL_DOWNLOAD_TIMEOUT", 300))

CAPTION_TEMPLATE = os.environ.get(
    "CAPTION_TEMPLATE",
    "<b>{title}</b>\n\n"
    "🎬 <b>{video_line}</b> | ⏳ <b>{duration}</b>\n"
    "🔊 <b>{audio}</b>\n"
    "💬 <b>{subtitle}</b>",
)

# ── Inline buttons added to every file sent to a movie channel ──
BUTTONS = [
    [("🎬 Movie Group", "https://t.me/+oPHDpmdBehk0OGM9"),
     ("📢 OTT Release", "https://t.me/Mn_OTT_Updates/10")],
    [("📺 Our All Channels", "https://t.me/MnMoviesLinks/4")],
]
