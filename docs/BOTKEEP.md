# Deploy to Botkeep Cloud (native Python runtime)

Music Sync Hub is a self-hosted **read-only import/snapshot and Sync Now** starter. Remote writes and deletions are **not implemented** in this revision. Keep `WRITE_ENABLED=false` even when `DELETE_MODE=auto` is configured.

## Botkeep setup

1. Sign in at https://botkeep.cloud/app and create an **Application > Python** workload (Python 3.12 if offered).
2. Select **GitHub**, repository `https://github.com/DKxai/Sync-Music`, branch `main`, project root `/`. Choose `requirements.txt` as dependency manifest.
3. Start command: **`python main.py`**. This does not require Docker, docker-compose or PostgreSQL. It serves FastAPI on `0.0.0.0:$SERVER_PORT` as Botkeep requires.
4. Suggested first allocation: **512 MB RAM, 50% CPU, 1 GB storage** if your account permits; monitor the first full import. A large music library can temporarily exceed 512 MB; do not assume zero OOM risk.
5. Set the following in **Environment** (Botkeep manages a private `.env`, which main.py loads). NEVER put live credentials in GitHub or a support chat.
6. If you configure a public web domain, enable HTTPS and use a long admin token. The dashboard is at `/`, health check at `/health`. Review Console and runtime after start: a successful GitHub import alone is not proof the app is running.

## Environment

```dotenv
APP_API_KEY=<random-token-at-least-32-characters>
ENCRYPTION_KEY=<Fernet-key>
SPOTIFY_POLL_MINUTES=5
YTM_POLL_MINUTES=10
APPLE_POLL_MINUTES=10
DELETE_MODE=auto
DELETE_CONFIRM_POLLS=2
MAX_DELETE_PERCENT=5
MAX_DELETE_ABSOLUTE=20
WRITE_ENABLED=false
APPLE_STOREFRONT=vn
SPOTIFY_CLIENT_ID=
SPOTIFY_REDIRECT_URI=
APPLE_TEAM_ID=
APPLE_KEY_ID=
```

Generate the first two values locally with:

```bash
python3 - <<'PY'
import base64,secrets
print('APP_API_KEY=' + secrets.token_urlsafe(48))
print('ENCRYPTION_KEY=' + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
PY
```

Do not override Botkeep's `SERVER_PORT`. Leave `DATA_DIR` unset for the `./data` SQLite directory, or set it to a **confirmed persistent writable path** from your Botkeep account (never `/tmp`). Avoid copying Docker `DATA_DIR=/data` from `.env.example` into the native runtime.

## Connect providers

- **Spotify:** Create a Spotify Web API app and set its Client ID as `SPOTIFY_CLIENT_ID` in Environment. Set `SPOTIFY_REDIRECT_URI=https://YOUR_DOMAIN/v1/auth/spotify/callback` and register the exact same URI in Spotify Developer Dashboard. Restart the workload, sign in to the Sync-Music dashboard, then select **Kết nối Spotify** and **Mở Spotify để cấp quyền**. The callback queues the first read-only import. The server stores the refresh token encrypted and renews access during later polls.
- **YouTube Music:** On a trusted machine create a ytmusicapi browser auth file. Upload it privately to `private/ytmusic_browser.json` using Botkeep Files. Never commit this file.
- **Apple Music:** Upload the MusicKit key to `private/apple_music_key.p8` and authorized user token to `private/apple_user_token.txt` in Botkeep Files; set `APPLE_TEAM_ID` and `APPLE_KEY_ID`.

The scheduler runs in the Botkeep process while the dashboard is closed. It skips providers with missing credentials and checks again at the next interval. With the native launcher, YouTube Music can start importing as soon as its auth file is present. Changes to Environment values require restarting the workload. When a YouTube Music session or Apple Music user token expires, replace the associated file and run a manual import to check it.

If the Botkeep runtime gives no persistent writable workspace or cannot upload private auth files, stop rather than deploy with unsafe defaults.

## Verify

1. In Botkeep, check workload **Running** and **Console** without startup exception.
2. GET `/health` should return `ok: true`. This proves only the API is alive.
3. With `X-API-Key`, GET `/v1/config` should show polling `5/10/10` and `write_enabled: false`.
4. Open the HTTPS dashboard `/`, enter `APP_API_KEY`, choose **Kết nối**, then **Sync Now**. Check `/v1/sync/status` and `/v1/snapshots`. Unconfigured accounts will safely report an error.
5. Import and verify complete snapshots from all 3 services before approving the baseline.
6. Confirm SQLite under `data/` survives a restart and GitHub update; export an **independent encrypted backup** outside Botkeep.

## RAM, CPU and storage optimizations

- One Uvicorn worker, one in-process scheduler/serialized sync lock, SQLite built-in: **no extra database container**.
- API access logging disabled. Botkeep Console retains errors; avoid writing unbounded logs to files.
- SQLite connection cache limited to about 2 MiB; bounded WAL settings and nightly history pruning already present.
- Keep `data/`, `private/`, `.env`, caches and media out of the repository.
- Full-library fetching may still cause RAM spikes. If encountered, move to paginated streaming fetches; do not increase polling/concurrency.

Source references: https://botkeep.cloud/docs/github and https://botkeep.cloud/docs/hosting.
