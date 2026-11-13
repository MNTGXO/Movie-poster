import asyncio
import html
import json
import logging
import os
import re
import uuid
from functools import lru_cache
from typing import Optional

from aiofiles import open as aiopen
from aiofiles.os import remove as aioremove
from pyrogram.errors import FloodWait

from config import CAPTION_TEMPLATE, FULL_DOWNLOAD_LIMIT_MB, FULL_DOWNLOAD_TIMEOUT

logger = logging.getLogger(__name__)
stream_semaphore = asyncio.Semaphore(4)

_LANGUAGE_MAP: dict[str, str] = {
    'en': 'English', 'eng': 'English', 'hi': 'Hindi', 'hin': 'Hindi', 'ta': 'Tamil', 'tam': 'Tamil',
    'te': 'Telugu', 'tel': 'Telugu', 'ml': 'Malayalam', 'mal': 'Malayalam', 'kn': 'Kannada', 'kan': 'Kannada',
    'bn': 'Bengali', 'ben': 'Bengali', 'mr': 'Marathi', 'mar': 'Marathi', 'gu': 'Gujarati', 'guj': 'Gujarati',
    'pa': 'Punjabi', 'pun': 'Punjabi', 'bho': 'Bhojpuri', 'zh': 'Chinese', 'chi': 'Chinese', 'cmn': 'Chinese',
    'ko': 'Korean', 'kor': 'Korean', 'pt': 'Portuguese', 'por': 'Portuguese', 'th': 'Thai', 'tha': 'Thai',
    'tl': 'Tagalog', 'tgl': 'Tagalog', 'fil': 'Tagalog', 'ja': 'Japanese', 'jpn': 'Japanese',
    'es': 'Spanish', 'spa': 'Spanish', 'sv': 'Swedish', 'swe': 'Swedish', 'fr': 'French', 'fra': 'French',
    'fre': 'French', 'de': 'German', 'deu': 'German', 'ger': 'German', 'it': 'Italian', 'ita': 'Italian',
    'ru': 'Russian', 'rus': 'Russian', 'ar': 'Arabic', 'ara': 'Arabic', 'tr': 'Turkish', 'tur': 'Turkish',
    'nl': 'Dutch', 'nld': 'Dutch', 'dut': 'Dutch', 'pl': 'Polish', 'pol': 'Polish', 'vi': 'Vietnamese',
    'vie': 'Vietnamese', 'id': 'Indonesian', 'ind': 'Indonesian', 'ms': 'Malay', 'msa': 'Malay', 'may': 'Malay',
    'fa': 'Persian', 'fas': 'Persian', 'per': 'Persian', 'ur': 'Urdu', 'urd': 'Urdu', 'he': 'Hebrew',
    'heb': 'Hebrew', 'el': 'Greek', 'ell': 'Greek', 'gre': 'Greek', 'hu': 'Hungarian', 'hun': 'Hungarian',
    'cs': 'Czech', 'ces': 'Czech', 'cze': 'Czech', 'ro': 'Romanian', 'ron': 'Romanian', 'rum': 'Romanian',
    'da': 'Danish', 'dan': 'Danish', 'fi': 'Finnish', 'fin': 'Finnish', 'no': 'Norwegian', 'nor': 'Norwegian',
    'uk': 'Ukrainian', 'ukr': 'Ukrainian', 'ca': 'Catalan', 'cat': 'Catalan', 'hr': 'Croatian', 'hrv': 'Croatian',
    'sk': 'Slovak', 'slk': 'Slovak', 'slo': 'Slovak', 'sr': 'Serbian', 'srp': 'Serbian', 'bg': 'Bulgarian',
    'bul': 'Bulgarian', 'unknown': 'Original Audio',
}


@lru_cache(maxsize=256)
def get_full_language_name(code: str) -> str:
    if not code:
        return 'Unknown'
    cleaned = code.split('(')[0].strip()
    return _LANGUAGE_MAP.get(cleaned.lower(), 'Original Audio')


@lru_cache(maxsize=64)
def get_standard_resolution(height: int) -> Optional[str]:
    if not height:
        return None
    for limit, label in ((240, "240p"), (360, "360p"), (480, "480p"), (720, "720p"),
                         (1080, "1080p"), (1440, "1440p"), (2160, "2160p")):
        if height <= limit:
            return label
    return "2160p+"


