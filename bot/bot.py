import discord
from discord.ext import commands
import asyncio
import yt_dlp
import os
import re
import base64
import urllib.request
import urllib.parse
import urllib.error
import ssl
import json
import logging
import shutil
import sys
import playlist as pl_db

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger('alxcer')

def resolve_ffmpeg_executable():
    override = os.environ.get('FFMPEG_EXECUTABLE')
    if override:
        log.info('ffmpeg override: %s', override)
        return override

    system_ffmpeg = shutil.which('ffmpeg')
    if system_ffmpeg:
        log.info('ffmpeg system binary: %s', system_ffmpeg)
        return system_ffmpeg

    log.warning('ffmpeg not found in PATH; playback will fail until ffmpeg is installed')
    return 'ffmpeg'


FFMPEG_EXECUTABLE = resolve_ffmpeg_executable()

if not discord.opus.is_loaded():
    for name in ('libopus.so.0', 'libopus.so', 'opus'):
        try:
            discord.opus.load_opus(name)
            log.info('opus loaded: %s', name)
            break
        except Exception as e:
            log.warning('opus load %s failed: %s', name, e)

FFMPEG_BEFORE = (
    '-nostdin '
    '-reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 '
    '-reconnect_on_http_error 4xx,5xx -reconnect_delay_max 30 '
    '-rw_timeout 15000000'
)
FFMPEG_PCM_OPTIONS = '-vn -f s16le -ar 48000 -ac 2'
COOKIES_FILE = os.path.join(os.path.dirname(__file__), 'cookies.txt')

_cookies_env = os.environ.get('YOUTUBE_COOKIES', '')
if _cookies_env and not os.path.exists(COOKIES_FILE):
    try:
        with open(COOKIES_FILE, 'w') as _f:
            _f.write(_cookies_env)
        log.info('cookies.txt written from YOUTUBE_COOKIES secret (%d bytes)', len(_cookies_env))
    except Exception as _e:
        log.warning('failed to write cookies: %s', _e)

# Piped instances — primary YouTube source (their servers extract, not GitHub Actions IP)
HTTP_INSECURE_CONTEXT = ssl._create_unverified_context()

PIPED_INSTANCES = [
    'https://api.piped.private.coffee',
    'https://api.piped.projectsegfau.lt',
    'https://pipedapi.tokhmi.xyz',
    'https://pipedapi.kavin.rocks',
    'https://pipedapi.adminforge.de',
    'https://pipedapi.syncpundit.io',
    'https://pipedapi.drgns.space',
    'https://piped-api.garudalinux.org',
    'https://pa.il.shn.hk',
    'https://pipedapi.lunar.icu',
    'https://piped.drgns.space',
]

# Invidious — last resort
PIPED_INSTANCES_URL = 'https://piped-instances.kavin.rocks/'
_PIPED_INSTANCES_CACHE = None
_PIPED_INSTANCES_TS = 0

INVIDIOUS_INSTANCES = [
    'https://invidious.privacyredirect.com',
    'https://invidious.nerdvpn.de',
    'https://inv.nadeko.net',
    'https://yewtu.be',
    'https://invidious.io.lol',
    'https://invidious.f5.si',
]


def http_get_json(url, timeout=8, allow_insecure_retry=False):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile',
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8', 'ignore'))
    except urllib.error.URLError as e:
        if not allow_insecure_retry:
            raise
        reason = getattr(e, 'reason', None)
        if not isinstance(reason, ssl.SSLCertVerificationError):
            raise
        log.warning('ssl verify failed for %s, retrying without verification', url)
        with urllib.request.urlopen(req, timeout=timeout, context=HTTP_INSECURE_CONTEXT) as r:
            return json.loads(r.read().decode('utf-8', 'ignore'))


def piped_instances():
    import time
    global _PIPED_INSTANCES_CACHE, _PIPED_INSTANCES_TS
    if _PIPED_INSTANCES_CACHE and time.time() - _PIPED_INSTANCES_TS < 3600:
        return _PIPED_INSTANCES_CACHE

    instances = list(PIPED_INSTANCES)
    try:
        data = http_get_json(PIPED_INSTANCES_URL, timeout=8, allow_insecure_retry=True)
        live = []
        for item in data:
            api_url = (item.get('api_url') or '').rstrip('/')
            uptime = float(item.get('uptime_24h') or 0)
            if api_url.startswith('https://') and uptime >= 80:
                live.append((uptime, api_url))
        for _, api_url in sorted(live, reverse=True):
            if api_url not in instances:
                instances.append(api_url)
    except Exception as e:
        log.warning('could not refresh Piped instances: %s', e)

    _PIPED_INSTANCES_CACHE = instances
    _PIPED_INSTANCES_TS = time.time()
    return instances


YOUTUBE_VIDEO_ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')


def clean_query(query):
    q = (query or '').strip()
    if q.startswith('<') and q.endswith('>'):
        q = q[1:-1].strip()
    return q


def _is_video_id(value):
    return bool(value and YOUTUBE_VIDEO_ID_RE.match(value))


def _is_youtube_host(host):
    host = (host or '').lower().split(':', 1)[0]
    return (
        host == 'youtu.be'
        or host.endswith('.youtube.com')
        or host == 'youtube.com'
        or host.endswith('.youtube-nocookie.com')
        or host == 'youtube-nocookie.com'
    )


def is_youtube_url(value):
    try:
        parsed = urllib.parse.urlparse(clean_query(value))
    except Exception:
        return False
    return parsed.scheme in ('http', 'https') and _is_youtube_host(parsed.netloc)


def extract_video_id(s):
    q = clean_query(s)
    try:
        parsed = urllib.parse.urlparse(q)
        host = parsed.netloc.lower().split(':', 1)[0]
        path_parts = [p for p in parsed.path.split('/') if p]

        if host == 'youtu.be' and path_parts and _is_video_id(path_parts[0]):
            return path_parts[0]

        if _is_youtube_host(host):
            params = urllib.parse.parse_qs(parsed.query)
            for key in ('v', 'vi'):
                for value in params.get(key, []):
                    if _is_video_id(value):
                        return value

            if parsed.path == '/attribution_link':
                for value in params.get('u', []):
                    nested = urllib.parse.unquote(value)
                    nested_id = extract_video_id(nested)
                    if nested_id:
                        return nested_id

            if path_parts:
                if path_parts[0] in ('shorts', 'embed', 'live', 'v', 'e') and len(path_parts) > 1:
                    if _is_video_id(path_parts[1]):
                        return path_parts[1]
                if _is_video_id(path_parts[0]):
                    return path_parts[0]
    except Exception:
        pass

    m = re.search(r'(?:v=|vi=|youtu\.be/|/shorts/|/embed/|/live/|/v/|/e/)([A-Za-z0-9_-]{11})', q)
    return m.group(1) if m else None


def canonical_youtube_url(query):
    vid = extract_video_id(query)
    if not vid:
        raise RuntimeError('no YouTube video ID found in URL')
    return 'https://www.youtube.com/watch?v=' + vid


_SC_CLIENT_ID = None
_SC_CLIENT_ID_TS = 0


