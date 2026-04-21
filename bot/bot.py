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
}

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

queues = {}

def is_url(text):
    return re.match(r'https?://', text.strip()) is not None

def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = []
    return queues[guild_id]

async def extract_info(query):
    loop = asyncio.get_event_loop()
    search_query = query.strip() if is_url(query) else f'ytsearch:{query}'

    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(search_query, download=False)
            if 'entries' in info:
                info = info['entries'][0]
            return info['url'], info.get('title', 'Unknown'), info.get('duration', 0)

    return await loop.run_in_executor(None, _extract)

async def play_next(ctx):
    queue = get_queue(ctx.guild.id)
    if queue:
        url, title, duration = queue.pop(0)
        source = discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS)
        ctx.voice_client.play(
            source,
            after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
        )
        mins, secs = divmod(int(duration), 60)
        dur_str = f'{mins}:{secs:02d}' if duration else '?'
        embed = discord.Embed(title='Now Playing', description=f'**{title}**', color=0x5865F2)
        embed.add_field(name='Duration', value=dur_str)
        await ctx.send(embed=embed)
    else:
        if ctx.voice_client:
            await ctx.send('Queue finished. Disconnecting...')
            await ctx.voice_client.disconnect()

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
    await ctx.send(f'Error: {error}')

@bot.command(name='play', aliases=['p'])
async def play(ctx, *, query):
    if not ctx.author.voice:
        await ctx.send('You need to join a voice channel first!')
        return

    voice_channel = ctx.author.voice.channel

    if ctx.voice_client is None:
        await voice_channel.connect()
    elif ctx.voice_client.channel != voice_channel:
        await ctx.voice_client.move_to(voice_channel)

    msg = await ctx.send(f'Searching: `{query}`...')

    try:
        url, title, duration = await extract_info(query)
    except Exception as e:
        await msg.edit(content=f'Could not find or play that.\nError: `{e}`')
        return

    queue = get_queue(ctx.guild.id)
    mins, secs = divmod(int(duration), 60)
    dur_str = f'{mins}:{secs:02d}' if duration else '?'

    if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
        queue.append((url, title, duration))
        embed = discord.Embed(title='Added to Queue', description=f'**{title}**', color=0x57F287)
        embed.add_field(name='Position in queue', value=str(len(queue)))
        embed.add_field(name='Duration', value=dur_str)
        await msg.edit(content=None, embed=embed)
    else:
        source = discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS)
        ctx.voice_client.play(
            source,
            after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
        )
        embed = discord.Embed(title='Now Playing', description=f'**{title}**', color=0x5865F2)
        embed.add_field(name='Duration', value=dur_str)
        await msg.edit(content=None, embed=embed)

@bot.command(name='skip', aliases=['s'])
async def skip(ctx):
    if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        ctx.voice_client.stop()
        await ctx.send('Skipped!')
    else:
        await ctx.send('Nothing is playing right now.')

@bot.command(name='queue', aliases=['q'])
async def show_queue(ctx):
    queue = get_queue(ctx.guild.id)
    if not queue:
        await ctx.send('Queue is empty.')
        return
    embed = discord.Embed(title=f'Queue ({len(queue)} songs)', color=0x5865F2)
    for i, (_, title, duration) in enumerate(queue[:10], 1):
        mins, secs = divmod(int(duration), 60)
        dur_str = f'{mins}:{secs:02d}' if duration else '?'
        embed.add_field(name=f'{i}. {title}', value=f'`{dur_str}`', inline=False)
    if len(queue) > 10:
        embed.set_footer(text=f'...and {len(queue) - 10} more songs')
    await ctx.send(embed=embed)

@bot.command(name='stop')
async def stop(ctx):
    if ctx.voice_client:
        queues[ctx.guild.id] = []
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
        await ctx.send('Stopped and left the voice channel.')
    else:
        await ctx.send('Not connected to a voice channel.')

@bot.command(name='pause')
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send('Paused.')
    else:
        await ctx.send('Nothing is playing.')

@bot.command(name='resume')
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send('Resumed.')
    else:
        await ctx.send('Nothing is paused.')

@bot.command(name='clear', aliases=['cl'])
async def clear_queue(ctx):
    queues[ctx.guild.id] = []
    await ctx.send('Queue cleared!')

@bot.command(name='np', aliases=['nowplaying'])
async def nowplaying(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        await ctx.send('A song is currently playing.')
    else:
        await ctx.send('Nothing is playing right now.')

@bot.command(name='commands', aliases=['h', 'help_music'])
async def show_commands(ctx):
    embed = discord.Embed(title='Music Bot Commands', color=0x5865F2)
    embed.add_field(name='!play <ชื่อเพลง หรือ YouTube URL>', value='เล่นเพลงหรือวาง YouTube link', inline=False)
    embed.add_field(name='!skip  (หรือ !s)', value='ข้ามเพลง', inline=False)
    embed.add_field(name='!queue  (หรือ !q)', value='ดูคิวเพลง', inline=False)
    embed.add_field(name='!pause', value='หยุดชั่วคราว', inline=False)
    embed.add_field(name='!resume', value='เล่นต่อ', inline=False)
    embed.add_field(name='!clear', value='ล้างคิวเพลง', inline=False)
    embed.add_field(name='!stop', value='หยุดและออกจาก voice channel', inline=False)
    embed.add_field(name='!np', value='แสดงเพลงที่กำลังเล่น', inline=False)
    await ctx.send(embed=embed)

TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
if not TOKEN:
    print('ERROR: DISCORD_BOT_TOKEN is not set!')
    exit(1)

bot.run(TOKEN)
