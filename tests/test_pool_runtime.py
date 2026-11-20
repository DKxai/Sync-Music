import asyncio
import os
import subprocess
import sys
from unittest.mock import Mock

from cryptography.fernet import Fernet

from app import pool
from app.pool_routes import Routes


def test_pool_schedule_reuses_primary_schedule_only_for_the_same_accounts(monkeypatch):
    routes = Routes({})
    monkeypatch.setattr(pool.main, 'active_direct_schedule', lambda: {'source': 'ytmusic', 'target': 'spotify'})
    assert routes.covered_by_primary_schedule('main/ytmusic', 'main/spotify')
    assert not routes.covered_by_primary_schedule('main/spotify', 'main/ytmusic')
    assert not routes.covered_by_primary_schedule('main/ytmusic', 'secondary/spotify')
    monkeypatch.setattr(pool.main, 'active_direct_schedule', lambda: None)
    assert not routes.covered_by_primary_schedule('main/ytmusic', 'main/spotify')


def test_covered_route_still_loads_its_worker_but_skips_new_scheduled_runs(monkeypatch):
    profile = Mock()
    profile.provider_ready.return_value = True
    source, target = 'main/ytmusic', 'main/spotify'
    routes = Routes({'main': profile})
    versions = {'spotify': ['1'], 'ytmusic': ['1']}
    monkeypatch.setattr(routes, 'rules', lambda: {
        'route': {'source': source, 'target': target, 'versions': versions, 'scheduled': True},
    })
    monkeypatch.setattr(routes, 'versions', lambda endpoints: versions)
    worker = Mock()
    worker.running.return_value = False
    worker.schedule.next_check.return_value = 0
    load_worker = Mock(return_value=worker)
    monkeypatch.setattr(routes, 'worker', load_worker)
    monkeypatch.setattr(pool.main, 'active_direct_schedule', lambda: {'source': 'both', 'target': 'both'})
    asyncio.run(routes.tick())
    load_worker.assert_called_once_with(source, target, versions)
    worker.submit.assert_not_called()


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
