import os
import subprocess
import sys

from cryptography.fernet import Fernet


def test_profile_login_disconnect_and_isolation(tmp_path):
    program = """
from fastapi.testclient import TestClient
from app.pool import app
from app.provider_tokens import save_token, has_token
from sync_profile_secondary import provider_tokens as second
with TestClient(app, base_url='https://testserver') as c:
    for path in ('/', '/profiles/secondary/'):
        assert c.get(path+'health').json()['ok']
        assert c.post(path+'v1/session', headers={'X-API-Key':'x'*32}).is_success
    save_token('spotify', {'access_token':'first'})
    second.save_token('spotify', {'access_token':'second'})
    assert c.delete('/profiles/secondary/v1/auth/spotify/connection',
                    headers={'Origin':'https://testserver'}).is_success
    assert has_token('spotify') and not second.has_token('spotify')
    assert '/profiles/secondary/' in c.get('/').text
    assert '/profiles/secondary/v1/auth/spotify/callback' in c.get(
        '/profiles/secondary/v1/config').json()['spotify_redirect_uri']
"""
    subprocess.run([sys.executable, '-c', program], check=True, env={
        **os.environ, 'DATA_DIR': str(tmp_path), 'APP_API_KEY': 'x'*32,
        'ENCRYPTION_KEY': Fernet.generate_key().decode(), 'SYNC_PROFILES': 'secondary',
        'SPOTIFY_REDIRECT_URI': 'https://example.com/v1/auth/spotify/callback'})
