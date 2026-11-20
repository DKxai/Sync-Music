import os
import subprocess
import sys

from cryptography.fernet import Fernet


def test_profile_login_disconnect_and_isolation(tmp_path):
    program = """
from fastapi.testclient import TestClient
from app.pool import app
from app.provider_tokens import save_token, has_token
with TestClient(app, base_url='https://testserver') as c:
    from sync_profile_secondary import provider_tokens as second
    for path in ('/', '/profiles/secondary/'):
        assert c.get(path+'health').json()['ok']
        assert c.post(path+'v1/session', headers={'X-API-Key':'x'*32}).is_success
    save_token('spotify', {'access_token':'first'})
    second.save_token('spotify', {'access_token':'second'})
    assert c.delete('/profiles/secondary/v1/auth/spotify/connection',
                    headers={'Origin':'https://testserver'}).is_success
    assert has_token('spotify') and not second.has_token('spotify')
    assert 'Pool tài khoản' in c.get('/').text
    from app import pool
    from sync_profile_secondary import main as second_main
    routes = pool.Routes({'main':pool.main,'secondary':second_main})
    endpoints=routes.resolve('main/spotify','secondary/ytmusic')
    worker=routes.worker('main/spotify','secondary/ytmusic',routes.versions(endpoints))
    assert worker.spotify is pool.main.spotify and worker.ytmusic is second_main.ytmusic
    assert worker.FETCHERS['ytmusic'] is second_main.ytmusic.fetch
    c.cookies.clear()
    assert c.get('/v1/pool/routes').status_code == 401
    assert c.post('/v1/session',headers={'X-API-Key':'x'*32}).is_success
    assert c.get('/profiles/secondary/v1/overview').is_success
    assert c.post('/v1/pool/accounts?name=family',headers={'Origin':'https://testserver'}).is_success
    assert c.get('/profiles/family/v1/overview').is_success
    assert 'family' in c.get('/').text
    assert '/profiles/secondary/v1/auth/spotify/callback' in c.get(
        '/profiles/secondary/v1/config').json()['spotify_redirect_uri']
"""
    subprocess.run([sys.executable, '-c', program], check=True, env={
        **os.environ, 'DATA_DIR': str(tmp_path), 'APP_API_KEY': 'x'*32,
        'ENCRYPTION_KEY': Fernet.generate_key().decode(), 'SYNC_PROFILES': 'secondary',
        'SPOTIFY_REDIRECT_URI': 'https://example.com/v1/auth/spotify/callback'})
