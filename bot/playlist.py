"""
Playlist storage and management for alxcer-music-v2.

Per-user playlists stored in bot/playlists.json (committed to GitHub via BOT_PAT
so they survive bot restarts on the GitHub Actions runner).

Public-page scraping is used for Spotify and Apple Music imports — no API keys
or user setup required. YouTube uses Piped (same as the rest of the bot).
"""

import json
import os
import re
import base64
import urllib.request
import urllib.error
import urllib.parse
import logging
import threading
import concurrent.futures

log = logging.getLogger('alxcer.playlist')

PLAYLISTS_FILE = os.path.join(os.path.dirname(__file__), 'playlists.json')
BOT_PAT = os.environ.get('BOT_PAT', '')
GITHUB_REPO = 'Perth321/alxcer-music-v2'
PLAYLISTS_PATH = 'bot/playlists.json'
MAX_TRACKS = 500
MAX_PLAYLISTS = 20
SPOTIFY_ENRICH_LIMIT = int(os.environ.get('SPOTIFY_ENRICH_LIMIT', '100'))
SPOTIFY_ENRICH_WORKERS = int(os.environ.get('SPOTIFY_ENRICH_WORKERS', '8'))

_data = {'playlists': {}}
_sha = None
_lock = threading.Lock()

UA_DESKTOP = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0 Safari/537.36'
)


