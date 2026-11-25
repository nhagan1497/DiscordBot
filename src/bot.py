import asyncio
import json
import logging
import os
import re
import signal
import time
from pathlib import Path

import discord
from aiohttp import BasicAuth, ClientError, web
from discord import app_commands

from admin import Admin
from common import PAIR_SPOTIFY, Problem, http_session

TRACK = re.compile(r'(?:spotify:track:|https://open\.spotify\.com/track/)([A-Za-z0-9]{22})(?:\?[^\s#]*)?')


def label(track):
    return f'{track["title"]} — {track["artist"]}' if track.get('artist') else track['title']


PLAYBACK_ACTIONS = {'pause': 'Pause sent to Spotify.', 'resume': 'Resume sent to Spotify.',
                    'skip': 'Skip sent to Spotify.'}
QUEUE_SOURCES = {'queue': 'Queued', 'context': 'Album or playlist', 'autoplay': 'Autoplay'}


class QueueSearch(discord.ui.Modal, title='Search Spotify'):
    def __init__(self, bot, voice):
        super().__init__(timeout=300)
        self.bot = bot
        self.voice = voice
        self.query = discord.ui.TextInput(max_length=160, placeholder='Enter a song title or artist')
        self.add_item(discord.ui.Label(text='Song title or artist', component=self.query))

    async def on_submit(self, interaction):
        await self.bot.music.show_search(interaction, self.query.value, voice=self.voice)

    async def on_error(self, interaction, error):
        await self.bot.report_error(interaction, error)


