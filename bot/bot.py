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
import subprocess
import sys
import importlib.util
import concurrent.futures
import time
import playlist as pl

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
STREAM_PREFLIGHT_SECONDS = 1
STREAM_PREFLIGHT_TIMEOUT = 8
FETCH_CACHE_TTL = 600
FETCH_RACE_TIMEOUT = 18
FETCH_RACE_WORKERS = 4
STREAM_URL_TTL = int(os.environ.get('STREAM_URL_TTL', '240'))
MAX_PIPED_INSTANCES = int(os.environ.get('MAX_PIPED_INSTANCES', '2'))
PIPED_STREAM_INSTANCES = int(os.environ.get('PIPED_STREAM_INSTANCES', '6'))
MAX_PIPED_STREAMS = int(os.environ.get('MAX_PIPED_STREAMS', '1'))
_FETCH_CACHE = {}

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

    base_instances = list(PIPED_INSTANCES)
    instances = []
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

    for api_url in base_instances:
        if api_url not in instances:
            instances.append(api_url)

    _PIPED_INSTANCES_CACHE = instances
    _PIPED_INSTANCES_TS = time.time()
    return instances


YOUTUBE_VIDEO_ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')


def clean_query(query):
    q = (query or '').strip()
    if q.startswith('<') and q.endswith('>'):
        q = q[1:-1].strip()
    return q


def _is_spaced_thai_piece(token):
    return bool(re.fullmatch(r'[\u0e00-\u0e7f]+', token or '')) and len(token) <= 4


def _is_spaced_latin_piece(token):
    return len(token or '') == 1 and token.isascii() and token.isalpha()


def normalize_search_text(text):
    """Collapse titles like 'ค น ตื่ น C l a u d e' into useful search text."""
    text = clean_query(text)
    text = re.sub(r'[\u200b-\u200d\ufeff]', '', text)
    tokens = re.sub(r'\s+', ' ', text).strip().split(' ')
    out = []
    run = []
    run_type = None

    def flush():
        nonlocal run, run_type
        if run:
            out.append(''.join(run))
        run = []
        run_type = None

    for token in tokens:
        if _is_spaced_latin_piece(token):
            typ = 'latin'
        elif _is_spaced_thai_piece(token):
            typ = 'thai'
        else:
            typ = None

        if typ:
            if run and run_type != typ:
                flush()
            run_type = typ
            run.append(token)
        else:
            flush()
            if token:
                out.append(token)
    flush()
    return re.sub(r'\s+', ' ', ' '.join(out)).strip()