def get_soundcloud_client_id():
    import time
    global _SC_CLIENT_ID, _SC_CLIENT_ID_TS
    if _SC_CLIENT_ID and time.time() - _SC_CLIENT_ID_TS < 3600:
        return _SC_CLIENT_ID
    req = urllib.request.Request('https://soundcloud.com/', headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36',
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        home = r.read().decode('utf-8', 'ignore')
    scripts = re.findall(r'https://[^"]+sndcdn\.com[^"]+\.js', home)
    for s in reversed(scripts[-6:]):
        try:
            sreq = urllib.request.Request(s, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(sreq, timeout=10) as r:
                js = r.read().decode('utf-8', 'ignore')
            m = re.search(r'client_id\s*[:=]\s*["\']([a-zA-Z0-9]{20,})["\']', js)
            if m:
                _SC_CLIENT_ID = m.group(1)
                _SC_CLIENT_ID_TS = time.time()
                log.info('soundcloud client_id refreshed')
                return _SC_CLIENT_ID
        except Exception:
            continue
    raise RuntimeError('could not get soundcloud client_id')


def fetch_via_soundcloud(query):
    """SoundCloud — works reliably without any auth, great for Thai music."""
    q = query.strip()
    if 'soundcloud.com' in q and re.match(r'https?://', q):
        track_url = q
        title = q.split('/')[-1].replace('-', ' ').title()
        duration = 0
        thumb = None
        uploader = 'SoundCloud'
    else:
        cid = get_soundcloud_client_id()
        url = ('https://api-v2.soundcloud.com/search/tracks?q=' +
               urllib.parse.quote(q) + '&limit=5&client_id=' + cid)
        data = http_get_json(url, timeout=10)
        items = [t for t in (data.get('collection') or []) if t.get('streamable')]
        if not items:
            raise RuntimeError('no SC results')
        t = items[0]
        track_url = t['permalink_url']
        title = t.get('title', 'Unknown')
        duration = int((t.get('duration') or 0) / 1000)
        thumb = t.get('artwork_url')
        uploader = (t.get('user') or {}).get('username', 'SoundCloud')

    opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'source_address': '0.0.0.0',
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(track_url, download=False)
    if 'entries' in info:
        info = info['entries'][0]
    if not info.get('url'):
        raise RuntimeError('no stream url')
    log.info('soundcloud ok: %s', title)
    return {
        'url': info['url'],
        'title': info.get('title', title),
        'duration': info.get('duration', duration) or duration,
        'thumbnail': info.get('thumbnail') or thumb,
        'webpage_url': track_url,
        'uploader': info.get('uploader', uploader) or uploader,
        'query': query,
    }


def youtube_html_search(query, n=5):
    query = clean_query(query)
    url = 'https://www.youtube.com/results?search_query=' + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        html = r.read().decode('utf-8', 'ignore')
    ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html)
    seen = set()
    out = []
    for v in ids:
        if v not in seen:
            seen.add(v)
            out.append(v)
        if len(out) >= n:
            break
    return out


def fetch_via_piped(query):
    """
    Piped API — Piped's own servers fetch from YouTube, so GitHub Actions IP
    is never seen by YouTube. This is the primary YouTube source.
    """
    query = clean_query(query)
    vid = extract_video_id(query)
    if not vid:
        # Search YouTube HTML to find video ID, then use Piped for stream
        try:
            ids = youtube_html_search(query, n=3)
        except Exception:
            ids = []
        if not ids:
            # Try Piped search API
            for inst in piped_instances()[:5]:
                try:
                    results = http_get_json(
                        inst + '/search?q=' + urllib.parse.quote(query) + '&filter=videos',
                        timeout=8,
                        allow_insecure_retry=True,
                    )
                    items = results.get('items') or []
                    vids = [extract_video_id(i.get('url', '')) for i in items if i.get('type') == 'stream']
                    if vids:
                        ids = [v for v in vids if v]
                        break
                except Exception as e:
                    log.debug('piped search %s: %s', inst, e)
        if not ids:
            raise RuntimeError('no YouTube video ID found for query')
        vid = ids[0]

    last_err = None
    for inst in piped_instances():
        try:
            streams = http_get_json(inst + '/streams/' + vid, timeout=10, allow_insecure_retry=True)
            audio = streams.get('audioStreams') or []
            if not audio:
                raise RuntimeError('no audio streams')
            opus_audio = [a for a in audio if (a.get('codec') or '').lower() == 'opus']
            candidates = opus_audio or audio
            candidates.sort(key=lambda a: a.get('bitrate', 0), reverse=True)
            best = candidates[0]
            stream_url = best['url']
            codec = (best.get('codec') or '').lower()
            log.info('piped ok via %s codec=%s bitrate=%s', inst, codec, best.get('bitrate'))
            related = streams.get('relatedStreams') or []
            related_ids = []
            for r in related:
                rid = extract_video_id(r.get('url', ''))
                if rid and len(related_ids) < 10:
                    related_ids.append(rid)
            log.info('piped ok via %s codec=%s bitrate=%s related=%d', inst, codec, best.get('bitrate'), len(related_ids))
            return {
                'url': stream_url,
                'title': streams.get('title', 'Unknown'),
                'duration': streams.get('duration', 0),
                'thumbnail': streams.get('thumbnailUrl'),
                'webpage_url': 'https://youtube.com/watch?v=' + vid,
                'uploader': streams.get('uploader', 'Unknown'),
                'codec': codec,
                'query': query,
                'related_ids': related_ids,
            }
        except Exception as e:
            last_err = e
            log.warning('piped %s: %s', inst, e)
    raise RuntimeError('piped all instances failed: ' + str(last_err))


def fetch_via_invidious(query):
    query = clean_query(query)
    vid = extract_video_id(query)
    if not vid:
        try:
            ids = youtube_html_search(query, n=1)
        except Exception:
            ids = []
        if not ids:
            raise RuntimeError('no video ID for invidious')
        vid = ids[0]
    last_err = None
    for inst in INVIDIOUS_INSTANCES:
        try:
            v = http_get_json(inst + '/api/v1/videos/' + vid, timeout=10)
            fmts = v.get('adaptiveFormats') or []
            audio_fmts = [f for f in fmts if 'audio' in (f.get('type') or '')]
            if not audio_fmts:
                raise RuntimeError('no audio formats')
            audio_fmts.sort(key=lambda a: a.get('bitrate', 0), reverse=True)
            best = audio_fmts[0]
            log.info('invidious ok via %s', inst)
            return {
                'url': best['url'],
                'title': v.get('title', 'Unknown'),
                'duration': v.get('lengthSeconds', 0),
                'thumbnail': (v.get('videoThumbnails') or [{}])[0].get('url'),
                'webpage_url': 'https://youtube.com/watch?v=' + vid,
                'uploader': v.get('author', 'Unknown'),
                'query': query,
            }
        except Exception as e:
            last_err = e
            log.warning('invidious %s: %s', inst, e)
    raise RuntimeError('invidious all failed: ' + str(last_err))


def fetch_via_pytubefix(query):
    """
    pytubefix — alternative YouTube extractor, sometimes bypasses bot detection
    via different internal mechanisms than yt-dlp.
    """
    try:
        from pytubefix import YouTube, Search
    except ImportError:
        raise RuntimeError('pytubefix not installed')

    q = clean_query(query)
    if is_youtube_url(q):
        yt = YouTube(canonical_youtube_url(q), use_oauth=False, allow_oauth_cache=False)
    else:
        results = Search(q).videos
        if not results:
            raise RuntimeError('no results from pytubefix search')
        yt = results[0]

    audio = yt.streams.filter(only_audio=True).order_by('abr').last()
    if not audio:
        raise RuntimeError('no audio stream from pytubefix')

    log.info('pytubefix ok: %s', yt.title)
    return {
        'url': audio.url,
        'title': yt.title,
        'duration': yt.length or 0,
        'thumbnail': yt.thumbnail_url,
        'webpage_url': yt.watch_url,
        'uploader': yt.author,
        'query': query,
    }


def ytdlp_target(query):
    q = clean_query(query)
    if is_youtube_url(q):
        return canonical_youtube_url(q)
    if re.match(r'https?://', q):
        return q
    ids = youtube_html_search(q, n=1)
    if not ids:
        raise RuntimeError('no results')
    return 'https://www.youtube.com/watch?v=' + ids[0]


def fetch_via_ytdlp(query, cookiefile=None, label='yt-dlp'):
    """yt-dlp fallback for direct URLs and YouTube searches."""
    opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'source_address': '0.0.0.0',
        'geo_bypass': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
            },
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120',
        },
    }
    if cookiefile:
        opts['cookiefile'] = cookiefile
    target = ytdlp_target(query)
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(target, download=False)
    if 'entries' in data:
        data = data['entries'][0]
    if not data.get('url'):
        raise RuntimeError('no stream url')
    log.info('%s ok: %s', label, data.get('title'))
    return {
        'url': data['url'],
        'title': data.get('title', 'Unknown'),
        'duration': data.get('duration', 0),
        'thumbnail': data.get('thumbnail'),
        'webpage_url': data.get('webpage_url', target),
        'uploader': data.get('uploader', 'Unknown'),
        'query': query,
    }


def fetch_via_ytdlp_direct(query):
    return fetch_via_ytdlp(query, label='yt-dlp')


def fetch_via_ytdlp_cookies(query):
    """yt-dlp with cookies - last resort if cookies secret is set."""
    if not os.path.exists(COOKIES_FILE):
        raise RuntimeError('no cookies file, skipping ytdlp')
    return fetch_via_ytdlp(query, cookiefile=COOKIES_FILE, label='ytdlp+cookies')


def fetch_spotify_info(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; Googlebot/2.1)',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        html = r.read().decode('utf-8', 'ignore')
    title = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
    desc = re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"', html)
    thumb = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
    og_title = title.group(1) if title else None
    og_desc = desc.group(1) if desc else ''
    og_thumb = thumb.group(1) if thumb else None
    if not og_title:
        raise RuntimeError('could not scrape Spotify info')
    artist = ''
    if og_desc:
        parts = [p.strip() for p in og_desc.split('·')]
        if len(parts) >= 2:
            artist = parts[1]
    return (og_title + ' ' + artist).strip(), og_title, og_thumb


def _detect_url_type(q):
    q = clean_query(q)
    if not re.match(r'https?://', q):
        return 'search'
    if is_youtube_url(q):
        return 'youtube'
    if 'soundcloud.com' in q:
        return 'soundcloud'
    if 'spotify.com' in q:
        return 'spotify'
    return 'other_url'


