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
    'cookiefile': None,
}

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents)

queues = {}

def is_url(text):
    return re.match(r'https?://', text) is not None

def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = []
    return queues[guild_id]

async def extract_info(query):
    ydl_opts = dict(YDL_OPTIONS)
    loop = asyncio.get_event_loop()

    if is_url(query):
        search_query = query
    else:
        search_query = f'ytsearch:{query}'

    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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
        mins, secs = divmod(duration, 60)
        dur_str = f'{mins}:{secs:02d}' if duration else 'Unknown'
        embed = discord.Embed(title='Now Playing', description=f'**{title}**', color=discord.Color.blurple())
        embed.add_field(name='Duration', value=dur_str)
        await ctx.send(embed=embed)
    else:
        await ctx.send('Queue is empty.')

@bot.event
async def on_ready():
    print(f'Bot is ready: {bot.user}')
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name='!play')
    )

@bot.command(name='play', aliases=['p'])
async def play(ctx, *, query):
    if not ctx.author.voice:
        await ctx.send('You need to be in a voice channel first!')
        return

    voice_channel = ctx.author.voice.channel

    if ctx.voice_client is None:
        await voice_channel.connect()
    elif ctx.voice_client.channel != voice_channel:
        await ctx.voice_client.move_to(voice_channel)

    msg = await ctx.send(f'Searching: **{query}**...')

    try:
        url, title, duration = await extract_info(query)
    except Exception as e:
        await msg.edit(content=f'Error: Could not find or play that. `{e}`')
        return

    queue = get_queue(ctx.guild.id)
    mins, secs = divmod(duration, 60)
    dur_str = f'{mins}:{secs:02d}' if duration else 'Unknown'

    if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
        queue.append((url, title, duration))
        embed = discord.Embed(title='Added to Queue', description=f'**{title}**', color=discord.Color.green())
        embed.add_field(name='Position', value=str(len(queue)))
        embed.add_field(name='Duration', value=dur_str)
        await msg.edit(content=None, embed=embed)
    else:
        source = discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS)
        ctx.voice_client.play(
            source,
            after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
        )
        embed = discord.Embed(title='Now Playing', description=f'**{title}**', color=discord.Color.blurple())
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
    embed = discord.Embed(title='Queue', color=discord.Color.blurple())
    for i, (url, title, duration) in enumerate(queue[:10], 1):
        mins, secs = divmod(duration, 60)
        dur_str = f'{mins}:{secs:02d}' if duration else '?'
        embed.add_field(name=f'{i}. {title}', value=f'Duration: {dur_str}', inline=False)
    if len(queue) > 10:
        embed.set_footer(text=f'...and {len(queue) - 10} more')
    await ctx.send(embed=embed)

@bot.command(name='stop')
async def stop(ctx):
    if ctx.voice_client:
        queues[ctx.guild.id] = []
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
        await ctx.send('Stopped and disconnected.')
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

@bot.command(name='nowplaying', aliases=['np'])
async def nowplaying(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        await ctx.send('A song is currently playing. Use `!queue` to see the queue.')
    else:
        await ctx.send('Nothing is playing right now.')

@bot.command(name='clear', aliases=['cl'])
async def clear_queue(ctx):
    queues[ctx.guild.id] = []
    await ctx.send('Queue cleared!')

@bot.command(name='help_music', aliases=['commands', 'h'])
async def help_music(ctx):
    embed = discord.Embed(title='Music Bot Commands', color=discord.Color.blurple())
    embed.add_field(name='!play <song or YouTube URL>', value='Play a song by name or paste a YouTube link', inline=False)
    embed.add_field(name='!skip  (!s)', value='Skip current song', inline=False)
    embed.add_field(name='!queue  (!q)', value='Show current queue', inline=False)
    embed.add_field(name='!pause', value='Pause the song', inline=False)
    embed.add_field(name='!resume', value='Resume the song', inline=False)
    embed.add_field(name='!stop', value='Stop and disconnect', inline=False)
    embed.add_field(name='!clear', value='Clear the queue', inline=False)
    embed.add_field(name='!nowplaying  (!np)', value='Show current song info', inline=False)
    await ctx.send(embed=embed)

TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
if not TOKEN:
    print('ERROR: DISCORD_BOT_TOKEN is not set!')
    exit(1)

bot.run(TOKEN)