def simplify_music_search(text):
    text = normalize_search_text(text)
    text = re.sub(r'[\(\[][^\)\]]*(official|video|mv|lyrics?|audio|remaster|4k|hd)[^\)\]]*[\)\]]', ' ', text, flags=re.I)
    text = re.sub(r'\b(official|music|video|mv|lyrics?|audio|remaster(?:ed)?|4k|hd)\b', ' ', text, flags=re.I)
    text = re.sub(r'\s*[-|]\s*$', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def search_candidates(title, uploader=None, original=None):
    candidates = []
    simplified = simplify_music_search(title)
    latin_words = re.findall(r'[A-Za-z][A-Za-z0-9]{2,}', simplified)
    has_thai = bool(re.search(r'[\u0e00-\u0e7f]', simplified))
    if has_thai and latin_words:
        candidates.append(' '.join(latin_words))
    for value in (simplified, normalize_search_text(title), title, original):
        if value:
            candidates.append(value)
    base = list(candidates)
    if uploader:
        for value in base:
            candidates.append((value + ' ' + uploader).strip())
    seen = set()
    out = []
    for value in candidates:
        key = value.casefold()
        if key and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def search_terms(text):
    text = simplify_music_search(text)
    terms = re.findall(
        r'[A-Za-z0-9]+|[\u0e00-\u0e7f]+|[\u3040-\u30ff]+|[\u3400-\u4dbf\u4e00-\u9fff]+|[\uac00-\ud7af]+',
        text,
    )
    return [t.casefold() for t in terms if len(t.strip()) >= 2]


def score_search_result(query, title, uploader=''):
    terms = search_terms(query)
    haystack = normalize_search_text((title or '') + ' ' + (uploader or '')).casefold()
    score = 0
    for term in terms:
        if term in haystack:
            score += 5 if re.fullmatch(r'[a-z0-9]+', term) else 3
    return score


BAD_MUSIC_VARIANTS = (
    r'\bremix(?:ed)?\b',
    r'\bbootleg\b',
    r'\bcover\b',
    r'\bkaraoke\b',
    r'\binstrumental\b',
    r'\blive\b',
    r'\bacoustic\b',
    r'\bspecial\s+version\b',
    r'\bdemo\b',
    r'\bsped\s*up\b',
    r'\bspeed\s*up\b',
    r'\bnightcore\b',
    r'\bslowed\b',
    r'\breverb\b',
    r'\blo-?fi\b',
    r'\b8d\b',
    r'\bbass\s*boost(?:ed)?\b',
    r'\bmashup\b',
    r'\bchipmunk\b',
    r'\btiktok\b',
    r'\bdj\s+(?:remix|edit|version)\b',
    r'伴奏(?:版)?',
    r'节奏(?:版)?',
    r'節奏(?:版)?',
    r'纯音乐',
    r'純音樂',
    r'翻唱',
    r'改编',
    r'改編',
    r'混音(?:版)?',
    r'重混',
    r'加速(?:版)?',
    r'減速(?:版)?',
    r'减速(?:版)?',
    r'慢速(?:版)?',
    r'快版',
    r'慢版',
    r'现场(?:版)?',
    r'現場(?:版)?',
    r'演唱会',
    r'演唱會',
    r'卡拉\s*ok',
    r'剪辑版',
    r'剪輯版',
    r'铃声版',
    r'鈴聲版',
    r'dj版',
    r'リミックス',
    r'カラオケ',
    r'歌ってみた',
    r'ライブ',
    r'インスト',
    r'커버',
    r'리믹스',
    r'라이브',
    r'노래방',
)

NON_SONG_HINTS = (
    r'\breaction\b',
    r'\breview\b',
    r'\btutorial\b',
    r'\blesson\b',
    r'\bbehind\s+the\s+scenes\b',
)


def _contains_pattern(text, pattern):
    return bool(re.search(pattern, text or '', flags=re.I))


def unwanted_variant(candidate_text, wanted_text):
    candidate_text = candidate_text or ''
    wanted_text = wanted_text or ''
    for pattern in BAD_MUSIC_VARIANTS:
        if _contains_pattern(candidate_text, pattern) and not _contains_pattern(wanted_text, pattern):
            return pattern
    return ''


def music_match_score(expected, title, uploader='', duration=0):
    wanted_title = expected.get('title') or expected.get('query') or ''
    wanted_artist = expected.get('uploader') or ''
    wanted_text = ' '.join(
        str(v or '') for v in (
            wanted_title,
            wanted_artist,
            expected.get('album'),
        )
    )
    candidate_title = normalize_search_text(title or '').casefold()
    candidate_all = normalize_search_text((title or '') + ' ' + (uploader or '')).casefold()
    wanted_norm = normalize_search_text(wanted_text).casefold()

    title_terms = search_terms(wanted_title)
    artist_terms = search_terms(wanted_artist)
    score = 0

    if title_terms:
        matched_title = sum(1 for term in title_terms if term in candidate_title)
        score += matched_title * 14
        score -= (len(title_terms) - matched_title) * 18
        if matched_title == len(title_terms):
            score += 12

    if artist_terms:
        matched_artist = sum(1 for term in artist_terms if term in candidate_all)
        score += matched_artist * 8
        if matched_artist:
            score += 6

    if unwanted_variant(candidate_all, wanted_norm):
        score -= 90
    for pattern in NON_SONG_HINTS:
        if _contains_pattern(candidate_all, pattern) and not _contains_pattern(wanted_norm, pattern):
            score -= 50

    wanted_duration = int(expected.get('duration') or 0)
    try:
        duration = int(duration or 0)
    except Exception:
        duration = 0
    if wanted_duration and duration:
        diff = abs(wanted_duration - duration)
        if diff <= 3:
            score += 24
        elif diff <= 10:
            score += 18
        elif diff <= 25:
            score += 8
        elif diff > 90:
            score -= 80
        elif diff > 45:
            score -= 25

    official_text = candidate_all
    if re.search(r'\b(official|vevo)\b', official_text, flags=re.I):
        score += 10
    if re.search(r'\btopic\b', official_text, flags=re.I) or ' - topic' in official_text:
        score += 14
    if re.search(r'\bofficial\s+(audio|music\s+video|mv)\b', official_text, flags=re.I):
        score += 12
    return score


def acceptable_music_match(expected, candidate):
    title_terms = search_terms(expected.get('title') or expected.get('query') or '')
    title = candidate.get('title') or ''
    uploader = candidate.get('uploader') or candidate.get('uploaderName') or ''
    duration = candidate.get('duration') or 0
    score = music_match_score(expected, title, uploader, duration)
    candidate_all = normalize_search_text(title + ' ' + uploader).casefold()
    wanted_all = normalize_search_text(
        (expected.get('title') or '') + ' ' + (expected.get('uploader') or '')
    ).casefold()
    if unwanted_variant(candidate_all, wanted_all):
        return False, score
    if title_terms:
        candidate_title = normalize_search_text(title).casefold()
        if not any(term in candidate_title for term in title_terms):
            return False, score
    return score >= 26, score


def relaxed_music_match(expected, candidate):
    title = candidate.get('title') or ''
    uploader = candidate.get('uploader') or candidate.get('uploaderName') or ''
    score = music_match_score(expected, title, uploader, candidate.get('duration') or 0)
    candidate_all = normalize_search_text(title + ' ' + uploader).casefold()
    wanted_all = normalize_search_text(
        (expected.get('title') or '') + ' ' + (expected.get('uploader') or '')
    ).casefold()
    if unwanted_variant(candidate_all, wanted_all):
        return False, score
    for pattern in NON_SONG_HINTS:
        if _contains_pattern(candidate_all, pattern) and not _contains_pattern(wanted_all, pattern):
            return False, score

    title_terms = search_terms(expected.get('title') or expected.get('query') or '')
    if not title_terms:
        return score >= 12, score
    candidate_title = normalize_search_text(title).casefold()
    matched = sum(1 for term in title_terms if term in candidate_title)
    return matched > 0 and score >= 8, score


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


def _cache_key(query):
    return clean_query(query).casefold()


def _cached_track(query):
    item = _FETCH_CACHE.get(_cache_key(query))
    if not item:
        return None
    ts, track = item
    if time.time() - ts > FETCH_CACHE_TTL:
        _FETCH_CACHE.pop(_cache_key(query), None)
        return None
    cached = dict(track)
    log.info('fetch cache hit: %s', clean_query(query)[:80])
    return cached


def _remember_track(query, track):
    if track and track.get('url'):
        _FETCH_CACHE[_cache_key(query)] = (time.time(), dict(track))
    return track


def run_provider_race(query, providers, timeout=FETCH_RACE_TIMEOUT):
    if not providers:
        raise RuntimeError('no providers configured')
    errors = []
    max_workers = min(FETCH_RACE_WORKERS, len(providers))
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    futures = {executor.submit(fn, query): name for fn, name in providers}
    try:
        try:
            for future in concurrent.futures.as_completed(futures, timeout=timeout):
                name = futures[future]
                try:
                    result = future.result()
                    log.info('resolver won: %s', name)
                    return result
                except Exception as e:
                    errors.append(name + ': ' + str(e))
        except concurrent.futures.TimeoutError:
            pending = [name for future, name in futures.items() if not future.done()]
            if pending:
                errors.append('timeout waiting for: ' + ', '.join(pending))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    raise RuntimeError(' | '.join(errors))


def preflight_stream(url, label='stream'):
    if os.environ.get('SKIP_STREAM_PREFLIGHT') == '1':
        return True
    cmd = [
        FFMPEG_EXECUTABLE,
        '-hide_banner',
        '-nostdin',
        '-v', 'error',
        '-reconnect', '1',
        '-reconnect_streamed', '1',
        '-reconnect_on_network_error', '1',
        '-i', url,
        '-t', str(STREAM_PREFLIGHT_SECONDS),
        '-vn',
        '-f', 'null',
        '-',
    ]
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=STREAM_PREFLIGHT_TIMEOUT,
        )
    except Exception as e:
        raise RuntimeError(label + ' preflight failed: ' + str(e))
    if p.returncode != 0:
        err = (p.stderr or '').strip().replace('\n', ' ')
        raise RuntimeError(label + ' preflight rc=' + str(p.returncode) + ': ' + err[-260:])
    return True


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
    items = None
    if 'soundcloud.com' in q and re.match(r'https?://', q):
        items = [{
            'permalink_url': q,
            'title': q.split('/')[-1].replace('-', ' ').title(),
            'duration': 0,
            'artwork_url': None,
            'user': {'username': 'SoundCloud'},
        }]
    else:
        cid = get_soundcloud_client_id()
        url = ('https://api-v2.soundcloud.com/search/tracks?q=' +
               urllib.parse.quote(q) + '&limit=10&client_id=' + cid)
        data = http_get_json(url, timeout=10)
        items = [t for t in (data.get('collection') or []) if t.get('streamable')]
        if not items:
            raise RuntimeError('no SC results')
        items = [
            t for _, t in sorted(
                enumerate(items),
                key=lambda pair: (
                    -score_search_result(
                        q,
                        pair[1].get('title', ''),
                        (pair[1].get('user') or {}).get('username', ''),
                    ),
                    pair[0],
                ),
            )
        ]

    opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'source_address': '0.0.0.0',
    }
    errors = []
    for t in items[:8]:
        track_url = t.get('permalink_url')
        if not track_url:
            continue
        title = t.get('title', 'Unknown')
        duration = int((t.get('duration') or 0) / 1000)
        thumb = t.get('artwork_url')
        uploader = (t.get('user') or {}).get('username', 'SoundCloud')
        try:
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
        except Exception as e:
            errors.append(title + ': ' + str(e))
            log.warning('soundcloud candidate failed %s: %s', track_url, e)
    raise RuntimeError('no playable SC result: ' + ' | '.join(errors[-3:]))


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


def add_youtube_candidate(out, seen, video_id=None, url=None, title='', uploader='', duration=0, source=''):
    video_id = video_id or extract_video_id(url or '')
    if not video_id or video_id in seen:
        return
    seen.add(video_id)
    out.append({
        'video_id': video_id,
        'webpage_url': 'https://www.youtube.com/watch?v=' + video_id,
        'title': title or 'YouTube result',
        'uploader': uploader or '',
        'duration': duration or 0,
        'source': source,
    })


def youtube_music_search_candidates(query, limit=8):
    query = clean_query(query)
    out = []
    seen = set()

    for inst in piped_instances()[:max(MAX_PIPED_INSTANCES, 4)]:
        try:
            data = http_get_json(
                inst + '/search?q=' + urllib.parse.quote(query) + '&filter=videos',
                timeout=8,
                allow_insecure_retry=True,
            )
            items = data.get('items') if isinstance(data, dict) else []
            for item in items or []:
                if item.get('type') not in ('stream', 'video'):
                    continue
                add_youtube_candidate(
                    out,
                    seen,
                    url=item.get('url') or item.get('webpageUrl') or '',
                    title=item.get('title') or '',
                    uploader=item.get('uploaderName') or item.get('uploader') or '',
                    duration=item.get('duration') or 0,
                    source='piped-search',
                )
                if len(out) >= limit:
                    return out
        except Exception as e:
            log.debug('piped music search %s: %s', inst, e)

    try:
        opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': 'in_playlist',
            'skip_download': True,
            'source_address': '0.0.0.0',
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info('ytsearch' + str(limit) + ':' + query, download=False)
        for entry in (data.get('entries') or []):
            if not entry:
                continue
            add_youtube_candidate(
                out,
                seen,
                video_id=entry.get('id'),
                url=entry.get('url') or entry.get('webpage_url') or '',
                title=entry.get('title') or '',
                uploader=entry.get('uploader') or entry.get('channel') or '',
                duration=entry.get('duration') or 0,
                source='ytsearch',
            )
            if len(out) >= limit:
                return out
    except Exception as e:
        log.debug('yt-dlp music search: %s', e)

    try:
        for video_id in youtube_html_search(query, n=limit):
            add_youtube_candidate(out, seen, video_id=video_id, source='youtube-html')
            if len(out) >= limit:
                return out
    except Exception as e:
        log.debug('youtube html music search: %s', e)
    return out


