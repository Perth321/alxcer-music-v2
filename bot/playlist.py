"""
Playlist storage and management for alxcer-music-v2.

Data is stored in bot/playlists.json in the GitHub repo (via BOT_PAT),
so playlists survive bot restarts.

Schema:
{
  "playlists": {
    "<discord_user_id>": {
      "<name_lowercase>": {
        "name": "Display Name",
        "tracks": [
          {
            "title": "...",
            "webpage_url": "https://...",
            "duration": 240,
            "uploader": "...",
            "thumbnail": "https://...",
            "source": "youtube|soundcloud|spotify|manual",
            "query": "search string (for spotify/manual)"
          }
        ]
      }
    }
  }
}
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

log = logging.getLogger('alxcer.playlist')

PLAYLISTS_FILE = os.path.join(os.path.dirname(__file__), 'playlists.json')
BOT_PAT = os.environ.get('BOT_PAT', '')
GITHUB_REPO = 'Perth321/alxcer-music-v2'
PLAYLISTS_PATH = 'bot/playlists.json'
MAX_TRACKS = 500
MAX_PLAYLISTS = 20

_data = {'playlists': {}}
_sha = None
_lock = threading.Lock()


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
            return
        except Exception as e:
            log.warning('local playlist load error: %s', e)

    if not BOT_PAT:
        _data = {'playlists': {}}
        return

    try:
        resp = _gh_request(PLAYLISTS_PATH)
        _sha = resp['sha']
        _data = json.loads(base64.b64decode(resp['content']).decode('utf-8'))
        _write_local()
        log.info('playlists loaded from GitHub (%d users)', len(_data.get('playlists', {})))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log.info('playlists.json not in repo yet, starting fresh')
        else:
            log.warning('GitHub playlist load error: %s', e)
        _data = {'playlists': {}}
    except Exception as e:
        log.warning('playlist load error: %s', e)
        _data = {'playlists': {}}


def _user_pls(user_id):
    return _data.setdefault('playlists', {}).setdefault(str(user_id), {})


def get_all(user_id):
    return dict(_user_pls(user_id))


def get(user_id, name):
    return _user_pls(user_id).get(name.strip().lower())


def create(user_id, name):
    name = name.strip()
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


def rename(user_id, old_name, new_name):
    old_name = old_name.strip()
    new_name = new_name.strip()
    if len(new_name) > 40:
        return False, 'ชื่อใหม่ยาวเกินไป'
    plists = _user_pls(user_id)
    old_key = old_name.lower()
    new_key = new_name.lower()
    if old_key not in plists:
        return False, 'ไม่พบ playlist **' + old_name + '**'
    if new_key in plists and new_key != old_key:
        return False, 'มี playlist ชื่อ **' + new_name + '** อยู่แล้ว'
    pl = plists.pop(old_key)
    pl['name'] = new_name
    plists[new_key] = pl
    _save_async()
    return True, pl


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
        'query': track.get('query') or track.get('webpage_url', ''),
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
        plists[key] = {'name': display_name, 'tracks': []}
    plists[key]['tracks'] = tracks[:MAX_TRACKS]
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
        'query': t.get('query') or t.get('webpage_url', ''),
        'url': None,
    }


def detect_import_type(url):
    url = url.strip()
    if ('youtube.com/playlist' in url or 'music.youtube.com/playlist' in url
            or ('list=' in url and 'youtube' in url)):
        return 'youtube'
    if 'soundcloud.com' in url and ('/sets/' in url or '/likes' in url or '/tracks' in url):
        return 'soundcloud'
    if 'spotify.com/playlist' in url or 'spotify.com/album' in url:
        return 'spotify'
    return None


def import_youtube_playlist(url, piped_instances_fn):
    """Import YouTube playlist via Piped API (no GitHub Actions IP exposure)."""
    m = re.search(r'[?&]list=([A-Za-z0-9_-]+)', url)
    if not m:
        raise RuntimeError('ไม่พบ Playlist ID ใน URL')
    pl_id = m.group(1)

    last_err = None
    for inst in piped_instances_fn()[:8]:
        try:
            req = urllib.request.Request(
                inst + '/playlists/' + pl_id,
                headers={'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())

            pl_name = data.get('name', 'YouTube Playlist')
            tracks = []
            for v in (data.get('relatedStreams') or []):
                raw_url = v.get('url', '')
                vid_m = re.search(r'[?&/]v(?:ideo)?(?:=|/)([A-Za-z0-9_-]{11})', raw_url)
                if not vid_m:
                    vid_m = re.search(r'([A-Za-z0-9_-]{11})$', raw_url)
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
            log.info('YouTube playlist imported: %s (%d tracks) via %s', pl_name, len(tracks), inst)
            return pl_name, tracks
        except Exception as e:
            last_err = e
            log.warning('piped playlist %s: %s', inst, e)
    raise RuntimeError('ไม่สามารถ import YouTube playlist ได้: ' + str(last_err))


def import_soundcloud_playlist(url):
    """Import SoundCloud playlist via yt-dlp."""
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
        sc_url = entry.get('url') or entry.get('webpage_url', url)
        tracks.append({
            'title': entry.get('title', 'Unknown'),
            'webpage_url': sc_url,
            'duration': entry.get('duration', 0),
            'uploader': entry.get('uploader', ''),
            'thumbnail': entry.get('thumbnail'),
            'source': 'soundcloud',
            'query': sc_url,
        })
    return data.get('title') or 'SoundCloud Playlist', tracks


def import_spotify_playlist(url):
    """Import Spotify playlist via yt-dlp (extracts track names for search)."""
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
        title = entry.get('title', '')
        artist = entry.get('artist') or entry.get('uploader', '')
        search_q = (title + (' - ' + artist if artist else '')).strip()
        tracks.append({
            'title': title or 'Unknown',
            'webpage_url': url,
            'duration': entry.get('duration', 0),
            'uploader': artist,
            'thumbnail': entry.get('thumbnail'),
            'source': 'spotify',
            'query': search_q,
        })
    return data.get('title') or 'Spotify Playlist', tracks
