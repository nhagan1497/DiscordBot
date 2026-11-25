import asyncio
import logging
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from aiohttp import ClientError, WSMsgType, web
from jinja2 import Environment, FileSystemLoader, select_autoescape
from yarl import URL

from common import Problem, service_app


WEB = Path(__file__).resolve().parent.parent / 'web'
TEMPLATES = Environment(loader=FileSystemLoader(WEB), autoescape=select_autoescape())
PLAYBACK_NOTICES = {'pause': 'Pause sent to Spotify.', 'resume': 'Resume sent to Spotify.',
                    'skip': 'Skip sent to Spotify.'}


def page(template, **context):
    return web.Response(text=TEMPLATES.get_template(template).render(**context),
                        content_type='text/html', headers={'Cache-Control': 'no-store'})


class Admin:
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.pairing = None
        self.notice = ''

    async def state(self, _):
        async def profile(identifier, name):
            pairing = await self.bot.player(identifier, 'pairing')
            return {'id': identifier, 'name': name, 'paired': pairing['paired']}

        profiles = await asyncio.gather(*(profile(key, name) for key, name in self.bot.profiles.items()))
        host = self.bot.host
        playback = {'current': None, 'queue': []}
        error = self.bot.error
        if host:
            try:
                playback = await self.bot.player(host, 'playback')
            except Problem as failure:
                error = str(failure)
        return {'profiles': profiles, 'host': {'id': host} if host else None,
                'playbackError': error, 'pairing': self.pairing[:2] if self.pairing else None, **playback}

    async def state_json(self, request):
        return web.json_response(await self.state(request))

    async def index(self, request, playback_notice=None):
        state = await self.state(request)
        host = state['host']
        host_name = self.bot.profiles.get(host['id'], host['id']) if host else None
        return page('index.html', state=state, host_name=host_name, notice=self.notice,
                    playback_notice=playback_notice or PLAYBACK_NOTICES.get(request.query.get('playback')))

    async def playback(self, request):
        if not self.same_origin(request):
            raise web.HTTPForbidden()
        member, action = request.match_info['member'], request.match_info['action']
        try:
            async with self.bot.lock:
                if self.bot.host != member or not self.bot.voice:
                    raise Problem('The active voice session changed. Refresh playback and try again.', 409)
                await self.bot.player(member, action, 'POST')
        except Problem as error:
            return await self.index(request, playback_notice=str(error))
        raise web.HTTPSeeOther(f'/?playback={action}#playback')

    async def pairing_status(self, _):
        waiting = self.pairing is not None
        return page('pairing-status.html', waiting=waiting, notice=self.notice, refresh=waiting)

    @staticmethod
    def same_origin(request):
        try:
            origin = urlsplit(request.headers.get('Origin', ''))
        except ValueError:
            return False
        return origin.netloc == request.host and origin.scheme in ('http', 'https')

    async def desktop_request(self, identifier, method):
        async with self.bot.web_client.request(method, f'http://pairing:6081/sessions/{identifier}') as response:
            if not response.ok:
                raise Problem('The pairing desktop is unavailable. Check its logs.')

    async def start(self, request):
        if not self.same_origin(request):
            raise web.HTTPForbidden()
        member = request.match_info['member']
        async with self.lock:
            if member not in self.bot.profiles:
                raise Problem('That Discord profile does not exist.', 404)
            if self.pairing or self.bot.host == member:
                self.notice = 'Finish the current pairing or stop hosting first.'
                raise web.HTTPSeeOther('/')
            identifier = str(uuid4())
            try:
                await self.bot.player(member, 'pairing', 'POST')
                try:
                    await self.desktop_request(identifier, 'POST')
                except (Problem, ClientError, TimeoutError):
                    await self.bot.player(member, 'pairing', 'DELETE')
                    raise Problem('Could not open the pairing desktop.')
                self.notice = ''
                self.pairing = (identifier, member, asyncio.create_task(self.watch(member)))
            except Problem as error:
                self.notice = str(error)
        raise web.HTTPSeeOther('/')

    async def watch(self, member):
        cancel = True
        try:
            async with asyncio.timeout(600):
                while (result := await self.bot.player(member, 'pairing'))['status'] == 'waiting':
                    await asyncio.sleep(1)
            cancel = False
            self.notice = 'Spotify paired.' if result['status'] == 'paired' else 'Spotify pairing failed. Check the player logs.'
        except (Problem, TimeoutError) as error:
            self.notice = str(error) or 'Spotify pairing timed out.'
        async with self.lock:
            await self.close(cancel)

    async def close(self, cancel=True):
        if self.pairing is None:
            return
        identifier, member, task = self.pairing
        self.pairing = None
        if task is not asyncio.current_task():
            task.cancel()
        if cancel:
            try:
                await self.bot.player(member, 'pairing', 'DELETE')
            except Problem as error:
                logging.warning('%s', error)
        try:
            await self.desktop_request(identifier, 'DELETE')
        except (Problem, ClientError, TimeoutError) as error:
            logging.warning('Could not close the desktop: %s', error)

    async def cancel(self, request):
        if not self.same_origin(request):
            raise web.HTTPForbidden()
        async with self.lock:
            if self.pairing:
                await self.close()
                self.notice = 'Pairing canceled.'
        raise web.HTTPSeeOther('/')

    async def desktop(self, request):
        identifier, asset = request.match_info['id'], request.match_info['asset']
        if not self.pairing or self.pairing[0] != identifier:
            raise web.HTTPNotFound()
        target = URL('http://pairing:6080').with_path('/' + asset).with_query(request.query)
        if asset == 'websockify':
            if not self.same_origin(request):
                raise web.HTTPForbidden()
            return await self.websocket(request, target)
        async with self.bot.web_client.get(target) as upstream:
            headers = {'Content-Type': upstream.headers.get('Content-Type', 'application/octet-stream'),
                       'Cache-Control': 'no-store', 'X-Frame-Options': 'SAMEORIGIN'}
            return web.Response(body=await upstream.read(), status=upstream.status, headers=headers)

    async def websocket(self, request, target):
        protocols = [value.strip() for value in request.headers.get('Sec-WebSocket-Protocol', '').split(',') if value.strip()]
        async with self.bot.web_client.ws_connect(target, protocols=protocols) as upstream:
            client = web.WebSocketResponse(protocols=(upstream.protocol,) if upstream.protocol else ())
            await client.prepare(request)

            async def relay(source, destination):
                async for message in source:
                    if message.type == WSMsgType.BINARY:
                        await destination.send_bytes(message.data)
                    elif message.type == WSMsgType.TEXT:
                        await destination.send_str(message.data)

            directions = [asyncio.create_task(relay(client, upstream)), asyncio.create_task(relay(upstream, client))]
            try:
                await asyncio.wait(directions, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in directions:
                    task.cancel()
                await asyncio.gather(*directions, return_exceptions=True)
                await client.close()
            return client

    async def stylesheet(self, _):
        return web.FileResponse(WEB / 'style.css')

    def app(self):
        app = service_app()
        app.router.add_get('/', self.index)
        app.router.add_get('/api/state', self.state_json)
        app.router.add_get('/style.css', self.stylesheet)
        app.router.add_post(r'/pair/{member:[0-9]{17,20}}', self.start)
        app.router.add_post(r'/playback/{member:[0-9]{17,20}}/{action:pause|resume|skip}', self.playback)
        app.router.add_post('/cancel', self.cancel)
        app.router.add_get('/pairing-status', self.pairing_status)
        app.router.add_get(r'/pairing-desktop/{id:[a-f0-9-]{36}}/{asset:.+}', self.desktop)
        return app