def strict_music_queries(track):
    title = normalize_search_text(track.get('title') or '')
    artist = normalize_search_text(track.get('uploader') or '')
    base = (title + ' ' + artist).strip() or (track.get('query') or '')
    queries = [
        base + ' official audio',
        base + ' official',
        base + ' topic',
        base,
    ]
    if title and artist:
        queries.append('"' + title + '" "' + artist + '"')
    seen = set()
    out = []
    for query in queries:
        query = re.sub(r'\s+', ' ', query).strip()
        key = query.casefold()
        if query and key not in seen:
            seen.add(key)
            out.append(query)
    return out


def track_cache_key(track):
    if track.get('source_uri'):
        return 'music:' + str(track.get('source')) + ':' + str(track.get('source_uri'))
    return 'music:' + '|'.join(str(track.get(k) or '') for k in ('source', 'title', 'uploader', 'duration'))


def fetch_youtube_candidate(candidate, original_query):
    errors = []
    url = candidate['webpage_url']
    providers = [
        (fetch_via_piped, 'piped'),
        (fetch_via_invidious, 'invidious'),
        (fetch_via_innertube, 'innertube'),
        (fetch_via_ytdlp_cookies, 'ytdlp+cookies'),
        (fetch_via_ytdlp_direct, 'yt-dlp'),
    ]
    for fn, name in providers:
        try:
            result = fn(url)
            result['query'] = original_query
            return result
        except Exception as e:
            errors.append(name + ': ' + str(e))
    raise RuntimeError('candidate stream failed: ' + ' | '.join(errors[-3:]))


def fetch_strict_music_track(track):
    cache_key = track_cache_key(track)
    cached = _cached_track(cache_key)
    if cached:
        return cached

    original_query = track.get('query') or ((track.get('title') or '') + ' ' + (track.get('uploader') or '')).strip()
    candidates = []
    seen = set()
    errors = []
    for query in strict_music_queries(track):
        try:
            for candidate in youtube_music_search_candidates(query, limit=12):
                video_id = candidate.get('video_id')
                if video_id and video_id not in seen:
                    seen.add(video_id)
                    candidates.append(candidate)
        except Exception as e:
            errors.append(query + ': ' + str(e))

    strict_scored = []
    relaxed_scored = []
    for candidate in candidates:
        ok, score = acceptable_music_match(track, candidate)
        candidate['match_score'] = score
        if ok:
            strict_scored.append(candidate)
        else:
            relaxed_ok, relaxed_score = relaxed_music_match(track, candidate)
            candidate['match_score'] = relaxed_score
            if relaxed_ok:
                relaxed_scored.append(candidate)
    strict_scored.sort(key=lambda c: c.get('match_score', 0), reverse=True)
    relaxed_scored.sort(key=lambda c: c.get('match_score', 0), reverse=True)
    log.info(
        'strict music candidates for %s: %s',
        track.get('title'),
        [(c.get('match_score'), c.get('title'), c.get('uploader')) for c in strict_scored[:4]],
    )

    for candidate in strict_scored[:10]:
        try:
            result = fetch_youtube_candidate(candidate, original_query)
            ok, score = acceptable_music_match(track, result)
            if not ok:
                raise RuntimeError('resolved mismatch score=' + str(score) + ' title=' + str(result.get('title')))
            result['resolved_match_score'] = score
            return _remember_track(cache_key, result)
        except Exception as e:
            errors.append((candidate.get('title') or '?') + ': ' + str(e))
            log.warning('strict candidate failed %s: %s', candidate.get('webpage_url'), e)

    log.info(
        'relaxed music candidates for %s: %s',
        track.get('title'),
        [(c.get('match_score'), c.get('title'), c.get('uploader')) for c in relaxed_scored[:4]],
    )
    for candidate in relaxed_scored[:10]:
        try:
            result = fetch_youtube_candidate(candidate, original_query)
            ok, score = relaxed_music_match(track, result)
            if not ok:
                raise RuntimeError('resolved mismatch score=' + str(score) + ' title=' + str(result.get('title')))
            result['resolved_match_score'] = score
            result['resolved_relaxed_match'] = True
            return _remember_track(cache_key, result)
        except Exception as e:
            errors.append((candidate.get('title') or '?') + ': ' + str(e))
            log.warning('relaxed candidate failed %s: %s', candidate.get('webpage_url'), e)

    for query in strict_music_queries(track)[-2:]:
        try:
            result = fetch_via_soundcloud(query)
            ok, score = relaxed_music_match(track, result)
            if not ok:
                raise RuntimeError('soundcloud mismatch score=' + str(score) + ' title=' + str(result.get('title')))
            result['resolved_match_score'] = score
            result['resolved_soundcloud_fallback'] = True
            return _remember_track(cache_key, result)
        except Exception as e:
            errors.append('soundcloud ' + query + ': ' + str(e))
            log.warning('soundcloud final fallback failed %s: %s', query, e)

    raise RuntimeError('หาเพลงที่ใกล้กับ Spotify ไม่เจอ: ' + ' | '.join(errors[-4:]))


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
            for inst in piped_instances()[:max(MAX_PIPED_INSTANCES, 4)]:
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
    for inst in piped_instances()[:PIPED_STREAM_INSTANCES]:
        try:
            streams = http_get_json(inst + '/streams/' + vid, timeout=10, allow_insecure_retry=True)
            audio = streams.get('audioStreams') or []
            if not audio:
                raise RuntimeError('no audio streams')
            candidates = list(audio)
            candidates.sort(
                key=lambda a: (
                    0 if (a.get('codec') or '').lower() == 'opus' else 1,
                    -(a.get('bitrate', 0) or 0),
                )
            )
            best = None
            last_stream_err = None
            for cand in candidates[:MAX_PIPED_STREAMS]:
                try:
                    preflight_stream(cand['url'], label='piped ' + inst)
                    best = cand
                    break
                except Exception as e:
                    last_stream_err = e
                    log.warning('piped stream failed %s bitrate=%s: %s', inst, cand.get('bitrate'), e)
            if not best:
                raise RuntimeError('all piped streams failed: ' + str(last_stream_err))
            stream_url = best['url']
            codec = (best.get('codec') or '').lower()
            log.info('piped ok via %s codec=%s bitrate=%s', inst, codec, best.get('bitrate'))
            return {
                'url': stream_url,
                'title': streams.get('title', 'Unknown'),
                'duration': streams.get('duration', 0),
                'thumbnail': streams.get('thumbnailUrl'),
                'webpage_url': 'https://youtube.com/watch?v=' + vid,
                'uploader': streams.get('uploader', 'Unknown'),
                'codec': codec,
                'query': query,
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
    base_opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'geo_bypass': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.200 Mobile Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        },
    }
    if importlib.util.find_spec('curl_cffi'):
        base_opts['impersonate'] = os.environ.get('YTDLP_IMPERSONATE', 'chrome')
    if cookiefile:
        base_opts['cookiefile'] = cookiefile
    target = ytdlp_target(query)
    formats = [
        'bestaudio[ext=webm]',
        'bestaudio[ext=m4a]',
        'bestaudio',
        'best',
    ]
    last_err = None
    for fmt in formats:
        opts = dict(base_opts)
        opts['format'] = fmt
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                data = ydl.extract_info(target, download=False)
            if 'entries' in data:
                data = data['entries'][0]
            if not data.get('url'):
                raise RuntimeError('no stream url')
            preflight_stream(data['url'], label=label + ' ' + fmt)
            log.info('%s ok: %s fmt=%s', label, data.get('title'), fmt)
            return {
                'url': data['url'],
                'title': data.get('title', 'Unknown'),
                'duration': data.get('duration', 0),
                'thumbnail': data.get('thumbnail'),
                'webpage_url': data.get('webpage_url', target),
                'uploader': data.get('uploader', 'Unknown'),
                'codec': data.get('acodec'),
                'query': query,
            }
        except Exception as e:
            last_err = e
            log.warning('%s failed fmt=%s: %s', label, fmt, e)
    raise RuntimeError(str(last_err))



