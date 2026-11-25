import asyncio
import os
import shutil
import signal
import tempfile
from contextlib import suppress

from aiohttp import ClientError, web

from common import Problem, http_session, service_app


class Desktop:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.current = None
        self.display = 91

    async def stop(self):
        if self.current is None:
            return
        _, process, directory, expiry = self.current
        self.current = None
        if expiry is not asyncio.current_task():
            expiry.cancel()
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        shutil.rmtree(directory)

    async def expire(self, process):
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), 600)
        async with self.lock:
            if self.current and self.current[1] is process:
                await self.stop()

    async def session(self, request):
        identifier = request.match_info['id']
        async with self.lock:
            if request.method == 'DELETE':
                if self.current and self.current[0] == identifier:
                    await self.stop()
                return web.Response(status=204)
            if self.current and self.current[0] == identifier:
                return web.Response(status=204)
            await self.stop()
            directory = tempfile.mkdtemp(prefix='music-pairing-')
            self.display += 1
            try:
                process = await asyncio.create_subprocess_exec(
                    '/usr/local/bin/pairing-desktop.sh', start_new_session=True,
                    env={'PATH': os.environ['PATH'], 'LANG': 'C.UTF-8', 'HOME': directory,
                         'PAIRING_DISPLAY': f':{self.display}'},
                )
            except OSError:
                shutil.rmtree(directory)
                raise
            self.current = identifier, process, directory, asyncio.create_task(self.expire(process))
            for _ in range(50):
                if process.returncode is not None:
                    break
                try:
                    async with asyncio.timeout(0.3), self.http.get('http://127.0.0.1:6080/vnc.html') as response:
                        if response.ok:
                            return web.Response(status=204)
                except (ClientError, TimeoutError):
                    pass
                await asyncio.sleep(0.2)
            await self.stop()
            raise Problem('The pairing desktop did not become ready.')

    async def run(self, _):
        self.http = await http_session()
        try:
            yield
        finally:
            await self.stop()
            await self.http.close()


def create_app():
    desktop = Desktop()
    app = service_app()
    app.cleanup_ctx.append(desktop.run)
    for method in ('POST', 'DELETE'):
        app.router.add_route(method, r'/sessions/{id:[a-f0-9-]{36}}', desktop.session)
    return app


if __name__ == '__main__':
    web.run_app(create_app(), port=6081, access_log=None)