async def fetch_track(query):
    loop = asyncio.get_event_loop()

    def _run():
        q = clean_query(query)
        url_type = _detect_url_type(q)
        errors = []

        if url_type == 'soundcloud':
            for fn, name in [
                (fetch_via_soundcloud, 'soundcloud'),
                (fetch_via_ytdlp_direct, 'yt-dlp'),
                (fetch_via_ytdlp_cookies, 'ytdlp+cookies'),
            ]:
                try:
                    return fn(q)
                except Exception as e:
                    errors.append(name + ': ' + str(e))
            raise RuntimeError(' | '.join(errors))

        if url_type == 'spotify':
            try:
                search_q, sp_title, sp_thumb = fetch_spotify_info(q)
                log.info('spotify → %s', search_q)
                for fn, name in [
                    (fetch_via_piped, 'piped'),
                    (fetch_via_pytubefix, 'pytubefix'),
                    (fetch_via_invidious, 'invidious'),
                    (fetch_via_ytdlp_cookies, 'ytdlp+cookies'),
                    (fetch_via_ytdlp_direct, 'yt-dlp'),
                ]:
                    try:
                        result = fn(search_q)
                        if not result.get('thumbnail') and sp_thumb:
                            result['thumbnail'] = sp_thumb
                        result['webpage_url'] = q
                        result['query'] = query
                        return result
                    except Exception as e:
                        errors.append(name + ': ' + str(e))
            except Exception as e:
                errors.append('spotify_scrape: ' + str(e))
            raise RuntimeError(' | '.join(errors))

        if url_type == 'youtube':
            # Piped first — its servers proxy to YouTube, bypassing GitHub Actions IP block
            for fn, name in [
                (fetch_via_piped, 'piped'),
                (fetch_via_pytubefix, 'pytubefix'),
                (fetch_via_invidious, 'invidious'),
                (fetch_via_ytdlp_cookies, 'ytdlp+cookies'),
                (fetch_via_ytdlp_direct, 'yt-dlp'),
            ]:
                try:
                    return fn(q)
                except Exception as e:
                    errors.append(name + ': ' + str(e))
            raise RuntimeError(' | '.join(errors))

        if url_type == 'other_url':
            for fn, name in [
                (fetch_via_ytdlp_direct, 'yt-dlp'),
                (fetch_via_ytdlp_cookies, 'ytdlp+cookies'),
            ]:
                try:
                    return fn(q)
                except Exception as e:
                    errors.append(name + ': ' + str(e))
            raise RuntimeError(' | '.join(errors))

        # Search query: SoundCloud first (always works), then Piped for YouTube
        for fn, name in [
            (fetch_via_soundcloud, 'soundcloud'),
            (fetch_via_piped, 'piped'),
            (fetch_via_pytubefix, 'pytubefix'),
            (fetch_via_invidious, 'invidious'),
            (fetch_via_ytdlp_cookies, 'ytdlp+cookies'),
            (fetch_via_ytdlp_direct, 'yt-dlp'),
        ]:
            try:
                return fn(q)
            except Exception as e:
                errors.append(name + ': ' + str(e))
        raise RuntimeError(' | '.join(errors))

    return await loop.run_in_executor(None, _run)


intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

queues = {}
now_playing = {}
loop_mode = {}
autoplay_mode = {}
_autoplay_related = {}
volume_levels = {}
_volume_sources = {}

LOOP_LABELS = {'off': 'ปิด', 'one': '🔂 1 เพลง', 'all': '🔁 ทั้งคิว'}


def get_autoplay(guild_id):
    return autoplay_mode.get(guild_id, False)


def set_autoplay(guild_id, value):
    autoplay_mode[guild_id] = value


def get_volume(guild_id):
    return volume_levels.get(guild_id, 1.0)


def set_volume(guild_id, vol):
    vol = max(0.1, min(2.0, round(vol, 1)))
    volume_levels[guild_id] = vol
    src = _volume_sources.get(guild_id)
    if src:
        src.volume = vol
    return vol


def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = []
    return queues[guild_id]


def get_loop(guild_id):
    return loop_mode.get(guild_id, 'off')


def set_loop(guild_id, mode):
    loop_mode[guild_id] = mode


def cycle_loop(guild_id):
    cur = get_loop(guild_id)
    nxt = {'off': 'one', 'one': 'all', 'all': 'off'}[cur]
    set_loop(guild_id, nxt)
    return nxt


def fmt_duration(seconds):
    if not seconds:
        return '?'
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return (str(h) + ':' if h else '') + str(m).zfill(2 if h else 1) + ':' + str(sec).zfill(2)


def make_np_embed(track, guild_id=None):
    embed = discord.Embed(
        title='🎵 กำลังเล่นเพลง',
        description='**[' + track['title'] + '](' + track['webpage_url'] + ')**',
        color=0x5865F2,
    )
    embed.add_field(name='⏱ ความยาว', value=fmt_duration(track['duration']), inline=True)
    embed.add_field(name='🎤 ช่อง', value=track['uploader'], inline=True)
    if guild_id is not None:
        embed.add_field(name='🔁 Loop', value=LOOP_LABELS[get_loop(guild_id)], inline=True)
        embed.add_field(name='🎧 Autoplay', value='เปิด ✅' if get_autoplay(guild_id) else 'ปิด ❌', inline=True)
        vol_pct = int(get_volume(guild_id) * 100)
        filled = vol_pct // 10
        vol_bar = '▓' * filled + '░' * (10 - filled)
        embed.add_field(name='🔊 Volume', value=vol_bar + ' ' + str(vol_pct) + '%', inline=True)
        queue = get_queue(guild_id)
        if queue:
            embed.add_field(name='📋 คิวถัดไป', value=str(len(queue)) + ' เพลง', inline=True)
    if track.get('thumbnail'):
        embed.set_thumbnail(url=track['thumbnail'])
    return embed

