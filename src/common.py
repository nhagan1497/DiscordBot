import asyncio
from contextlib import suppress

from aiohttp import ClientSession, ClientTimeout, web


PAIRING_URL = 'https://spotify-bot.nathaniel.systems'
PAIR_SPOTIFY = f'Sign in to Spotify at {PAIRING_URL} and pair your profile first.'


class Problem(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


async def stop_process(process):
    if process is None or process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()


def service_app():
    async def health(_):
        return web.Response(status=204)

    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except Problem as error:
            return web.json_response({'error': str(error)}, status=error.status)

    app = web.Application(middlewares=[errors])
    app.router.add_get('/health', health)
    return app


async def http_session():
    return ClientSession(timeout=ClientTimeout(total=20))