def _gh_request(path, method='GET', payload=None):
    url = 'https://api.github.com/repos/' + GITHUB_REPO + '/contents/' + path
    headers = {
        'Authorization': 'token ' + BOT_PAT,
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
    }
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _write_local():
    try:
        with open(PLAYLISTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning('local playlist write error: %s', e)


def _push_to_github():
    global _sha
    if not BOT_PAT:
        return
    with _lock:
        try:
            content_b64 = base64.b64encode(
                json.dumps(_data, ensure_ascii=False, indent=2).encode('utf-8')
            ).decode()
            payload = {
                'message': 'chore: sync playlists.json',
                'content': content_b64,
                'branch': 'main',
            }
            if _sha:
                payload['sha'] = _sha
            resp = _gh_request(PLAYLISTS_PATH, method='PUT', payload=payload)
            _sha = resp['content']['sha']
            log.info('playlists pushed to GitHub')
        except Exception as e:
            log.warning('GitHub playlist push error: %s', e)


def _save_async():
    _write_local()
    threading.Thread(target=_push_to_github, daemon=True).start()


def load():
    global _data, _sha
    if os.path.exists(PLAYLISTS_FILE):
        try:
            with open(PLAYLISTS_FILE, 'r', encoding='utf-8') as f:
                _data = json.load(f)
            log.info('playlists loaded from local file (%d users)', len(_data.get('playlists', {})))
        except Exception as e:
            log.warning('local playlist load error: %s', e)

    if not BOT_PAT:
        return

    try:
        resp = _gh_request(PLAYLISTS_PATH)
        _sha = resp['sha']
        remote = json.loads(base64.b64decode(resp['content']).decode('utf-8'))
        # Prefer remote if it has more entries (covers fresh container start)
        if len(remote.get('playlists', {})) >= len(_data.get('playlists', {})):
            _data = remote
            _write_local()
        log.info('playlists synced from GitHub (%d users, sha=%s)',
                 len(_data.get('playlists', {})), _sha[:7] if _sha else '?')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log.info('playlists.json not in repo yet, starting fresh')
        else:
            log.warning('GitHub playlist load error: %s', e)
    except Exception as e:
        log.warning('playlist load error: %s', e)


def _user_pls(user_id):
    return _data.setdefault('playlists', {}).setdefault(str(user_id), {})


def get_all(user_id):
    return dict(_user_pls(user_id))


def get(user_id, name):
    return _user_pls(user_id).get(name.strip().lower())


def create(user_id, name):
    name = name.strip()
    if not name:
        return False, 'ใส่ชื่อ playlist ด้วย'
    if len(name) > 40:
        return False, 'ชื่อ playlist ยาวเกินไป (สูงสุด 40 ตัวอักษร)'
    plists = _user_pls(user_id)
    if len(plists) >= MAX_PLAYLISTS:
        return False, 'มี playlist ครบ ' + str(MAX_PLAYLISTS) + ' อันแล้ว'
    key = name.lower()
    if key in plists:
        return False, 'มี playlist ชื่อ **' + name + '** อยู่แล้ว'
    pl = {'name': name, 'tracks': []}
    plists[key] = pl
    _save_async()
    return True, pl


def delete(user_id, name):
    plists = _user_pls(user_id)
    key = name.strip().lower()
    if key not in plists:
        return False, 'ไม่พบ playlist **' + name + '**'
    del plists[key]
    _save_async()
    return True, None


def add_track(user_id, name, track):
    pl = get(user_id, name)
    if not pl:
        return False, 'ไม่พบ playlist **' + name + '**'
    if len(pl['tracks']) >= MAX_TRACKS:
        return False, 'playlist เต็มแล้ว (' + str(MAX_TRACKS) + ' เพลง)'
    entry = {
        'title': track.get('title', 'Unknown'),
        'webpage_url': track.get('webpage_url', ''),
        'duration': track.get('duration', 0),
        'uploader': track.get('uploader', ''),
        'thumbnail': track.get('thumbnail'),
        'source': track.get('source', 'manual'),
        'query': track.get('query') or track.get('webpage_url', '') or track.get('title', ''),
        'album': track.get('album', ''),
        'added_at': track.get('added_at', ''),
        'release_date': track.get('release_date', ''),
        'source_position': track.get('source_position', 0),
        'source_uri': track.get('source_uri', ''),
        'preview_url': track.get('preview_url', ''),
        'explicit': bool(track.get('explicit')),
    }
    pl['tracks'].append(entry)
    _save_async()
    return True, entry


def remove_track(user_id, name, index):
    pl = get(user_id, name)
    if not pl:
        return False, 'ไม่พบ playlist **' + name + '**'
    tracks = pl['tracks']
    if not (1 <= index <= len(tracks)):
        return False, 'ลำดับต้องอยู่ระหว่าง 1–' + str(len(tracks))
    removed = tracks.pop(index - 1)
    _save_async()
    return True, removed


def set_tracks(user_id, name, display_name, tracks):
    plists = _user_pls(user_id)
    key = name.lower()
    if key not in plists:
        if len(plists) >= MAX_PLAYLISTS:
            raise RuntimeError('มี playlist ครบ ' + str(MAX_PLAYLISTS) + ' อันแล้ว — ลบของเก่าก่อน')
        plists[key] = {'name': display_name, 'tracks': []}
    plists[key]['tracks'] = tracks[:MAX_TRACKS]
    plists[key]['name'] = display_name
    _save_async()
    return plists[key]


def track_to_queue_entry(t):
    """Convert a stored playlist track to a queue-ready dict (no stream URL yet)."""
    return {
        'title': t.get('title', 'Unknown'),
        'webpage_url': t.get('webpage_url', ''),
        'duration': t.get('duration', 0),
        'uploader': t.get('uploader', ''),
        'thumbnail': t.get('thumbnail'),
        'source': t.get('source', 'manual'),
        'query': t.get('query') or t.get('webpage_url', '') or t.get('title', ''),
        'album': t.get('album', ''),
        'added_at': t.get('added_at', ''),
        'release_date': t.get('release_date', ''),
        'source_position': t.get('source_position', 0),
        'source_uri': t.get('source_uri', ''),
        'preview_url': t.get('preview_url', ''),
        'explicit': bool(t.get('explicit')),
        'url': None,
    }


# ─── Import detection ────────────────────────────────────────────────────────

def detect_import_type(url):
    url = (url or '').strip()
    if not url:
        return None
    if ('youtube.com/playlist' in url
            or 'music.youtube.com/playlist' in url
            or ('list=' in url and 'youtube' in url)):
        return 'youtube'
    if 'soundcloud.com' in url and ('/sets/' in url or '/likes' in url):
        return 'soundcloud'
    if 'spotify.com/playlist' in url or 'spotify.com/album' in url:
        return 'spotify'
    if 'music.apple.com' in url and ('/playlist/' in url or '/album/' in url):
        return 'apple'
    return None


# ─── Importers ───────────────────────────────────────────────────────────────

def _http_get(url, headers=None, timeout=15):
    h = {'User-Agent': UA_DESKTOP, 'Accept-Language': 'en-US,en;q=0.9'}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'ignore')


