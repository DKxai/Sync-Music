# Music Sync Hub (self-hosted / single user)

Version **0.4.0** supports scheduled snapshots, manual Sync Now, Spotify OAuth, Google device authorization for YouTube Music, and MusicKit user authorization. The reviewed additions writer can add mapped liked tracks and followed artists between Spotify and YouTube Music. Its default is off. See [Botkeep setup](docs/BOTKEEP.md) for current setup and release scope.

## 1. Requirements

- Linux server with Docker Engine and Docker Compose plugin (about 512 MB memory assigned to this container; adjust as needed).
- Spotify Premium for the owner of a Development Mode app.
- Apple Music developer key (MusicKit permissions) and Music User Token for personalized API reads.
- Browser session credentials for ytmusicapi.
- Python 3.12+ on your PC (optional, to generate YTM browser session).
- SSH access to server for local-only dashboard/API access.

## 2. Configure secrets

```bash
cp .env.example .env
python3 - <<'PY'
import base64,secrets
print('APP_API_KEY='+secrets.token_urlsafe(48))
print('ENCRYPTION_KEY='+base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
PY
# Copy these generated two values into .env. Do not paste tokens into public chats.
mkdir -p data private
sudo chown -R 10001:10001 data private
chmod 700 data private
chmod 600 .env
```

Edit `.env`, providing a random key for each secret. If using SSH port forwarding, set `SPOTIFY_REDIRECT_URI=http://127.0.0.1:8787/v1/auth/spotify/callback` and register exactly the same redirect URI with Spotify. Do NOT use `localhost` (Spotify rejects it). For external domain callbacks use HTTPS.

## 3. Provider setup

**Spotify:** create an app at https://developer.spotify.com/dashboard and enter `SPOTIFY_CLIENT_ID`, redirect URL. You can connect after starting the service.

**YouTube Music:** follow https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html on a trusted desktop, run `ytmusicapi browser`, and securely copy the resulting `browser.json` to `private/ytmusic_browser.json` on the server. `chown 10001:10001` and `chmod 600` for this file. Avoid adding cookies manually unless you understand the risks.

**Apple Music:** obtain MusicKit private key `.p8` and put it at `private/apple_music_key.p8`; set `APPLE_TEAM_ID` and `APPLE_KEY_ID`. Obtain a personal Music User Token via a trusted MusicKit authorization web integration and write it to `private/apple_user_token.txt`. This starter does not implement the MusicKit browser sign-in UI; until provided, Apple poll reports NotConfigured. Apple library songs are NOT the same thing as Favorited songs.

## 4. Start

Open the local dashboard via the SSH tunnel at `http://127.0.0.1:8787/`, paste `APP_API_KEY`, then use **Sync tất cả / Spotify / YouTube Music / Apple Music**. Jobs are queued and their status refreshed automatically. The key is kept in memory only.

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f --tail 50 api
curl http://127.0.0.1:8787/health
```

API binds only to server loopback. To administer from your PC, create an SSH tunnel **on your PC**:

```bash
ssh -N -L 8787:127.0.0.1:8787 USER@SERVER_IP
```

With this tunnel, access `http://127.0.0.1:8787` in your PC browser or `curl` in another PC terminal. Keep the tunnel active during Spotify OAuth redirect.

## 5. Authorize Spotify

```bash
# Run from PC with SSH tunnel, or on the server itself.
export KEY='APP_API_KEY_VALUE_FROM_ENV'
curl -s -X POST -H "X-API-Key: $KEY" http://127.0.0.1:8787/v1/auth/spotify/start
```

Open returned `authorization_url` in your PC browser with tunnel still active. Successful callback securely stores Spotify OAuth credentials in the database.

## 6. Initial full import, merge and baseline

```bash
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:8787/v1/accounts
curl -s -X POST -H "X-API-Key: $KEY" http://127.0.0.1:8787/v1/sync/spotify
curl -s -X POST -H "X-API-Key: $KEY" http://127.0.0.1:8787/v1/sync/ytmusic
curl -s -X POST -H "X-API-Key: $KEY" http://127.0.0.1:8787/v1/sync/apple
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:8787/v1/snapshots
```

Only after reviewing all three complete snapshots and backing up SQLite:

```bash
curl -s -X POST -H "X-API-Key: $KEY" http://127.0.0.1:8787/v1/baseline/approve
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:8787/v1/events
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:8787/v1/conflicts
```

**Important:** "baseline approved" means change detection and planning of reviewed additions. Remote deletion is still detection-only. Initial import has no cross-service writes. The canonical `music_items` table is a staging index; cross-platform no-ISRC matches still require further matching logic.

## 7. Backup / rollback

Back up the SQLite database using SQLite's online backup API, NOT a `cp` of the live WAL database:

```bash
sudo ./scripts/backup.sh
```