class QueueMenu(discord.ui.View):
    page_size = 5

    def __init__(self, bot, page):
        super().__init__(timeout=300)
        self.bot = bot
        self.voice = bot.voice
        self.page = page

    def render(self, playback, notice=''):
        queue = playback['queue']
        pages = max(1, (len(queue) + self.page_size - 1) // self.page_size)
        self.page = min(max(1, self.page), pages)
        self.previous.disabled = self.page == 1
        self.next_page.disabled = self.page == pages
        offset = (self.page - 1) * self.page_size
        lines = []
        for index, item in enumerate(queue[offset:offset + self.page_size], offset + 1):
            source = QUEUE_SOURCES.get(item.get('source'))
            name = discord.utils.escape_markdown(label(item))[:130]
            lines.append(f'{index}. {name}' + (f' · {source}' if source else ''))
        current = playback['current']
        title = discord.utils.escape_markdown(label(current))[:130] if current else 'No current track'
        status = playback['status'].capitalize()
        return '\n'.join(filter(None, [
            f'**{status}** · {title}', f'**Upcoming** · Page {self.page}/{pages}',
            '\n'.join(lines) or 'The queue is empty.', notice,
        ]))

    async def update(self, interaction, direction=0, action=None):
        await interaction.response.defer()
        try:
            async with self.bot.lock:
                host = self.bot.active_host(interaction.user, voice=self.voice)
                if action:
                    await self.bot.player(host, action, 'POST')
                playback = await self.bot.player(host, 'playback')
                self.page += direction
                await interaction.edit_original_response(
                    content=self.render(playback, PLAYBACK_ACTIONS.get(action, '')),
                    view=self, allowed_mentions=discord.AllowedMentions.none(),
                )
        except Problem as error:
            await interaction.followup.send(str(error), ephemeral=True)

    @discord.ui.button(label='Previous page')
    async def previous(self, interaction, _):
        await self.update(interaction, direction=-1)

    @discord.ui.button(label='Next page')
    async def next_page(self, interaction, _):
        await self.update(interaction, direction=1)

    @discord.ui.button(label='Refresh')
    async def refresh(self, interaction, _):
        await self.update(interaction)

    @discord.ui.button(label='Search', style=discord.ButtonStyle.primary)
    async def search(self, interaction, _):
        try:
            self.bot.active_host(interaction.user, voice=self.voice)
        except Problem as error:
            await self.bot.report_error(interaction, error)
        else:
            await interaction.response.send_modal(QueueSearch(self.bot, self.voice))

    @discord.ui.button(label='Pause', row=1)
    async def pause(self, interaction, _):
        await self.update(interaction, action='pause')

    @discord.ui.button(label='Resume', row=1)
    async def resume(self, interaction, _):
        await self.update(interaction, action='resume')

    @discord.ui.button(label='Skip', row=1)
    async def skip(self, interaction, _):
        await self.update(interaction, action='skip')


class Catalog:
    def __init__(self, http):
        self.http = http
        self.credentials = BasicAuth(os.environ['SPOTIFY_CLIENT_ID'], os.environ['SPOTIFY_CLIENT_SECRET'])
        self.token = None
        self.expires = 0
        self.lock = asyncio.Lock()

    async def request(self, path, **params):
        try:
            for _ in range(2):
                async with self.lock:
                    if not self.token or self.expires <= time.monotonic():
                        async with self.http.post('https://accounts.spotify.com/api/token', auth=self.credentials,
                                                  data={'grant_type': 'client_credentials'}) as response:
                            if not response.ok:
                                raise Problem('Spotify catalog sign-in failed. Check the catalog app credentials.')
                            token = await response.json()
                            self.token = token['access_token']
                            self.expires = time.monotonic() + token['expires_in'] - 30
                    access_token = self.token
                async with self.http.get(f'https://api.spotify.com/v1/{path}', params={'market': 'US', **params},
                                         headers={'Authorization': f'Bearer {access_token}'}) as response:
                    if response.status == 401:
                        if self.token == access_token:
                            self.token = None
                        continue
                    if not response.ok:
                        raise Problem('Spotify could not look up that track.', response.status)
                    return await response.json()
        except (ClientError, TimeoutError) as error:
            raise Problem('Spotify catalog is unreachable.') from error
        raise Problem('Spotify catalog authorization failed.')

    @staticmethod
    def track(item):
        return {'uri': item['uri'], 'title': item['name'],
                'artist': ', '.join(artist['name'] for artist in item['artists'])}

    async def search(self, query):
        result = await self.request('search', q=query, type='track', limit=5)
        return [self.track(item) for item in result['tracks']['items'] if item.get('is_playable', True)]

    async def get(self, track_id):
        item = await self.request(f'tracks/{track_id}')
        if not item.get('is_playable', True):
            raise Problem('This track is unavailable in the current market.', 404)
        return self.track(item)


class Bot(discord.Client):
    def __init__(self, http):
        super().__init__(intents=discord.Intents(guilds=True, voice_states=True))
        self.web_client = http
        self.catalog = Catalog(http)
        self.store = Path('/app/data/profiles.json')
        self.profiles = {}
        if self.store.exists():
            saved = json.loads(self.store.read_text(encoding='utf-8'))
            self.profiles = {str(item['id']): item['name'] for item in saved}
        self.host = None
        self.voice = None
        self.error = None
        self.guild = None
        self.lock = asyncio.Lock()
        self.tree = app_commands.CommandTree(self)
        self.tree.on_error = self.report_error
        self.music = Music(self)

    async def on_ready(self):
        if self.guild:
            return
        if len(self.guilds) != 1:
            logging.error('Install this bot in exactly one Discord server.')
            return
        guild = self.guilds[0]
        self.tree.add_command(self.music, guild=guild, override=True)
        try:
            await self.tree.sync(guild=guild)
        except discord.HTTPException:
            logging.exception('Could not register Discord commands')
        else:
            self.guild = guild
            logging.info('Discord commands registered.')

    async def report_error(self, interaction, error):
        original = getattr(error, 'original', error)
        if not isinstance(original, Problem):
            logging.error('Discord command failed', exc_info=original)
        message = str(original) if isinstance(original, Problem) else 'The bot could not complete that command.'
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    def enroll(self, member):
        identifier = str(member.id)
        name = member.display_name
        if self.profiles.get(identifier) != name:
            updated = {**self.profiles, identifier: name}
            temp = self.store.with_suffix('.tmp')
            temp.write_text(json.dumps([{'id': key, 'name': value} for key, value in updated.items()]), encoding='utf-8')
            temp.replace(self.store)
            self.profiles = updated
        return identifier

    async def player(self, member, action, method='GET', **params):
        try:
            async with self.web_client.request(method, f'http://player:3001/profiles/{member}/{action}', params=params) as response:
                result = await response.json()
                if not response.ok:
                    raise Problem(result.get('error', 'Soloist rejected the request.'), response.status)
                return result
        except (ClientError, TimeoutError, ValueError) as error:
            raise Problem('Could not reach the Soloist player.') from error

    def active_host(self, member, *, voice=None):
        if not self.host or not self.voice:
            raise Problem('Nobody is hosting yet. Join a voice channel and run `/music host` first.', 409)
        channel = getattr(getattr(member, 'voice', None), 'channel', None)
        if channel != self.voice.channel:
            raise Problem('Join the voice channel where the bot is playing to use this command.', 403)
        if voice is not None and self.voice is not voice:
            raise Problem('That session ended. Open a new queue or search menu.', 409)
        return self.host

    async def start_host(self, member):
        async with self.lock:
            identifier = self.enroll(member)
            if self.host:
                raise Problem('A voice session is already active.', 409)
            channel = getattr(getattr(member, 'voice', None), 'channel', None)
            if not channel:
                raise Problem('Join a voice channel, then run `/music host` again.', 403)
            self.host = identifier
            try:
                await self.player(identifier, 'host', 'POST')
                voice = self.voice = await channel.connect(timeout=12, self_deaf=True)
                loop = asyncio.get_running_loop()
                source = discord.FFmpegPCMAudio(
                    'soloist.monitor', before_options='-f pulse -server unix:/run/soloist-audio/native',
                )
                try:
                    voice.play(source, after=lambda error: loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(self.audio_ended(voice, error))))
                except Exception:
                    source.cleanup()
                    raise
                await self.player(identifier, 'resume', 'POST')
                self.error = None
            except (Exception, asyncio.CancelledError):
                await self.stop_host()
                raise

    async def audio_ended(self, voice, error):
        async with self.lock:
            if self.voice is voice:
                self.error = f'Discord audio ended{": " + str(error) if error else "."}'
                logging.warning('%s', self.error)
                await self.stop_host()

    async def stop_host(self):
        host, voice = self.host, self.voice
        self.host = self.voice = None
        try:
            if voice:
                await voice.disconnect(force=True)
        finally:
            if host:
                try:
                    await self.player(host, 'host', 'DELETE')
                except Problem as error:
                    logging.error('Could not stop Soloist: %s', error)

    async def on_voice_state_update(self, member, before, after):
        if before.channel == after.channel:
            return
        async with self.lock:
            if not self.voice or not self.host:
                return
            if (str(member.id) == self.host and after.channel != self.voice.channel) or (
                self.user and member.id == self.user.id and after.channel is None
            ):
                await self.stop_host()