def get_youtube_title_oembed(vid):
    """Get YouTube video title via oEmbed — never blocked on any IP."""
    try:
        data = http_get_json(
            'https://www.youtube.com/oembed?url=https://youtube.com/watch?v=' + vid + '&format=json',
            timeout=8,
        )
        return data.get('title', ''), data.get('author_name', '')
    except Exception as e:
        log.debug('oembed %s: %s', vid, e)
        return '', ''


def fetch_via_yt_to_soundcloud(query):
    """
    Final fallback: get YouTube title via oEmbed, then search SoundCloud.
    SoundCloud always works from GitHub Actions IPs.
    """
    q = clean_query(query)
    vid = extract_video_id(q)
    title = ''
    uploader = ''
    if vid:
        title, uploader = get_youtube_title_oembed(vid)
        if title:
            log.info('yt->sc fallback title: "%s"', title)
    errors = []
    for search_q in search_candidates(title or q, uploader=uploader, original=q):
        try:
            log.info('yt->sc trying: "%s"', search_q)
            result = fetch_via_soundcloud(search_q)
            result['query'] = query
            return result
        except Exception as e:
            errors.append(search_q + ': ' + str(e))
    raise RuntimeError('yt->soundcloud failed: ' + ' | '.join(errors[-3:]))


def fetch_via_ytdlp_direct(query):
    return fetch_via_ytdlp(query, label='yt-dlp')


def fetch_via_ytdlp_cookies(query):
    """yt-dlp with cookies - last resort if cookies secret is set."""
    if not os.path.exists(COOKIES_FILE):
        raise RuntimeError('no cookies file, skipping ytdlp')
    return fetch_via_ytdlp(query, cookiefile=COOKIES_FILE, label='ytdlp+cookies')


def fetch_via_innertube(query):
    """Direct YouTube InnerTube API fallback when frontends/proxies fail."""
    import json as _json
    query = clean_query(query)
    vid = extract_video_id(query)
    if not vid:
        try:
            ids = youtube_html_search(query, n=1)
        except Exception:
            ids = []
        if not ids:
            raise RuntimeError('no video ID for innertube')
        vid = ids[0]

    payload = _json.dumps({
        'videoId': vid,
        'context': {
            'client': {
                'clientName': 'TVHTML5_SIMPLY_EMBEDDED_PLAYER',
                'clientVersion': '2.0',
                'hl': 'en',
            }
        }
    }).encode()
    req = urllib.request.Request(
        'https://www.youtube.com/youtubei/v1/player?key=AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8',
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (SMART-TV; Linux; Tizen 6.0) AppleWebKit/538.1 (KHTML, like Gecko) Version/6.0 TV Safari/538.1',
            'X-YouTube-Client-Name': '85',
            'X-YouTube-Client-Version': '2.0',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        data = _json.loads(r.read())

    streaming = data.get('streamingData') or {}
    fmts = streaming.get('adaptiveFormats') or streaming.get('formats') or []
    audio = [f for f in fmts if f.get('mimeType', '').startswith('audio/')]
    if not audio:
        raise RuntimeError('no audio formats from innertube')
    audio.sort(key=lambda f: f.get('bitrate', 0), reverse=True)
    best = None
    best_url = None
    last_err = None
    for cand in audio[:6]:
        stream_url = cand.get('url') or cand.get('signatureCipher')
        if not stream_url or not stream_url.startswith('http'):
            last_err = RuntimeError('stream URL requires signature cipher')
            continue
        try:
            preflight_stream(stream_url, label='innertube')
            best = cand
            best_url = stream_url
            break
        except Exception as e:
            last_err = e
            log.warning('innertube stream failed bitrate=%s: %s', cand.get('bitrate'), e)
    if not best:
        raise RuntimeError('innertube streams failed: ' + str(last_err))
    title = (data.get('videoDetails') or {}).get('title', 'Unknown')
    duration = int((data.get('videoDetails') or {}).get('lengthSeconds', 0))
    uploader = (data.get('videoDetails') or {}).get('author', 'Unknown')
    thumbnail = 'https://img.youtube.com/vi/' + vid + '/hqdefault.jpg'
    log.info('innertube ok: %s', title)
    return {
        'url': best_url,
        'title': title,
        'duration': duration,
        'thumbnail': thumbnail,
        'webpage_url': 'https://youtube.com/watch?v=' + vid,
        'uploader': uploader,
        'codec': 'opus' if 'opus' in best.get('mimeType', '') else '',
        'query': query,
    }


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
    if _is_video_id(q):
        return 'youtube'
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
        cached = _cached_track(q)
        if cached:
            return cached
        url_type = _detect_url_type(q)
        errors = []

        if url_type == 'soundcloud':
            providers = [
                (fetch_via_soundcloud, 'soundcloud'),
                (fetch_via_ytdlp_direct, 'yt-dlp'),
                (fetch_via_ytdlp_cookies, 'ytdlp+cookies'),
            ]
            return _remember_track(q, run_provider_race(q, providers, timeout=12))

        if url_type == 'spotify':
            try:
                search_q, sp_title, sp_thumb = fetch_spotify_info(q)
                log.info('spotify → %s', search_q)
                sp_artist = search_q.replace(sp_title, '', 1).strip()
                sp_track = {
                    'title': sp_title,
                    'uploader': sp_artist,
                    'thumbnail': sp_thumb,
                    'webpage_url': q,
                    'source': 'spotify',
                    'query': search_q,
                }
                try:
                    result = fetch_strict_music_track(sp_track)
                except Exception as e:
                    errors.append(str(e))
                    result = run_provider_race(search_q, [
                        (fetch_via_pytubefix, 'pytubefix'),
                        (fetch_via_invidious, 'invidious'),
                    ], timeout=12)
                if not result.get('thumbnail') and sp_thumb:
                    result['thumbnail'] = sp_thumb
                result['webpage_url'] = q
                result['query'] = query
                return _remember_track(q, result)
            except Exception as e:
                errors.append('spotify_scrape: ' + str(e))
            raise RuntimeError(' | '.join(errors))

        if url_type == 'youtube':
            providers = [
                (fetch_via_ytdlp_direct, 'yt-dlp'),
                (fetch_via_piped, 'piped'),
                (fetch_via_innertube, 'innertube'),
                (fetch_via_ytdlp_cookies, 'ytdlp+cookies'),
            ]
            try:
                return _remember_track(q, run_provider_race(q, providers))
            except Exception as e:
                errors.append(str(e))
            try:
                return _remember_track(q, run_provider_race(q, [
                    (fetch_via_pytubefix, 'pytubefix'),
                    (fetch_via_invidious, 'invidious'),
                ], timeout=12))
            except Exception as e:
                errors.append(str(e))
            try:
                return _remember_track(q, fetch_via_yt_to_soundcloud(q))
            except Exception as e:
                errors.append('yt->soundcloud: ' + str(e))
            raise RuntimeError(' | '.join(errors))

        if url_type == 'other_url':
            providers = [
                (fetch_via_ytdlp_direct, 'yt-dlp'),
                (fetch_via_ytdlp_cookies, 'ytdlp+cookies'),
            ]
            return _remember_track(q, run_provider_race(q, providers, timeout=12))

        providers = [
            (fetch_via_ytdlp_direct, 'yt-dlp'),
            (fetch_via_soundcloud, 'soundcloud'),
            (fetch_via_piped, 'piped'),
            (fetch_via_innertube, 'innertube'),
            (fetch_via_ytdlp_cookies, 'ytdlp+cookies'),
        ]
        try:
            return _remember_track(q, run_provider_race(q, providers))
        except Exception as e:
            errors.append(str(e))
        return _remember_track(q, run_provider_race(q, [
            (fetch_via_pytubefix, 'pytubefix'),
            (fetch_via_invidious, 'invidious'),
        ], timeout=12))

    return await loop.run_in_executor(None, _run)


intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

queues = {}
now_playing = {}
loop_mode = {}
skip_auto_next = set()

LOOP_LABELS = {'off': 'ปิด', 'one': '🔂 1 เพลง', 'all': '🔁 ทั้งคิว'}


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
    if track.get('album'):
        embed.add_field(name='💿 Album', value=_clip(track.get('album'), 80), inline=True)
    elif track.get('release_date'):
        embed.add_field(name='📅 Release', value=_fmt_source_date(track.get('release_date')), inline=True)
    if guild_id is not None:
        embed.add_field(name='🔁 Loop', value=LOOP_LABELS[get_loop(guild_id)], inline=True)
    if track.get('thumbnail'):
        embed.set_thumbnail(url=track['thumbnail'])
    return embed


class SpotifyImportModal(discord.ui.Modal, title='Spotify playlist sync'):
    playlist_name = discord.ui.TextInput(
        label='ชื่อ playlist ในบอท',
        placeholder='เว้นว่างไว้เพื่อใช้ชื่อจาก Spotify',
        required=False,
        max_length=40,
    )
    spotify_url = discord.ui.TextInput(
        label='Spotify playlist/album link',
        placeholder='https://open.spotify.com/playlist/... หรือ /album/...',
        required=True,
        max_length=500,
    )

    def __init__(self):
        super().__init__(timeout=180)

    async def on_submit(self, i: discord.Interaction):
        url = str(self.spotify_url.value).strip()
        name = str(self.playlist_name.value).strip()
        if not ('spotify.com/playlist' in url or 'spotify.com/album' in url):
            await i.response.send_message('❌ ใส่ลิงก์ Spotify playlist/album เท่านั้น', ephemeral=True)
            return

        await i.response.defer(ephemeral=True, thinking=True)
        loop = asyncio.get_event_loop()
        try:
            src_name, tracks = await loop.run_in_executor(
                None, lambda: pl.import_spotify_playlist(url)
            )
            display_name = (name or src_name or 'Spotify').strip()[:40]
            saved = pl.set_tracks(i.user.id, display_name.lower(), display_name, tracks)
        except Exception as e:
            await i.followup.send('❌ sync Spotify ไม่สำเร็จ: ' + str(e)[:300], ephemeral=True)
            return

        await i.followup.send(
            '✅ sync **' + saved['name'] + '** จาก Spotify แล้ว — '
            + str(len(saved['tracks'])) + ' เพลง\n'
            + 'เล่นได้ด้วย `!pl play ' + saved['name'] + '`',
            ephemeral=True,
        )


class PlayerView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=None)
        self.ctx = ctx
        self._refresh_loop_button()

    def _refresh_loop_button(self):
        mode = get_loop(self.ctx.guild.id)
        for child in self.children:
            if getattr(child, 'custom_id', None) == 'loop':
                child.label = {'off': 'Loop: Off', 'one': 'Loop: 1 เพลง', 'all': 'Loop: ทั้งคิว'}[mode]
                child.emoji = '🔂' if mode == 'one' else '🔁'
                child.style = discord.ButtonStyle.secondary if mode == 'off' else discord.ButtonStyle.success

    async def _ack(self, i):
        try:
            await i.response.defer()
        except Exception:
            pass

    @discord.ui.button(emoji='⏯️', label='Pause/Resume', style=discord.ButtonStyle.primary, custom_id='pp')
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

    @discord.ui.button(emoji='⏭️', label='Skip', style=discord.ButtonStyle.primary, custom_id='skip')
    async def skip_btn(self, i: discord.Interaction, b):
        await self._ack(i)
        vc = self.ctx.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop(); await i.followup.send('⏭️ ข้ามเพลง', ephemeral=True)
        else:
            await i.followup.send('❌ ไม่มีเพลงเล่นอยู่', ephemeral=True)

    @discord.ui.button(emoji='🔁', label='Loop: Off', style=discord.ButtonStyle.secondary, custom_id='loop')
    async def loop_btn(self, i: discord.Interaction, b):
        await self._ack(i)
        mode = cycle_loop(self.ctx.guild.id)
        self._refresh_loop_button()
        try:
            await i.message.edit(view=self)
        except Exception:
            pass
        await i.followup.send('🔁 Loop: **' + LOOP_LABELS[mode] + '**', ephemeral=True)

    @discord.ui.button(emoji='⏹️', label='Stop', style=discord.ButtonStyle.danger, custom_id='stop')
    async def stop_btn(self, i: discord.Interaction, b):
        await self._ack(i)
        vc = self.ctx.voice_client
        if vc:
            queues[self.ctx.guild.id] = []
            now_playing.pop(self.ctx.guild.id, None)
            set_loop(self.ctx.guild.id, 'off')
            vc.stop()
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            await i.followup.send('⏹️ หยุดและออกจาก voice', ephemeral=True)
        else:
            await i.followup.send('❌ บอทไม่ได้อยู่ใน voice', ephemeral=True)

    @discord.ui.button(emoji='🟢', label='Spotify Sync', style=discord.ButtonStyle.success, custom_id='spotify_sync', row=1)
    async def spotify_sync_btn(self, i: discord.Interaction, b):
        await i.response.send_modal(SpotifyImportModal())

    @discord.ui.button(emoji='📀', label='Playlists', style=discord.ButtonStyle.secondary, custom_id='playlist_ui', row=1)
    async def playlists_btn(self, i: discord.Interaction, b):
        view = PlaylistBrowserView(self.ctx)
        await i.response.send_message(embed=view.make_embed(), view=view, ephemeral=True)


