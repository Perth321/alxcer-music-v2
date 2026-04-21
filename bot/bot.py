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
    'https://pipedapi.kavin.rocks',
    'https://pipedapi.r4fo.com',
    'https://pipedapi.adminforge.de',
    'https://pipedapi.darkness.services',
    'https://pipedapi.leptons.xyz',
    'https://api.piped.private.coffee',
    'https://pipedapi.reallyaweso.me',
    'https://pipedapi.smnz.de',
    'https://api-piped.mha.fi',
]

INVIDIOUS_INSTANCES = [
    'https://invidious.nerdvpn.de',
    'https://invidious.privacyredirect.com',
    'https://iv.melmac.space',
    'https://inv.nadeko.net',
    'https://invidious.materialio.us',
    'https://invidious.f5.si',
    'https://invidious.jing.rocks',
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


def http_get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', 'ignore'))


def extract_video_id(s):
    m = re.search(r'(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})', s)
    return m.group(1) if m else None


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
    search = query.strip() if re.match(r'https?://', query.strip()) else 'ytsearch1:' + query
    last_err = None
    for client in YT_CLIENT_FALLBACKS:
        try:
            with yt_dlp.YoutubeDL(make_ydl_opts(client)) as ydl:
                data = ydl.extract_info(search, download=False)
                if 'entries' in data:
                    if not data['entries']:
                        raise RuntimeError('no results')
                    data = data['entries'][0]
                if not data.get('url'):
                    raise RuntimeError('no stream url')
                log.info('ytdlp ok client=%s', client)
                return {
                    'url': data['url'],
                    'title': data.get('title', 'Unknown'),
                    'duration': data.get('duration', 0),
                    'thumbnail': data.get('thumbnail'),
                    'webpage_url': data.get('webpage_url', ''),
                    'uploader': data.get('uploader', 'Unknown'),
                }
        except Exception as e:
            last_err = e
            log.warning('ytdlp client=%s fail: %s', client, e)
    raise RuntimeError('ytdlp failed: ' + str(last_err))


def fetch_via_piped(query):
    vid = extract_video_id(query)
    last_err = None
    for inst in PIPED_INSTANCES:
        try:
            if not vid:
                s = http_get_json(inst + '/search?q=' + urllib.parse.quote(query) + '&filter=music_songs')
                items = s.get('items') or []
                if not items:
                    s = http_get_json(inst + '/search?q=' + urllib.parse.quote(query) + '&filter=videos')
                    items = s.get('items') or []
                if not items:
                    raise RuntimeError('no search results')
                first = next((it for it in items if it.get('url', '').startswith('/watch?v=')), items[0])
                vid_local = first['url'].split('v=')[-1].split('&')[0]
            else:
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
            }
        except Exception as e:
            last_err = e
            log.warning('piped %s: %s', inst, e)
    raise RuntimeError('piped failed: ' + str(last_err))


def fetch_via_invidious(query):
    vid = extract_video_id(query)
    last_err = None
    for inst in INVIDIOUS_INSTANCES:
        try:
            if not vid:
                s = http_get_json(inst + '/api/v1/search?q=' + urllib.parse.quote(query) + '&type=video')
                if not s:
                    raise RuntimeError('no results')
                vid_local = s[0]['videoId']
            else:
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
            }
        except Exception as e:
            last_err = e
            log.warning('invidious %s: %s', inst, e)
    raise RuntimeError('invidious failed: ' + str(last_err))


async def fetch_track(query):
    loop = asyncio.get_event_loop()

    def _run():
        errors = []
        for fn, name in [(fetch_via_ytdlp, 'ytdlp'),
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


def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = []
    return queues[guild_id]


def fmt_duration(seconds):
    if not seconds:
        return '?'
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return str(h) + ':' + str(m).zfill(2) + ':' + str(sec).zfill(2)
    return str(m) + ':' + str(sec).zfill(2)


def make_np_embed(track):
    embed = discord.Embed(
        title='🎵 กำลังเล่นเพลง',
        description='**[' + track['title'] + '](' + track['webpage_url'] + ')**',
        color=0x5865F2,
    )
    embed.add_field(name='⏱ ความยาว', value=fmt_duration(track['duration']), inline=True)
    embed.add_field(name='🎤 ช่อง', value=track['uploader'], inline=True)
    if track.get('thumbnail'):
        embed.set_thumbnail(url=track['thumbnail'])
    return embed


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


async def play_next(ctx):
    queue = get_queue(ctx.guild.id)
    if not queue:
        now_playing.pop(ctx.guild.id, None)
        return

    track = queue.pop(0)
    now_playing[ctx.guild.id] = track

    try:
        if not ctx.voice_client or not ctx.voice_client.is_connected():
            log.warning('no voice client when playing next; skipping')
            return
        source = discord.FFmpegPCMAudio(
            track['url'],
            before_options=FFMPEG_BEFORE,
            options=FFMPEG_OPTIONS,
        )
        ctx.voice_client.play(
            source,
            after=lambda err: (log.warning('after-play err: %s', err) if err else None,
                                asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)),
        )
        await ctx.send(embed=make_np_embed(track))
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
            await status.edit(content=None, embed=make_np_embed(track))
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
    await ctx.send(embed=make_np_embed(track))


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


@bot.command(name='clear', aliases=['cl'])
async def clear_queue(ctx):
    queues[ctx.guild.id] = []
    await ctx.send('🗑️ ล้างคิวแล้ว')


@bot.command(name='leave', aliases=['dc', 'disconnect'])
async def leave(ctx):
    if ctx.voice_client:
        queues[ctx.guild.id] = []
        now_playing.pop(ctx.guild.id, None)
        await ctx.voice_client.disconnect(force=True)
        await ctx.send('👋 ออกจาก voice channel แล้ว')
    else:
        await ctx.send('❌ บอทไม่ได้อยู่ใน voice channel')


@bot.command(name='stop')
async def stop(ctx):
    if ctx.voice_client:
        queues[ctx.guild.id] = []
        now_playing.pop(ctx.guild.id, None)
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
    embed.add_field(name='!np', value='ดูเพลงที่เล่นอยู่', inline=False)
    embed.add_field(name='!pause / !resume', value='หยุดชั่วคราว / เล่นต่อ', inline=False)
    embed.add_field(name='!reconnect (!rc)', value='เชื่อมต่อ voice ใหม่ถ้าหลุด', inline=False)
    embed.add_field(name='!clear', value='ล้างคิว', inline=False)
    embed.add_field(name='!leave  (!dc)', value='ออกจาก voice channel', inline=False)
    embed.add_field(name='!stop', value='หยุดเพลง + ออก voice channel', inline=False)
    await ctx.send(embed=embed)


TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
if not TOKEN:
    raise RuntimeError('DISCORD_BOT_TOKEN is not set!')
bot.run(TOKEN, log_handler=None)