@lru_cache(maxsize=128)
def get_video_format(codec: str, transfer: str = '', hdr: str = '', bit_depth: str = '') -> Optional[str]:
    if not codec:
        return None
    codec = codec.lower()
    parts: list[str] = []
    if any(x in codec for x in ('hevc', 'h.265', 'h265')):
        parts.append('HEVC')
    elif 'av1' in codec:
        parts.append('AV1')
    elif any(x in codec for x in ('avc', 'avc1', 'h.264', 'h264')):
        parts.append('x264')
    elif 'vp9' in codec:
        parts.append('VP9')
    elif any(x in codec for x in ('mpeg4', 'xvid')):
        parts.append('MPEG4')
    else:
        return None
    try:
        if bit_depth and int(bit_depth) > 8:
            parts.append(f"{bit_depth}bit")
    except (ValueError, TypeError):
        pass
    t, h = transfer.lower(), hdr.lower()
    if any(x in t for x in ('pq', 'hlg', 'smpte', '2084', 'st 2084')) or 'hdr' in h or 'dolby' in h:
        parts.append('HDR')
    return ' '.join(parts)


def _parse_duration(value) -> float:
    try:
        if not value:
            return 0
        v = str(value).strip()
        if v.replace('.', '', 1).lstrip('-').isdigit():
            f = float(v)
            if f > 86_400_000:
                return f / 1_000_000
            if f > 86_400:
                return f / 1_000
            return f
        if ':' in v:
            p = [float(x) for x in v.split(':')]
            if len(p) == 3:
                return p[0] * 3600 + p[1] * 60 + p[2]
            if len(p) == 2:
                return p[0] * 60 + p[1]
    except Exception:
        pass
    return 0


def _fmt_duration(s: float) -> str:
    s = int(s)
    return f"{s // 3600:02}:{(s % 3600) // 60:02}:{s % 60:02}"


async def _run(*cmd: str) -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {}
        return json.loads(out.decode() or '{}')
    except Exception as e:
        logger.warning(f"{cmd[0]} error: {e}")
        return {}


def _parse_ffprobe(data: dict) -> tuple:
    duration = _parse_duration((data.get('format') or {}).get('duration'))
    width = height = None
    codec = bit_depth = hdr = transfer = ''
    audio_langs: set[str] = set()
    sub_langs: set[str] = set()
    has_sub = False

    for s in data.get('streams', []):
        ctype = (s.get('codec_type') or '').lower()
        tags = s.get('tags') or {}
        lang = tags.get('language') or tags.get('LANGUAGE') or ''
        if ctype == 'video':
            width = width or s.get('width') or s.get('coded_width')
            height = height or s.get('height') or s.get('coded_height')
            name = (s.get('codec_name') or '').lower()
            codec = ('hevc' if 'hevc' in name or 'h265' in name else
                     'avc' if 'h264' in name or 'avc' in name else
                     'av1' if 'av1' in name else
                     'vp9' if 'vp9' in name else
                     'mpeg4' if 'mpeg4' in name or 'xvid' in name else name)
            bps = str(s.get('bits_per_raw_sample') or s.get('bits_per_coded_sample') or '')
            if bps.isdigit() and bps != '0':
                bit_depth = bps
            ct = (s.get('color_transfer') or '').lower()
            if any(x in ct for x in ('smpte2084', 'arib-std-b67', 'smpte428')) or \
                    'bt2020' in (s.get('color_space') or '').lower():
                hdr = 'HDR'
        elif ctype == 'audio':
            audio_langs.add(get_full_language_name(lang or 'unknown'))
        elif ctype == 'subtitle':
            has_sub = True
            if lang:
                sub_langs.add(get_full_language_name(lang))

    audio = ', '.join(sorted(audio_langs)) or 'Original Audio'
    sub = ', '.join(sorted(sub_langs)) or ('ESUB' if has_sub else 'No Esubs')
    return duration, width, height, codec, bit_depth, hdr, transfer, audio, sub


def _parse_tracks(tracks: list) -> tuple:
    duration = 0.0
    width = height = None
    codec = bit_depth = hdr = transfer = ''
    audio_langs: set[str] = set()
    sub_langs: set[str] = set()
    has_text = False

    for track in tracks:
        if not isinstance(track, dict):
            continue
        t = (track.get('@type', '') or '').lower()

        if t == 'general':
            duration = duration or _parse_duration(track.get('Duration'))
        elif t == 'video':
            for field in ('Height', 'Sampled_Height', 'Encoded_Height'):
                raw = str(track.get(field, '') or '').split()[0:1]
                if raw and raw[0].isdigit():
                    height = int(raw[0])
                    break
            for field in ('Width', 'Sampled_Width', 'Encoded_Width'):
                raw = str(track.get(field, '') or '').split()[0:1]
                if raw and raw[0].isdigit():
                    width = int(raw[0])
                    break
            codec = (track.get('Format', '') or '').lower()
            bit_depth = track.get('BitDepth', '') or ''
            transfer = (track.get('transfer_characteristics', '') or
                        track.get('TransferCharacteristics', '') or '').lower()
            hdr = track.get('HDR_Format', '') or track.get('HDR_Format_Compatibility', '') or ''
            duration = duration or _parse_duration(track.get('Duration'))
            if 'dolby vision' in str(track).lower():
                hdr = 'Dolby Vision'
        elif t == 'audio':
            lang = track.get('Language') or track.get('Language_String') or 'unknown'
            audio_langs.add(get_full_language_name(lang))
        elif t == 'text':  # real subtitle tracks only (not chapter "menu" tracks)
            has_text = True
            lang = track.get('Language') or track.get('Language_String')
            if lang:
                name = get_full_language_name(lang)
                if name != 'Original Audio':
                    sub_langs.add(name)

    audio = ', '.join(sorted(audio_langs)) or 'Original Audio'
    sub = ', '.join(sorted(sub_langs)) or ('ESUB' if has_text else 'No Esubs')
    return duration, width, height, codec, bit_depth, hdr, transfer, audio, sub