async def ensure_voice(ctx):
    if not ctx.author.voice:
        raise RuntimeError('join a voice channel first')
    return await ensure_voice_channel(ctx, ctx.author.voice.channel)


async def ensure_voice_for_member(ctx, member):
    if not getattr(member, 'voice', None):
        raise RuntimeError('join a voice channel first')
    return await ensure_voice_channel(ctx, member.voice.channel)


async def ensure_voice_channel(ctx, target):
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
    source = discord.FFmpegPCMAudio(
        track['url'],
        executable=FFMPEG_EXECUTABLE,
        before_options=FFMPEG_BEFORE,
        options=FFMPEG_PCM_OPTIONS,
    )

    def after_play(err):
        guild_id = ctx.guild.id
        if guild_id in skip_auto_next:
            skip_auto_next.discard(guild_id)
            return
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
            now_playing.pop(guild_id, None)
            return
        next_track = queue.pop(0)

    now_playing[guild_id] = next_track

    # Fetch stream URL if missing (playlist tracks come in unresolved) OR
    # if the resolved URL is stale (playlist loop URLs can expire).
    resolved_at = float(next_track.get('resolved_at') or 0)
    url_is_stale = bool(resolved_at and time.time() - resolved_at > STREAM_URL_TTL)
    needs_refetch = (
        not next_track.get('url')
        or (mode == 'one')
        or (mode == 'all' and next_track is current)
        or url_is_stale
    )
    if needs_refetch and track_play_query(next_track):
        try:
            await resolve_track_audio(next_track)
        except Exception as e:
            log.warning('re-fetch failed for %r: %s', next_track.get('title'), e)
            try:
                await ctx.send('⚠️ ข้าม **' + (next_track.get('title') or '?') + '** (resolve ไม่ได้: ' + str(e)[:120] + ')')
            except Exception:
                pass
            now_playing.pop(guild_id, None)
            return await play_next(ctx)

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
    try:
        pl.load()
    except Exception as e:
        log.warning('playlist load failed: %s', e)
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name='!play · !pl help'))


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
    embed.add_field(name='🔁 Loop', value=LOOP_LABELS[get_loop(ctx.guild.id)], inline=False)
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


@bot.command(name='leave', aliases=['dc', 'disconnect'])
async def leave(ctx):
    if ctx.voice_client:
        queues[ctx.guild.id] = []
        now_playing.pop(ctx.guild.id, None)
        set_loop(ctx.guild.id, 'off')
        await ctx.voice_client.disconnect(force=True)
        await ctx.send('👋 ออกจาก voice แล้ว')
    else:
        await ctx.send('❌ บอทไม่ได้อยู่ใน voice')


@bot.command(name='stop')
async def stop(ctx):
    if ctx.voice_client:
        queues[ctx.guild.id] = []
        now_playing.pop(ctx.guild.id, None)
        set_loop(ctx.guild.id, 'off')
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect(force=True)
        await ctx.send('⏹️ หยุดและออกจาก voice แล้ว')
    else:
        await ctx.send('❌ บอทไม่ได้อยู่ใน voice')


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


SOURCE_EMOJI = {'youtube': '▶️', 'soundcloud': '🔶', 'spotify': '🟢',
                'apple': '🍎', 'manual': '🎵'}
PLAYLIST_PAGE_SIZE = 15


