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

# iOS client bypasses YouTube bot detection on server environments
YDL_OPTIONS = {
    'format': 'bestaudio[ext=m4a]/bestaudio/best',
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
    'source_address': '0.0.0.0',
    'extractor_args': {
        'youtube': {
            'player_client': ['ios'],
        }
    },
}

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
    """Extract track info using yt-dlp. Works with both URLs and search terms."""
    loop = asyncio.get_event_loop()
    search = query.strip() if is_url(query) else f'ytsearch1:{query}'

    def _run():
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            data = ydl.extract_info(search, download=False)
            # ytsearch returns a list under 'entries'
            if 'entries' in data:
                data = data['entries'][0]
            return {
                'url': data['url'],
                'title': data.get('title', 'Unknown'),
                'duration': data.get('duration', 0),
                'thumbnail': data.get('thumbnail'),
                'webpage_url': data.get('webpage_url', ''),
                'uploader': data.get('uploader', 'Unknown'),
            }

    return await loop.run_in_executor(None, _run)


def make_now_playing_embed(track: dict) -> discord.Embed:
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
    """Play the next track in queue. Called automatically after each song ends."""
    queue = get_queue(ctx.guild.id)
    if not queue:
        now_playing.pop(ctx.guild.id, None)
        if ctx.voice_client:
            await ctx.send('✅ เล่นเพลงครบทุกเพลงแล้ว')
        return

    track = queue.pop(0)
    now_playing[ctx.guild.id] = track

    try:
        source = discord.FFmpegPCMAudio(track['url'], **FFMPEG_OPTIONS)
        ctx.voice_client.play(
            source,
            after=lambda _: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop),
        )
        # Auto-show now playing embed every time a song starts
        await ctx.send(embed=make_now_playing_embed(track))
    except Exception as e:
        await ctx.send(f'⚠️ เล่นเพลงไม่ได้: `{e}`\nข้ามไปเพลงถัดไป...')
        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)


# ─── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f'[OK] Bot online: {bot.user}  (ID: {bot.user.id})')
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name='!play')
    )


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f'⚠️ ขาด argument: `{error.param.name}`')
        return
    await ctx.send(f'⚠️ เกิดข้อผิดพลาด: `{error}`')


# ─── Commands ──────────────────────────────────────────────────────────────────

@bot.command(name='play', aliases=['p'])
async def play(ctx: commands.Context, *, query: str):
    """เล่นเพลงจากชื่อเพลงหรือ YouTube link"""
    if not ctx.author.voice:
        await ctx.send('❌ คุณต้องเข้า voice channel ก่อน!')
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
        # Add to queue
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
        # Play immediately — show now playing embed automatically
        now_playing[ctx.guild.id] = track
        source = discord.FFmpegPCMAudio(track['url'], **FFMPEG_OPTIONS)
        vc.play(
            source,
            after=lambda _: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop),
        )
        await status.edit(content=None, embed=make_now_playing_embed(track))


@bot.command(name='skip', aliases=['s'])
async def skip(ctx: commands.Context):
    """ข้ามเพลงปัจจุบัน"""
    if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        ctx.voice_client.stop()
        await ctx.send('⏭️ ข้ามเพลงแล้ว')
    else:
        await ctx.send('❌ ไม่มีเพลงเล่นอยู่ตอนนี้')


@bot.command(name='queue', aliases=['q'])
async def show_queue(ctx: commands.Context):
    """ดูคิวเพลงทั้งหมด"""
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
    """แสดงเพลงที่กำลังเล่นอยู่"""
    track = now_playing.get(ctx.guild.id)
    if not track:
        await ctx.send('❌ ไม่มีเพลงเล่นอยู่ตอนนี้')
        return
    await ctx.send(embed=make_now_playing_embed(track))


@bot.command(name='pause')
async def pause(ctx: commands.Context):
    """หยุดเพลงชั่วคราว"""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send('⏸️ หยุดชั่วคราว')
    else:
        await ctx.send('❌ ไม่มีเพลงเล่นอยู่')


@bot.command(name='resume')
async def resume(ctx: commands.Context):
    """เล่นเพลงต่อ"""
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send('▶️ เล่นต่อแล้ว')
    else:
        await ctx.send('❌ ไม่มีเพลงที่หยุดไว้')


@bot.command(name='clear', aliases=['cl'])
async def clear_queue(ctx: commands.Context):
    """ล้างคิวเพลงทั้งหมด"""
    queues[ctx.guild.id] = []
    await ctx.send('🗑️ ล้างคิวเพลงแล้ว')


@bot.command(name='leave', aliases=['dc', 'disconnect'])
async def leave(ctx: commands.Context):
    """บอทออกจาก voice channel"""
    if ctx.voice_client:
        queues[ctx.guild.id] = []
        now_playing.pop(ctx.guild.id, None)
        await ctx.voice_client.disconnect()
        await ctx.send('👋 ออกจาก voice channel แล้ว')
    else:
        await ctx.send('❌ บอทไม่ได้อยู่ใน voice channel')


@bot.command(name='stop')
async def stop(ctx: commands.Context):
    """หยุดเพลงและออกจาก voice channel"""
    if ctx.voice_client:
        queues[ctx.guild.id] = []
        now_playing.pop(ctx.guild.id, None)
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
        await ctx.send('⏹️ หยุดเพลงและออกจาก voice channel แล้ว')
    else:
        await ctx.send('❌ บอทไม่ได้อยู่ใน voice channel')


@bot.command(name='help', aliases=['h', 'commands'])
async def help_cmd(ctx: commands.Context):
    """แสดงคำสั่งทั้งหมด"""
    embed = discord.Embed(title='🎵 คำสั่ง Music Bot', color=0x5865F2)
    cmds = [
        ('!play <ชื่อเพลง หรือ YouTube link>', 'เล่นเพลง — ใส่ชื่อเพลงหรือวาง link ก็ได้'),
        ('!skip  (!s)', 'ข้ามเพลงปัจจุบัน'),
        ('!queue  (!q)', 'ดูคิวเพลงทั้งหมด + เพลงที่กำลังเล่น'),
        ('!np', 'ดูเพลงที่กำลังเล่นอยู่'),
        ('!pause / !resume', 'หยุดชั่วคราว / เล่นต่อ'),
        ('!clear', 'ล้างคิวเพลง'),
        ('!leave  (!dc)', 'บอทออกจาก voice channel'),
        ('!stop', 'หยุดเพลง + ออก voice channel'),
    ]
    for name, value in cmds:
        embed.add_field(name=name, value=value, inline=False)
    await ctx.send(embed=embed)


# ─── Run ───────────────────────────────────────────────────────────────────────

TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
if not TOKEN:
    raise RuntimeError('DISCORD_BOT_TOKEN is not set!')

bot.run(TOKEN)
