import asyncio
import difflib
import html
import logging
import os
import re
import tempfile

from aiohttp import ClientSession, web
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, filters, idle
from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup
from pyrogram.types import InputMediaPhoto

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("moviebot")

from config import (  # noqa: E402
    API_ID, API_HASH, BOT_TOKEN, MONGO_URI, DB_NAME, TMDB_API_KEY, ADMINS, POST_CHANNEL,
    DEFAULT_QUALITY, PORT, AUTO_CAPTION, BUTTONS, LOG_LEVEL,
)
from config import SOURCE_CHANNELS as ENV_SOURCES  # noqa: E402
from mediainfo import build_caption  # noqa: E402

BUTTON_MARKUP = Markup([[Btn(text, url=url) for text, url in row] for row in BUTTONS])

TMDB = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w780"

app = Client(
    "moviebot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,  # no session file needed on Koyeb's ephemeral disk
    parse_mode=ParseMode.HTML,
)
db = AsyncIOMotorClient(MONGO_URI)[DB_NAME]
_http: ClientSession | None = None


def http() -> ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = ClientSession()
    return _http


def esc(s) -> str:
    return html.escape(str(s or ""))


# ───────────────────────── TMDB ─────────────────────────
async def tmdb(path: str, **params):
    params["api_key"] = TMDB_API_KEY
    async with http().get(TMDB + path, params=params) as r:
        r.raise_for_status()
        return await r.json()