def _fmt_track_line(idx, t):
    src = SOURCE_EMOJI.get(t.get('source', 'manual'), '🎵')
    title = (t.get('title') or 'Unknown')[:60]
    uploader = t.get('uploader') or ''
    extra = ' — ' + uploader[:30] if uploader else ''
    dur = t.get('duration') or 0
    dur_s = ' (' + fmt_duration(dur) + ')' if dur else ''
    return '`' + str(idx).rjust(2) + '` ' + src + ' ' + title + extra + dur_s


def _clip(value, limit):
    value = str(value or '').strip()
    return value if len(value) <= limit else value[:max(0, limit - 1)] + '…'


def _fmt_source_date(value):
    value = str(value or '').strip()
    if not value:
        return ''
    return value[:10]


def merge_resolved_audio(track, fresh):
    track['url'] = fresh['url']
    track['codec'] = fresh.get('codec')
    if not track.get('duration') and fresh.get('duration'):
        track['duration'] = fresh['duration']
    if not track.get('thumbnail') and fresh.get('thumbnail'):
        track['thumbnail'] = fresh['thumbnail']
    track['resolved_title'] = fresh.get('title')
    track['resolved_webpage_url'] = fresh.get('webpage_url')
    track['resolved_at'] = time.time()
    if fresh.get('resolved_match_score') is not None:
        track['resolved_match_score'] = fresh.get('resolved_match_score')
    return track


def track_play_query(track):
    return (
        track.get('query')
        or track.get('webpage_url')
        or ((track.get('title') or '') + ' ' + (track.get('uploader') or '')).strip()
        or track.get('title')
    )


async def resolve_track_audio(track):
    if track.get('source') in ('spotify', 'apple'):
        loop = asyncio.get_event_loop()
        fresh = await loop.run_in_executor(None, lambda: fetch_strict_music_track(track))
    else:
        fresh = await fetch_track(track_play_query(track))
    return merge_resolved_audio(track, fresh)


def _playlist_items(user_id):
    return sorted(
        pl.get_all(user_id).items(),
        key=lambda item: (item[1].get('name') or item[0]).casefold(),
    )


def _track_line(idx, t, selected=False):
    marker = '▶' if selected else str(t.get('source_position') or idx).rjust(2)
    src = SOURCE_EMOJI.get(t.get('source', 'manual'), '🎵')
    title = _clip(t.get('title') or 'Unknown', 56)
    uploader = _clip(t.get('uploader') or 'Unknown', 34)
    dur = fmt_duration(t.get('duration') or 0) if t.get('duration') else '--:--'
    album = _clip(t.get('album') or '', 30)
    date = _fmt_source_date(t.get('added_at') or t.get('release_date'))
    meta = [uploader]
    if album:
        meta.append(album)
    if date:
        meta.append(date)
    meta.append('`' + dur + '`')
    return '`' + marker + '` ' + src + ' **' + title + '**\n    ' + ' • '.join(meta)


def _playlist_browser_embed(ctx):
    items = _playlist_items(ctx.author.id)
    embed = discord.Embed(
        title='📀 Playlists ของ ' + ctx.author.display_name,
        color=0x5865F2,
    )
    if not items:
        embed.description = 'ยังไม่มี playlist กด **Spotify Sync** หรือใช้ `!pl import <ชื่อ> <link>` เพื่อเพิ่มเพลง'
        return embed
    lines = []
    for idx, (_, p) in enumerate(items[:20], 1):
        lines.append('`' + str(idx).rjust(2) + '` **' + _clip(p.get('name'), 44) + '**  •  ' + str(len(p.get('tracks') or [])) + ' เพลง')
    if len(items) > 20:
        lines.append('… อีก ' + str(len(items) - 20) + ' playlist')
    embed.description = '\n'.join(lines)
    embed.set_footer(text='เลือก playlist จากเมนูด้านล่าง แล้วเลือกเพลงกดเล่นได้เลย')
    return embed


def _playlist_tracks_embed(ctx, playlist_name, page=0):
    p = pl.get(ctx.author.id, playlist_name)
    if not p:
        return discord.Embed(title='❌ ไม่พบ playlist', description=playlist_name, color=0xED4245)
    tracks = p.get('tracks') or []
    total_pages = max(1, (len(tracks) + PLAYLIST_PAGE_SIZE - 1) // PLAYLIST_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PLAYLIST_PAGE_SIZE
    page_tracks = tracks[start:start + PLAYLIST_PAGE_SIZE]
    embed = discord.Embed(
        title='📀 ' + p.get('name', playlist_name),
        color=0x5865F2,
    )
    if not tracks:
        embed.description = 'playlist นี้ยังว่าง กด **Spotify Sync** หรือใช้ `!pl import ' + p.get('name', playlist_name) + ' <link>`'
    else:
        embed.description = '\n'.join(_track_line(start + i + 1, t) for i, t in enumerate(page_tracks))
        for t in page_tracks:
            if t.get('thumbnail'):
                embed.set_thumbnail(url=t['thumbnail'])
                break
    embed.set_footer(text='หน้า ' + str(page + 1) + '/' + str(total_pages) + ' • เลือกเพลง = loop playlist จากเพลงนั้น')
    return embed


async def play_playlist_track_now(ctx, member, playlist_name, index):
    p = pl.get(member.id, playlist_name)
    if not p:
        raise RuntimeError('ไม่พบ playlist')
    tracks = p.get('tracks') or []
    if not (0 <= index < len(tracks)):
        raise RuntimeError('ไม่พบเพลงลำดับนี้')
    vc = await ensure_voice_for_member(ctx, member)
    entries = [pl.track_to_queue_entry(t) for t in tracks]
    track = entries[index]
    guild_id = ctx.guild.id
    queue = get_queue(guild_id)
    queue.clear()
    queue.extend(entries[index + 1:] + entries[:index])
    set_loop(guild_id, 'all')
    if vc.is_playing() or vc.is_paused():
        skip_auto_next.add(guild_id)
        vc.stop()
        await asyncio.sleep(0.25)
    await resolve_track_audio(track)
    now_playing[guild_id] = track
    ok = await _start_playback(ctx, track)
    if not ok:
        raise RuntimeError('เริ่มเล่นไม่ได้')
    return track


async def queue_playlist_from_ui(ctx, member, playlist_name, loop_playlist=False):
    p = pl.get(member.id, playlist_name)
    if not p:
        raise RuntimeError('ไม่พบ playlist')
    tracks = p.get('tracks') or []
    if not tracks:
        raise RuntimeError('playlist ว่าง')
    vc = await ensure_voice_for_member(ctx, member)
    queue = get_queue(ctx.guild.id)
    entries = [pl.track_to_queue_entry(t) for t in tracks]
    if loop_playlist:
        set_loop(ctx.guild.id, 'all')
        queue.clear()
        if vc.is_playing() or vc.is_paused():
            skip_auto_next.add(ctx.guild.id)
            vc.stop()
            await asyncio.sleep(0.25)

    if vc.is_playing() or vc.is_paused():
        queue.extend(entries)
        return None, len(entries), p.get('name', playlist_name)

    first = entries[0]
    await resolve_track_audio(first)
    now_playing[ctx.guild.id] = first
    ok = await _start_playback(ctx, first)
    if not ok:
        raise RuntimeError('เริ่มเล่นไม่ได้')
    queue.extend(entries[1:])
    return first, len(entries), p.get('name', playlist_name)


class PlaylistPickerSelect(discord.ui.Select):
    def __init__(self, owner):
        self.owner = owner
        options = []
        for key, p in _playlist_items(owner.ctx.author.id)[:25]:
            options.append(discord.SelectOption(
                label=_clip(p.get('name') or key, 100),
                description=str(len(p.get('tracks') or [])) + ' เพลง',
                value=key[:100],
                emoji='📀',
            ))
        super().__init__(placeholder='เลือก playlist', min_values=1, max_values=1, options=options, row=0)

    async def callback(self, i: discord.Interaction):
        name = self.values[0]
        view = PlaylistTracksView(self.owner.ctx, name, page=0)
        await i.response.edit_message(embed=view.make_embed(), view=view)


class PlaylistTrackSelect(discord.ui.Select):
    def __init__(self, owner):
        self.owner = owner
        p = pl.get(owner.ctx.author.id, owner.playlist_name)
        tracks = (p or {}).get('tracks') or []
        start = owner.page * PLAYLIST_PAGE_SIZE
        options = []
        for abs_idx, t in enumerate(tracks[start:start + PLAYLIST_PAGE_SIZE], start):
            title = _clip(str(abs_idx + 1) + '. ' + (t.get('title') or 'Unknown'), 100)
            bits = [t.get('uploader') or 'Unknown']
            if t.get('album'):
                bits.append(t.get('album'))
            date = _fmt_source_date(t.get('added_at') or t.get('release_date'))
            if date:
                bits.append(date)
            bits.append(fmt_duration(t.get('duration') or 0) if t.get('duration') else '--:--')
            desc = _clip(' • '.join(bits), 100)
            options.append(discord.SelectOption(
                label=title,
                description=desc,
                value=str(abs_idx),
                emoji=SOURCE_EMOJI.get(t.get('source', 'manual'), '🎵'),
            ))
        super().__init__(placeholder='เลือกเพลงเพื่อเล่นทันที', min_values=1, max_values=1, options=options, row=0)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True, thinking=True)
        idx = int(self.values[0])
        try:
            track = await play_playlist_track_now(self.owner.ctx, i.user, self.owner.playlist_name, idx)
        except Exception as e:
            await i.followup.send('❌ เล่นเพลงไม่ได้: ' + str(e)[:250], ephemeral=True)
            return
        await self.owner.ctx.send(embed=make_np_embed(track, self.owner.ctx.guild.id), view=PlayerView(self.owner.ctx))
        await i.followup.send('🔁 เริ่ม loop playlist จาก **' + _clip(track.get('title'), 80) + '**', ephemeral=True)


