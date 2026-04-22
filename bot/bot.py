import discord
from discord.ext import commands
import asyncio
import yt_dlp
import os
import re
import urllib.request
import urllib.parse
import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger('alxcer')

# Try to load opus explicitly (required for voice)
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
FFMPEG_OPTIONS = '-vn -b:a 128k'

COOKIES_FILE = os.path.join(os.path.dirname(__file__), 'cookies.txt')

PIPED_INSTANCES = [
    'https://api.piped.private.coffee',
    'https://pipedapi.kavin.rocks',
    'https://pipedapi.adminforge.de',
    'https://pipedapi.leptons.xyz',
    'https://pipedapi.r4fo.com',
]

INVIDIOUS_INSTANCES = [
    'https://invidious.nerdvpn.de',
    'https://invidious.privacyredirect.com',
    'https://inv.nadeko.net',
    'https://invidious.f5.si',
    'https://yewtu.be',
]

YT_CLIENT_FALLBACKS = [
    ['web', 'android', 'ios', 'mweb', 'tv_embedded', 'web_embedded'],
    ['android_vr'],
    ['tv'],
    ['web_safari'],
    ['ios'],
    ['android'],
]


def http_get_json(url, timeout=6):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', 'ignore'))


def extract_video_id(s):
    m = re.search(r'(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})', s)
    return m.group(1) if m else None


_SC_CLIENT_ID = None
_SC_CLIENT_ID_TS = 0


def get_soundcloud_client_id():
    """Scrape a fresh SoundCloud client_id from their homepage (cached 1h)."""
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
    """Search SoundCloud (no auth needed) and return playable stream."""
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
            raise RuntimeError('no results')
        t = items[0]
        track_url = t['permalink_url']
        title = t.get('title', 'Unknown')
        duration = int((t.get('duration') or 0) / 1000)
        thumb = t.get('artwork_url')
        uploader = (t.get('user') or {}).get('username', 'SoundCloud')

    # Use yt-dlp to extract the actual stream URL (handles SoundCloud cleanly, no auth)
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
        raise RuntimeError('no stream url from yt-dlp')
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
    """Scrape youtube.com/results for video IDs. Works from any IP."""
    url = 'https://www.youtube.com/results?search_query=' + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9',
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


def make_ydl_opts(client):
    opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'source_address': '0.0.0.0',
        'geo_bypass': True,
        'nocheckcertificate': True,
        'extractor_args': {'youtube': {'player_client': client}},
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
        },
    }
    if os.path.exists(COOKIES_FILE):
        opts['cookiefile'] = COOKIES_FILE
    return opts


def fetch_via_ytdlp(query):
    q = query.strip()
    if re.match(r'https?://', q):
        targets = [q]
    else:
        try:
            ids = youtube_html_search(q, n=5)
        except Exception as e:
            log.warning('html search failed: %s', e)
            ids = []
        if not ids:
            raise RuntimeError('no results')
        targets = ['https://www.youtube.com/watch?v=' + v for v in ids]

    last_err = None
    for target in targets:
        for client in YT_CLIENT_FALLBACKS:
            try:
                with yt_dlp.YoutubeDL(make_ydl_opts(client)) as ydl:
                    data = ydl.extract_info(target, download=False)
                    if 'entries' in data:
                        if not data['entries']:
                            raise RuntimeError('no entries')
                        data = data['entries'][0]
                    if not data.get('url'):
                        raise RuntimeError('no stream url')
                    log.info('ytdlp ok client=%s url=%s', client, target)
                    return {
                        'url': data['url'],
                        'title': data.get('title', 'Unknown'),
                        'duration': data.get('duration', 0),
                        'thumbnail': data.get('thumbnail'),
                        'webpage_url': data.get('webpage_url', target),
                        'uploader': data.get('uploader', 'Unknown'),
                        'query': query,
                    }
            except Exception as e:
                last_err = e
                log.warning('ytdlp client=%s target=%s fail: %s', client, target, e)
    raise RuntimeError('ytdlp failed: ' + str(last_err))