async def download(url: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    async with http().get(url) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(await r.read())
    return path


# ───────────────────────── Smart name detection ─────────────────────────
JUNK = {
    "480p", "540p", "576p", "720p", "1080p", "1440p", "2160p", "4k", "uhd", "hdrip", "webrip", "web", "dl",
    "webdl", "bluray", "brrip", "bdrip", "dvdrip", "dvdscr", "hdtc", "hdts", "hdcam", "camrip", "cam", "predvd",
    "pre", "hevc", "x264", "x265", "h264", "h265", "avc", "10bit", "aac", "ac3", "dd", "ddp", "ddp5", "dd5",
    "atmos", "esub", "esubs", "msub", "sub", "subs", "hq", "hdr", "proper", "repack", "amzn", "nf", "zee5", "z5",
    "hotstar", "sonyliv", "jio", "dual", "audio", "multi", "org", "malayalam", "tamil", "telugu", "hindi",
    "kannada", "english", "mal", "tam", "tel", "hin", "mkv", "mp4", "full", "movie", "true", "clean",
}
YEAR = re.compile(r"^(19|20)\d{2}$")
QUALITIES = {
    "hdrip": "HDRip", "webrip": "WEBRip", "web-dl": "WEB-DL", "webdl": "WEB-DL", "bluray": "BluRay",
    "brrip": "BRRip", "dvdrip": "DVDRip", "hdtc": "HDTC", "predvd": "PreDVD", "hdcam": "HDCAM",
    "camrip": "CAMRip", "480p": "480p", "720p": "720p", "1080p": "1080p", "4k": "4K",
}


def parse_name(text: str):
    """'@ch - Bandra.2026.720p.HDRip.mkv' / 'Bandra (2026)' -> ('bandra', 2026)"""
    if not text:
        return "", None
    text = re.sub(r"\.(mkv|mp4|avi|webm|mov|m4v|zip|rar)\s*$", "", text, flags=re.I)
    text = re.sub(r"^\s*(\[[^\]]*\]\s*)+", "", text)          # leading [tags]
    text = re.sub(r"^\s*(https?://)?www\.\S+\s*[-–|:]*\s*", "", text, flags=re.I)  # leading site
    text = re.sub(r"@\w+", " ", text)                           # @handles
    text = text.lower().replace("&", " and ")
    text = re.sub(r"['’`]", "", text)
    text = re.sub(r"[\[\]\(\)\{\}._\-+,:;|~!]", " ", text)
    tokens = text.split()
    if not tokens:
        return "", None

    year_idx = next((i for i, t in enumerate(tokens) if i > 0 and YEAR.match(t)), None)
    if year_idx is not None:
        title_tokens, year = tokens[:year_idx], int(tokens[year_idx])
    else:
        year = None
        cut = next((i for i, t in enumerate(tokens) if i > 0 and t in JUNK), len(tokens))
        title_tokens = tokens[:cut]
    norm = "".join(re.sub(r"[^a-z0-9]", "", t) for t in title_tokens)
    return norm, year


def best_match(movies: list, norm: str, year):
    best, best_score = None, 0.0
    for m in movies:
        if year and m.get("year") and year != m["year"]:
            continue
        for key in m.get("keys", []):
            if not key:
                continue
            score = 1.0 if key == norm else difflib.SequenceMatcher(None, key, norm).ratio()
            need = 0.9 if year else 0.96  # be stricter when the file has no year
            if score >= need and score > best_score:
                best, best_score = m, score
    return best


# ───────────────────────── Filters ─────────────────────────
async def _is_source(_, __, m):
    if not (m.document or m.video):
        return False
    if m.chat.type == ChatType.PRIVATE:
        return bool(m.from_user and m.from_user.id in ADMINS)
    if m.chat.id in ENV_SOURCES:
        return True
    return await db.sources.count_documents({"_id": m.chat.id}, limit=1) > 0


async def _awaiting(_, __, m):
    if not m.from_user or m.document or m.video:
        return False
    if m.text and m.text.startswith("/"):
        return False
    return await db.sessions.count_documents({"_id": m.from_user.id, "state": "await_channel"}, limit=1) > 0


is_source = filters.create(_is_source)
awaiting = filters.create(_awaiting)
admin = filters.user(ADMINS)


# ───────────────────────── Helpers ─────────────────────────
def render(sess):
    i, results = sess["page"], sess["results"]
    r = results[i]
    cap = (
        f"<b>{esc(r['title'])}</b> ({r['year'] or 'N/A'})\n"
        f"⭐ {r['rating']:.1f}  •  {i + 1}/{len(results)}\n\n"
        f"{esc(r['overview'][:350])}"
    )
    nav = []
    if i > 0:
        nav.append(Btn("⬅️ Prev", callback_data=f"mv:pg:{i - 1}"))
    if i < len(results) - 1:
        nav.append(Btn("Next ➡️", callback_data=f"mv:pg:{i + 1}"))
    rows = [nav] if nav else []
    rows.append([Btn("✅ Select", callback_data=f"mv:sel:{i}")])
    rows.append([Btn("❌ Cancel", callback_data="mv:x")])
    return IMG + r["poster"], cap, Markup(rows)


def post_caption(m: dict) -> str:
    return (
        f"🎬 Title : {esc(m['title'])}\n"
        f"📅 Year : {m['year'] or 'N/A'}\n"
        f"🔊 Language : {esc(m['language'])}\n"
        f"🎞 Quality : <b>{esc(m['quality'])}</b>"
    )


def post_markup(m: dict) -> Markup:
    return Markup([[Btn("📥 DOWNLOAD NOW 📥", url=m["invite_link"])]])


async def resolve_chat(c: Client, m, text: str | None = None):
    """Forwarded msg from channel / numeric id / t.me link / @username -> Chat"""
    fc = getattr(m, "forward_from_chat", None) or getattr(getattr(m, "forward_origin", None), "chat", None)
    if fc:
        return await c.get_chat(fc.id)
    t = (text if text is not None else (m.text or "")).strip()
    if not t:
        return None
    if re.fullmatch(r"-?\d+", t):
        ref = int(t) if t.startswith("-") else int("-100" + t)
    elif (mm := re.search(r"t\.me/c/(\d+)", t)):
        ref = int("-100" + mm.group(1))
    elif (mm := re.search(r"t\.me/([A-Za-z0-9_]{4,})", t)) and mm.group(1) not in ("joinchat", "c"):
        ref = "@" + mm.group(1)
    elif t.startswith("@"):
        ref = t
    else:
        return None
    return await c.get_chat(ref)


# ───────────────────────── Commands ─────────────────────────
@app.on_message(filters.command(["start", "help"]) & filters.private)
async def start(_, m):
    if m.from_user.id not in ADMINS:
        return await m.reply(
            "👋 Movie bot.\n\n"
            f"Your ID: <code>{m.from_user.id}</code>\n"
            "If you are the owner, add this ID to the <code>ADMINS</code> environment variable and redeploy."
        )
    await m.reply(
        "<b>Admin commands</b>\n"
        "/link <code>Bandra 2026</code> [quality] – link a movie to a channel\n"
        "/addsource <code>id|link</code> – add a source channel (or reply to a forwarded msg)\n"
        "/delsource <code>id</code> – remove a source channel\n"
        "/sources – list source channels\n"
        "/movies – list linked movies\n"
        "/delmovie <code>tmdb_id</code> – unlink a movie\n\n"
        "Send files to this chat or post them in a source channel – the bot routes them automatically."
    )


@app.on_message(filters.command("id"))
async def my_id(_, m):
    await m.reply(f"Your ID: <code>{m.from_user.id}</code>\nChat ID: <code>{m.chat.id}</code>")


ADMIN_CMDS = ["link", "addsource", "delsource", "sources", "movies", "delmovie"]


@app.on_message(filters.command(ADMIN_CMDS) & ~admin)
async def not_admin(_, m):
    await m.reply(
        "⛔ You are not an admin of this bot.\n"
        f"Your ID: <code>{m.from_user.id}</code>\n"
        "Add it to the <code>ADMINS</code> environment variable and redeploy."
    )


@app.on_message(filters.command("link") & admin)
async def link_cmd(c, m):
    args = m.command[1:]
    if not args:
        return await m.reply("Usage: <code>/link Bandra 2026</code>  (optional quality at the end, e.g. <code>HDRip</code>)")
    quality = DEFAULT_QUALITY
    if args[-1].lower() in QUALITIES:
        quality = QUALITIES[args.pop().lower()]
    year = None
    if len(args) > 1 and YEAR.match(args[-1].strip("()")):
        year = args.pop().strip("()")
    query = " ".join(args)

    try:
        data = await tmdb("/search/movie", query=query, **({"year": year} if year else {}))
        if not data["results"] and year:
            data = await tmdb("/search/movie", query=query)
    except Exception as e:
        return await m.reply(f"❌ TMDB error: <code>{esc(e)}</code>")

    results = [
        {
            "id": r["id"],
            "title": r["title"],
            "original_title": r.get("original_title") or r["title"],
            "year": int(r["release_date"][:4]) if r.get("release_date") else None,
            "poster": r["poster_path"],
            "overview": r.get("overview") or "No overview.",
            "rating": r.get("vote_average") or 0,
            "lang": r.get("original_language"),
        }
        for r in data["results"][:20]
        if r.get("poster_path")
    ]
    if not results:
        return await m.reply("❌ No results (with poster) found on TMDB.")

    sess = {"_id": m.from_user.id, "results": results, "page": 0, "quality": quality, "state": "choose"}
    await db.sessions.replace_one({"_id": sess["_id"]}, sess, upsert=True)
    photo, cap, mk = render(sess)
    await m.reply_photo(photo, caption=cap, reply_markup=mk)


@app.on_callback_query(filters.regex(r"^mv:") & admin)
async def cb(c, q):
    parts = q.data.split(":")
    act = parts[1]
    uid = q.from_user.id
    if act == "x":
        await db.sessions.delete_one({"_id": uid})
        await q.message.delete()
        return
    sess = await db.sessions.find_one({"_id": uid})
    if not sess:
        return await q.answer("Session expired – run /link again.", show_alert=True)

    if act == "pg":
        sess["page"] = int(parts[2])
        await db.sessions.update_one({"_id": uid}, {"$set": {"page": sess["page"]}})
        photo, cap, mk = render(sess)
        try:
            await q.message.edit_media(InputMediaPhoto(photo, caption=cap), reply_markup=mk)
        except MessageNotModified:
            pass
        await q.answer()
    elif act == "sel":
        i = int(parts[2])
        await db.sessions.update_one({"_id": uid}, {"$set": {"chosen": i, "state": "await_channel"}})
        r = sess["results"][i]
        await q.message.edit_caption(
            f"✅ Selected: <b>{esc(r['title'])}</b> ({r['year']})\n\n"
            "Now <b>forward any message</b> from the movie channel, or send its <b>link / @username / ID</b>.\n"
            "The bot must be an admin there (change info + invite users).",
            reply_markup=Markup([[Btn("❌ Cancel", callback_data="mv:x")]]),
        )
        await q.answer()


@app.on_message(filters.private & admin & awaiting)
async def channel_input(c, m):
    uid = m.from_user.id
    sess = await db.sessions.find_one({"_id": uid})
    r = sess["results"][sess["chosen"]]

    try:
        chat = await resolve_chat(c, m)
    except Exception as e:
        return await m.reply(f"❌ Can't access that chat: <code>{esc(e)}</code>\nMake sure the bot is added as admin.")
    if chat is None:
        return await m.reply("Send a forwarded channel message, a channel link, @username or numeric ID.")
    if chat.type not in (ChatType.CHANNEL, ChatType.SUPERGROUP):
        return await m.reply("❌ That's not a channel/group.")
    try:
        me = await c.get_chat_member(chat.id, "me")
        if me.status != ChatMemberStatus.ADMINISTRATOR:
            raise ValueError("not admin")
    except Exception:
        return await m.reply("❌ Make the bot an admin in that channel first, then send it again.")

    wait = await m.reply("⏳ Setting up…")
    notes = []

    try:
        details = await tmdb(f"/movie/{r['id']}")
        langs = details.get("spoken_languages") or []
        language = next((l["english_name"] for l in langs if l["iso_639_1"] == r["lang"]), None) \
            or (langs[0]["english_name"] if langs else (r["lang"] or "N/A").upper())
    except Exception:
        language = (r["lang"] or "N/A").upper()

    # channel title
    try:
        await c.set_chat_title(chat.id, f"{r['title']} ({r['year']})" if r["year"] else r["title"])
    except Exception as e:
        notes.append(f"⚠️ Title not changed: {esc(e)}")

    # channel photo
    path = None
    try:
        path = await download(IMG + r["poster"])
        await c.set_chat_photo(chat.id, photo=path)
    except Exception as e:
        notes.append(f"⚠️ Photo not changed: {esc(e)}")
    finally:
        if path and os.path.exists(path):
            os.remove(path)

    # join-request invite link (reuse if this channel already has one)
    old = await db.movies.find_one({"channel_id": chat.id})
    invite = old["invite_link"] if old else None
    if not invite:
        try:
            il = await c.create_chat_invite_link(chat.id, name="Movie Request", creates_join_request=True)
            invite = il.invite_link
        except Exception as e:
            await wait.edit_text(f"❌ Couldn't create invite link: <code>{esc(e)}</code>")
            return

    movie = {
        "_id": r["id"],
        "title": r["title"],
        "year": r["year"],
        "keys": list({parse_name(r["title"])[0], parse_name(r["original_title"])[0]} - {""}),
        "channel_id": chat.id,
        "invite_link": invite,
        "poster": IMG + r["poster"],
        "language": language,
        "quality": sess["quality"],
    }
    if old and old["_id"] != r["id"]:
        await db.movies.delete_one({"_id": old["_id"]})  # channel re-linked to a different movie
    await db.movies.replace_one({"_id": r["id"]}, movie, upsert=True)
    await db.sessions.delete_one({"_id": uid})

    targets = [m.chat.id] + ([POST_CHANNEL] if POST_CHANNEL else [])
    for t in targets:
        try:
            await c.send_photo(t, movie["poster"], caption=post_caption(movie), reply_markup=post_markup(movie))
        except Exception as e:
            notes.append(f"⚠️ Couldn't post to {t}: {esc(e)}")
    await wait.edit_text(
        f"✅ Linked <b>{esc(movie['title'])}</b> → <code>{chat.id}</code>\n" + "\n".join(notes)
    )


# ───────────── Source channel management ─────────────
@app.on_message(filters.command("addsource") & admin)
async def addsource(c, m):
    arg = m.command[1] if len(m.command) > 1 else None
    try:
        chat = await resolve_chat(c, m.reply_to_message, arg) if m.reply_to_message and not arg \
            else await resolve_chat(c, m, arg)
    except Exception as e:
        return await m.reply(f"❌ <code>{esc(e)}</code>")
    if not chat:
        return await m.reply("Usage: <code>/addsource -100123…</code> or reply to a forwarded channel message.")
    await db.sources.replace_one({"_id": chat.id}, {"_id": chat.id, "title": chat.title}, upsert=True)
    await m.reply(f"✅ Source added: <b>{esc(chat.title)}</b> (<code>{chat.id}</code>)")


@app.on_message(filters.command("delsource") & admin)
async def delsource(c, m):
    if len(m.command) < 2:
        return await m.reply("Usage: <code>/delsource -100123…</code>")
    res = await db.sources.delete_one({"_id": int(m.command[1])})
    await m.reply("🗑 Removed." if res.deleted_count else "Not found.")


@app.on_message(filters.command("sources") & admin)
async def sources(c, m):
    docs = await db.sources.find().to_list(None)
    lines = [f"• {esc(d.get('title'))} <code>{d['_id']}</code>" for d in docs]
    lines += [f"• (env) <code>{i}</code>" for i in ENV_SOURCES]
    await m.reply("\n".join(lines) or "No source channels yet.")


@app.on_message(filters.command("movies") & admin)
async def movies_cmd(c, m):
    docs = await db.movies.find().to_list(None)
    lines = [f"• <b>{esc(d['title'])}</b> ({d['year']}) → <code>{d['channel_id']}</code>  [id <code>{d['_id']}</code>]" for d in docs]
    await m.reply("\n".join(lines)[:4000] or "No movies linked yet.")


@app.on_message(filters.command("delmovie") & admin)
async def delmovie(c, m):
    if len(m.command) < 2 or not m.command[1].isdigit():
        return await m.reply("Usage: <code>/delmovie tmdb_id</code>")
    res = await db.movies.delete_one({"_id": int(m.command[1])})
    await m.reply("🗑 Unlinked." if res.deleted_count else "Not found.")


# ───────────────────────── File routing ─────────────────────────
@app.on_message(is_source, group=1)
async def on_file(c, m):
    media = m.document or m.video
    caption_line = (m.caption or "").strip().split("\n")[0]
    movies = await db.movies.find().to_list(None)
    if not movies:
        return

    movie = None
    for cand in (getattr(media, "file_name", None), caption_line):
        norm, year = parse_name(cand or "")
        if norm and (movie := best_match(movies, norm, year)):
            break

    if not movie:
        if m.chat.type == ChatType.PRIVATE:
            await m.reply("❓ No linked movie matches this file name.", quote=True)
        return

    if await db.sent.count_documents({"channel": movie["channel_id"], "uid": media.file_unique_id}, limit=1):
        log.info("duplicate skipped: %s", getattr(media, "file_name", ""))
        if m.chat.type == ChatType.PRIVATE:
            await m.reply("⚠️ Already sent to that channel.", quote=True)
        return

    caption = None
    if AUTO_CAPTION:
        try:
            caption = await build_caption(c, m)
        except Exception as e:
            log.warning("auto caption failed: %s", e)

    for _ in range(3):
        try:
            # caption=None keeps the original caption if analysis failed
            await m.copy(movie["channel_id"], caption=caption, reply_markup=BUTTON_MARKUP)
            await db.sent.insert_one({"channel": movie["channel_id"], "uid": media.file_unique_id})
            log.info("sent %s → %s", getattr(media, "file_name", ""), movie["title"])
            if m.chat.type == ChatType.PRIVATE:
                await m.reply(f"✅ Sent to <b>{esc(movie['title'])}</b> channel.", quote=True)
            break
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except Exception as e:
            log.error("copy failed: %s", e)
            if m.chat.type == ChatType.PRIVATE:
                await m.reply(f"❌ Failed: <code>{esc(e)}</code>", quote=True)
            break


# ───────────────────────── Koyeb health server + main ─────────────────────────
async def health(_):
    return web.Response(text="ok")


async def main():
    await db.sent.create_index([("channel", 1), ("uid", 1)], unique=True)
    await db.movies.create_index("channel_id")

    web_app = web.Application()
    web_app.router.add_get("/", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    if not ADMINS:
        log.warning("ADMINS is empty – set ADMINS=your_telegram_id or no command will work!")
    else:
        log.info("Admins: %s", ADMINS)
    await app.start()
    log.info("Bot started as @%s", (await app.get_me()).username)
    await idle()
    await app.stop()
    if _http:
        await _http.close()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