class PlayerView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=None)
        self.ctx = ctx
        self._refresh_loop_button()
        self._refresh_autoplay_button()

    def _refresh_loop_button(self):
        mode = get_loop(self.ctx.guild.id)
        for child in self.children:
            if getattr(child, 'custom_id', None) == 'loop':
                child.label = {'off': 'Loop: Off', 'one': 'Loop: 1 เพลง', 'all': 'Loop: ทั้งคิว'}[mode]
                child.emoji = '🔂' if mode == 'one' else '🔁'
                child.style = discord.ButtonStyle.secondary if mode == 'off' else discord.ButtonStyle.success

    def _refresh_autoplay_button(self):
        on = get_autoplay(self.ctx.guild.id)
        for child in self.children:
            if getattr(child, 'custom_id', None) == 'autoplay':
                child.label = 'Autoplay: On' if on else 'Autoplay: Off'
                child.style = discord.ButtonStyle.success if on else discord.ButtonStyle.secondary

    async def _ack(self, i):
        try:
            await i.response.defer()
        except Exception:
            pass

    async def _update_embed(self, i):
        track = now_playing.get(self.ctx.guild.id)
        if track:
            try:
                await i.message.edit(embed=make_np_embed(track, self.ctx.guild.id), view=self)
            except Exception:
                pass

    # ── Row 1: Playback controls ──────────────────────────────────────────────

    @discord.ui.button(emoji='⏯️', label='Pause/Resume', style=discord.ButtonStyle.primary, custom_id='pp', row=0)
    async def pause_resume(self, i: discord.Interaction, b):
        await self._ack(i)
        vc = self.ctx.voice_client
        if not vc:
            await i.followup.send('❌ บอทไม่ได้อยู่ใน voice', ephemeral=True); return
        if vc.is_playing():
            vc.pause(); await i.followup.send('⏸️ หยุดชั่วคราว', ephemeral=True)
        elif vc.is_paused():
            vc.resume(); await i.followup.send('▶️ เล่นต่อ', ephemeral=True)
        else:
            await i.followup.send('❌ ไม่มีเพลงเล่นอยู่', ephemeral=True)

    @discord.ui.button(emoji='⏭️', label='Skip', style=discord.ButtonStyle.primary, custom_id='skip', row=0)
    async def skip_btn(self, i: discord.Interaction, b):
        await self._ack(i)
        vc = self.ctx.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop(); await i.followup.send('⏭️ ข้ามเพลง', ephemeral=True)
        else:
            await i.followup.send('❌ ไม่มีเพลงเล่นอยู่', ephemeral=True)

    @discord.ui.button(emoji='🔁', label='Loop: Off', style=discord.ButtonStyle.secondary, custom_id='loop', row=0)
    async def loop_btn(self, i: discord.Interaction, b):
        await self._ack(i)
        mode = cycle_loop(self.ctx.guild.id)
        self._refresh_loop_button()
        await self._update_embed(i)
        await i.followup.send('🔁 Loop: **' + LOOP_LABELS[mode] + '**', ephemeral=True)

    @discord.ui.button(emoji='🎧', label='Autoplay: Off', style=discord.ButtonStyle.secondary, custom_id='autoplay', row=0)
    async def autoplay_btn(self, i: discord.Interaction, b):
        await self._ack(i)
        on = not get_autoplay(self.ctx.guild.id)
        set_autoplay(self.ctx.guild.id, on)
        self._refresh_autoplay_button()
        await self._update_embed(i)
        await i.followup.send(
            '🎧 Autoplay: **' + ('เปิด ✅ — เมื่อคิวหมดจะเล่นเพลงที่เกี่ยวข้องต่อ' if on else 'ปิด ❌') + '**',
            ephemeral=True,
        )

    @discord.ui.button(emoji='⏹️', label='Stop', style=discord.ButtonStyle.danger, custom_id='stop', row=0)
    async def stop_btn(self, i: discord.Interaction, b):
        await self._ack(i)
        vc = self.ctx.voice_client
        if vc:
            queues[self.ctx.guild.id] = []
            now_playing.pop(self.ctx.guild.id, None)
            _autoplay_related.pop(self.ctx.guild.id, None)
            _volume_sources.pop(self.ctx.guild.id, None)
            set_loop(self.ctx.guild.id, 'off')
            set_autoplay(self.ctx.guild.id, False)
            vc.stop()
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            await i.followup.send('⏹️ หยุดและออกจาก voice', ephemeral=True)
        else:
            await i.followup.send('❌ บอทไม่ได้อยู่ใน voice', ephemeral=True)

    # ── Row 2: Queue, Shuffle, Add to PL, Volume ─────────────────────────────

    @discord.ui.button(emoji='📋', label='Queue', style=discord.ButtonStyle.secondary, custom_id='queue_btn', row=1)
    async def queue_btn(self, i: discord.Interaction, b):
        await self._ack(i)
        queue = get_queue(self.ctx.guild.id)
        np = now_playing.get(self.ctx.guild.id)
        embed = discord.Embed(title='📋 คิวเพลง', color=0x5865F2)
        if np:
            embed.add_field(
                name='🎵 กำลังเล่น',
                value='**' + np['title'][:60] + '** `' + fmt_duration(np['duration']) + '`',
                inline=False,
            )
        if queue:
            lines = [str(idx) + '. **' + t['title'][:50] + '** `' + fmt_duration(t['duration']) + '`'
                     for idx, t in enumerate(queue[:10], 1)]
            if len(queue) > 10:
                lines.append('...อีก ' + str(len(queue) - 10) + ' เพลง')
            embed.add_field(name='ถัดไป (' + str(len(queue)) + ')', value='\n'.join(lines), inline=False)
        elif not np:
            embed.description = 'คิวว่างเปล่า 🎵'
        embed.add_field(name='🔁 Loop', value=LOOP_LABELS[get_loop(self.ctx.guild.id)], inline=True)
        embed.add_field(name='🎧 Autoplay', value='เปิด ✅' if get_autoplay(self.ctx.guild.id) else 'ปิด ❌', inline=True)
        await i.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(emoji='🔀', label='Shuffle', style=discord.ButtonStyle.secondary, custom_id='shuffle_btn', row=1)
    async def shuffle_btn(self, i: discord.Interaction, b):
        await self._ack(i)
        import random
        queue = get_queue(self.ctx.guild.id)
        if not queue:
            await i.followup.send('❌ คิวว่างเปล่า', ephemeral=True); return
        random.shuffle(queue)
        await i.followup.send('🔀 สับเปลี่ยนคิว **' + str(len(queue)) + '** เพลงแล้ว', ephemeral=True)

    @discord.ui.button(emoji='➕', label='Add to PL', style=discord.ButtonStyle.secondary, custom_id='addpl_btn', row=1)
    async def addpl_btn(self, i: discord.Interaction, b):
        await self._ack(i)
        track = now_playing.get(self.ctx.guild.id)
        if not track:
            await i.followup.send('❌ ไม่มีเพลงเล่นอยู่', ephemeral=True); return
        playlists = pl_db.get_all(i.user.id)
        if not playlists:
            await i.followup.send('❌ ยังไม่มี playlist\nใช้ `!pl create <ชื่อ>` เพื่อสร้าง', ephemeral=True); return
        options = [
            discord.SelectOption(
                label=pl['name'][:25],
                value=key,
                description=str(len(pl.get('tracks', []))) + ' เพลง',
                emoji='📁',
            )
            for key, pl in list(playlists.items())[:25]
        ]
        select = discord.ui.Select(placeholder='เลือก playlist ที่จะเพิ่มเพลง...', options=options, custom_id='pl_select')
        pl_snapshot = dict(playlists)
        track_snapshot = dict(track)
        user_id = i.user.id

        async def select_callback(sel_i: discord.Interaction):
            chosen_key = select.values[0]
            ok, result = pl_db.add_track(user_id, chosen_key, track_snapshot)
            pl_name = pl_snapshot[chosen_key]['name'] if chosen_key in pl_snapshot else chosen_key
            if ok:
                await sel_i.response.edit_message(
                    content='✅ เพิ่ม **' + track_snapshot['title'][:50] + '** ลง playlist **' + pl_name + '** แล้ว',
                    view=None,
                )
            else:
                await sel_i.response.edit_message(content='❌ ' + result, view=None)

        select.callback = select_callback
        view = discord.ui.View(timeout=30)
        view.add_item(select)
        await i.followup.send('➕ เพิ่ม **' + track['title'][:50] + '** ลง playlist ไหน?', view=view, ephemeral=True)

    @discord.ui.button(emoji='🔉', label='Vol-', style=discord.ButtonStyle.secondary, custom_id='vol_down', row=1)
    async def vol_down(self, i: discord.Interaction, b):
        await self._ack(i)
        new_vol = set_volume(self.ctx.guild.id, get_volume(self.ctx.guild.id) - 0.1)
        await self._update_embed(i)
        await i.followup.send('🔉 Volume: **' + str(int(new_vol * 100)) + '%**', ephemeral=True)

    @discord.ui.button(emoji='🔊', label='Vol+', style=discord.ButtonStyle.secondary, custom_id='vol_up', row=1)
    async def vol_up(self, i: discord.Interaction, b):
        await self._ack(i)
        new_vol = set_volume(self.ctx.guild.id, get_volume(self.ctx.guild.id) + 0.1)
        await self._update_embed(i)
        await i.followup.send('🔊 Volume: **' + str(int(new_vol * 100)) + '%**', ephemeral=True)


async def ensure_voice(ctx):
    target = ctx.author.voice.channel
    vc = ctx.voice_client
    for attempt in range(1, 5):
        try:
            if vc and vc.is_connected():
                if vc.channel != target:
                    await vc.move_to(target)
                return vc
            vc = await target.connect(timeout=30.0, reconnect=True, self_deaf=True)
            return vc
        except (asyncio.TimeoutError, discord.errors.ConnectionClosed, discord.ClientException) as e:
            log.warning('voice attempt %d: %s', attempt, e)
            try:
                if ctx.voice_client:
                    await ctx.voice_client.disconnect(force=True)
            except Exception:
                pass
            vc = None
            await asyncio.sleep(2 * attempt)
    raise RuntimeError('voice connect failed')


async def _start_playback(ctx, track):
    vc = ctx.voice_client
    if not vc or not vc.is_connected():
        return False
    if not discord.opus.is_loaded():
        raise RuntimeError('Discord opus library is not loaded')
    log.info('starting playback via PCM: source_codec=%s title=%s', track.get('codec'), track.get('title'))
    raw_source = discord.FFmpegPCMAudio(
        track['url'],
        executable=FFMPEG_EXECUTABLE,
        before_options=FFMPEG_BEFORE,
        options=FFMPEG_PCM_OPTIONS,
    )
    source = discord.PCMVolumeTransformer(raw_source, volume=get_volume(ctx.guild.id))
    _volume_sources[ctx.guild.id] = source

    def after_play(err):
        if err:
            log.warning('after-play: %s', err)
            try:
                asyncio.run_coroutine_threadsafe(
                    ctx.send('Audio stopped with error: `' + str(err)[:180] + '`'),
                    bot.loop,
                )
            except Exception:
                pass
        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)

    vc.play(source, after=after_play)
    return True


async def fetch_autoplay_track(guild_id):
    """Pick next related video from stored related_ids and fetch it via Piped."""
    related = _autoplay_related.get(guild_id) or []
    if not related:
        return None
    vid = related.pop(0)
    _autoplay_related[guild_id] = related
    log.info('autoplay: fetching related video %s', vid)
    url = 'https://www.youtube.com/watch?v=' + vid
    loop = asyncio.get_event_loop()
    track = await loop.run_in_executor(None, lambda: fetch_via_piped(url))
    track['query'] = url
    return track


