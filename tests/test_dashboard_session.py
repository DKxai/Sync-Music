from dataclasses import replace

from fastapi.testclient import TestClient

from app import dashboard_session, db, main


def test_dashboard_session_restore_and_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'music.db')
    monkeypatch.setattr(db, 'settings', replace(db.settings, data_dir=tmp_path))
    monkeypatch.setattr(dashboard_session, 'settings',
                        replace(dashboard_session.settings, api_key='test-admin-key'))
    db.init_db()
    client = TestClient(main.app, base_url='https://sync.example')
    assert client.get('/v1/config').status_code == 401
    assert client.get('/v1/config', headers={'X-API-Key': 'test-admin-key'}).status_code == 200
    logged_in = client.post('/v1/session', headers={'X-API-Key': 'test-admin-key'})
    assert logged_in.status_code == 200
    assert 'HttpOnly' in logged_in.headers['set-cookie']
    assert 'Secure' in logged_in.headers['set-cookie']
    assert client.get('/v1/config').status_code == 200
    assert client.delete('/v1/session', headers={'Origin': 'https://another.example'}).status_code == 403
    assert client.delete('/v1/session', headers={'Origin': 'https://sync.example'}).status_code == 200
    assert client.get('/v1/config').status_code == 401
