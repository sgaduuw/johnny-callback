# -*- coding: utf-8 -*-
# Copyright (c) 2026 Eelco Wesemann
# MIT License (see LICENSE)

"""Ansible callback plugin: ship playbook results to johnny-api.

Buffers facts (from setup tasks) and events (from every task result)
during a play, flushes at v2_playbook_on_stats. Best-effort: HTTP
failures are logged via display.warning() and never raised. The wire
contract lives in johnny/contracts/v1.py (sibling repo).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib import request as _urlrequest
from urllib.error import HTTPError, URLError

from ansible.plugins.callback import CallbackBase

DOCUMENTATION = """
  name: callback
  type: notification
  author: Eelco Wesemann (@sgaduuw)
  short_description: Ship playbook results to johnny-api
  version_added: "0.1.0"
  description:
    - >
      Observes playbook execution via the v2_* callback hooks, buffers
      facts and events per play, and POSTs to johnny-api at the end.
    - >
      Best-effort design. Any HTTP failure is logged as a warning and
      the play continues. johnny's idempotent ingest (event_uuid UNIQUE,
      plugin-generated playbook_id) makes plugin retries safe.
  requirements:
    - johnny-api reachable from the controller running ansible
  options:
    api_url:
      description: Base URL of johnny-api (no trailing slash, no /api/v1).
      type: str
      required: true
      env:
        - name: JOHNNY_API_URL
      ini:
        - section: callback_johnny
          key: api_url
    api_token:
      description: Bearer token sent in Authorization header.
      type: str
      required: true
      env:
        - name: JOHNNY_API_TOKEN
      ini:
        - section: callback_johnny
          key: api_token
    timeout_seconds:
      description: Per-request HTTP timeout in seconds.
      type: int
      default: 30
      env:
        - name: JOHNNY_API_TIMEOUT
      ini:
        - section: callback_johnny
          key: timeout_seconds