async def play_next(ctx):
    guild_id = ctx.guild.id
    mode = get_loop(guild_id)
    current = now_playing.get(guild_id)
    queue = get_queue(guild_id)

    if mode == 'one' and current:
        next_track = current
    else:
        if mode == 'all' and current:
            queue.append(current)
        if not queue:
            if get_autoplay(guild_id) and mode == 'off':
                try:
                    next_track = await fetch_autoplay_track(guild_id)
                    if not next_track:
                        now_playing.pop(guild_id, None)
                        await ctx.send('🎧 Autoplay: ไม่มีเพลงที่เกี่ยวข้องแล้ว')
                        return
                    log.info('autoplay: playing %s', next_track.get('title'))
                except Exception as e:
                    log.warning('autoplay fetch failed: %s', e)
                    now_playing.pop(guild_id, None)
                    return
            else:
                now_playing.pop(guild_id, None)
                return
        else:
            next_track = queue.pop(0)

    now_playing[guild_id] = next_track

    if next_track.get('related_ids'):
        _autoplay_related[guild_id] = next_track['related_ids']

    needs_refetch = (mode == 'one') or (mode == 'all' and next_track is current)
    if needs_refetch and next_track.get('query'):
        try:
            fresh = await fetch_track(next_track['query'])
            next_track['url'] = fresh['url']
            next_track['codec'] = fresh.get('codec')
        except Exception as e:
            log.warning('re-fetch failed: %s', e)

    if not next_track.get('url'):
        fetch_q = next_track.get('query') or next_track.get('webpage_url', '')
        if fetch_q:
            try:
                log.info('lazy-fetch for queued track: %s', next_track.get('title'))
                fresh = await fetch_track(fetch_q)
                next_track.update(fresh)
            except Exception as e:
                log.warning('lazy-fetch failed "%s": %s', next_track.get('title'), e)
                try:
                    await ctx.send('⚠️ ข้ามเพลง **' + next_track.get('title', '?') + '** (หาไม่เจอ)')
                except Exception:
                    pass
                asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
                return

    try:
        ok = await _start_playback(ctx, next_track)
        if not ok:
            return
        await ctx.send(embed=make_np_embed(next_track, guild_id), view=PlayerView(ctx))
    except Exception as e:
        log.exception('play_next error: %s', e)
        try:
            await ctx.send('⚠️ เล่นไม่ได้ ข้ามเพลงถัดไป...')
        except Exception:
            pass
        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)


@bot.event
async def on_ready():
    log.info('%s online (id=%s)', bot.user, bot.user.id)
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name='!play'))
    pl_db.load()


@bot.event
async def on_voice_state_update(member, before, after):
    if member.id == bot.user.id:
        log.info('voice: %s → %s', before.channel, after.channel)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send('⚠️ ใส่ชื่อเพลงหรือ link ด้วย เช่น `!play จี๋หอย`')
        return
    log.exception('command error: %s', error)
    try:
        await ctx.send('⚠️ Error: ' + str(error))
    except Exception:
        pass


@bot.command(name='play', aliases=['p'])
async def play(ctx, *, query):
    if not ctx.author.voice:
        await ctx.send('❌ เข้า voice channel ก่อนนะ!')
        return

    url_type = _detect_url_type(query.strip())
    labels = {'youtube': '▶️ YouTube', 'soundcloud': '🔶 SoundCloud',
              'spotify': '🟢 Spotify', 'other_url': '🔗 Link', 'search': '🔍 ค้นหา'}
    status = await ctx.send(labels.get(url_type, '🔍') + ': `' + query[:80] + '` ...')

    try:
        vc = await ensure_voice(ctx)
    except Exception as e:
        await status.edit(content='❌ เชื่อมต่อ voice ไม่ได้: ' + str(e))
        return

    try:
        track = await fetch_track(query)
    except Exception as e:
        await status.edit(content='❌ ไม่พบเพลงนั้น\n```\n' + str(e)[:400] + '\n```')
        return

    queue = get_queue(ctx.guild.id)
    if vc.is_playing() or vc.is_paused():
        queue.append(track)
        embed = discord.Embed(
            title='✅ เพิ่มเข้าคิวแล้ว',
            description='**[' + track['title'] + '](' + track['webpage_url'] + ')**',
            color=0x57F287,
        )
        embed.add_field(name='ลำดับ', value=str(len(queue)), inline=True)
        embed.add_field(name='⏱', value=fmt_duration(track['duration']), inline=True)
        if track.get('thumbnail'):
            embed.set_thumbnail(url=track['thumbnail'])
        await status.edit(content=None, embed=embed)
    else:
        now_playing[ctx.guild.id] = track
        if track.get('related_ids'):
            _autoplay_related[ctx.guild.id] = track['related_ids']
        try:
            ok = await _start_playback(ctx, track)
            if not ok:
                await status.edit(content='❌ เริ่มเล่นไม่ได้')
                return
            await status.edit(content=None, embed=make_np_embed(track, ctx.guild.id), view=PlayerView(ctx))
        except Exception as e:
            await status.edit(content='❌ เริ่มเล่นไม่ได้: ' + str(e))


@bot.command(name='skip', aliases=['s'])
async def skip(ctx):
    if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        ctx.voice_client.stop()
        await ctx.send('⏭️ ข้ามเพลงแล้ว')
    else:
        await ctx.send('❌ ไม่มีเพลงเล่นอยู่')


@bot.command(name='queue', aliases=['q'])
async def show_queue(ctx):
    queue = get_queue(ctx.guild.id)
    np = now_playing.get(ctx.guild.id)
    embed = discord.Embed(title='📋 คิวเพลง', color=0x5865F2)
    embed.add_field(name='🔁 Loop', value=LOOP_LABELS[get_loop(ctx.guild.id)], inline=True)
    embed.add_field(name='🎧 Autoplay', value='เปิด ✅' if get_autoplay(ctx.guild.id) else 'ปิด ❌', inline=True)
    if np:
        embed.add_field(name='🎵 กำลังเล่น', value='**' + np['title'] + '**  ' + fmt_duration(np['duration']), inline=False)
    if queue:
        lines = [str(i) + '. **' + t['title'] + '**  ' + fmt_duration(t['duration']) for i, t in enumerate(queue[:10], 1)]
        if len(queue) > 10:
            lines.append('...อีก ' + str(len(queue) - 10) + ' เพลง')
        embed.add_field(name='คิว (' + str(len(queue)) + ')', value='\n'.join(lines), inline=False)
    elif not np:
        embed.description = 'คิวว่างเปล่า'
    await ctx.send(embed=embed)


@bot.command(name='np', aliases=['nowplaying'])
async def nowplaying(ctx):
    track = now_playing.get(ctx.guild.id)
    if not track:
        await ctx.send('❌ ไม่มีเพลงเล่นอยู่')
        return
    await ctx.send(embed=make_np_embed(track, ctx.guild.id), view=PlayerView(ctx))


@bot.command(name='pause')
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send('⏸️ หยุดชั่วคราว')
    else:
        await ctx.send('❌ ไม่มีเพลงเล่นอยู่')


@bot.command(name='resume')
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send('▶️ เล่นต่อแล้ว')
    else:
        await ctx.send('❌ ไม่มีเพลงที่หยุดไว้')


@bot.command(name='loop', aliases=['l'])
async def loop_cmd(ctx, mode: str = None):
    valid = {'off', 'one', 'all'}
    aliases_map = {'no': 'off', 'none': 'off', '0': 'off', '1': 'one',
                   'single': 'one', 'song': 'one', 'queue': 'all', 'q': 'all', 'a': 'all'}
    if mode is None:
        new_mode = cycle_loop(ctx.guild.id)
    else:
        m = aliases_map.get(mode.lower(), mode.lower())
        if m not in valid:
            await ctx.send('❌ ใช้: `!loop off | one | all`')
            return
        set_loop(ctx.guild.id, m)
        new_mode = m
    await ctx.send('🔁 Loop: **' + LOOP_LABELS[new_mode] + '**')


@bot.command(name='clear', aliases=['cl'])
async def clear_queue(ctx):
    queues[ctx.guild.id] = []
    await ctx.send('🗑️ ล้างคิวแล้ว')



@bot.command(name='shuffle', aliases=['sh'])
async def shuffle_queue(ctx):
    import random
    queue = get_queue(ctx.guild.id)
    if not queue:
        await ctx.send('❌ คิวว่างเปล่า')
        return
    random.shuffle(queue)
    await ctx.send('🔀 สับเปลี่ยนคิว **' + str(len(queue)) + '** เพลงแล้ว')


@bot.command(name='volume', aliases=['vol'])
async def volume_cmd(ctx, level: int = None):
    if level is None:
        vol_pct = int(get_volume(ctx.guild.id) * 100)
        await ctx.send('🔊 Volume ปัจจุบัน: **' + str(vol_pct) + '%**')
        return
    if not 10 <= level <= 200:
        await ctx.send('❌ ใส่ระหว่าง 10–200')
        return
    new_vol = set_volume(ctx.guild.id, level / 100.0)
    await ctx.send('🔊 Volume: **' + str(int(new_vol * 100)) + '%**')


@bot.command(name='leave', aliases=['dc', 'disconnect'])
async def leave(ctx):
    if ctx.voice_client:
        queues[ctx.guild.id] = []
        now_playing.pop(ctx.guild.id, None)
        _autoplay_related.pop(ctx.guild.id, None)
        set_loop(ctx.guild.id, 'off')
        set_autoplay(ctx.guild.id, False)
        await ctx.voice_client.disconnect(force=True)
        await ctx.send('👋 ออกจาก voice แล้ว')
    else:
        await ctx.send('❌ บอทไม่ได้อยู่ใน voice')


