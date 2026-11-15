"""MusicKit authorization page, isolated from the dashboard and its admin key."""

from datetime import datetime, timedelta, timezone
import secrets
from urllib.parse import urlencode, urlparse

from .config import settings
from .connectors import apple
from .connectors.base import NotConfigured
from .db import connect
from .provider_tokens import save_token


def configured() -> bool:
    origin = urlparse(settings.apple_web_origin)
    return bool(origin.scheme == 'https' and origin.hostname and
                not (origin.path or origin.query or origin.fragment or origin.username) and
                apple.developer_configured())


def start() -> str:
    if not configured():
        raise NotConfigured('Set APPLE_WEB_ORIGIN to the dashboard HTTPS origin and configure the MusicKit key')
    token = apple.developer_token(settings.apple_web_origin)
    state = 'apple-' + secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')
    with connect() as con:
        con.execute("DELETE FROM oauth_states WHERE state LIKE 'apple-%'")
        con.execute('INSERT INTO oauth_states(state, verifier_ciphertext, expires_at) VALUES (?,?,?)',
                    (state, b'', expires_at))
    return '/v1/auth/apple/connect#' + urlencode({'state': state, 'developer_token': token})


async def complete(state: str, user_token: str) -> None:
    if not state.startswith('apple-') or not user_token.isascii() or any(not 33 <= ord(char) <= 126 for char in user_token):
        raise ValueError('Invalid Apple authorization')
    with connect() as con:
        consumed = con.execute(
            """UPDATE oauth_states SET used_at=datetime('now')
               WHERE state=? AND used_at IS NULL AND expires_at > datetime('now')""", (state,)
        )
        if not consumed.rowcount:
            raise ValueError('Apple authorization expired or already used')
    await apple.verify_token(user_token)
    save_token('apple', {'user_token': user_token}, reconnect=True)


HTML = """<!doctype html>
<html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kết nối Apple Music</title>
<h1>Kết nối Apple Music</h1>
<button id="authorize" disabled>Đăng nhập và cấp quyền Apple Music</button>
<p id="authStatus" role="status">Đang tải MusicKit…</p>
<script>
const parameters = new URLSearchParams(location.hash.slice(1));
history.replaceState(null, '', location.pathname);
const button = document.getElementById('authorize');
const status = document.getElementById('authStatus');
document.addEventListener('musickitloaded', async () => {
  try {
    const developerToken = parameters.get('developer_token');
    if (!developerToken || !parameters.get('state')) throw new Error();
    await MusicKit.configure({developerToken, app: {name: 'Sync-Music', build: '1'}});
    status.textContent = 'Chọn nút bên trên để cấp quyền.';
    button.disabled = false;
  } catch (_) { status.textContent = 'Không thể mở MusicKit. Hãy bắt đầu lại từ dashboard.'; }
});
button.onclick = async () => {
  button.disabled = true;
  try {
    const user_token = await MusicKit.getInstance().authorize();
    const response = await fetch('/v1/auth/apple/complete', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, cache: 'no-store',
      body: JSON.stringify({state: parameters.get('state'), user_token})
    });
    if (!response.ok) throw new Error();
    status.textContent = 'Apple Music đã kết nối. Máy chủ đã xếp hàng nhập dữ liệu.';
  } catch (_) {
    status.textContent = 'Cấp quyền chưa hoàn tất. Hãy đóng tab này và thử lại từ dashboard.';
  }
};
</script>
<script async src="https://js-cdn.music.apple.com/musickit/v3/musickit.js"
  onerror="document.getElementById('authStatus').textContent='Không tải được MusicKit.'"></script>
</html>"""