def import_youtube_playlist(url, piped_instances_fn):
    """Import a YouTube playlist via Piped (no GitHub Actions IP exposure)."""
    m = re.search(r'[?&]list=([A-Za-z0-9_-]+)', url)
    if not m:
        raise RuntimeError('ไม่พบ Playlist ID ใน URL')
    pl_id = m.group(1)

    last_err = None
    for inst in piped_instances_fn()[:8]:
        try:
            req = urllib.request.Request(
                inst + '/playlists/' + pl_id,
                headers={'Accept': 'application/json', 'User-Agent': UA_DESKTOP},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())

            pl_name = data.get('name', 'YouTube Playlist')
            tracks = []
            for v in (data.get('relatedStreams') or []):
                raw_url = v.get('url', '')
                vid_m = re.search(r'[?&]v=([A-Za-z0-9_-]{11})', raw_url) \
                    or re.search(r'/(?:watch\?v=|shorts/|embed/)?([A-Za-z0-9_-]{11})$', raw_url) \
                    or re.search(r'([A-Za-z0-9_-]{11})$', raw_url)
                if not vid_m:
                    continue
                vid = vid_m.group(1)
                yt_url = 'https://www.youtube.com/watch?v=' + vid
                tracks.append({
                    'title': v.get('title', 'Unknown'),
                    'webpage_url': yt_url,
                    'duration': v.get('duration', 0),
                    'uploader': v.get('uploaderName', ''),
                    'thumbnail': v.get('thumbnail'),
                    'source': 'youtube',
                    'query': yt_url,
                })
            if not tracks:
                raise RuntimeError('playlist ว่าง')
            log.info('YouTube playlist imported: %s (%d tracks) via %s', pl_name, len(tracks), inst)
            return pl_name, tracks
        except Exception as e:
            last_err = e
            log.warning('piped playlist %s: %s', inst, e)
    raise RuntimeError('ไม่สามารถ import YouTube playlist ได้: ' + str(last_err))