"""

STDOUT_MAX = 4096
DIFF_MAX = 16384

# UUIDv7 polyfill. uuid.uuid7() lands in Python 3.14 stdlib; until
# 3.14+ is our floor, generate inline per RFC 9562. Behaviour-
# identical to the eventual stdlib version, so the migration is a
# one-line drop.
try:
    from uuid import uuid7  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    def uuid7() -> uuid.UUID:
        unix_ts_ms = int(time.time() * 1000)
        rand = os.urandom(10)
        b = bytearray(16)
        b[0] = (unix_ts_ms >> 40) & 0xff
        b[1] = (unix_ts_ms >> 32) & 0xff
        b[2] = (unix_ts_ms >> 24) & 0xff
        b[3] = (unix_ts_ms >> 16) & 0xff
        b[4] = (unix_ts_ms >> 8) & 0xff
        b[5] = unix_ts_ms & 0xff
        b[6] = 0x70 | (rand[0] & 0x0f)  # version 7 in upper nibble
        b[7] = rand[1]
        b[8] = 0x80 | (rand[2] & 0x3f)  # variant 10xx in upper bits
        b[9:16] = rand[3:10]
        return uuid.UUID(bytes=bytes(b))


def _resolve_fqdn(facts: dict, inventory_hostname: str) -> str:
    """Fallback chain: ansible_fqdn -> ansible_nodename -> inventory_hostname."""
    return (
        facts.get("ansible_fqdn")
        or facts.get("ansible_nodename")
        or inventory_hostname
    )


def _truncate(s: str | None, cap: int) -> tuple[str, bool]:
    """Return (text, was_truncated). None becomes ('', False)."""
    if not s:
        return ("", False)
    if len(s) <= cap:
        return (s, False)
    return (s[:cap], True)


def _iso_utc(dt: datetime) -> str:
    """ISO 8601 string in UTC; naive datetimes assumed UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _delta_to_ms(delta: str | None) -> int:
    """Ansible's `delta` field is 'HH:MM:SS.ffffff'."""
    if not delta:
        return 0
    try:
        h, m, s = delta.split(":")
        return int((int(h) * 3600 + int(m) * 60 + float(s)) * 1000)
    except (ValueError, AttributeError):
        return 0


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "notification"
    CALLBACK_NAME = "sgaduuw.johnny.callback"
    CALLBACK_NEEDS_ENABLED = True  # opt-in via callbacks_enabled

    def __init__(self) -> None:
        super().__init__()
        self.playbook_id: uuid.UUID | None = None
        self.playbook_started_at: datetime | None = None
        self.playbook_name: str = ""
        self.inventory_sources: list[str] = []
        self.user: str = ""
        self.limit: str | None = None
        self.tags: list[str] = []
        self.skip_tags: list[str] = []
        self.check_mode: bool = False
        self._facts: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self._started_posted = False

    def set_options(self, task_keys=None, var_options=None, direct=None) -> None:
        super().set_options(
            task_keys=task_keys, var_options=var_options, direct=direct
        )
        self.api_url: str = (self.get_option("api_url") or "").rstrip("/")
        self.api_token: str = self.get_option("api_token") or ""
        self.timeout: int = int(self.get_option("timeout_seconds") or 30)

    # -------- ansible callback hooks --------

    def v2_playbook_on_start(self, playbook) -> None:
        self.playbook_id = uuid7()
        self.playbook_started_at = datetime.now(timezone.utc)
        try:
            self.playbook_name = os.path.basename(playbook._file_name)
        except AttributeError:
            self.playbook_name = "<unknown>"
        self.user = os.environ.get("USER", "ansible")

    def v2_playbook_on_play_start(self, play) -> None:
        # Inventory sources are play-scoped via the variable_manager.
        # First play in the playbook is enough; later plays reuse.
        if not self.inventory_sources:
            try:
                vm = play.get_variable_manager()
                inv = getattr(vm, "_inventory", None)
                sources = getattr(inv, "_sources", None) if inv else None
                if sources:
                    self.inventory_sources = [str(s) for s in sources]
            except Exception:  # noqa: BLE001 - defensive; never raise
                pass
            self._post_start()

    def v2_runner_on_ok(self, result) -> None:
        self._record_event(result, base_status="ok")
        if self._is_setup_task(result._task):
            self._record_facts(result)

    def v2_runner_on_failed(self, result, ignore_errors: bool = False) -> None:
        self._record_event(result, base_status="failed")

    def v2_runner_on_unreachable(self, result) -> None:
        self._record_event(result, base_status="unreachable")

    def v2_runner_on_skipped(self, result) -> None:
        self._record_event(result, base_status="skipped")

    def v2_playbook_on_stats(self, stats) -> None:
        if self._facts:
            self._post_facts()
        if self._events:
            self._post_events()
        self._post_finish(stats)

    # -------- buffer fillers --------

    @staticmethod
    def _is_setup_task(task) -> bool:
        action = getattr(task, "action", "") or ""
        return action in ("setup", "ansible.builtin.setup", "gather_facts")

    def _record_event(self, result, base_status: str) -> None:
        task = result._task
        host = result._host
        rdata = result._result or {}

        # ok + changed -> "changed" enum value (matches johnny's TaskStatus)
        status = base_status
        if base_status == "ok" and rdata.get("changed", False):
            status = "changed"

        stdout = rdata.get("stdout") or rdata.get("module_stderr") or ""
        stdout_t, stdout_was_truncated = _truncate(str(stdout), STDOUT_MAX)

        diff_raw = rdata.get("diff")
        diff_str: str | None = None
        if diff_raw:
            diff_str = (
                diff_raw if isinstance(diff_raw, str) else json.dumps(diff_raw)
            )
            diff_str, _ = _truncate(diff_str, DIFF_MAX)

        fqdn = _resolve_fqdn(rdata.get("ansible_facts", {}) or {}, host.get_name())

        self._events.append({
            "event_uuid": str(uuid7()),
            "fqdn": fqdn,
            "task_name": task.get_name() or "<unnamed>",
            "task_action": getattr(task, "action", "") or "unknown",
            "status": status,
            "started_at": _iso_utc(datetime.now(timezone.utc)),
            "duration_ms": _delta_to_ms(rdata.get("delta")),
            "stdout": stdout_t,
            "stdout_truncated": stdout_was_truncated,
            "diff": diff_str,
        })

    def _record_facts(self, result) -> None:
        host = result._host
        ansible_facts = (result._result or {}).get("ansible_facts") or {}
        if not ansible_facts:
            return
        try:
            groups = [g.get_name() for g in host.get_groups()]
        except Exception:  # noqa: BLE001
            groups = []
        self._facts.append({
            "fqdn": _resolve_fqdn(ansible_facts, host.get_name()),
            "inventory_hostname": host.get_name(),
            "groups": groups,
            "ansible_facts": dict(ansible_facts),
        })

    # -------- HTTP --------

    def _post_start(self) -> None:
        if self._started_posted:
            return  # plays after the first reuse the same playbook record
        self._started_posted = True
        body = {
            "id": str(self.playbook_id),
            "name": self.playbook_name,
            "inventory_sources": self.inventory_sources or ["<unknown>"],
            "started_at": _iso_utc(self.playbook_started_at or datetime.now(timezone.utc)),
            "user": self.user or "ansible",
            "limit": self.limit,
            "tags": self.tags,
            "skip_tags": self.skip_tags,
            "check_mode": self.check_mode,
        }
        self._post("/api/v1/playbooks", body)

    def _post_facts(self) -> None:
        body = {
            "captured_at": _iso_utc(datetime.now(timezone.utc)),
            "hosts": self._facts,
        }
        self._post(f"/api/v1/playbooks/{self.playbook_id}/facts", body)

    def _post_events(self) -> None:
        body = {"events": self._events}
        self._post(f"/api/v1/playbooks/{self.playbook_id}/events", body)

    def _post_finish(self, stats) -> None:
        per_host: dict[str, dict[str, int]] = {}
        try:
            hosts = sorted(stats.processed.keys())
        except AttributeError:
            hosts = []
        for hostname in hosts:
            try:
                s = stats.summarize(hostname)
            except Exception:  # noqa: BLE001
                continue
            per_host[hostname] = {
                "ok": s.get("ok", 0),
                "changed": s.get("changed", 0),
                "failed": s.get("failures", 0),
                "unreachable": s.get("unreachable", 0),
                "skipped": s.get("skipped", 0),
                "rescued": s.get("rescued", 0),
                "ignored": s.get("ignored", 0),
            }
        body = {
            "finished_at": _iso_utc(datetime.now(timezone.utc)),
            "stats": per_host,
        }
        self._post(f"/api/v1/playbooks/{self.playbook_id}/finish", body)

    def _post(self, path: str, body: dict[str, Any]) -> None:
        if not self.api_url:
            self._display.warning(
                f"johnny-callback: api_url not configured; skipping {path}"
            )
            return
        if not self.api_token:
            self._display.warning(
                f"johnny-callback: api_token not configured; skipping {path}"
            )
            return
        url = f"{self.api_url}{path}"
        data = json.dumps(body).encode("utf-8")
        req = _urlrequest.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_token}",
            },
        )
        try:
            with _urlrequest.urlopen(req, timeout=self.timeout) as resp:
                if resp.status >= 400:
                    self._display.warning(
                        f"johnny-callback: {path} -> HTTP {resp.status}"
                    )
        except HTTPError as e:
            self._display.warning(
                f"johnny-callback: {path} -> HTTP {e.code} {e.reason}"
            )
        except URLError as e:
            self._display.warning(
                f"johnny-callback: {path} -> {e.reason}"
            )
        except Exception as e:  # noqa: BLE001 - never raise from a hook
            self._display.warning(
                f"johnny-callback: {path} -> unexpected error: {e}"
            )