def fetch_via_piped(query):
    vid = extract_video_id(query)
    if not vid:
        ids = youtube_html_search(query, n=1)
        if not ids:
            raise RuntimeError('no search results')
        vid = ids[0]
    last_err = None
    for inst in PIPED_INSTANCES:
        try:
            vid_local = vid
            streams = http_get_json(inst + '/streams/' + vid_local)
            audio = streams.get('audioStreams') or []
            if not audio:
                raise RuntimeError('no audio streams')
            audio.sort(key=lambda a: a.get('bitrate', 0), reverse=True)
            best = audio[0]
            log.info('piped ok via %s', inst)
            return {
                'url': best['url'],
                'title': streams.get('title', 'Unknown'),
                'duration': streams.get('duration', 0),
                'thumbnail': streams.get('thumbnailUrl'),
                'webpage_url': 'https://youtube.com/watch?v=' + vid_local,
                'uploader': streams.get('uploader', 'Unknown'),
                'query': query,
            }
        except Exception as e:
            last_err = e
            log.warning('piped %s: %s', inst, e)
    raise RuntimeError('piped failed: ' + str(last_err))


def fetch_via_invidious(query):
    vid = extract_video_id(query)
    if not vid:
        ids = youtube_html_search(query, n=1)
        if not ids:
            raise RuntimeError('no search results')
        vid = ids[0]
    last_err = None
    for inst in INVIDIOUS_INSTANCES:
        try:
            vid_local = vid
            v = http_get_json(inst + '/api/v1/videos/' + vid_local)
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
                'webpage_url': 'https://youtube.com/watch?v=' + vid_local,
                'uploader': v.get('author', 'Unknown'),
                'query': query,
            }
        except Exception as e:
            last_err = e
            log.warning('invidious %s: %s', inst, e)
    raise RuntimeError('invidious failed: ' + str(last_err))


async def fetch_track(query):
    loop = asyncio.get_event_loop()

    def _run():
        errors = []
        for fn, name in [(fetch_via_soundcloud, 'soundcloud'),
                         (fetch_via_ytdlp, 'ytdlp'),
                         (fetch_via_piped, 'piped'),
                         (fetch_via_invidious, 'invidious')]:
            try:
                return fn(query)
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
loop_mode = {}  # guild_id -> 'off' | 'one' | 'all'

LOOP_LABELS = {
    'off': 'ปิด',
    'one': '🔂 1 เพลง',
    'all': '🔁 ทั้งคิว',
}


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
    if h:
        return str(h) + ':' + str(m).zfill(2) + ':' + str(sec).zfill(2)
    return str(m) + ':' + str(sec).zfill(2)


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
    if track.get('thumbnail'):
        embed.set_thumbnail(url=track['thumbnail'])
    return embed


class PlayerView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=None)
        self.ctx = ctx
        self._refresh_loop_button()

    def _refresh_loop_button(self):
        mode = get_loop(self.ctx.guild.id)
        for child in self.children:
            if getattr(child, 'custom_id', None) == 'loop':
                if mode == 'off':
                    child.label = 'Loop: Off'
                    child.emoji = '🔁'
                    child.style = discord.ButtonStyle.secondary
                elif mode == 'one':
                    child.label = 'Loop: 1 เพลง'
                    child.emoji = '🔂'
                    child.style = discord.ButtonStyle.success
                else:
                    child.label = 'Loop: ทั้งคิว'
                    child.emoji = '🔁'
                    child.style = discord.ButtonStyle.success

    async def _ack(self, interaction):
        try:
            await interaction.response.defer()
        except Exception:
            pass

    @discord.ui.button(emoji='⏯️', label='Pause/Resume', style=discord.ButtonStyle.primary, custom_id='pp')
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._ack(interaction)
        vc = self.ctx.voice_client
        if not vc:
            await interaction.followup.send('❌ บอทไม่ได้อยู่ใน voice', ephemeral=True)
            return
        if vc.is_playing():
            vc.pause()
            await interaction.followup.send('⏸️ หยุดชั่วคราว', ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            await interaction.followup.send('▶️ เล่นต่อ', ephemeral=True)
        else:
            await interaction.followup.send('❌ ไม่มีเพลงเล่นอยู่', ephemeral=True)

    @discord.ui.button(emoji='⏭️', label='Skip', style=discord.ButtonStyle.primary, custom_id='skip')
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._ack(interaction)
        vc = self.ctx.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await interaction.followup.send('⏭️ ข้ามเพลง', ephemeral=True)
        else:
            await interaction.followup.send('❌ ไม่มีเพลงเล่นอยู่', ephemeral=True)

    @discord.ui.button(emoji='🔁', label='Loop: Off', style=discord.ButtonStyle.secondary, custom_id='loop')
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._ack(interaction)
        mode = cycle_loop(self.ctx.guild.id)
        self._refresh_loop_button()
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        await interaction.followup.send('🔁 เปลี่ยนโหมด Loop เป็น: **' + LOOP_LABELS[mode] + '**', ephemeral=True)

    @discord.ui.button(emoji='⏹️', label='Stop', style=discord.ButtonStyle.danger, custom_id='stop')
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._ack(interaction)
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
            await interaction.followup.send('⏹️ หยุดและออกจาก voice แล้ว', ephemeral=True)
        else:
            await interaction.followup.send('❌ บอทไม่ได้อยู่ใน voice', ephemeral=True)


async def ensure_voice(ctx):
    """Connect/move to caller's voice channel with retries."""
    target = ctx.author.voice.channel
    vc = ctx.voice_client

    for attempt in range(1, 5):
        try:
            if vc and vc.is_connected():
                if vc.channel != target:
                    await vc.move_to(target)
                return vc
            vc = await target.connect(timeout=30.0, reconnect=True, self_deaf=True)
            log.info('voice connected (attempt %d)', attempt)
            return vc
        except (asyncio.TimeoutError, discord.errors.ConnectionClosed, discord.ClientException) as e:
            log.warning('voice connect attempt %d failed: %s', attempt, e)
            try:
                if ctx.voice_client:
                    await ctx.voice_client.disconnect(force=True)
            except Exception:
                pass
            vc = None
            await asyncio.sleep(2 * attempt)
    raise RuntimeError('voice connect failed after 4 attempts')


async def _start_playback(ctx, track):
    """Start playing the given track on the current voice client."""
    vc = ctx.voice_client
    if not vc or not vc.is_connected():
        log.warning('no voice client when starting playback')
        return False
    source = discord.FFmpegPCMAudio(
        track['url'],
        before_options=FFMPEG_BEFORE,
        options=FFMPEG_OPTIONS,
    )
    vc.play(
        source,
        after=lambda err: (log.warning('after-play err: %s', err) if err else None,
                            asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)),
    )
    return True