def import_soundcloud_playlist(url):
    """Import SoundCloud playlist via yt-dlp (works reliably for SC sets)."""
    import yt_dlp
    opts = {
        'quiet': True, 'no_warnings': True,
        'extract_flat': True, 'noplaylist': False,
        'source_address': '0.0.0.0',
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(url, download=False)
    if not data:
        raise RuntimeError('ดึงข้อมูล playlist ไม่ได้')
    tracks = []
    for entry in (data.get('entries') or []):
        if not entry:
            continue
        sc_url = entry.get('url') or entry.get('webpage_url', '')
        if not sc_url:
            continue
        tracks.append({
            'title': entry.get('title', 'Unknown'),
            'webpage_url': sc_url,
            'duration': entry.get('duration', 0),
            'uploader': entry.get('uploader', ''),
            'thumbnail': entry.get('thumbnail'),
            'source': 'soundcloud',
            'query': sc_url,
        })
    if not tracks:
        raise RuntimeError('playlist ว่าง')
    return data.get('title') or 'SoundCloud Playlist', tracks


def _spotify_resource_id(url):
    m = re.search(r'/(playlist|album)/([A-Za-z0-9]+)', url)
    if not m:
        raise RuntimeError('ไม่พบ Spotify ID')
    return m.group(1), m.group(2)


def _best_spotify_image(images):
    if not isinstance(images, list) or not images:
        return None
    best = None
    best_size = -1
    for img in images:
        if not isinstance(img, dict):
            continue
        url = img.get('url')
        if not url:
            continue
        size = int(img.get('maxWidth') or img.get('width') or img.get('maxHeight') or img.get('height') or 0)
        if best is None or size > best_size:
            best = url
            best_size = size
    return best


def _spotify_track_embed_details(track_id):
    if not track_id:
        return {}
    try:
        html = _http_get('https://open.spotify.com/embed/track/' + track_id, timeout=8)
        m = re.search(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.+?)</script>',
            html, re.DOTALL,
        )
        if not m:
            return {}
        next_data = json.loads(m.group(1))
    except Exception as e:
        log.debug('Spotify track detail %s failed: %s', track_id, e)
        return {}

    track_node = None

    def walk(node):
        nonlocal track_node
        if track_node is not None:
            return
        if isinstance(node, dict):
            if node.get('type') == 'track' and (node.get('id') == track_id or node.get('uri', '').endswith(track_id)):
                track_node = node
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(next_data)
    if not track_node:
        return {}

    visual = track_node.get('visualIdentity') or {}
    artists = track_node.get('artists') or []
    release = track_node.get('releaseDate') or {}
    preview = track_node.get('audioPreview') or {}
    return {
        'thumbnail': _best_spotify_image(visual.get('image')),
        'release_date': release.get('isoString', ''),
        'uploader': ', '.join(a.get('name', '') for a in artists if isinstance(a, dict) and a.get('name')),
        'preview_url': preview.get('url', ''),
    }


