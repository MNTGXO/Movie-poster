# Add to imports
from typing import Dict, Optional

# Add to Config section
MOVIE_CHANNELS = {}  # type: Dict[str, int]  # {"movie_norm": channel_id}

# Add new handler
@app.on_message(filters.command("link") & admin)
async def link_movie(_, msg):
    """Usage: /link movie_norm @channel"""
    if len(msg.command) < 3:
        return await msg.reply("Usage: /link <movie_norm> @channel")
    
    try:
        movie_norm = msg.command[1].lower()
        channel = msg.command[2]
        if channel.startswith("@"):
            channel = channel[1:]
        
        # Resolve channel ID
        chat = await app.get_chat(channel)
        if chat.type not in (ChatType.CHANNEL, ChatType.SUPERGROUP):
            return await msg.reply("Must be a channel or supergroup")
        
        # Store mapping
        MOVIE_CHANNELS[movie_norm] = chat.id
        await db.movie_channels.update_one(
            {"_id": movie_norm},
            {"$set": {"channel_id": chat.id}},
            upsert=True
        )
        await msg.reply(f"✅ Linked {movie_norm} to {chat.title}")
    except Exception as e:
        await msg.reply(f"Error: {str(e)}")

# Modify posting logic (where movies are forwarded)
async def forward_to_linked_channel(movie_norm: str, message):
    # Check DB if not in memory
    if movie_norm not in MOVIE_CHANNELS:
        mapping = await db.movie_channels.find_one({"_id": movie_norm})
        if mapping:
            MOVIE_CHANNELS[movie_norm] = mapping["channel_id"]
    
    if movie_norm in MOVIE_CHANNELS:
        try:
            await message.copy(MOVIE_CHANNELS[movie_norm])
            return True
        except Exception as e:
            log.error(f"Failed to forward to linked channel: {e}")
    return False

# In your movie processing function, replace POST_CHANNEL logic with:
if not await forward_to_linked_channel(movie_norm, message):
    await message.reply("No linked channel for this movie. Use /link to set one.")