async def play_next(ctx):
    guild_id = ctx.guild.id
    mode = get_loop(guild_id)
    current = now_playing.get(guild_id)
    queue = get_queue(guild_id)

    # Decide next track based on loop mode
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

    # Re-resolve stream URL if it might be expired (re-fetch on every loop iteration of same song)
    needs_refetch = (mode == 'one') or (mode == 'all' and next_track is current)
    if needs_refetch and next_track.get('query'):
        try:
            fresh = await fetch_track(next_track['query'])
            next_track['url'] = fresh['url']
        except Exception as e:
            log.warning('re-fetch for loop failed: %s', e)

    try:
        ok = await _start_playback(ctx, next_track)
        if not ok:
            return
        await ctx.send(embed=make_np_embed(next_track, guild_id), view=PlayerView(ctx))
    except Exception as e:
        log.exception('play_next error')
        try:
            await ctx.send('⚠️ เล่นไม่ได้: ' + str(e) + '\nข้ามเพลงถัดไป...')
        except Exception:
            pass
        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)


@bot.event
async def on_ready():
    log.info('%s online (id=%s)', bot.user, bot.user.id)
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name='!play')
    )


@bot.event
async def on_voice_state_update(member, before, after):
    if member.id != bot.user.id:
        return
    log.info('voice state: before.channel=%s after.channel=%s', before.channel, after.channel)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send('⚠️ ใส่ชื่อเพลงหรือ link ด้วย เช่น !play จี๋หอย')
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

    status = await ctx.send('🔍 กำลังเชื่อมต่อและค้นหา: ' + query + ' ...')

    try:
        vc = await ensure_voice(ctx)
    except Exception as e:
        await status.edit(content='❌ เชื่อมต่อ voice ไม่ได้: ' + str(e))
        return

    try:
        track = await fetch_track(query)
    except Exception as e:
        await status.edit(content='❌ ไม่พบเพลงนั้น\n' + str(e)[:500])
        return

    queue = get_queue(ctx.guild.id)

    if vc.is_playing() or vc.is_paused():
        queue.append(track)
        embed = discord.Embed(
            title='✅ เพิ่มเข้าคิวแล้ว',
            description='**[' + track['title'] + '](' + track['webpage_url'] + ')**',
            color=0x57F287,
        )
        embed.add_field(name='ลำดับในคิว', value=str(len(queue)), inline=True)
        embed.add_field(name='⏱ ความยาว', value=fmt_duration(track['duration']), inline=True)
        if track.get('thumbnail'):
            embed.set_thumbnail(url=track['thumbnail'])
        await status.edit(content=None, embed=embed)
    else:
        now_playing[ctx.guild.id] = track
        try:
            ok = await _start_playback(ctx, track)
            if not ok:
                await status.edit(content='❌ เริ่มเล่นไม่ได้ (voice ไม่พร้อม)')
                return
            await status.edit(content=None, embed=make_np_embed(track, ctx.guild.id), view=PlayerView(ctx))
        except Exception as e:
            log.exception('play start error')
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
        embed.add_field(name='🎵 กำลังเล่น',
            value='**' + np['title'] + '**  ' + fmt_duration(np['duration']),
            inline=False)
    if queue:
        lines = []
        for i, t in enumerate(queue[:10], 1):
            lines.append(str(i) + '. **' + t['title'] + '**  ' + fmt_duration(t['duration']))
        if len(queue) > 10:
            lines.append('...และอีก ' + str(len(queue) - 10) + ' เพลง')
        embed.add_field(name='รอในคิว (' + str(len(queue)) + ' เพลง)',
                        value='\n'.join(lines), inline=False)
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
    """!loop [off|one|all]  — ไม่ใส่ค่าจะหมุนสลับโหมด"""
    valid = {'off', 'one', 'all'}
    aliases = {
        'no': 'off', 'none': 'off', '0': 'off',
        '1': 'one', 'single': 'one', 'song': 'one', 'track': 'one',
        'queue': 'all', 'q': 'all', 'a': 'all',
    }
    if mode is None:
        new_mode = cycle_loop(ctx.guild.id)
    else:
        m = mode.lower()
        m = aliases.get(m, m)
        if m not in valid:
            await ctx.send('❌ ใช้: !loop off | one | all')
            return
        set_loop(ctx.guild.id, m)
        new_mode = m
    await ctx.send('🔁 โหมด Loop: **' + LOOP_LABELS[new_mode] + '**')


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
        await ctx.send('👋 ออกจาก voice channel แล้ว')
    else:
        await ctx.send('❌ บอทไม่ได้อยู่ใน voice channel')


