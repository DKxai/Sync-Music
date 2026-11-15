# Music Sync Hub — Technical design (single user)

## Scope, invariants and actual status

The current release imports snapshots and supports reviewed cross-platform additions for Spotify and YouTube Music liked tracks and followed artists. Only exact, operator-approved provider ID pairs can be queued. The single worker checks source and target membership against both providers and records success after read-back. Writes are disabled by default. Apple Music snapshot import, removal detection and quarantining are available. Playlist mutation, remote removals and undo are still planned.

- Authority: the union of all three completed initial snapshots, once manually approved.
- No deletion inference before baseline approval.
- An unavailable API, any pagination failure, lost login, or an empty/incomplete response must not be construed as user deletion. Full polls are atomic.
- A missing item must be absent from at least two successful complete polls.
- A large delete spike quarantines the whole affected scope and emits conflict evidence.
- Library, Favorites/Liked, Following and playlist membership are distinct resource types.
- The same musical work may have multiple recordings. Exact ISRC is a provisional canonical link. ISRC alone must not authorize destructive cross-platform writes. For no-ISRC records, retain platform-native identity and queue matching for review.
- Private Apple Music uploaded or purchased tracks may have no equivalent playable catalog recording.

## Lifecycle

1. Launch and connect each platform. `GET /v1/accounts` reports auth/read status.
2. Manually POST `/v1/sync/spotify`, `/v1/sync/ytmusic`, `/v1/sync/apple` and audit the returned counts.
3. Back up `music.db` and inspect the `source_states` table.
4. POST `/v1/baseline/approve` only after three complete initial imports.
5. Scheduled polls detect additions/removals and quarantine unsafe drops. Scope-matched additions with an approved pair enter the audit queue. The runtime applies them only with `WRITE_ENABLED=true`.
6. The next milestone covers remote removals, catalog searches for new items, playlist pairing and mutation, and undo.

## Scopes and semantics

- `liked`: Spotify saved tracks and YTM liked songs (NOT YTM in-library songs).
- `library`: Apple Music library songs; these are NOT equivalent to Apple Music Favorite Songs. Apple Favorites extraction requires separate verified handling.
- `followed_artist`: Spotify followed artists and YTM subscriptions. Apple artist Favorites need separate confirmed read support.
- `owned_playlist`: owned/collaborative Spotify playlists, user-owned YT Music playlists and Apple library playlists.
- `playlist:<remote_id>`: per-playlist membership. Spotify/Apple repeated tracks use occurrence counters; YTM uses its `setVideoId` where available.

## Matcher algorithm specification (to implement next)

First use exact service IDs within a provider. For cross-provider matches: validate ISRC, normalized title, main artist(s), remix/live/acoustic markers, duration delta, and catalog country. Rank candidates (`>=0.95` auto-add candidate only after verified; `0.80..0.95` review; `<0.80` unresolved). Do not merge different live/remix/clean/explicit variants solely based on name. Protect duplicate playlist occurrences and order. The current code imports exact ISRC mappings only; candidate score is for diagnostics, not live writes.

## Deletion state machine

`present` -> 1st complete absence (`missing_streak=1`) -> 2nd complete absence (`missing_streak=2`, `present=0`) -> `removed` event. Confirmation poll count is configurable. If absences exceed min(MAX_DELETE_ABSOLUTE, max(1, ceil(MAX_DELETE_PERCENT * previous_scope_size / 100))), quarantine scope and create `conflicts` record. Never delete through a platform that does not support the requested operation.

Even after cross-service writers are built, require: baseline approved, a reviewed mapping, originating change confirmed, no conflicting newer destination update, destination capability supported, destination previously synced/belongs to the same pairing, good destination snapshot, idempotency key, source echo suppression, transactional outgoing op, and read-back verification. Merge-union baseline should not generate removals.

## Authentication

### Spotify — OAuth 2.0 authorization code with PKCE

- Requires Spotify Premium for Dev Mode app owner. Dev mode apps have endpoint and per-app limits.
- Set `SPOTIFY_CLIENT_ID` and exact `SPOTIFY_REDIRECT_URI`. Recommended: `http://127.0.0.1:8787/v1/auth/spotify/callback` with SSH port forwarding *from the PC browser* to the server, or HTTPS custom domain via protected reverse proxy.
- `POST /v1/auth/spotify/start` with `X-API-Key` returns the auth URL. Open it in the PC browser. Redirect callback swaps the code, encrypts credentials with Fernet, stores in SQLite. Refresh is automatic.
- Scopes: `user-library-read user-library-modify user-follow-read user-follow-modify playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public`.
- Spotify 2026 Dev Mode library actions: `PUT/DELETE /me/library` with Spotify URIs; playlist items: `/playlists/{id}/items`.

