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

YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'quiet': True,
    'no_warnings': True,
    'noplaylist': False,
    'source_address': '0.0.0.0',
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'web'],
        }
    },
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.91 Mobile Safari/537.36'
    }
}

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

queues = {}
now_playing = {}

def is_url(text):
    return re.match(r'https?://', text.strip()) is not None

def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = []
    return queues[guild_id]

def fmt_duration(duration):
    if not duration:
        return '?'
    mins, secs = divmod(int(duration), 60)
    hrs, mins = divmod(mins, 60)
    if hrs:
        return f'{hrs}:{mins:02d}:{secs:02d}'
    return f'{mins}:{secs:02d}'

async def extract_info(query):
    loop = asyncio.get_event_loop()
    search_query = query.strip() if is_url(query) else f'ytsearch:{query}'

    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(search_query, download=False)
            if 'entries' in info:
                info = info['entries'][0]
            return {
                'url': info['url'],
                'title': info.get('title', 'Unknown'),
                'duration': info.get('duration', 0),
                'thumbnail': info.get('thumbnail', None),
                'webpage_url': info.get('webpage_url', ''),
                'uploader': info.get('uploader', 'Unknown'),
            }

    return await loop.run_in_executor(None, _extract)

async def play_next(ctx):
    queue = get_queue(ctx.guild.id)
    if queue:
        track = queue.pop(0)
        now_playing[ctx.guild.id] = track
        source = discord.FFmpegPCMAudio(track['url'], **FFMPEG_OPTIONS)
        ctx.voice_client.play(
            source,
            after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
        )
        embed = discord.Embed(
            title='🎵 กำลังเล่นเพลง',
            description=f'**[{track["title"]}]({track["webpage_url"]})**',
            color=0x5865F2
        )
        embed.add_field(name='ความยาว', value=fmt_duration(track['duration']), inline=True)
        embed.add_field(name='ช่อง', value=track['uploader'], inline=True)
        if track['thumbnail']:
            embed.set_thumbnail(url=track['thumbnail'])
        await ctx.send(embed=embed)
    else:
        now_playing.pop(ctx.guild.id, None)
        await ctx.send('คิวเพลงหมดแล้ว')

@bot.event
async def on_ready():
    print(f'[OK] Bot online: {bot.user} (ID: {bot.user.id})')
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name='!play')
    )

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    await ctx.send(f'เกิดข้อผิดพลาด: {error}')

@bot.command(name='play', aliases=['p'])
async def play(ctx, *, query):
    if not ctx.author.voice:
        await ctx.send('คุณต้องเข้า voice channel ก่อน!')
        return

    voice_channel = ctx.author.voice.channel

    if ctx.voice_client is None:
        await voice_channel.connect()
    elif ctx.voice_client.channel != voice_channel:
        await ctx.voice_client.move_to(voice_channel)

    msg = await ctx.send(f'🔍 กำลังค้นหา: `{query}`...')

    try:
        track = await extract_info(query)
    except Exception as e:
        await msg.edit(content=f'ไม่พบเพลงนั้น\nError: `{e}`')
        return

    queue = get_queue(ctx.guild.id)

    if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
        queue.append(track)
        embed = discord.Embed(
            title='✅ เพิ่มเข้าคิวแล้ว',
            description=f'**[{track["title"]}]({track["webpage_url"]})**',
            color=0x57F287
        )
        embed.add_field(name='ลำดับในคิว', value=str(len(queue)), inline=True)
        embed.add_field(name='ความยาว', value=fmt_duration(track['duration']), inline=True)
        if track['thumbnail']:
            embed.set_thumbnail(url=track['thumbnail'])
        await msg.edit(content=None, embed=embed)
    else:
        now_playing[ctx.guild.id] = track
        source = discord.FFmpegPCMAudio(track['url'], **FFMPEG_OPTIONS)
        ctx.voice_client.play(
            source,
            after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
        )
        embed = discord.Embed(
            title='🎵 กำลังเล่นเพลง',
            description=f'**[{track["title"]}]({track["webpage_url"]})**',
            color=0x5865F2
        )
        embed.add_field(name='ความยาว', value=fmt_duration(track['duration']), inline=True)
        embed.add_field(name='ช่อง', value=track['uploader'], inline=True)
        if track['thumbnail']:
            embed.set_thumbnail(url=track['thumbnail'])
        await msg.edit(content=None, embed=embed)