async def _probe(path: str) -> tuple:
    mi_data = await _run('mediainfo', '--ParseSpeed=0', '--Language=raw', '--Output=JSON', path)
    mi = _parse_tracks(mi_data.get('media', {}).get('track', []))
    fp_data = await _run('ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', path)
    if not fp_data:
        return mi
    fp = _parse_ffprobe(fp_data)
    return (
        mi[0] or fp[0], mi[1] or fp[1], mi[2] or fp[2], mi[3] or fp[3], mi[4] or fp[4], mi[5] or fp[5],
        mi[6] or fp[6],
        mi[7] if mi[7] != 'Original Audio' else fp[7],
        mi[8] if mi[8] != 'No Esubs' else fp[8],
    )


def _title(message, media) -> str:
    name = getattr(media, 'file_name', None)
    if name:
        return re.sub(r"\.(mkv|mp4|avi|webm|mov|m4v)$", "", name, flags=re.I)
    return (message.caption or '').strip().split("\n")[0] or 'Video'


def _caption(title: str, result: tuple) -> str:
    duration, width, height, codec, bit_depth, hdr, transfer, audio, sub = result
    dims = [d for d in (width, height) if d]
    quality = get_standard_resolution(min(dims)) if dims else None
    fmt = get_video_format(codec, transfer, hdr, bit_depth)
    video_line = ' '.join(filter(None, [quality, fmt])) or 'Unknown'

    def build(s: str) -> str:
        return CAPTION_TEMPLATE.format(
            title=html.escape(title), video_line=html.escape(video_line),
            duration=_fmt_duration(duration) if duration else 'Unknown',
            audio=html.escape(audio), subtitle=html.escape(s),
        ).strip()

    cap = build(sub)
    if len(cap) > 1024:  # Telegram caption limit – trim the subtitle list
        sub = sub[: -(len(cap) - 1024 + 1)] + "…"
        cap = build(sub)
    return cap


_STEPS = [("16KB", 16 * 1024), ("1MB", 1024 ** 2), ("3MB", 3 * 1024 ** 2), ("8MB", 8 * 1024 ** 2)]


async def _stream_chunk(client, media, size: int, path: str) -> bool:
    try:
        written = 0
        async with stream_semaphore:
            async with aiopen(path, 'wb') as f:
                async for chunk in client.stream_media(media):
                    if not chunk:
                        break
                    piece = chunk[: size - written]
                    await f.write(piece)
                    written += len(piece)
                    if written >= size:
                        break
        return os.path.exists(path) and os.path.getsize(path) > 0
    except Exception as e:
        logger.warning(f"stream_chunk failed ({size}): {e}")
        return False


async def build_caption(client, message) -> Optional[str]:
    """Returns an auto caption, or None if the file couldn't be analysed."""
    media = message.video or message.document
    if not media:
        return None
    title = _title(message, media)

    # 1) scan the first 16KB → 8MB of the file (fast, no full download)
    for label, size in _STEPS:
        tmp = f"probe_{label}_{message.id}_{uuid.uuid4().hex[:8]}.bin"
        try:
            if not await _stream_chunk(client, media, size, tmp):
                continue
            result = await _probe(tmp)
            if result[1] or result[2]:
                return _caption(title, result)
        except Exception as e:
            logger.warning(f"{label} probe error: {e}")
        finally:
            if os.path.exists(tmp):
                await aioremove(tmp)

    # 2) fallback: full download (e.g. MP4 with moov atom at the end)
    if (getattr(media, 'file_size', 0) or 0) > FULL_DOWNLOAD_LIMIT_MB * 1024 ** 2:
        return None
    path = None
    try:
        try:
            path = await asyncio.wait_for(message.download(), timeout=FULL_DOWNLOAD_TIMEOUT)
        except FloodWait as e:
            await asyncio.sleep(e.value)
            path = await message.download()
        return _caption(title, await _probe(path))
    except Exception as e:
        logger.error(f"Full download probe failed: {e}")
        return None
    finally:
        if path and os.path.exists(path):
            await aioremove(path)