@bot.command(name='stop')
async def stop(ctx):
    if ctx.voice_client:
        queues[ctx.guild.id] = []
        now_playing.pop(ctx.guild.id, None)
        set_loop(ctx.guild.id, 'off')
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect(force=True)
        await ctx.send('⏹️ หยุดและออกจาก voice channel แล้ว')
    else:
        await ctx.send('❌ บอทไม่ได้อยู่ใน voice channel')


@bot.command(name='reconnect', aliases=['rc'])
async def reconnect(ctx):
    """Force reconnect voice."""
    if ctx.voice_client:
        try:
            await ctx.voice_client.disconnect(force=True)
        except Exception:
            pass
    if not ctx.author.voice:
        await ctx.send('❌ เข้า voice channel ก่อน แล้วใช้ !rc')
        return
    try:
        await ensure_voice(ctx)
        await ctx.send('🔄 เชื่อมต่อ voice ใหม่แล้ว')
    except Exception as e:
        await ctx.send('❌ เชื่อมต่อไม่ได้: ' + str(e))


@bot.command(name='help', aliases=['h', 'commands'])
async def help_cmd(ctx):
    embed = discord.Embed(title='🎵 คำสั่งทั้งหมด', color=0x5865F2)
    embed.add_field(name='!play <ชื่อเพลง หรือ link>', value='เล่นเพลง / เพิ่มเข้าคิว', inline=False)
    embed.add_field(name='!skip  (!s)', value='ข้ามเพลง', inline=False)
    embed.add_field(name='!queue  (!q)', value='ดูคิวเพลง', inline=False)
    embed.add_field(name='!np', value='ดูเพลงที่เล่นอยู่ + ปุ่มควบคุม', inline=False)
    embed.add_field(name='!loop [off|one|all]', value='ตั้งโหมด Loop (one = วนเพลงเดียว, all = วนทั้งคิว)', inline=False)
    embed.add_field(name='!pause / !resume', value='หยุดชั่วคราว / เล่นต่อ', inline=False)
    embed.add_field(name='!reconnect (!rc)', value='เชื่อมต่อ voice ใหม่ถ้าหลุด', inline=False)
    embed.add_field(name='!clear', value='ล้างคิว', inline=False)
    embed.add_field(name='!leave  (!dc)', value='ออกจาก voice channel', inline=False)
    embed.add_field(name='!stop', value='หยุดเพลง + ออก voice channel', inline=False)
    embed.set_footer(text='ปุ่มใต้ข้อความ "กำลังเล่นเพลง": ⏯️ Pause/Resume  ⏭️ Skip  🔁 Loop  ⏹️ Stop')
    await ctx.send(embed=embed)


TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
if not TOKEN:
    raise RuntimeError('DISCORD_BOT_TOKEN is not set!')
bot.run(TOKEN, log_handler=None)