@bot.command(name='stop')
async def stop(ctx):
    if ctx.voice_client:
        queues[ctx.guild.id] = []
        now_playing.pop(ctx.guild.id, None)
        _autoplay_related.pop(ctx.guild.id, None)
        set_loop(ctx.guild.id, 'off')
        set_autoplay(ctx.guild.id, False)
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect(force=True)
        await ctx.send('⏹️ หยุดและออกจาก voice แล้ว')
    else:
        await ctx.send('❌ บอทไม่ได้อยู่ใน voice')


@bot.command(name='autoplay', aliases=['ap'])
async def autoplay_cmd(ctx):
    on = not get_autoplay(ctx.guild.id)
    set_autoplay(ctx.guild.id, on)
    if on:
        await ctx.send('🎧 Autoplay **เปิด ✅** — เมื่อคิวหมด บอทจะเล่นเพลงที่เกี่ยวข้องต่อเองอัตโนมัติ')
    else:
        await ctx.send('🎧 Autoplay **ปิด ❌**')


@bot.command(name='reconnect', aliases=['rc'])
async def reconnect(ctx):
    if ctx.voice_client:
        try:
            await ctx.voice_client.disconnect(force=True)
        except Exception:
            pass
    if not ctx.author.voice:
        await ctx.send('❌ เข้า voice channel ก่อน')
        return
    try:
        await ensure_voice(ctx)
        await ctx.send('🔄 เชื่อมต่อใหม่แล้ว')
    except Exception as e:
        await ctx.send('❌ เชื่อมต่อไม่ได้: ' + str(e))


@bot.command(name='testaudio', aliases=['beep', 'tone'])
async def testaudio(ctx, seconds: int = 8):
    if not ctx.author.voice:
        await ctx.send('Join a voice channel first.')
        return
    seconds = max(2, min(seconds, 20))
    try:
        vc = await ensure_voice(ctx)
    except Exception as e:
        await ctx.send('Voice connect failed: ' + str(e))
        return

    if vc.is_playing() or vc.is_paused():
        vc.stop()

    if not discord.opus.is_loaded():
        await ctx.send('Discord opus library is not loaded.')
        return

    source = discord.FFmpegPCMAudio(
        'sine=frequency=880:duration=' + str(seconds),
        executable=FFMPEG_EXECUTABLE,
        before_options='-f lavfi',
        options=FFMPEG_PCM_OPTIONS,
    )

    def after_test(err):
        if err:
            log.warning('testaudio after-play: %s', err)
            try:
                asyncio.run_coroutine_threadsafe(
                    ctx.send('Test audio error: `' + str(err)[:180] + '`'),
                    bot.loop,
                )
            except Exception:
                pass

    vc.play(source, after=after_test)
    await ctx.send('Playing test tone for ' + str(seconds) + ' seconds.')


@bot.command(name='diag')
async def diag(ctx):
    vc = ctx.voice_client
    if not vc:
        voice = 'not connected'
    else:
        voice = (
            'connected=' + str(vc.is_connected())
            + ' playing=' + str(vc.is_playing())
            + ' paused=' + str(vc.is_paused())
            + ' channel=' + str(vc.channel)
        )
    await ctx.send(
        'ffmpeg=`' + FFMPEG_EXECUTABLE + '`\n'
        + 'opus_loaded=`' + str(discord.opus.is_loaded()) + '`\n'
        + 'voice: `' + voice + '`'
    )


@bot.command(name='help', aliases=['h', 'commands'])
async def help_cmd(ctx):
    embed = discord.Embed(title='🎵 คำสั่งทั้งหมด', color=0x5865F2)
    embed.add_field(
        name='!play <ชื่อเพลง หรือ link>',
        value='รองรับ: 🔍 ค้นหา · ▶️ YouTube · 🟢 Spotify · 🔶 SoundCloud\n'
              '```!play จี๋หอย\n!play https://youtu.be/...\n!play https://open.spotify.com/track/...\n!play https://soundcloud.com/...```',
        inline=False,
    )
    embed.add_field(name='!skip (!s)', value='ข้ามเพลง', inline=True)
    embed.add_field(name='!queue (!q)', value='ดูคิว', inline=True)
    embed.add_field(name='!np', value='Now Playing + ปุ่ม', inline=True)
    embed.add_field(name='!loop [off|one|all]', value='โหมด Loop', inline=True)
    embed.add_field(name='!autoplay (!ap)', value='เปิด/ปิด เล่นต่ออัตโนมัติ', inline=True)
    embed.add_field(name='!pause / !resume', value='หยุด / เล่นต่อ', inline=True)
    embed.add_field(name='!stop', value='หยุด + ออก voice', inline=True)
    embed.add_field(name='!clear', value='ล้างคิว', inline=True)
    embed.add_field(name='!leave (!dc)', value='ออก voice', inline=True)
    embed.add_field(name='!reconnect (!rc)', value='เชื่อมใหม่', inline=True)
    embed.add_field(name='!shuffle (!sh)', value='สับเปลี่ยนคิว', inline=True)
    embed.add_field(name='!volume [10-200]', value='ปรับความดัง', inline=True)
    embed.add_field(
        name='📂 Playlist (!pl)',
        value='`!pl` ดู playlist ทั้งหมด\n`!pl create <ชื่อ>` สร้างใหม่\n`!pl play <ชื่อ>` เล่น\n`!pl add <ชื่อ>` เพิ่มเพลงที่เล่นอยู่\n`!pl view <ชื่อ>` ดูเพลง\n`!pl import <url> [ชื่อ]` import จาก YouTube/Spotify/SoundCloud',
        inline=False,
    )
    embed.set_footer(text='Row 1: ⏯️ Pause/Resume  ⏭️ Skip  🔁 Loop  🎧 Autoplay  ⏹️ Stop\nRow 2: 📋 Queue  🔀 Shuffle  ➕ Add to PL  🔉 Vol-  🔊 Vol+')
    await ctx.send(embed=embed)


PL_COLOR = 0xE91E63
PL_PAGE_SIZE = 10


def _source_emoji(source):
    return {'youtube': '▶️', 'soundcloud': '🔶', 'spotify': '🟢', 'manual': '🎵'}.get(source or 'manual', '🎵')


def _pl_list_embed(user, playlists):
    embed = discord.Embed(
        title='📂 Playlist ของ ' + user.display_name,
        color=PL_COLOR,
    )
    if not playlists:
        embed.description = 'ยังไม่มี playlist\nใช้ `!pl create <ชื่อ>` เพื่อสร้าง'
        return embed
    lines = []
    for key, pl in playlists.items():
        cnt = len(pl.get('tracks', []))
        lines.append('📁 **' + pl['name'] + '** — ' + str(cnt) + ' เพลง')
    embed.description = '\n'.join(lines)
    embed.set_footer(text='!pl play <ชื่อ>  •  !pl view <ชื่อ>  •  !pl import <url>')
    return embed


