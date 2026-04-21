import discord
from discord.ext import commands
import asyncio
import yt_dlp
import os
import re

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

COOKIES_FILE = os.path.join(os.path.dirname(__file__), 'cookies.txt')

# Multiple extractor client fallbacks — first that works wins
YT_CLIENT_FALLBACKS = [
    ['ios'],
    ['android_vr'],
    ['mweb'],
    ['tv_embedded'],
    ['web_safari'],
    ['android'],
]


def make_ydl_opts(client):
    opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'source_address': '0.0.0.0',
        'geo_bypass': True,
        'nocheckcertificate': True,
        'extractor_args': {
            'youtube': {
                'player_client': client,
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
        },
    }
    if os.path.exists(COOKIES_FILE):
        opts['cookiefile'] = COOKIES_FILE
    return opts


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

queues = {}
now_playing = {}


def is_url(text: str) -> bool:
    return bool(re.match(r'https?://', text.strip()))


def get_queue(guild_id: int) -> list:
    if guild_id not in queues:
        queues[guild_id] = []
    return queues[guild_id]


def fmt_duration(seconds) -> str:
    if not seconds:
        return '?'
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f'{h}:{m:02d}:{sec:02d}'
    return f'{m}:{sec:02d}'


async def fetch_track(query: str) -> dict:
    loop = asyncio.get_event_loop()
    search = query.strip() if is_url(query) else f'ytsearch1:{query}'

    def _run():
        last_err = None
        for client in YT_CLIENT_FALLBACKS:
            try:
                with yt_dlp.YoutubeDL(make_ydl_opts(client)) as ydl:
                    data = ydl.extract_info(search, download=False)
                    if 'entries' in data:
                        if not data['entries']:
                            raise RuntimeError('No results')
                        data = data['entries'][0]
                    if not data.get('url'):
                        raise RuntimeError('No stream URL')
                    print(f'[OK] extracted via client={client}')
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
                print(f'[WARN] client={client} failed: {e}')
                continue
        raise RuntimeError(f'All YouTube clients failed. Last error: {last_err}')

    return await loop.run_in_executor(None, _run)


def make_np_embed(track: dict) -> discord.Embed:
    embed = discord.Embed(
        title='🎵 กำลังเล่นเพลง',
        description=f'**[{track["title"]}]({track["webpage_url"]})**',
        color=0x5865F2,
    )
    embed.add_field(name='⏱ ความยาว', value=fmt_duration(track['duration']), inline=True)
    embed.add_field(name='🎤 ช่อง', value=track['uploader'], inline=True)
    if track.get('thumbnail'):
        embed.set_thumbnail(url=track['thumbnail'])
    return embed


async def play_next(ctx: commands.Context):
    queue = get_queue(ctx.guild.id)
    if not queue:
        now_playing.pop(ctx.guild.id, None)
        if ctx.voice_client and ctx.voice_client.is_connected():
            await ctx.send('✅ เล่นครบทุกเพลงแล้ว')
        return

    track = queue.pop(0)
    now_playing[ctx.guild.id] = track

    try:
        source = discord.FFmpegPCMAudio(track['url'], **FFMPEG_OPTIONS)
        ctx.voice_client.play(
            source,
            after=lambda _: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop),
        )
        await ctx.send(embed=make_np_embed(track))
    except Exception as e:
        await ctx.send(f'⚠️ เล่นไม่ได้: `{e}`\nข้ามเพลงถัดไป...')
        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)


@bot.event
async def on_ready():
    print(f'[OK] {bot.user} online (ID: {bot.user.id})')
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name='!play')
    )


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send('⚠️ ใส่ชื่อเพลงหรือ link ด้วย เช่น `!play จี๋หอย`')
        return
    await ctx.send(f'⚠️ Error: `{error}`')


@bot.command(name='play', aliases=['p'])
async def play(ctx: commands.Context, *, query: str):
    if not ctx.author.voice:
        await ctx.send('❌ เข้า voice channel ก่อนนะ!')
        return

    vc = ctx.voice_client
    if vc is None:
        vc = await ctx.author.voice.channel.connect()
    elif vc.channel != ctx.author.voice.channel:
        await vc.move_to(ctx.author.voice.channel)

    status = await ctx.send(f'🔍 กำลังค้นหา: `{query}`...')

    try:
        track = await fetch_track(query)
    except Exception as e:
        await status.edit(content=f'❌ ไม่พบเพลงนั้น\n`{e}`')
        return

    queue = get_queue(ctx.guild.id)

    if vc.is_playing() or vc.is_paused():
        queue.append(track)
        embed = discord.Embed(
            title='✅ เพิ่มเข้าคิวแล้ว',
            description=f'**[{track["title"]}]({track["webpage_url"]})**',
            color=0x57F287,
        )
        embed.add_field(name='ลำดับในคิว', value=str(len(queue)), inline=True)
        embed.add_field(name='⏱ ความยาว', value=fmt_duration(track['duration']), inline=True)
        if track.get('thumbnail'):
            embed.set_thumbnail(url=track['thumbnail'])
        await status.edit(content=None, embed=embed)
    else:
        now_playing[ctx.guild.id] = track
        source = discord.FFmpegPCMAudio(track['url'], **FFMPEG_OPTIONS)
        vc.play(
            source,
            after=lambda _: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop),
        )
        await status.edit(content=None, embed=make_np_embed(track))


