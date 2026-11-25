import asyncio
import logging
import os
import re
from contextlib import suppress
from pathlib import Path

from aiohttp import ClientError, WSMsgType, web

from common import PAIR_SPOTIFY, PAIRING_URL, Problem, http_session, service_app, stop_process

SOCKET = 'ws://127.0.0.1:9090'
TRACK = re.compile(r'spotify:track:[a-zA-Z0-9]{22}')


def song(item):
    if not item or not item.get('uri'):
        return None
    details = item.get('decorations', {})
    artists = [creator['entity'].get('decorations', {}).get('identity', {}).get('name')
               for creator in details.get('creators', []) if creator.get('entity')]
    return {'uri': item['uri'], 'title': details.get('identity', {}).get('name') or item['uri'],
            'artist': ', '.join(filter(None, artists))}


async def event(socket, kind, command=None, timeout=4):
    async with asyncio.timeout(timeout):
        while True:
            message = await socket.receive()
            if message.type != WSMsgType.TEXT:
                raise Problem('Soloist disconnected.')
            data = message.json()
            if data['type'] == 'error':
                logging.warning('Soloist rejected a command: %s', data.get('message'))
                raise Problem('Soloist rejected the playback command.')
            if data['type'] == kind and (command is None or data.get('command') == command):
                return data


async def command(socket, name, **fields):
    await socket.send_json({'type': 'command', 'command': name, **fields})
    await event(socket, 'command_result', name)


async def activate(socket, auth):
    if auth.get('is_active'):
        return
    await command(socket, 'activate')
    async with asyncio.timeout(8):
        while True:
            await socket.send_json({'type': 'command', 'command': 'get_auth_state'})
            auth = await event(socket, 'auth_state')
            if not auth.get('logged_in'):
                raise Problem('Soloist signed out while switching playback devices.', 409)
            if auth.get('is_active'):
                return
            await asyncio.sleep(0.2)


async def upcoming(socket, limit=0):
    await socket.send_json({'type': 'command', 'command': 'get_queue', 'limit': limit})
    return [{**track, 'source': entry.get('source')} for entry in (await event(socket, 'queue_changed')).get('upcoming', [])
            if (track := song(entry.get('item')))]