def import_spotify_playlist(url):
    """Scrape Spotify embed page (no API key). Returns tracks with title+artist
    as `query` so the bot resolves them via YouTube/SoundCloud at play time."""
    kind, sp_id = _spotify_resource_id(url)
    embed_url = 'https://open.spotify.com/embed/' + kind + '/' + sp_id

    html = _http_get(embed_url)

    # The embed page ships its full state in <script id="__NEXT_DATA__">
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.+?)</script>',
        html, re.DOTALL,
    )
    if not m:
        raise RuntimeError('Spotify embed format เปลี่ยน — ดึงข้อมูลไม่ได้')
    try:
        next_data = json.loads(m.group(1))
    except Exception as e:
        raise RuntimeError('parse Spotify JSON ไม่ได้: ' + str(e))

    # Walk the nested entity tree looking for the most complete trackList.
    # Spotify embeds may include more than one entity-looking object; the first
    # one is not always the complete playlist.
    pl_name = None
    track_list = None
    track_container = None
    track_list_candidates = []

    def walk(node):
        nonlocal pl_name
        if isinstance(node, dict):
            if 'trackList' in node and isinstance(node['trackList'], list):
                tracks = [
                    t for t in node['trackList']
                    if isinstance(t, dict) and (t.get('title') or t.get('name') or t.get('uri'))
                ]
                if tracks:
                    track_list_candidates.append((len(tracks), node, node['trackList']))
            if 'name' in node and isinstance(node['name'], str) and not pl_name:
                # First "name" inside an entity-looking dict
                if any(k in node for k in ('trackList', 'subtitle', 'coverArt', 'type')):
                    pl_name = node['name']
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(next_data)
    if track_list_candidates:
        track_list_candidates.sort(key=lambda item: item[0], reverse=True)
        _, track_container, track_list = track_list_candidates[0]
        if isinstance(track_container, dict) and track_container.get('name'):
            pl_name = track_container.get('name')
        log.info('Spotify trackList candidates: %s', [item[0] for item in track_list_candidates[:5]])

    if not track_list:
        raise RuntimeError('Spotify ไม่มีรายชื่อเพลง (อาจเป็น playlist ส่วนตัว)')

    detail_map = {}
    ids_to_enrich = []
    for t in track_list[:SPOTIFY_ENRICH_LIMIT]:
        uri = t.get('uri', '')
        sp_track_id = uri.split(':')[-1] if uri else ''
        if sp_track_id:
            ids_to_enrich.append(sp_track_id)

    if ids_to_enrich:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, SPOTIFY_ENRICH_WORKERS)) as executor:
            futures = {executor.submit(_spotify_track_embed_details, track_id): track_id for track_id in ids_to_enrich}
            for future in concurrent.futures.as_completed(futures):
                track_id = futures[future]
                try:
                    detail_map[track_id] = future.result()
                except Exception:
                    detail_map[track_id] = {}

    tracks = []
    for idx, t in enumerate(track_list, 1):
        title = t.get('title') or t.get('name') or 'Unknown'
        artists = t.get('subtitle') or ''
        if isinstance(artists, list):
            artists = ', '.join(a.get('name', '') for a in artists if isinstance(a, dict))
        artists = str(artists).strip()
        uri = t.get('uri', '')
        sp_track_id = uri.split(':')[-1] if uri else ''
        detail = detail_map.get(sp_track_id) or {}
        if detail.get('uploader'):
            artists = detail['uploader']
        webpage = ('https://open.spotify.com/track/' + sp_track_id) if sp_track_id else url
        search_q = (title + ' ' + artists).strip()
        dur_ms = t.get('duration') or t.get('duration_ms') or 0
        try:
            dur_s = int(dur_ms) // 1000 if dur_ms else 0
        except Exception:
            dur_s = 0
        tracks.append({
            'title': title,
            'webpage_url': webpage,
            'duration': dur_s,
            'uploader': artists,
            'thumbnail': detail.get('thumbnail'),
            'source': 'spotify',
            'query': search_q,
            'album': t.get('album') or t.get('albumName') or '',
            'added_at': t.get('addedAt') or t.get('added_at') or '',
            'release_date': detail.get('release_date', ''),
            'source_position': idx,
            'source_uri': uri,
            'preview_url': detail.get('preview_url', ''),
            'explicit': bool(t.get('isExplicit')),
        })

    if not tracks:
        raise RuntimeError('playlist ว่าง')

    log.info('Spotify %s imported: %s (%d tracks)', kind, pl_name, len(tracks))
    return pl_name or ('Spotify ' + kind.title()), tracks