def _pl_view_embed(pl, page=0):
    tracks = pl.get('tracks', [])
    total = len(tracks)
    total_pages = max(1, (total + PL_PAGE_SIZE - 1) // PL_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PL_PAGE_SIZE
    chunk = tracks[start:start + PL_PAGE_SIZE]

    total_dur = sum(t.get('duration', 0) for t in tracks)
    embed = discord.Embed(
        title='📁 ' + pl['name'],
        color=PL_COLOR,
        description=str(total) + ' เพลง • ' + fmt_duration(total_dur),
    )
    if not tracks:
        embed.description = 'ยังไม่มีเพลงใน playlist นี้'
        return embed, total_pages

    lines = []
    for i, t in enumerate(chunk, start + 1):
        emoji = _source_emoji(t.get('source'))
        dur = fmt_duration(t.get('duration', 0))
        lines.append(str(i) + '. ' + emoji + ' **' + t['title'][:50] + '** `' + dur + '`')
    embed.add_field(name='เพลง', value='\n'.join(lines), inline=False)
    embed.set_footer(text='หน้า ' + str(page + 1) + '/' + str(total_pages))
    return embed, total_pages


class PlaylistPageView(discord.ui.View):
    def __init__(self, pl, page=0):
        super().__init__(timeout=120)
        self.pl = pl
        self.page = page
        _, self.total_pages = _pl_view_embed(pl)
        self._update_buttons()

    def _update_buttons(self):
        for child in self.children:
            if getattr(child, 'custom_id', None) == 'prev':
                child.disabled = self.page == 0
            elif getattr(child, 'custom_id', None) == 'next':
                child.disabled = self.page >= self.total_pages - 1

    @discord.ui.button(emoji='◀️', style=discord.ButtonStyle.secondary, custom_id='prev')
    async def prev_page(self, i: discord.Interaction, b):
        self.page -= 1
        self._update_buttons()
        embed, _ = _pl_view_embed(self.pl, self.page)
        await i.response.edit_message(embed=embed, view=self)

    @discord.ui.button(emoji='▶️', style=discord.ButtonStyle.secondary, custom_id='next')
    async def next_page(self, i: discord.Interaction, b):
        self.page += 1
        self._update_buttons()
        embed, _ = _pl_view_embed(self.pl, self.page)
        await i.response.edit_message(embed=embed, view=self)



# ── Playlist Import Modal ─────────────────────────────────────────────────────

class ImportPlaylistModal(discord.ui.Modal, title='📥 Import Playlist จากแอพอื่น'):
    url_input = discord.ui.TextInput(
        label='URL Playlist',
        placeholder='https://open.spotify.com/playlist/...  หรือ YouTube / SoundCloud',
        style=discord.TextStyle.short,
        required=True,
        max_length=500,
    )
    name_input = discord.ui.TextInput(
        label='ตั้งชื่อ Playlist (ไม่บังคับ)',
        placeholder='เว้นว่างเพื่อใช้ชื่อต้นฉบับ',
        style=discord.TextStyle.short,
        required=False,
        max_length=80,
    )

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx

    async def on_submit(self, i: discord.Interaction):
        url = self.url_input.value.strip()
        custom_name = self.name_input.value.strip() or None

        import_type = pl_db.detect_import_type(url)
        type_labels = {
            'youtube': '▶️ YouTube',
            'spotify': '🟢 Spotify',
            'soundcloud': '🔶 SoundCloud',
        }
        if not import_type:
            await i.response.send_message(
                '❌ รองรับแค่ YouTube Playlist, Spotify Playlist, หรือ SoundCloud Sets\n'
                'ตัวอย่าง:\n'
                '• `https://open.spotify.com/playlist/...`\n'
                '• `https://youtube.com/playlist?list=...`\n'
                '• `https://soundcloud.com/user/sets/...`',
                ephemeral=True,
            )
            return

        await i.response.send_message(
            type_labels.get(import_type, '🔗') + ' กำลัง import playlist... รอสักครู่ 🔄',
            ephemeral=True,
        )

        loop = asyncio.get_event_loop()
        try:
            if import_type == 'youtube':
                pl_name, tracks = await loop.run_in_executor(
                    None, lambda: pl_db.import_youtube_playlist(url, piped_instances)
                )
            elif import_type == 'soundcloud':
                pl_name, tracks = await loop.run_in_executor(
                    None, lambda: pl_db.import_soundcloud_playlist(url)
                )
            else:
                pl_name, tracks = await loop.run_in_executor(
                    None, lambda: pl_db.import_spotify_playlist(url)
                )
        except Exception as e:
            await i.edit_original_response(content='❌ Import ไม่สำเร็จ: ' + str(e)[:200])
            return

        if not tracks:
            await i.edit_original_response(content='❌ ไม่พบเพลงใน playlist นั้น')
            return

        save_name = custom_name or pl_name
        save_key = save_name.strip().lower()
        existing = pl_db.get(i.user.id, save_key)

        if existing:
            old_cnt = len(existing.get('tracks', []))
            save_name = save_name + ' (imported)'
            save_key = save_name.lower()

        pl_db.set_tracks(i.user.id, save_key, save_name, tracks)

        embed = discord.Embed(
            title='✅ Import สำเร็จ!',
            description='📁 **' + save_name + '**',
            color=PL_COLOR,
        )
        embed.add_field(name=type_labels.get(import_type, '🔗') + ' แหล่งที่มา', value=url[:80], inline=False)
        embed.add_field(name='🎵 เพลงทั้งหมด', value=str(len(tracks)) + ' เพลง', inline=True)
        if existing:
            embed.add_field(name='ℹ️', value='มีชื่อซ้ำ บันทึกเป็น **' + save_name + '**', inline=True)
        embed.set_footer(text='ใช้ !pl play ' + save_name + ' เพื่อเล่น')
        await i.edit_original_response(content=None, embed=embed)


# ── Playlist Manager View ─────────────────────────────────────────────────────

class PlaylistManagerView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=120)
        self.ctx = ctx

    @discord.ui.button(emoji='📥', label='Import จาก Spotify / YouTube / SoundCloud',
                       style=discord.ButtonStyle.success, custom_id='import_pl', row=0)
    async def import_btn(self, i: discord.Interaction, b):
        if i.user.id != self.ctx.author.id:
            await i.response.send_message('❌ ปุ่มนี้สำหรับ ' + self.ctx.author.mention + ' เท่านั้น', ephemeral=True)
            return
        await i.response.send_modal(ImportPlaylistModal(self.ctx))

    @discord.ui.button(emoji='➕', label='สร้าง Playlist ใหม่',
                       style=discord.ButtonStyle.primary, custom_id='create_pl', row=0)
    async def create_btn(self, i: discord.Interaction, b):
        if i.user.id != self.ctx.author.id:
            await i.response.send_message('❌ ปุ่มนี้สำหรับ ' + self.ctx.author.mention + ' เท่านั้น', ephemeral=True)
            return
        await i.response.send_modal(CreatePlaylistModal(self.ctx))

    @discord.ui.button(emoji='🔄', label='รีเฟรช',
                       style=discord.ButtonStyle.secondary, custom_id='refresh_pl', row=0)
    async def refresh_btn(self, i: discord.Interaction, b):
        if i.user.id != self.ctx.author.id:
            await i.response.send_message('❌ ปุ่มนี้สำหรับ ' + self.ctx.author.mention + ' เท่านั้น', ephemeral=True)
            return
        playlists = pl_db.get_all(i.user.id)
        embed = _pl_list_embed(i.user, playlists)
        await i.response.edit_message(embed=embed, view=self)


class CreatePlaylistModal(discord.ui.Modal, title='➕ สร้าง Playlist ใหม่'):
    name_input = discord.ui.TextInput(
        label='ชื่อ Playlist',
        placeholder='เช่น เพลงโปรด, Chill Vibes, ...',
        style=discord.TextStyle.short,
        required=True,
        max_length=80,
    )

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx

    async def on_submit(self, i: discord.Interaction):
        name = self.name_input.value.strip()
        ok, result = pl_db.create(i.user.id, name)
        if not ok:
            await i.response.send_message('❌ ' + result, ephemeral=True)
            return
        playlists = pl_db.get_all(i.user.id)
        embed = _pl_list_embed(i.user, playlists)
        embed.colour = discord.Colour.green()
        embed.set_footer(text='✅ สร้าง "' + name + '" แล้ว')
        await i.response.edit_message(embed=embed, view=PlaylistManagerView(self.ctx))

@bot.group(name='playlist', aliases=['pl'], invoke_without_command=True)
async def playlist_group(ctx):
    playlists = pl_db.get_all(ctx.author.id)
    embed = _pl_list_embed(ctx.author, playlists)
    await ctx.send(embed=embed, view=PlaylistManagerView(ctx))


@playlist_group.command(name='create', aliases=['new', 'c'])
async def pl_create(ctx, *, name: str):
    ok, result = pl_db.create(ctx.author.id, name)
    if not ok:
        await ctx.send('❌ ' + result)
        return
    embed = discord.Embed(
        title='✅ สร้าง Playlist แล้ว',
        description='📁 **' + name + '**\nใช้ `!pl add ' + name + '` เพื่อเพิ่มเพลงที่กำลังเล่น\nหรือ `!pl import <url> ' + name + '` เพื่อ import จากแอพอื่น',
        color=PL_COLOR,
    )
    await ctx.send(embed=embed)


@playlist_group.command(name='delete', aliases=['del', 'd'])
async def pl_delete(ctx, *, name: str):
    pl = pl_db.get(ctx.author.id, name)
    if not pl:
        await ctx.send('❌ ไม่พบ playlist **' + name + '**')
        return
    cnt = len(pl.get('tracks', []))

    class ConfirmDelete(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)
            self.confirmed = False

        @discord.ui.button(label='ลบเลย', style=discord.ButtonStyle.danger, emoji='🗑️')
        async def confirm(self, i: discord.Interaction, b):
            if i.user.id != ctx.author.id:
                await i.response.send_message('❌ ไม่ใช่ playlist ของคุณ', ephemeral=True)
                return
            pl_db.delete(ctx.author.id, name)
            self.confirmed = True
            self.stop()
            await i.response.edit_message(
                content='🗑️ ลบ playlist **' + pl['name'] + '** แล้ว (' + str(cnt) + ' เพลง)',
                embed=None, view=None,
            )

        @discord.ui.button(label='ยกเลิก', style=discord.ButtonStyle.secondary)
        async def cancel(self, i: discord.Interaction, b):
            self.stop()
            await i.response.edit_message(content='ยกเลิกแล้ว', embed=None, view=None)

    embed = discord.Embed(
        title='⚠️ ยืนยันการลบ',
        description='ลบ playlist **' + pl['name'] + '** (' + str(cnt) + ' เพลง) ?\nไม่สามารถกู้คืนได้',
        color=0xFF6B6B,
    )
    await ctx.send(embed=embed, view=ConfirmDelete())