class Player:
    def __init__(self):
        self.data = Path('/app/data')
        self.key = os.environ['SOLOIST_API_KEY']
        self.lock = asyncio.Lock()
        self.pairings = {}
        self.host = None
        self.daemon = None

    async def spawn(self, member, *options):
        root = self.data / member
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return await asyncio.create_subprocess_exec(
            'soloist', '--device-name', f'Discord Music {member}', '--api-key', self.key,
            '--data-dir', str(root / 'connect'), '--cache-dir', str(root / 'cache'), *options,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        )

    async def finish_pairing(self, process, marker):
        try:
            if await asyncio.wait_for(process.wait(), 600) == 0:
                marker.touch(mode=0o600)
                return 'paired'
            logging.warning('Soloist pairing exited with code %s', process.returncode)
        except TimeoutError:
            await stop_process(process)
        except OSError:
            logging.exception('Could not save the pairing marker')
        return 'failed'

    async def pairing(self, request):
        member = request.match_info['member']
        marker = self.data / member / 'soloist-paired'
        async with self.lock:
            entry = self.pairings.get(member)
            if request.method == 'POST':
                if self.host == member or (entry and not entry[1].done()):
                    raise Problem('Stop hosting or finish the current pairing first.', 409)
                process = await self.spawn(member, '--pair')
                entry = process, asyncio.create_task(self.finish_pairing(process, marker))
                self.pairings[member] = entry
            elif request.method == 'DELETE' and entry:
                self.pairings.pop(member)
                if not entry[1].done():
                    await stop_process(entry[0])
                    await entry[1]
                entry = None
            status = 'idle' if entry is None else entry[1].result() if entry[1].done() else 'waiting'
            return web.json_response({'paired': marker.exists(), 'status': status})

    async def stop_host(self):
        await stop_process(self.daemon)
        self.host = self.daemon = None

    async def hosting(self, request):
        member = request.match_info['member']
        async with self.lock:
            if request.method == 'DELETE':
                if self.host == member:
                    await self.stop_host()
                return web.json_response({})
            if not (self.data / member / 'soloist-paired').exists():
                raise Problem(PAIR_SPOTIFY, 409)
            entry = self.pairings.get(member)
            if entry and not entry[1].done():
                raise Problem(f'Finish pairing at {PAIRING_URL} before hosting.', 409)
            await self.stop_host()
            self.daemon = await self.spawn(member, '--ws', '127.0.0.1:9090')
            self.host = member
            failure = f'Soloist could not sign in with your saved session. Pair again at {PAIRING_URL} or check the player logs.'
            try:
                async with asyncio.timeout(12):
                    while self.daemon.returncode is None:
                        try:
                            async with self.http.ws_connect(SOCKET) as socket:
                                auth = await event(socket, 'auth_state', timeout=12)
                                while not auth['logged_in']:
                                    auth = await event(socket, 'auth_state', timeout=12)
                                failure = 'Spotify did not switch playback to the Discord Music device.'
                                await activate(socket, auth)
                                return web.json_response({})
                        except ClientError:
                            await asyncio.sleep(0.15)
            except (TimeoutError, Problem) as error:
                logging.warning('Soloist startup: %s', str(error) or failure)
            await self.stop_host()
            raise Problem(failure)

    async def playback(self, request):
        member, action = request.match_info['member'], request.match_info['action']
        async with self.lock:
            if self.host != member or self.daemon.returncode is not None:
                raise Problem('There is no active Soloist session for this host.', 409)
            try:
                async with self.http.ws_connect(SOCKET) as socket:
                    auth = await event(socket, 'auth_state')
                    if not auth['logged_in']:
                        raise Problem(f'The host’s Spotify session is signed out. Ask the host to end the session '
                                      f'and pair again at {PAIRING_URL}.', 409)
                    if action in ('pause', 'skip'):
                        await command(socket, 'pause' if action == 'pause' else 'skip_next')
                        return web.json_response({})
                    if action in ('queue', 'resume') and not auth.get('is_active'):
                        await activate(socket, auth)
                        await socket.send_json({'type': 'command', 'command': 'get_state'})
                    state = await event(socket, 'playback_state')
                    status = state.get('status', 'idle')
                    if action == 'queue':
                        uri = request.query.get('track', '')
                        if not TRACK.fullmatch(uri):
                            raise Problem('Expected a Spotify track URI.', 400)
                        queued = status in ('playing', 'buffering')
                        await command(socket, 'add_to_queue' if queued else 'play', uri=uri)
                        return web.json_response({'queued': queued})
                    if action == 'resume':
                        if status == 'paused' and song(state.get('item')):
                            await command(socket, 'play')
                        elif status in ('idle', 'paused'):
                            queue = await upcoming(socket, limit=1)
                            if queue:
                                await command(socket, 'play', uri=queue[0]['uri'])
                        return web.json_response({})
                    return web.json_response({'status': status, 'queue': await upcoming(socket),
                                              'current': song(state.get('item')) if status != 'idle' else None})
            except (ClientError, TimeoutError) as error:
                raise Problem('Could not read playback from Soloist.') from error

    async def run(self, _):
        self.http = await http_session()
        try:
            yield
        finally:
            await self.stop_host()
            for process, task in self.pairings.values():
                await stop_process(process)
                with suppress(asyncio.CancelledError):
                    await task
            await self.http.close()


def create_app():
    player = Player()
    app = service_app()
    app.cleanup_ctx.append(player.run)
    path = r'/profiles/{member:[0-9]{17,20}}/'
    for method in ('GET', 'POST', 'DELETE'):
        app.router.add_route(method, path + 'pairing', player.pairing)
    for method in ('POST', 'DELETE'):
        app.router.add_route(method, path + 'host', player.hosting)
    app.router.add_get(path + '{action:playback}', player.playback)
    app.router.add_post(path + '{action:queue|pause|skip|resume}', player.playback)
    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_app(), port=3001, access_log=None)