class Music(app_commands.Group, name='music', description='Play and queue Spotify music in voice'):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    @app_commands.command(description='Create your profile or check its Spotify pairing')
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        member = self.bot.enroll(interaction.user)
        paired = (await self.bot.player(member, 'pairing'))['paired']
        await interaction.followup.send('Your Spotify profile is paired.' if paired else
                                        PAIR_SPOTIFY, ephemeral=True)

    @app_commands.command(description='Start a voice session with your Spotify profile')
    async def host(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.bot.start_host(interaction.user)
        await interaction.followup.send(f'Voice session started on Discord Music {interaction.user.id}. Use /music play to request a track.', ephemeral=True)

    async def add(self, interaction, track, *, voice=None):
        async with self.bot.lock:
            host = self.bot.active_host(interaction.user, voice=voice)
            result = await self.bot.player(host, 'queue', 'POST', track=track['uri'])
        action = 'Added to the Spotify queue' if result['queued'] else 'Sent play to Spotify'
        await interaction.followup.send(f'{action} · {label(track)}', ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(description='Play or queue a song by title, artist, or Spotify link')
    async def play(self, interaction: discord.Interaction, query: app_commands.Range[str, 1, 160]):
        await interaction.response.defer(ephemeral=True)
        self.bot.enroll(interaction.user)
        self.bot.active_host(interaction.user)
        query = query.strip()
        link = TRACK.fullmatch(query)
        if link:
            track = await self.bot.catalog.get(link[1])
        else:
            if not query or query.lower().startswith(('http:', 'https:', 'spotify:')):
                raise Problem('Enter a Spotify track link or a song title.', 400)
            tracks = await self.bot.catalog.search(query)
            if not tracks:
                raise Problem('No matching tracks found. Try adding the artist.', 404)
            track = tracks[0]
        await self.add(interaction, track)

    @app_commands.command(description='Find songs by title and artist')
    async def search(self, interaction: discord.Interaction, query: app_commands.Range[str, 1, 160]):
        await self.show_search(interaction, query)

    async def show_search(self, interaction, query, *, voice=None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.bot.enroll(interaction.user)
        self.bot.active_host(interaction.user, voice=voice)
        voice = self.bot.voice
        query = query.strip()
        if not query:
            raise Problem('Enter a song title or artist.', 400)
        tracks = await self.bot.catalog.search(query)
        if not tracks:
            raise Problem('No matching tracks found. Try adding the artist.', 404)
        select = discord.ui.Select(placeholder='Choose a track', options=[
            discord.SelectOption(label=item['title'][:100], description=item['artist'][:100], value=str(index))
            for index, item in enumerate(tracks)
        ])

        async def selected(selection):
            track = tracks[int(selection.data['values'][0])]
            await selection.response.defer(ephemeral=True, thinking=True)
            try:
                await self.add(selection, track, voice=voice)
            except Problem as error:
                await selection.followup.send(str(error), ephemeral=True)
            else:
                await interaction.edit_original_response(view=None)

        select.callback = selected
        view = discord.ui.View(timeout=300)
        view.add_item(select)
        await interaction.edit_original_response(content='Choose a track.', view=view)

    async def playback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        self.bot.enroll(interaction.user)
        return await self.bot.player(self.bot.active_host(interaction.user), 'playback')

    @app_commands.command(description='Show the current Spotify track')
    async def now(self, interaction: discord.Interaction):
        playback = await self.playback(interaction)
        text = label(playback['current']) if playback['current'] else 'Nothing is playing.'
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(description='Open the Spotify queue and playback controls')
    async def queue(self, interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1):
        await interaction.response.defer(ephemeral=True)
        self.bot.enroll(interaction.user)
        async with self.bot.lock:
            host = self.bot.active_host(interaction.user)
            playback = await self.bot.player(host, 'playback')
            menu = QueueMenu(self.bot, page)
        await interaction.edit_original_response(content=menu.render(playback), view=menu,
                                                 allowed_mentions=discord.AllowedMentions.none())

    async def control(self, interaction, action):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.lock:
            member = self.bot.enroll(interaction.user)
            host = self.bot.active_host(interaction.user)
            if action == 'stop':
                if host != member:
                    raise Problem('Only the active host can do that.', 403)
                await self.bot.stop_host()
            else:
                await self.bot.player(host, action, 'POST')
        await interaction.followup.send('Voice session ended.' if action == 'stop' else PLAYBACK_ACTIONS[action],
                                        ephemeral=True)

    @app_commands.command(description='Pause Spotify playback in your voice channel')
    async def pause(self, interaction: discord.Interaction):
        await self.control(interaction, 'pause')

    @app_commands.command(description='Resume Spotify playback in your voice channel')
    async def resume(self, interaction: discord.Interaction):
        await self.control(interaction, 'resume')

    @app_commands.command(description='Skip the Spotify track in your voice channel')
    async def skip(self, interaction: discord.Interaction):
        await self.control(interaction, 'skip')

    @app_commands.command(description='End the music session (host only)')
    async def stop(self, interaction: discord.Interaction):
        await self.control(interaction, 'stop')


async def main():
    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    async with await http_session() as http, Bot(http) as bot:
        admin = Admin(bot)
        runner = web.AppRunner(admin.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', 3000).start()
        logging.info('Administration listening on port 3000.')
        try:
            await bot.start(os.environ['DISCORD_TOKEN'])
        finally:
            await bot.stop_host()
            await admin.close()
            await runner.cleanup()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