@playlist_group.command(name='rename', aliases=['mv'])
async def pl_rename(ctx, old_name: str, *, new_name: str):
    ok, result = pl_db.rename(ctx.author.id, old_name, new_name)
    if not ok:
        await ctx.send('❌ ' + result)
        return
    await ctx.send('✅ เปลี่ยนชื่อ **' + old_name + '** → **' + new_name + '** แล้ว')


@playlist_group.command(name='add', aliases=['a'])
async def pl_add(ctx, *, name: str):
    track = now_playing.get(ctx.guild.id)
    if not track:
        await ctx.send('❌ ไม่มีเพลงเล่นอยู่ตอนนี้')
        return
    ok, result = pl_db.add_track(ctx.author.id, name, track)
    if not ok:
        await ctx.send('❌ ' + result)
        return
    embed = discord.Embed(
        title='✅ เพิ่มเพลงแล้ว',
        description='**' + track['title'] + '**\nเพิ่มลง playlist **' + name + '**',
        color=PL_COLOR,
    )
    if track.get('thumbnail'):
        embed.set_thumbnail(url=track['thumbnail'])
    await ctx.send(embed=embed)


@playlist_group.command(name='remove', aliases=['rm', 'r'])
async def pl_remove(ctx, playlist_name: str, index: int):
    ok, result = pl_db.remove_track(ctx.author.id, playlist_name, index)
    if not ok:
        await ctx.send('❌ ' + result)
        return
    await ctx.send('🗑️ ลบ **' + result['title'] + '** ออกจาก playlist **' + playlist_name + '** แล้ว')


@playlist_group.command(name='view', aliases=['show', 'v', 'ls'])
async def pl_view(ctx, *, name: str):
    pl = pl_db.get(ctx.author.id, name)
    if not pl:
        await ctx.send('❌ ไม่พบ playlist **' + name + '** (ตรวจสอบชื่อด้วย `!pl`)')
        return
    embed, total_pages = _pl_view_embed(pl, 0)
    view = PlaylistPageView(pl, 0) if total_pages > 1 else None
    await ctx.send(embed=embed, view=view)


@playlist_group.command(name='play', aliases=['start', 'p'])
async def pl_play(ctx, *, name: str):
    if not ctx.author.voice:
        await ctx.send('❌ เข้า voice channel ก่อนนะ!')
        return
    pl = pl_db.get(ctx.author.id, name)
    if not pl:
        await ctx.send('❌ ไม่พบ playlist **' + name + '** (ตรวจสอบชื่อด้วย `!pl`)')
        return
    tracks = pl.get('tracks', [])
    if not tracks:
        await ctx.send('❌ playlist **' + pl['name'] + '** ว่างเปล่า')
        return

    try:
        vc = await ensure_voice(ctx)
    except Exception as e:
        await ctx.send('❌ เชื่อมต่อ voice ไม่ได้: ' + str(e))
        return

    queue = get_queue(ctx.guild.id)
    entries = [pl_db.track_to_queue_entry(t) for t in tracks]

    if vc.is_playing() or vc.is_paused():
        queue.extend(entries)
        embed = discord.Embed(
            title='📂 เพิ่ม Playlist เข้าคิวแล้ว',
            description='**' + pl['name'] + '** — ' + str(len(entries)) + ' เพลง',
            color=PL_COLOR,
        )
        await ctx.send(embed=embed)
    else:
        first = entries.pop(0)
        queue.extend(entries)
        status = await ctx.send('📂 กำลังโหลด **' + pl['name'] + '** (' + str(len(tracks)) + ' เพลง)...')
        try:
            fresh = await fetch_track(first.get('query') or first.get('webpage_url', ''))
            first.update(fresh)
        except Exception as e:
            await status.edit(content='❌ เล่นเพลงแรกไม่ได้: ' + str(e))
            return
        now_playing[ctx.guild.id] = first
        if first.get('related_ids'):
            _autoplay_related[ctx.guild.id] = first['related_ids']
        try:
            ok = await _start_playback(ctx, first)
            if not ok:
                await status.edit(content='❌ เริ่มเล่นไม่ได้')
                return
            embed = discord.Embed(
                title='📂 เล่น Playlist: ' + pl['name'],
                description='**[' + first['title'] + '](' + first['webpage_url'] + ')**',
                color=PL_COLOR,
            )
            embed.add_field(name='🎵 คิว', value=str(len(tracks)) + ' เพลง', inline=True)
            embed.add_field(name='⏱', value=fmt_duration(first['duration']), inline=True)
            if first.get('thumbnail'):
                embed.set_thumbnail(url=first['thumbnail'])
            await status.edit(content=None, embed=embed, view=PlayerView(ctx))
        except Exception as e:
            await status.edit(content='❌ เริ่มเล่นไม่ได้: ' + str(e))


@playlist_group.command(name='import', aliases=['imp', 'i'])
async def pl_import(ctx, url: str, *, name: str = None):
    import_type = pl_db.detect_import_type(url)
    if not import_type:
        await ctx.send('❌ รองรับแค่ YouTube Playlist, Spotify Playlist, หรือ SoundCloud Sets')
        return

    type_labels = {'youtube': '▶️ YouTube', 'spotify': '🟢 Spotify', 'soundcloud': '🔶 SoundCloud'}
    status = await ctx.send(type_labels.get(import_type, '🔗') + ' กำลัง import playlist...')

    loop = asyncio.get_event_loop()
    try:
        if import_type == 'youtube':
            pl_name, tracks = await loop.run_in_executor(
                None, lambda: pl_db.import_youtube_playlist(url, piped_instances)
            )
        elif import_type == 'soundcloud':
            pl_name, tracks = await loop.run_in_executor(
                None, lambda: pl_db.import_soundcloud_playlist(url)
            )
        else:
            pl_name, tracks = await loop.run_in_executor(
                None, lambda: pl_db.import_spotify_playlist(url)
            )
    except Exception as e:
        await status.edit(content='❌ Import ไม่สำเร็จ: ' + str(e)[:200])
        return

    if not tracks:
        await status.edit(content='❌ ไม่พบเพลงใน playlist นั้น')
        return

    save_name = name or pl_name
    save_key = save_name.strip().lower()

    existing = pl_db.get(ctx.author.id, save_key)
    if existing:
        class OverwriteView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=30)
                self.choice = None

            @discord.ui.button(label='เขียนทับ', style=discord.ButtonStyle.danger)
            async def overwrite(self, i: discord.Interaction, b):
                if i.user.id != ctx.author.id:
                    await i.response.send_message('ไม่ใช่ของคุณ', ephemeral=True); return
                self.choice = 'overwrite'
                self.stop()
                await i.response.defer()

            @discord.ui.button(label='สร้างใหม่ชื่ออื่น', style=discord.ButtonStyle.primary)
            async def new_name(self, i: discord.Interaction, b):
                if i.user.id != ctx.author.id:
                    await i.response.send_message('ไม่ใช่ของคุณ', ephemeral=True); return
                self.choice = 'new'
                self.stop()
                await i.response.defer()

            @discord.ui.button(label='ยกเลิก', style=discord.ButtonStyle.secondary)
            async def cancel_btn(self, i: discord.Interaction, b):
                self.choice = 'cancel'
                self.stop()
                await i.response.defer()

        view = OverwriteView()
        await status.edit(
            content='⚠️ มี playlist **' + save_name + '** อยู่แล้ว (' + str(len(existing['tracks'])) + ' เพลง)\nต้องการทำอะไร?',
            view=view,
        )
        await view.wait()
        if view.choice == 'cancel' or view.choice is None:
            await status.edit(content='ยกเลิกแล้ว', view=None)
            return
        if view.choice == 'new':
            save_name = pl_name + ' (imported)'
            save_key = save_name.lower()

    pl_db.set_tracks(ctx.author.id, save_key, save_name, tracks)

    embed = discord.Embed(
        title='✅ Import สำเร็จ!',
        description='📁 **' + save_name + '**',
        color=PL_COLOR,
    )
    embed.add_field(name=type_labels.get(import_type, '🔗') + ' แหล่งที่มา', value=url[:60] + ('...' if len(url) > 60 else ''), inline=False)
    embed.add_field(name='🎵 เพลงทั้งหมด', value=str(len(tracks)) + ' เพลง', inline=True)
    if len(tracks) > pl_db.MAX_TRACKS:
        embed.add_field(name='⚠️', value='นำเข้าได้สูงสุด ' + str(pl_db.MAX_TRACKS) + ' เพลง', inline=True)
    embed.set_footer(text='ใช้ !pl play ' + save_name + ' เพื่อเล่น')
    await status.edit(content=None, embed=embed, view=None)


TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
if not TOKEN:
    raise RuntimeError('DISCORD_BOT_TOKEN is not set!')
bot.run(TOKEN, log_handler=None)