### YouTube Music — unofficial ytmusicapi

- Browser session headers in `private/ytmusic_browser.json`, generated on a trusted desktop via `ytmusicapi browser` (or ytmusicapi.setup). Never share or commit these cookies.
- No official personal-library OAuth with the same breadth. Unofficial session can expire and the endpoint structure can change. Read and eventual write must be restricted to this one trusted account.
- Separate `Liked Songs` from library songs. Subscribed artists from `get_library_subscriptions()`.

### Apple Music — MusicKit and REST

- Obtain Apple developer credentials: Team ID, MusicKit-enabled Key ID and `.p8` signing key. Generate short-lived ES256 Developer Tokens on server; never expose `.p8` to browser.
- Authorize a **Music User Token via MusicKit on a trusted browser/web origin**, following Apple's user authorization flow. Mount as `private/apple_user_token.txt` for this starter (temporary, manual token provisioning). Add authenticated reauthorization UI, encrypted token storage, and token lifecycle checks before unattended long-term rollout.
- `GET /v1/me/library/songs` supplies library songs, not Favorites. `POST /v1/me/favorites` supports adding a favorite, but not full two-way removal. Playlist API supports create/add but lacks general deletion/reorder support needed by a full bidirectional engine.
- A Music User Token is personal and sensitive. Keep it off GitHub and out of logs.

## SQLite entities

- `accounts`, `encrypted_tokens`, `oauth_states`: connection states and credential handling.
- `music_items`, `provider_mappings`: canonical identifiers and platform IDs.
- `playlist_entries`: planned detailed occurrence-level playlist projection.
- `source_states`, `snapshot_runs`: last complete provider snapshot per scope and poll audit.
- `sync_events`, `sync_operations`: inbound change log and future outgoing idempotent operation queue.
- `conflicts`: manual review and quarantine.
- `settings`: baseline approval and future safety toggles.

## API contract

| Method | Route | Purpose | Stage |
|---|---|---|---|
| GET | `/health` | container health | implemented, public on loopback |
| GET | `/v1/config` | safe config/status | implemented |
| GET | `/v1/accounts` | connected/error state | implemented |
| GET | `/v1/snapshots` | latest read runs | implemented |
| GET | `/v1/events` | additions/deletion detections | implemented |
| GET | `/v1/conflicts` | mass deletion guard events | implemented |
| POST | `/v1/sync/{provider}` | execute one read snapshot | implemented |
| POST | `/v1/baseline/approve` | enable change detection | implemented |
| POST | `/v1/auth/spotify/start` | issue PKCE auth URL | implemented |
| GET | `/v1/auth/spotify/callback` | Spotify OAuth callback | implemented |
| GET | `/v1/matches/candidates` | candidate review by exact source ID | implemented |
| POST | `/v1/matches/approve` | approve a one-to-one ID pair | implemented |
| POST | `/v1/sync/preview` | preview incoming/outgoing diff | planned |
| GET | `/v1/operations` | inspect durable planned and completed additions | implemented |
| POST | `/v1/operations/{id}/undo` | safe rollback of supported operations | planned |
| POST | `/v1/auth/apple/complete` | verify token and consume one-time authorization state | implemented |

Administrative routes require `X-API-Key`. Provider authorization callback routes use short-lived one-time state. Do not expose them publicly even with an API key alone. Rate-limit on reverse proxy if one is used.

## Operations to implement after baseline (per-platform capabilities)

| Operation | Spotify | YouTube Music | Apple Music |
|---|---|---|---|
| Read liked/saved songs | Yes | Unofficial | Favorites extraction needs verification; library read yes |
| Add/remove liked | Yes, `/me/library` | Unofficial `rate_song` | Add Favorite yes; removing Favorite not generally exposed |
| Read/create own playlist | Yes | Unofficial | Yes |
| Add playlist item | Yes | Unofficial | Yes |
| Remove/reorder playlist item | Yes | Unofficial | Not fully supported by public REST API |
| Read/follow artists | Yes | Unofficial subscriptions | Favorite Artist semantics differ; verify available APIs |

Avoid passing cookies, token values, and music identifiers into exception logs unnecessarily. Implement rate-limit-aware retry with `Retry-After` and a global per-provider request budget before enabling remote writes.