@bot.command(name='skip', aliases=['s'])
async def skip(ctx):
    if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        ctx.voice_client.stop()
        await ctx.send('⏭️ ข้ามเพลงแล้ว')
    else:
        await ctx.send('ไม่มีเพลงเล่นอยู่ตอนนี้')

@bot.command(name='queue', aliases=['q'])
async def show_queue(ctx):
    queue = get_queue(ctx.guild.id)
    np = now_playing.get(ctx.guild.id)

    embed = discord.Embed(title='📋 คิวเพลง', color=0x5865F2)

    if np:
        embed.add_field(
            name='🎵 กำลังเล่น',
            value=f'**{np["title"]}** `{fmt_duration(np["duration"])}`',
            inline=False
        )

    if not queue:
        embed.add_field(name='คิว', value='ว่างเปล่า', inline=False)
    else:
        queue_text = ''
        for i, track in enumerate(queue[:10], 1):
            queue_text += f'`{i}.` **{track["title"]}** `{fmt_duration(track["duration"])}`\n'
        if len(queue) > 10:
            queue_text += f'\n...และอีก {len(queue) - 10} เพลง'
        embed.add_field(name=f'รอในคิว ({len(queue)} เพลง)', value=queue_text, inline=False)

    await ctx.send(embed=embed)

@bot.command(name='np', aliases=['nowplaying'])
async def nowplaying(ctx):
    track = now_playing.get(ctx.guild.id)
    if not track:
        await ctx.send('ไม่มีเพลงเล่นอยู่ตอนนี้')
        return
    embed = discord.Embed(
        title='🎵 กำลังเล่นอยู่',
        description=f'**[{track["title"]}]({track["webpage_url"]})**',
        color=0x5865F2
    )
    embed.add_field(name='ความยาว', value=fmt_duration(track['duration']), inline=True)
    embed.add_field(name='ช่อง', value=track['uploader'], inline=True)
    if track['thumbnail']:
        embed.set_thumbnail(url=track['thumbnail'])
    await ctx.send(embed=embed)

@bot.command(name='stop')
async def stop(ctx):
    if ctx.voice_client:
        queues[ctx.guild.id] = []
        now_playing.pop(ctx.guild.id, None)
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
        await ctx.send('⏹️ หยุดเพลงและออกจาก voice channel แล้ว')
    else:
        await ctx.send('บอทไม่ได้อยู่ใน voice channel')

@bot.command(name='leave', aliases=['dc', 'disconnect'])
async def leave(ctx):
    if ctx.voice_client:
        queues[ctx.guild.id] = []
        now_playing.pop(ctx.guild.id, None)
        await ctx.voice_client.disconnect()
        await ctx.send('👋 ออกจาก voice channel แล้ว')
    else:
        await ctx.send('บอทไม่ได้อยู่ใน voice channel')

@bot.command(name='pause')
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send('⏸️ หยุดชั่วคราว')
    else:
        await ctx.send('ไม่มีเพลงเล่นอยู่')

@bot.command(name='resume')
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send('▶️ เล่นต่อแล้ว')
    else:
        await ctx.send('ไม่มีเพลงที่หยุดไว้')

@bot.command(name='clear', aliases=['cl'])
async def clear_queue(ctx):
    queues[ctx.guild.id] = []
    await ctx.send('🗑️ ล้างคิวเพลงแล้ว')

@bot.command(name='commands', aliases=['h'])
async def show_commands(ctx):
    embed = discord.Embed(title='🎵 คำสั่ง Music Bot', color=0x5865F2)
    embed.add_field(name='!play <ชื่อเพลง หรือ YouTube link>', value='เล่นเพลงหรือเพิ่มเข้าคิว', inline=False)
    embed.add_field(name='!np', value='ดูเพลงที่กำลังเล่นอยู่', inline=False)
    embed.add_field(name='!queue (!q)', value='ดูคิวเพลงทั้งหมด', inline=False)
    embed.add_field(name='!skip (!s)', value='ข้ามเพลง', inline=False)
    embed.add_field(name='!pause / !resume', value='หยุดชั่วคราว / เล่นต่อ', inline=False)
    embed.add_field(name='!clear', value='ล้างคิวเพลง', inline=False)
    embed.add_field(name='!leave (!dc)', value='บอทออกจาก voice channel', inline=False)
    embed.add_field(name='!stop', value='หยุดเพลงและออกจาก voice channel', inline=False)
    await ctx.send(embed=embed)

TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
if not TOKEN:
    print('ERROR: DISCORD_BOT_TOKEN is not set!')
    exit(1)

bot.run(TOKEN)
