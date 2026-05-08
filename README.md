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
ansible-galaxy collection install sgaduuw.johnny:0.1.0
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

| Option            | Env var               | ini key            | Required        |
|-------------------|-----------------------|--------------------|-----------------|
| `api_url`         | `JOHNNY_API_URL`      | `api_url`          | yes             |
| `api_token`       | `JOHNNY_API_TOKEN`    | `api_token`        | yes             |
| `timeout_seconds` | `JOHNNY_API_TIMEOUT`  | `timeout_seconds`  | no (default 30) |

## Compatibility

- ansible-core >= 2.16
- Python >= 3.10 (controller-side; plugin polyfills UUIDv7 until 3.14)

## License

MIT — see [LICENSE](LICENSE).