def import_apple_music_playlist(url):
    """Scrape Apple Music public playlist/album page (no API key).
    Returns tracks with title+artist as `query` for resolution at play time.
    Strategy: prefer JSON-LD MusicPlaylist (clean & reliable on every page),
    fall back to <script id="serialized-server-data"> if absent."""
    # Normalize: strip query string (Apple sometimes appends ?i=trackId)
    base_url = url.split('?', 1)[0]
    html = _http_get(base_url)

    pl_name = None
    raw_tracks = []

    # 1) Primary path: JSON-LD MusicPlaylist / MusicAlbum (verified reliable)
    for ld_m in re.finditer(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.+?)</script>',
        html, re.DOTALL,
    ):
        try:
            ld = json.loads(ld_m.group(1))
        except Exception:
            continue
        if not isinstance(ld, dict):
            continue
        if ld.get('@type') in ('MusicPlaylist', 'MusicAlbum'):
            if not pl_name:
                pl_name = ld.get('name')
            items = ld.get('track') or []
            if isinstance(items, dict):
                items = items.get('itemListElement') or []
            for it in items:
                if isinstance(it, dict) and it.get('@type') == 'ListItem':
                    it = it.get('item') or {}
                if isinstance(it, dict) and it.get('@type') == 'MusicRecording':
                    artist = ''
                    by = it.get('byArtist')
                    if isinstance(by, dict):
                        artist = by.get('name', '') or ''
                    elif isinstance(by, str):
                        artist = by
                    elif isinstance(by, list):
                        artist = ', '.join(
                            (a.get('name', '') if isinstance(a, dict) else str(a))
                            for a in by
                        )
                    raw_tracks.append({
                        'name': it.get('name'),
                        'artistName': artist,
                        'url': it.get('url'),
                        'duration': 0,  # JSON-LD doesn't include duration
                    })

    # 2) Fallback: serialized-server-data (only if JSON-LD absent)
    if not raw_tracks:
        m = re.search(
            r'<script[^>]+id="serialized-server-data"[^>]*>(.+?)</script>',
            html, re.DOTALL,
        )
        if m:
            try:
                ssd = json.loads(m.group(1))
            except Exception:
                ssd = None
            if ssd:
                root = ssd[0] if isinstance(ssd, list) and ssd else ssd
                data = root.get('data') if isinstance(root, dict) else None
                if isinstance(data, dict):
                    seo = data.get('seoData') or {}
                    pl_name = pl_name or seo.get('appleMusicTitle') or data.get('seoTitle')

                    def collect(node):
                        if isinstance(node, dict):
                            # Apple lockup item: has both 'name' (string) and 'artistName'
                            nm = node.get('name')
                            an = node.get('artistName')
                            if isinstance(nm, str) and isinstance(an, str) and nm and an:
                                raw_tracks.append(node)
                            else:
                                for v in node.values():
                                    collect(v)
                        elif isinstance(node, list):
                            for v in node:
                                collect(v)

                    collect(data.get('sections') or data)

    # 3) Last resort: og:music:song meta tags (URLs only)
    if not raw_tracks:
        for tag in re.finditer(
            r'<meta[^>]+property="music:song"[^>]+content="([^"]+)"', html
        ):
            raw_tracks.append({'url': tag.group(1)})

    if not raw_tracks:
        raise RuntimeError('Apple Music page ไม่มีข้อมูลเพลง (อาจเป็น playlist ส่วนตัว)')

    tracks = []
    for t in raw_tracks:
        title = t.get('name') or t.get('title') or t.get('songName') or 'Unknown'
        artist = t.get('artistName') or t.get('artist') or t.get('subtitle') or ''
        if isinstance(artist, list):
            artist = ', '.join(str(a) for a in artist if a)
        artist = str(artist).strip()
        webpage = t.get('url') or base_url
        search_q = (str(title) + ' ' + artist).strip()
        dur_ms = t.get('durationInMillis') or t.get('duration') or 0
        try:
            dur_s = int(dur_ms) // 1000 if dur_ms else 0
        except Exception:
            dur_s = 0
        tracks.append({
            'title': str(title),
            'webpage_url': webpage,
            'duration': dur_s,
            'uploader': artist,
            'thumbnail': t.get('artwork', {}).get('url') if isinstance(t.get('artwork'), dict) else None,
            'source': 'apple',
            'query': search_q,
        })

    if not pl_name:
        # Try og:title from HTML
        og = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
        if og:
            pl_name = og.group(1)

    log.info('Apple Music imported: %s (%d tracks)', pl_name, len(tracks))
    return pl_name or 'Apple Music Playlist', tracks


def import_any(url, piped_instances_fn):
    """Auto-detect and dispatch to the right importer."""
    kind = detect_import_type(url)
    if kind == 'youtube':
        return 'YouTube', *import_youtube_playlist(url, piped_instances_fn)
    if kind == 'soundcloud':
        return 'SoundCloud', *import_soundcloud_playlist(url)
    if kind == 'spotify':
        return 'Spotify', *import_spotify_playlist(url)
    if kind == 'apple':
        return 'Apple Music', *import_apple_music_playlist(url)
    raise RuntimeError(
        'ไม่รู้จัก link นี้ — รองรับ Spotify, YouTube, SoundCloud, Apple Music เท่านั้น'
    )