Store a backup in another encrypted location, separate from this small server. Before changes, stop the service, restore the selected `.sqlite3` snapshot into `data/music.db`, fix file ownership, restart. A database rollback cannot undo already-applied cloud mutations; write replay/undo and provider verification must be added before enabling live writes.

## 8. Hardening checklist

- Keep `.env` and `private/` outside Git and any public repo; never commit OAuth refresh tokens, browser cookies, Apple `.p8` or user token.
- Use `ssh` tunnel / Tailscale for API access; avoid binding `0.0.0.0:8787` without a properly configured identity-aware reverse proxy.
- Back up and rotate the encryption key together with encrypted database credentials, or old stored Spotify tokens become unreadable.
- Only one container worker and one scheduler owner; do not scale Compose replicas without distributed locking.
- Spotify rate/quota policy can require longer polling for large collections; failed full pagination is treated as incomplete and no deletions are derived.

## 9. Tests

```bash
python3 -m compileall -q app
python3 -m pytest -q tests
```

## Reference API docs

- https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide
- https://developer.spotify.com/documentation/web-api/concepts/redirect_uri
- https://ytmusicapi.readthedocs.io/en/stable/reference/library.html
- https://developer.apple.com/documentation/applemusicapi/user-authentication-for-musickit

## 10. RAM, CPU and storage optimization

- A single `uvicorn --workers 1` process and one serialized polling queue keeps peak memory bounded.
- Container limit is 512 MB / 0.75 CPU and 16 MB tmpfs; raise memory limit only after measuring a large first import.
- Logs rotate automatically (`5 MB × 2`); remove verbose diagnostics before production.
- SQLite WAL auto-checkpoints around 200 pages and limits retained WAL file size; daily cleanup keeps 30 days of finished snapshots, 90 days of **resolved** events/conflicts. Live source states and pending changes are never pruned.
- `docker system prune` can delete resources used by other projects: do not run it on a shared server. Use `docker image prune` only after reviewing image usage.
- Monitor with `docker stats music-sync-hub`, `du -sh data`, `docker compose logs --tail 50 api`.
- **Sync Now** triggers `POST /v1/sync/all` or `POST /v1/sync/{provider}` (HTTP 202), status at `GET /v1/sync/status`, per-job status at `/v1/sync/jobs/{job_id}`. Manual/scheduled polling share a single queue and duplicate jobs are coalesced.
- With `WRITE_ENABLED=false` this runs in read-only mode. With writes enabled, only reviewed Spotify–YouTube Music additions are executed. `DELETE_MODE=auto` still controls removal detection only.

## Botkeep Cloud (native Python, no Docker)

Use `python main.py` as Botkeep start command. The launcher reads Botkeep's `SERVER_PORT` and managed `.env`, creates a single Uvicorn worker, and uses `data/` for SQLite by default. See [Botkeep deployment guide](docs/BOTKEEP.md). Docker Compose is for separate Docker-capable servers only.

### Direct transfers and undo

Set `AUTO_TRANSFER_ENABLED=true` on Botkeep and redeploy from `main`. Then connect
Spotify again from the dashboard to grant the additional scopes. The existing
YouTube Music browser configuration can be reused. Keep the same persistent
`DATA_DIR` and `ENCRYPTION_KEY` when updating the server. The dashboard sign-in
uses a secure HTTPS cookie valid for 14 days. Existing API clients can continue
to send `X-API-Key`.

Choose Spotify → YouTube Music, YouTube Music → Spotify, or a two-way transfer.
Two-way mode reads each library once and copies additions in both directions from
those initial snapshots. Removing a track from just one service does not remove it
from the other and a later run may restore it. Use **Đồng bộ ngay** or save a
schedule to repeat the selected mode every ten minutes. The dashboard provides
paginated track results and reasons for the latest transfer. Native likes and playlist tracks are copied separately. For an owned
playlist, Sync-Music links an unambiguous same-name eligible playlist or creates a
private one. Spotify destinations must be private and non-collaborative.
Extra destination tracks keep their order. New tracks are appended in source
order. Providers apply their own timestamps to new likes.

A run searches for up to 40 tracks and attempts up to 200 additions, with up to
100 additions to any one playlist. Large libraries may need several runs.
Tracks without a unique match and ambiguous playlists are reported in the
dashboard. Apple Music remains available for library reads and manual export.

Undo is available for direct transfers that recorded verified additions. Undo
processes the latest outstanding run per destination and pauses the schedule.
It removes verified new likes and playlist entries. Playlists created during
the run are retained. Spotify skips playlists whose snapshot changed or where
a newly added Spotify track duplicates an older occurrence. YouTube Music
uses individual playlist item IDs to remove only the new occurrences. Its
native undo clears the like to neutral. A previous dislike is not restored.
An unconfirmed provider response is reported for manual review. Earlier
manual imports have no undo journal.
