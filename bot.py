from pyrogram import filters, enums
from pyrogram.types import (
    Message, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    CallbackQuery
)
from typing import Dict, List, Optional
import re
from tmdbv3api import TMDb, Search

# Initialize TMDB
tmdb = TMDb()
tmdb.api_key = os.getenv("TMDB_API_KEY")
tmdb.language = 'en'
movie_search = Search()

# Database collections
MOVIE_CHANNELS = {}  # Cache
SOURCE_CHANNELS = []  # List of monitored channels

# --- Helper Functions ---
def normalize_movie_name(name: str) -> str:
    """Normalize movie names for matching"""
    name = re.sub(r'[^\w\s]', '', name.lower())  # Remove special chars
    name = re.sub(r'\s+', ' ', name).strip()     # Normalize spaces
    year_match = re.search(r'(19|20)\d{2}', name)
    year = f"({year_match.group()})" if year_match else ""
    name = re.sub(r'(19|20)\d{2}', '', name).strip()
    return f"{name} {year}".strip()

async def get_tmdb_results(query: str) -> List[dict]:
    """Search TMDB for movies"""
    results = movie_search.movies({"query": query})
    return [{
        'id': m.id,
        'title': m.title,
        'year': m.release_date[:4] if m.release_date else "N/A",
        'poster': f"https://image.tmdb.org/t/p/w500{m.poster_path}" if m.poster_path else None
    } for m in results]

# --- Handlers ---
@app.on_message(filters.command("link") & admin)
async def link_movie_start(_, msg: Message):
    """Start movie linking process"""
    if len(msg.command) < 2:
        return await msg.reply("Usage: /link <movie_name>")
    
    raw_name = " ".join(msg.command[1:])
    normalized = normalize_movie_name(raw_name)
    
    # Step 1: Search TMDB
    results = await get_tmdb_results(normalized)
    if not results:
        return await msg.reply("No TMDB results found")
    
    # Create inline keyboard with results
    buttons = []
    for movie in results[:5]:  # Show top 5 results
        btn_text = f"{movie['title']} ({movie['year']})"
        buttons.append([InlineKeyboardButton(
            btn_text, 
            callback_data=f"select_movie:{movie['id']}:{normalized}"
        )])
    
    await msg.reply(
        "Select the correct movie:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@app.on_callback_query(filters.regex(r"^select_movie:(\d+):(.+)$"))
async def select_movie_callback(_, query: CallbackQuery):
    """Handle movie selection from TMDB results"""
    movie_id, raw_name = query.data.split(":")[1:]
    movie = movie_search.movie_details(movie_id)
    
    # Ask for channel
    await query.message.edit(
        f"Selected: **{movie.title}** ({movie.release_date[:4]})\n\n"
        "Now forward a message from the channel you want to link or send the channel ID/username:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Cancel", "cancel_link")
        ]])
    )
    # Store temp data
    await db.temp_links.insert_one({
        "user_id": query.from_user.id,
        "movie_id": movie_id,
        "movie_title": movie.title,
        "movie_year": movie.release_date[:4],
        "normalized_name": normalize_movie_name(raw_name),
        "poster_url": f"https://image.tmdb.org/t/p/w500{movie.poster_path}" if movie.poster_path else None
    })

@app.on_message(filters.forwarded & admin)
async def handle_channel_forward(_, msg: Message):
    """Handle forwarded channel message for linking"""
    if not msg.forward_from_chat or msg.forward_from_chat.type not in (enums.ChatType.CHANNEL, enums.ChatType.SUPERGROUP):
        return
    
    # Get temp data
    temp_data = await db.temp_links.find_one({"user_id": msg.from_user.id})
    if not temp_data:
        return
    
    channel = msg.forward_from_chat
    normalized_name = temp_data["normalized_name"]
    
    # Create invite link
    try:
        invite_link = await app.create_chat_invite_link(
            channel.id, 
            member_limit=1,
            name=f"MovieBot: {temp_data['movie_title']}"
        )
    except Exception:
        invite_link = None
    
    # Update channel info
    try:
        if temp_data.get("poster_url"):
            async with aiohttp.ClientSession() as session:
                async with session.get(temp_data["poster_url"]) as resp:
                    if resp.status == 200:
                        photo = await resp.read()
                        await app.set_chat_photo(channel.id, photo)
        
        await app.set_chat_title(
            channel.id,
            f"{temp_data['movie_title']} ({temp_data['movie_year']})"
        )
    except Exception as e:
        log.error(f"Couldn't update channel: {e}")
    
    # Store mapping
    await db.movie_channels.update_one(
        {"_id": normalized_name},
        {"$set": {
            "channel_id": channel.id,
            "movie_id": temp_data["movie_id"],
            "invite_link": invite_link.invite_link if invite_link else None,
            "title": temp_data["movie_title"],
            "year": temp_data["movie_year"]
        }},
        upsert=True
    )
    
    # Create confirmation message
    buttons = []
    if invite_link:
        buttons.append([InlineKeyboardButton(
            "Join Channel", 
            url=invite_link.invite_link
        )])
    
    await msg.reply(
        f"✅ Successfully linked **{temp_data['movie_title']}** to channel: {channel.title}",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    
    # Cleanup
    await db.temp_links.delete_one({"_id": temp_data["_id"]})
    MOVIE_CHANNELS[normalized_name] = channel.id

# --- File Processing ---
async def process_movie_file(message: Message):
    """Process incoming movie files"""
    if not message.document and not message.video:
        return
    
    filename = message.document.file_name if message.document else message.video.file_name
    normalized = normalize_movie_name(filename)
    
    # Check if we have a linked channel
    if normalized in MOVIE_CHANNELS or await db.movie_channels.find_one({"_id": normalized}):
        channel_id = MOVIE_CHANNELS.get(normalized)
        if not channel_id:
            mapping = await db.movie_channels.find_one({"_id": normalized})
            channel_id = mapping["channel_id"]
            MOVIE_CHANNELS[normalized] = channel_id
        
        try:
            await message.copy(channel_id)
            return
        except Exception as e:
            log.error(f"Failed to forward to channel: {e}")
    
    # Fallback to default channel if set
    if POST_CHANNEL:
        await message.copy(POST_CHANNEL)

# --- Source Channel Monitoring ---
@app.on_message(filters.chat(SOURCE_CHANNELS))
async def handle_source_channels(_, msg: Message):
    """Monitor source channels for movie files"""
    await process_movie_file(msg)