class PlaylistBrowserView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=300)
        self.ctx = ctx
        if _playlist_items(ctx.author.id):
            self.add_item(PlaylistPickerSelect(self))

    async def interaction_check(self, i: discord.Interaction):
        if i.user.id != self.ctx.author.id:
            await i.response.send_message('เมนูนี้เป็นของ ' + self.ctx.author.mention, ephemeral=True)
            return False
        return True

    def make_embed(self):
        return _playlist_browser_embed(self.ctx)

    @discord.ui.button(emoji='🔄', label='Refresh', style=discord.ButtonStyle.secondary, row=1)
    async def refresh_btn(self, i: discord.Interaction, b):
        view = PlaylistBrowserView(self.ctx)
        await i.response.edit_message(embed=view.make_embed(), view=view)

    @discord.ui.button(emoji='🟢', label='Spotify Sync', style=discord.ButtonStyle.success, row=1)
    async def spotify_btn(self, i: discord.Interaction, b):
        await i.response.send_modal(SpotifyImportModal())


class PlaylistTracksView(discord.ui.View):
    def __init__(self, ctx, playlist_name, page=0):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.playlist_name = playlist_name
        p = pl.get(ctx.author.id, playlist_name)
        tracks = (p or {}).get('tracks') or []
        total_pages = max(1, (len(tracks) + PLAYLIST_PAGE_SIZE - 1) // PLAYLIST_PAGE_SIZE)
        self.page = max(0, min(page, total_pages - 1))
        if tracks:
            self.add_item(PlaylistTrackSelect(self))

    async def interaction_check(self, i: discord.Interaction):
        if i.user.id != self.ctx.author.id:
            await i.response.send_message('เมนูนี้เป็นของ ' + self.ctx.author.mention, ephemeral=True)
            return False
        return True

    def make_embed(self):
        return _playlist_tracks_embed(self.ctx, self.playlist_name, self.page)

    @discord.ui.button(emoji='📀', label='Playlists', style=discord.ButtonStyle.secondary, row=1)
    async def back_btn(self, i: discord.Interaction, b):
        view = PlaylistBrowserView(self.ctx)
        await i.response.edit_message(embed=view.make_embed(), view=view)

    @discord.ui.button(emoji='◀️', label='Prev', style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, i: discord.Interaction, b):
        view = PlaylistTracksView(self.ctx, self.playlist_name, self.page - 1)
        await i.response.edit_message(embed=view.make_embed(), view=view)

    @discord.ui.button(emoji='▶️', label='Next', style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, i: discord.Interaction, b):
        view = PlaylistTracksView(self.ctx, self.playlist_name, self.page + 1)
        await i.response.edit_message(embed=view.make_embed(), view=view)

    @discord.ui.button(emoji='▶️', label='Play All', style=discord.ButtonStyle.success, row=2)
    async def play_all_btn(self, i: discord.Interaction, b):
        await i.response.defer(ephemeral=True, thinking=True)
        try:
            first, count, name = await queue_playlist_from_ui(
                self.ctx, i.user, self.playlist_name, loop_playlist=True
            )
        except Exception as e:
            await i.followup.send('❌ เล่น playlist ไม่ได้: ' + str(e)[:250], ephemeral=True)
            return
        if first:
            await self.ctx.send(embed=make_np_embed(first, self.ctx.guild.id), view=PlayerView(self.ctx))
            await i.followup.send('🔁 เริ่ม loop **' + name + '** ทั้งหมด ' + str(count) + ' เพลง', ephemeral=True)
        else:
            await i.followup.send('🔁 Loop **' + name + '** แล้ว — ' + str(count) + ' เพลง', ephemeral=True)

    @discord.ui.button(emoji='🔁', label='Loop Playlist', style=discord.ButtonStyle.primary, row=2)
    async def loop_playlist_btn(self, i: discord.Interaction, b):
        await i.response.defer(ephemeral=True, thinking=True)
        try:
            first, count, name = await queue_playlist_from_ui(
                self.ctx, i.user, self.playlist_name, loop_playlist=True
            )
        except Exception as e:
            await i.followup.send('❌ loop playlist ไม่ได้: ' + str(e)[:250], ephemeral=True)
            return
        if first:
            await self.ctx.send(embed=make_np_embed(first, self.ctx.guild.id), view=PlayerView(self.ctx))
        await i.followup.send('🔁 Loop **' + name + '** แล้ว — ' + str(count) + ' เพลง', ephemeral=True)

    @discord.ui.button(emoji='⏹️', label='Loop Off', style=discord.ButtonStyle.secondary, row=3)
    async def loop_off_btn(self, i: discord.Interaction, b):
        set_loop(self.ctx.guild.id, 'off')
        await i.response.send_message('🔁 ปิด loop แล้ว', ephemeral=True)

    @discord.ui.button(emoji='🟢', label='Spotify Sync', style=discord.ButtonStyle.success, row=2)
    async def spotify_btn(self, i: discord.Interaction, b):
        await i.response.send_modal(SpotifyImportModal())


@bot.group(name='pl', aliases=['playlist'], invoke_without_command=True)
async def pl_group(ctx, *, name: str = None):
    """!pl                → ดู playlist ของตัวเอง
    !pl <ชื่อ>         → ดูเพลงใน playlist
    !pl help            → คำสั่งทั้งหมด"""
    if name and name.strip().lower() == 'help':
        return await pl_help(ctx)
    if name:
        return await pl_show(ctx, name=name)
    view = PlaylistBrowserView(ctx)
    await ctx.send(embed=view.make_embed(), view=view)


@pl_group.command(name='help')
async def pl_help(ctx):
    embed = discord.Embed(title='📀 คำสั่ง Playlist (ของแต่ละคน)', color=0x5865F2)
    embed.add_field(
        name='สร้าง / ลบ',
        value='`!pl create <ชื่อ>` — สร้าง playlist ใหม่\n'
              '`!pl delete <ชื่อ>` — ลบ playlist',
        inline=False,
    )
    embed.add_field(
        name='เพิ่ม / นำเข้าเพลง',
        value='`!pl import <ชื่อ> <link>` — import จาก Spotify, YouTube, SoundCloud, **Apple Music** (auto-detect)\n'
              '`!pl add <ชื่อ>` — เพิ่มเพลงที่กำลังเล่นอยู่ลงใน playlist\n'
              '`!pl remove <ชื่อ> <ลำดับ>` — ลบเพลงตามเลขลำดับ',
        inline=False,
    )
    embed.add_field(
        name='เล่น / ดู',
        value='`!pl` — เปิด UI เลือก playlist\n'
              '`!pl <ชื่อ>` — เปิด UI เลือกเพลงใน playlist\n'
              '`!pl play <ชื่อ>` — โหลดทั้ง playlist เข้าคิว\n'
              '`!pl loop <ชื่อ>` — เล่น playlist แล้ววนทั้งชุด',
        inline=False,
    )
    embed.add_field(
        name='ส่วนตัวของแต่ละคน',
        value='Playlist แยกตาม Discord account — คนอื่นเปิดของเขา ของคุณก็ของคุณ ไม่ทับกัน',
        inline=False,
    )
    embed.set_footer(text='รองรับ: 🟢 Spotify · ▶️ YouTube · 🔶 SoundCloud · 🍎 Apple Music')
    await ctx.send(embed=embed)


@pl_group.command(name='create', aliases=['new', 'add_pl'])
async def pl_create(ctx, *, name: str):
    ok, res = pl.create(ctx.author.id, name)
    if ok:
        await ctx.send('✅ สร้าง playlist **' + res['name'] + '** แล้ว — เพิ่มเพลงด้วย `!pl import` หรือ `!pl add`')
    else:
        await ctx.send('❌ ' + res)


@pl_group.command(name='delete', aliases=['del', 'rm_pl'])
async def pl_delete(ctx, *, name: str):
    ok, err = pl.delete(ctx.author.id, name)
    if ok:
        await ctx.send('🗑️ ลบ playlist **' + name + '** แล้ว')
    else:
        await ctx.send('❌ ' + err)


@pl_group.command(name='show', aliases=['view'])
async def pl_show(ctx, *, name: str):
    p = pl.get(ctx.author.id, name)
    if not p:
        await ctx.send('❌ ไม่พบ playlist **' + name + '**')
        return
    view = PlaylistTracksView(ctx, name, page=0)
    await ctx.send(embed=view.make_embed(), view=view)


@pl_group.command(name='import', aliases=['imp', 'sync'])
async def pl_import(ctx, name: str, *, url: str):
    if not pl.detect_import_type(url):
        await ctx.send(
            '❌ ไม่รู้จัก link นี้\nรองรับ:\n'
            '• 🟢 Spotify playlist/album: `https://open.spotify.com/playlist/...`\n'
            '• ▶️ YouTube playlist: `https://www.youtube.com/playlist?list=...`\n'
            '• 🔶 SoundCloud set: `https://soundcloud.com/<user>/sets/...`\n'
            '• 🍎 Apple Music playlist/album: `https://music.apple.com/.../playlist/...`'
        )
        return

    msg = await ctx.send('⏳ กำลัง import playlist เข้า **' + name + '** …')
    loop = asyncio.get_event_loop()
    try:
        kind, src_name, tracks = await loop.run_in_executor(
            None, lambda: pl.import_any(url, piped_instances)
        )
    except Exception as e:
        await msg.edit(content='❌ import ล้มเหลว: ' + str(e)[:300])
        return

    try:
        saved = pl.set_tracks(ctx.author.id, name.lower(), name, tracks)
    except Exception as e:
        await msg.edit(content='❌ บันทึกไม่ได้: ' + str(e))
        return

    await msg.edit(
        content='✅ import จาก **' + kind + '** ("' + (src_name or '?') + '") '
        'เข้า playlist **' + saved['name'] + '** เรียบร้อย — ' + str(len(saved['tracks'])) + ' เพลง\n'
        'เล่นเลย: `!pl play ' + name + '`'
    )


@pl_group.command(name='add')
async def pl_add(ctx, *, name: str):
    """Add the currently-playing track to a playlist."""
    cur = now_playing.get(ctx.guild.id)
    if not cur:
        await ctx.send('❌ ไม่มีเพลงเล่นอยู่ตอนนี้')
        return
    if not pl.get(ctx.author.id, name):
        ok, _ = pl.create(ctx.author.id, name)
        if not ok:
            pass  # might already exist or limit reached, continue trying add
    ok, res = pl.add_track(ctx.author.id, name, cur)
    if ok:
        await ctx.send('✅ เพิ่ม **' + (cur.get('title') or 'เพลง') + '** เข้า **' + name + '** แล้ว')
    else:
        await ctx.send('❌ ' + res)


@pl_group.command(name='remove', aliases=['rm'])
async def pl_remove(ctx, name: str, index: int):
    ok, res = pl.remove_track(ctx.author.id, name, index)
    if ok:
        await ctx.send('🗑️ ลบ **' + (res.get('title') or '?') + '** ออกจาก **' + name + '** แล้ว')
    else:
        await ctx.send('❌ ' + res)


@pl_group.command(name='loop', aliases=['repeat'])
async def pl_loop(ctx, *, name: str):
    if not ctx.author.voice:
        await ctx.send('❌ เข้า voice channel ก่อนนะ!')
        return
    try:
        first, count, display_name = await queue_playlist_from_ui(ctx, ctx.author, name, loop_playlist=True)
    except Exception as e:
        await ctx.send('❌ loop playlist ไม่ได้: ' + str(e)[:250])
        return
    if first:
        await ctx.send(embed=make_np_embed(first, ctx.guild.id), view=PlayerView(ctx))
    await ctx.send('🔁 Loop **' + display_name + '** แล้ว — ' + str(count) + ' เพลง')


@pl_group.command(name='play')
async def pl_play(ctx, *, name: str):
    if not ctx.author.voice:
        await ctx.send('❌ เข้า voice channel ก่อนนะ!')
        return
    p = pl.get(ctx.author.id, name)
    if not p:
        await ctx.send('❌ ไม่พบ playlist **' + name + '**')
        return
    if not p['tracks']:
        await ctx.send('❌ playlist ว่างเปล่า')
        return

    try:
        await ensure_voice(ctx)
    except Exception as e:
        await ctx.send('❌ เข้า voice ไม่ได้: ' + str(e))
        return

    msg = await ctx.send(
        '📥 โหลด **' + p['name'] + '** (' + str(len(p['tracks'])) + ' เพลง) เข้าคิว — '
        'จะ resolve เพลงแรกแล้วเริ่มเล่น เพลงถัดไปจะ resolve อัตโนมัติเมื่อใกล้ถึงคิว'
    )

    queue = get_queue(ctx.guild.id)
    enqueued = 0

    # Fetch first track right away so playback starts immediately
    first_track = pl.track_to_queue_entry(p['tracks'][0])
    try:
        await resolve_track_audio(first_track)
    except Exception as e:
        log.warning('first-track resolve failed: %s', e)
        await msg.edit(content='⚠️ เพลงแรกเล่นไม่ได้ (' + str(e)[:120] + ') — ข้ามไปเพลงถัดไป')

    if first_track.get('url'):
        if not ctx.voice_client or not ctx.voice_client.is_playing():
            now_playing[ctx.guild.id] = first_track
            await _start_playback(ctx, first_track)
            await ctx.send(embed=make_np_embed(first_track, ctx.guild.id), view=PlayerView(ctx))
        else:
            queue.append(first_track)
        enqueued += 1

    # Lazy: rest go in queue with url=None; play_next will fetch via the
    # track.query when each one comes up (mode-one/all branch already does this
    # for repeats; we need to also fetch on first play of unresolved tracks).
    for t in p['tracks'][1:]:
        queue.append(pl.track_to_queue_entry(t))
        enqueued += 1

    await msg.edit(
        content='✅ เพิ่ม **' + p['name'] + '** เข้าคิว ' + str(enqueued) + ' เพลง '
        '(เพลงที่ยังไม่ resolve จะถูก fetch ตอนถึงคิว)'
    )


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
    embed.add_field(name='!pause / !resume', value='หยุด / เล่นต่อ', inline=True)
    embed.add_field(name='!stop', value='หยุด + ออก voice', inline=True)
    embed.add_field(name='!clear', value='ล้างคิว', inline=True)
    embed.add_field(name='!leave (!dc)', value='ออก voice', inline=True)
    embed.add_field(name='!reconnect (!rc)', value='เชื่อมใหม่', inline=True)
    embed.add_field(
        name='📀 Playlist ส่วนตัว — `!pl help`',
        value='สร้าง/import playlist ของตัวเอง รองรับ Spotify, YouTube, SoundCloud, **Apple Music**\n'
              'แต่ละคนมี playlist ของตัวเอง ไม่ทับกัน',
        inline=False,
    )
    embed.set_footer(text='ปุ่ม Now Playing: ⏯️ Pause/Resume  ⏭️ Skip  🔁 Loop  ⏹️ Stop')
    await ctx.send(embed=embed)


TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
if not TOKEN:
    raise RuntimeError('DISCORD_BOT_TOKEN is not set!')
bot.run(TOKEN, log_handler=None)
