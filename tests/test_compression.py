import gzip

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from casm_monitor.web.compression import TextCompression


def test_text_compression_preserves_binary_and_existing_gzip():
    app = FastAPI()
    app.add_middleware(TextCompression)
    raw = b'{"data":"' + b'abc123' * 2000 + b'"}'

    @app.get('/plain')
    def plain():
        return Response(raw, media_type='application/json')

    @app.get('/encoded')
    def encoded():
        return Response(gzip.compress(raw), media_type='application/json', headers={'Content-Encoding':'gzip'})

    @app.get('/plot.png')
    def binary():
        return Response(raw, media_type='image/png')

    @app.get('/small')
    def small():
        return {'ok':True}

    with TestClient(app) as client:
        response = client.get('/plain', headers={'Accept-Encoding':'gzip'})
        assert response.content == raw
        assert response.headers['content-encoding'] == 'gzip'
        assert int(response.headers['content-length']) < len(raw)/10
        assert 'Accept-Encoding' in response.headers['vary']
        for encoding in ('identity', 'gzip;q=0'):
            response = client.get('/plain', headers={'Accept-Encoding':encoding})
            assert response.content == raw and 'content-encoding' not in response.headers
            assert 'Accept-Encoding' in response.headers['vary']
        assert client.get('/encoded').content == raw
        assert 'content-encoding' not in client.get('/plot.png').headers
        assert 'content-encoding' not in client.get('/small').headers
        assert 'content-encoding' not in client.get('/plain', headers={'Range':'bytes=0-100'}).headers
