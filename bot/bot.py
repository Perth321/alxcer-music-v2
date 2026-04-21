import discord
from discord.ext import commands
import asyncio
import yt_dlp
import os

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
}

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents)

queues = {}

def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = []
    return queues[guild_id]

async def play_next(ctx):
    queue = get_queue(ctx.guild.id)
    if queue:
        url, title = queue.pop(0)
        source = discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS)
        ctx.voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))
        await ctx.send(f'Now playing: **{title}**')
    else:
        await ctx.send('Queue is empty.')

@bot.event
async def on_ready():
    print(f'Bot is ready: {bot.user}')

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

    await ctx.send(f'Searching for: **{query}**...')

    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        info = ydl.extract_info(f'ytsearch:{query}', download=False)
        if 'entries' in info:
            info = info['entries'][0]
        url = info['url']
        title = info.get('title', 'Unknown')

    queue = get_queue(ctx.guild.id)

    if ctx.voice_client.is_playing():
        queue.append((url, title))
        await ctx.send(f'Added to queue: **{title}** (Position: {len(queue)})')
    else:
        source = discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS)
        ctx.voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))
        await ctx.send(f'Now playing: **{title}**')

@bot.command(name='skip', aliases=['s'])
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
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
    msg = '**Queue:**\n'
    for i, (url, title) in enumerate(queue, 1):
        msg += f'{i}. {title}\n'
    await ctx.send(msg)

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
        await ctx.send('A song is currently playing.')
    else:
        await ctx.send('Nothing is playing right now.')

@bot.command(name='help_music', aliases=['commands'])
async def help_music(ctx):
    embed = discord.Embed(title='Music Bot Commands', color=discord.Color.blurple())
    embed.add_field(name='!play <song>', value='Play a song or add to queue', inline=False)
    embed.add_field(name='!skip', value='Skip current song', inline=False)
    embed.add_field(name='!queue', value='Show current queue', inline=False)
    embed.add_field(name='!pause', value='Pause the song', inline=False)
    embed.add_field(name='!resume', value='Resume the song', inline=False)
    embed.add_field(name='!stop', value='Stop and disconnect', inline=False)
    embed.add_field(name='!nowplaying', value='Show current song', inline=False)
    await ctx.send(embed=embed)

TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
if not TOKEN:
    print('ERROR: DISCORD_BOT_TOKEN is not set!')
    exit(1)

bot.run(TOKEN)
