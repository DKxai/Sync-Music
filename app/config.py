import os
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv('DATA_DIR', '/data'))
    api_key: str = os.getenv('APP_API_KEY', '')
    encryption_key: str = os.getenv('ENCRYPTION_KEY', '')
    spotify_minutes: int = int(os.getenv('SPOTIFY_POLL_MINUTES', '5'))
    ytm_minutes: int = int(os.getenv('YTM_POLL_MINUTES', '10'))
    apple_minutes: int = int(os.getenv('APPLE_POLL_MINUTES', '10'))
    delete_mode: str = os.getenv('DELETE_MODE', 'auto')
    delete_confirm_polls: int = int(os.getenv('DELETE_CONFIRM_POLLS', '2'))
    max_delete_percent: float = float(os.getenv('MAX_DELETE_PERCENT', '5'))
    max_delete_absolute: int = int(os.getenv('MAX_DELETE_ABSOLUTE', '20'))
    write_enabled: bool = os.getenv('WRITE_ENABLED', 'false').lower() == 'true'
    spotify_client_id: str = os.getenv('SPOTIFY_CLIENT_ID', '')
    spotify_redirect_uri: str = os.getenv('SPOTIFY_REDIRECT_URI', '')
    ytm_client_id: str = os.getenv('YTM_CLIENT_ID', '')
    ytm_client_secret: str = os.getenv('YTM_CLIENT_SECRET', '')
    ytm_auth_path: Path = Path(os.getenv('YTM_AUTH_PATH', '/run/private/ytmusic_browser.json'))
    apple_key_path: Path = Path(os.getenv('APPLE_KEY_PATH', '/run/private/apple_music_key.p8'))
    apple_user_token_path: Path = Path(os.getenv('APPLE_USER_TOKEN_PATH', '/run/private/apple_user_token.txt'))
    apple_team_id: str = os.getenv('APPLE_TEAM_ID', '')
    apple_key_id: str = os.getenv('APPLE_KEY_ID', '')
    apple_web_origin: str = os.getenv('APPLE_WEB_ORIGIN', '').rstrip('/')
    apple_storefront: str = os.getenv('APPLE_STOREFRONT', 'vn')

settings = Settings()

def validate() -> None:
    from cryptography.fernet import Fernet
    if len(settings.api_key) < 32 or settings.api_key.startswith('REPLACE_'):
        raise RuntimeError('APP_API_KEY must be a random token of at least 32 characters')
    try:
        Fernet(settings.encryption_key.encode())
    except (ValueError, TypeError) as exc:
        raise RuntimeError('ENCRYPTION_KEY must be a valid Fernet key') from exc
    if settings.delete_mode not in ('auto','review','off'):
        raise RuntimeError('DELETE_MODE must be auto, review, or off')
    if not 1 <= settings.delete_confirm_polls <= 10:
        raise RuntimeError('DELETE_CONFIRM_POLLS must be in 1..10')
    if not 0 < settings.max_delete_percent <= 100:
        raise RuntimeError('MAX_DELETE_PERCENT must be in (0..100]')
    if settings.max_delete_absolute < 1:
        raise RuntimeError('MAX_DELETE_ABSOLUTE must be >= 1')
    for minutes in (settings.spotify_minutes, settings.ytm_minutes, settings.apple_minutes):
        if not 1 <= minutes <= 1440:
            raise RuntimeError('poll intervals must be within 1..1440 minutes')
