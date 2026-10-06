"""Compress textual responses over SSH without recompressing plots or archives."""
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.gzip import GZipResponder


def textual(headers):
    kind = headers.get('content-type', '').split(';')[0]
    return kind in ('application/json', 'application/javascript', 'text/javascript',
                    'text/html', 'text/css', 'image/svg+xml')


class TextResponder(GZipResponder):
    async def send_with_compression(self, message):
        await super().send_with_compression(message)
        if message['type'] == 'http.response.start':
            self.content_type_is_excluded |= not textual(Headers(raw=message['headers']))


class TextCompression:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or 'range' in Headers(scope=scope):
            return await self.app(scope, receive, send)
        accepted = Headers(scope=scope).get('accept-encoding', '')
        # Honour explicit gzip;q=0. Browsers ordinarily send an unqualified gzip.
        gzip_ok = False
        for token in accepted.lower().split(','):
            name, *params = token.strip().split(';')
            if name == 'gzip':
                try:
                    gzip_ok = all(float(p.strip()[2:]) > 0 for p in params if p.strip().startswith('q='))
                except ValueError:
                    gzip_ok = False
        if gzip_ok:
            return await TextResponder(self.app, minimum_size=2048, compresslevel=3)(scope, receive, send)

        async def vary(message):
            if message['type'] == 'http.response.start' and textual(Headers(raw=message['headers'])):
                MutableHeaders(raw=message['headers']).add_vary_header('Accept-Encoding')
            await send(message)
        await self.app(scope, receive, vary)