@bot.command(name='skip', aliases=['s'])
async def skip(ctx: commands.Context):
    if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        ctx.voice_client.stop()
        await ctx.send('⏭️ ข้ามเพลงแล้ว')
    else:
        await ctx.send('❌ ไม่มีเพลงเล่นอยู่')


@bot.command(name='queue', aliases=['q'])
async def show_queue(ctx: commands.Context):
    queue = get_queue(ctx.guild.id)
    np = now_playing.get(ctx.guild.id)
    embed = discord.Embed(title='📋 คิวเพลง', color=0x5865F2)

    if np:
        embed.add_field(
            name='🎵 กำลังเล่น',
            value=f'**{np["title"]}**  `{fmt_duration(np["duration"])}`',
            inline=False,
        )
    if queue:
        lines = []
        for i, t in enumerate(queue[:10], 1):
            lines.append(f'`{i}.` **{t["title"]}**  `{fmt_duration(t["duration"])}`')
        if len(queue) > 10:
            lines.append(f'_...และอีก {len(queue) - 10} เพลง_')
        embed.add_field(name=f'รอในคิว ({len(queue)} เพลง)', value='\n'.join(lines), inline=False)
    elif not np:
        embed.description = 'คิวว่างเปล่า'

    await ctx.send(embed=embed)


@bot.command(name='np', aliases=['nowplaying'])
async def nowplaying(ctx: commands.Context):
    track = now_playing.get(ctx.guild.id)
    if not track:
        await ctx.send('❌ ไม่มีเพลงเล่นอยู่')
        return
    await ctx.send(embed=make_np_embed(track))


@bot.command(name='pause')
async def pause(ctx: commands.Context):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send('⏸️ หยุดชั่วคราว')
    else:
        await ctx.send('❌ ไม่มีเพลงเล่นอยู่')


@bot.command(name='resume')
async def resume(ctx: commands.Context):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send('▶️ เล่นต่อแล้ว')
    else:
        await ctx.send('❌ ไม่มีเพลงที่หยุดไว้')


@bot.command(name='clear', aliases=['cl'])
async def clear_queue(ctx: commands.Context):
    queues[ctx.guild.id] = []
    await ctx.send('🗑️ ล้างคิวแล้ว')


@bot.command(name='leave', aliases=['dc', 'disconnect'])
async def leave(ctx: commands.Context):
    if ctx.voice_client:
        queues[ctx.guild.id] = []
        now_playing.pop(ctx.guild.id, None)
        await ctx.voice_client.disconnect()
        await ctx.send('👋 ออกจาก voice channel แล้ว')
    else:
        await ctx.send('❌ บอทไม่ได้อยู่ใน voice channel')


@bot.command(name='stop')
async def stop(ctx: commands.Context):
    if ctx.voice_client:
        queues[ctx.guild.id] = []
        now_playing.pop(ctx.guild.id, None)
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
        await ctx.send('⏹️ หยุดและออกจาก voice channel แล้ว')
    else:
        await ctx.send('❌ บอทไม่ได้อยู่ใน voice channel')


@bot.command(name='help', aliases=['h', 'commands'])
async def help_cmd(ctx: commands.Context):
    embed = discord.Embed(title='🎵 คำสั่งทั้งหมด', color=0x5865F2)
    embed.add_field(name='!play <ชื่อเพลง หรือ link>', value='เล่นเพลง / เพิ่มเข้าคิว', inline=False)
    embed.add_field(name='!skip  (!s)', value='ข้ามเพลง', inline=False)
    embed.add_field(name='!queue  (!q)', value='ดูคิวเพลง', inline=False)
    embed.add_field(name='!np', value='ดูเพลงที่เล่นอยู่', inline=False)
    embed.add_field(name='!pause / !resume', value='หยุดชั่วคราว / เล่นต่อ', inline=False)
    embed.add_field(name='!clear', value='ล้างคิว', inline=False)
    embed.add_field(name='!leave  (!dc)', value='ออกจาก voice channel', inline=False)
    embed.add_field(name='!stop', value='หยุดเพลง + ออก voice channel', inline=False)
    await ctx.send(embed=embed)


TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
if not TOKEN:
    raise RuntimeError('DISCORD_BOT_TOKEN is not set!')

bot.run(TOKEN)
