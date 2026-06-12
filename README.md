# johnny-callback

Ansible callback plugin that POSTs playbook results (host facts,
per-task events, run stats) to a [johnny][johnny] instance for
fleet-state reporting.

[johnny]: https://github.com/sgaduuw/johnny

Shipped as the **`sgaduuw.johnny`** Ansible collection, FQCN
`sgaduuw.johnny.callback`.

## What it does

On every playbook run, the plugin observes the v2_* callback hooks,
buffers facts and events per play, and at `v2_playbook_on_stats`
sends four POSTs to johnny-api:

| Endpoint                              | When                                     |
|---------------------------------------|------------------------------------------|
| `POST /api/v1/playbooks`              | First play start, with run metadata      |
| `POST /api/v1/playbooks/{id}/facts`   | After all plays end, host fact snapshots |
| `POST /api/v1/playbooks/{id}/events`  | After all plays end, per-task results    |
| `POST /api/v1/playbooks/{id}/finish`  | Last, with the per-host stats summary    |

The start body (`POST /api/v1/playbooks`) includes
`groups_topology`: a map of parent group name to its sorted direct
child group names, derived from the inventory at play start.
Requires **johnny server >= 0.5.0** to receive it (older servers
use `extra="forbid"` and will reject the field).

Best-effort: if johnny-api is down or unreachable, the plugin logs
a warning and the play continues. johnny's idempotent ingest
(plugin-generated UUIDv7 IDs + `INSERT OR IGNORE` on `event_uuid`)
makes plugin retries safe.

Zero runtime dependencies — stdlib `urllib` only.

## Install

From [galaxy.ansible.com](https://galaxy.ansible.com/ui/repo/published/sgaduuw/johnny/)
(canonical):

```sh
# Latest released
ansible-galaxy collection install sgaduuw.johnny

# Pinned to a specific version
ansible-galaxy collection install sgaduuw.johnny:0.2.1
```

From git, for an unreleased `main`:

```sh
ansible-galaxy collection install \
    git+https://github.com/sgaduuw/johnny-callback.git
```

## Configure

Two equivalent paths; environment variables win on conflict.

**Via `ansible.cfg`:**

```ini
[defaults]
callbacks_enabled = sgaduuw.johnny.callback

[callback_johnny]
api_url = https://johnny-api.internal:8001
timeout_seconds = 30
```

**Via environment:**

```sh
export JOHNNY_API_URL=https://johnny-api.internal:8001
export JOHNNY_API_TOKEN=<bearer-token-matching-johnny-api>
```

The bearer token should *always* come from environment / secrets
manager, never `ansible.cfg`.

| Option              | Env var                  | ini key              | Required         |
|---------------------|--------------------------|----------------------|------------------|
| `api_url`           | `JOHNNY_API_URL`         | `api_url`            | yes              |
| `api_token`         | `JOHNNY_API_TOKEN`       | `api_token`          | yes              |
| `timeout_seconds`   | `JOHNNY_API_TIMEOUT`     | `timeout_seconds`    | no (default 30)  |
| `facts_chunk_size`  | `JOHNNY_API_FACTS_CHUNK` | `facts_chunk_size`   | no (default 25)  |
| `events_chunk_size` | `JOHNNY_API_EVENTS_CHUNK`| `events_chunk_size`  | no (default 500) |

### Chunked flush (large fleets)

By default the plugin flushes facts in batches of **25 hosts** and
events in batches of **500** while the play runs, instead of holding
everything in memory until `v2_playbook_on_stats`. This bounds plugin
memory on the controller and lets hosts appear in johnny mid-play.

Tuning:

- `JOHNNY_API_FACTS_CHUNK=0` / `JOHNNY_API_EVENTS_CHUNK=0` disables
  chunking and reverts to the pre-0.2 single-flush-at-end behaviour.
- Each chunk POST is synchronous and best-effort — a failed chunk is
  logged via `display.warning()` and dropped, matching the existing
  log-and-swallow contract. Set `JOHNNY_API_TIMEOUT` lower (e.g. 10s)
  in chunked mode so a dead api fails fast per chunk rather than
  stretching the play with N × 30s timeouts.
- For small fleets (< ~100 hosts) the chunking is irrelevant; the
  defaults emit one or two POSTs per endpoint regardless.

## Compatibility

- ansible-core >= 2.16
- Python >= 3.10 (controller-side; plugin polyfills UUIDv7 until 3.14)
- johnny server >= 0.5.0 (required to accept the `groups_topology` field)

## License

MIT — see [LICENSE](LICENSE).
