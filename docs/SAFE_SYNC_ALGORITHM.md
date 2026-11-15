# Design for future bidirectional writer (NOT included in 0.1.0)

## Target behavior

On first connection, import all accounts to a union catalog. After baseline approval, detect changes on 5/10/10-minute polls. Sync additions and removals automatically when the service capability supports the operation and safety preconditions hold. Different concepts (`liked`, `library`, `followed_artist`, `playlist_membership`) MUST not be conflated.

## Inbound event protocol

1. Poll provider to completion, including every page. Identify account and playlists authorized to edit.
2. On ANY partial page, 401, 403, 429, timeout, auth failure or API shape change: mark poll as failed, do NOT persist missing items, schedule an adaptive retry.
3. Use stable occurrence keys for each playlist. Compare previous fully-confirmed snapshot with new full snapshot per scope. Remove detection requires 2 full missing observations separated by the polling interval.
4. On a mass-deletion threshold violation, quarantine scope and require manual review. On provider outage/re-auth, require re-baseline before destructive sync.
5. Resolve canonical identity. Prefer exact ISRC + artist/title/duration; detect version/recording differences. Zero destructive writes when mapping is ambiguous.
6. Generate idempotent `sync_operations` with `(origin_provider,event_id,target_provider,canonical_id,action,scope)` fingerprint; only when target mapping is reviewed and capability allows action.
7. Before executing removal, require target to have previously been associated with the same mirrored liked state or playlist pairing; never remove unrelated personal songs merely because a song disappeared elsewhere.
8. Verify target state immediately before mutation (fresh read/check). If target has newer independent change, quarantine as conflict.
9. Apply write with capped retries, `Retry-After`, jitter, request budgets. Read back once the platform becomes consistent. Set `applied` only after verification. Every operation has a durable audit entry.
10. Store `origin_event_id`, fingerprint and read-back timestamp to suppress feedback loops; do not suppress genuine independent changes.

## Authoritative state and conflicts

This is not a last-write-wins global mirror. Each action belongs to a scope (`liked`, `artist`, or a paired playlist) and a provider. On simultaneous add/remove of the same item, preserve the item and queue review (add-wins conflict resolution) unless reliable platform event timestamps conclusively order the changes. Detection time is NOT the user's action time and must not be used as though it were authoritative.

Deleting an entire playlist on one service must not delete a playlist created by another person or a non-mirrored destination. In paired playlists, deleting the source does not by default delete the destination playlist; create a tombstone and flag for review.

Playlist reordering requires a stable position/occurrence mapping and an API that supports reordering. Position shifts must not be interpreted as removes and adds. Keep duplicate tracks separately by their occurrence IDs, and preserve order only when there is a verified one-to-one pairing.

## Per-platform write capability (validation step)

- Spotify: save/remove song via `PUT/DELETE /me/library` with `uris=spotify:track:<id>` and appropriate OAuth scopes. Playlist item management via `/playlists/{id}/items` (Dev Mode changes from Feb 2026). Artist follow/unfollow via generic library endpoints, verify URI support/capabilities before enabling.
- YT Music: `rate_song(videoId,'LIKE'/'INDIFFERENT')`, `subscribe_artists` and `unsubscribe_artists`, `add_playlist_items`, `remove_playlist_items`, `edit_playlist(moveItem=...)` via unofficial `ytmusicapi`. Validate methods against installed version and live account before turning on writes. Beware that 'LIKE' does not equal adding to Songs Library.
- Apple Music: `POST /v1/me/favorites` can add favorites. No general third-party unfavorite operation; do not claim 'removed' success. Add library songs/playlists and add playlist items only with official supported endpoints. An unsupported removal goes to `unsupported` without retry spam. The user's Apple Library Songs list must not be interpreted as the same as Favorited Songs.

## Extra safe deletion policy

`DELETE_MODE=auto` means the user wants removals to propagate **where possible** after full testing. It does NOT override missing capability, initial baseline protection, unmatched identity, or mass-deletion guards. A future operator can enable per-target flags such as:

```yaml
spotify:
  liked: {add: true, remove: true}
  playlist: {add: true, remove: true, reorder: true}
ytmusic:
  liked: {add: true, remove: true}
  playlist: {add: true, remove: true, reorder: true}
apple:
  favorites: {add: true, remove: false}
  playlist: {add: true, remove: false, reorder: false}
```

No direct provider-to-provider writes: all events go via canonical records and operation queue with idempotency keys. This makes audit and rollback possible.
