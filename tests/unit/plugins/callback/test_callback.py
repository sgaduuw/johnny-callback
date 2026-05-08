"""Unit tests for the johnny callback plugin.

Covers the pure helpers (fqdn fallback, truncation, delta parsing,
UUIDv7 polyfill) and the HTTP boundary behaviour (mocked urlopen,
asserting the wire-shape of each POST body against johnny's
contracts/v1.py contract).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from plugins.callback.callback import (
    DIFF_MAX,
    STDOUT_MAX,
    CallbackModule,
    _delta_to_ms,
    _iso_utc,
    _resolve_fqdn,
    _truncate,
    uuid7,
)


# ---------------------------------------------------------- helpers


class TestDocumentationYaml:
    """Ansible parses DOCUMENTATION as YAML at plugin-load time.

    A leading 'word: rest' inside a list item is a mapping key trap;
    folded scalars (``- >``) avoid it. ansible-doc / ansible -m setup
    are the only callers that exercise this path, so guard it here.
    """

    def test_documentation_is_valid_yaml(self) -> None:
        import yaml

        from plugins.callback import callback as mod

        doc = yaml.safe_load(mod.DOCUMENTATION)
        assert isinstance(doc, dict)
        assert doc["name"] == "callback"
        assert doc["type"] == "notification"
        assert isinstance(doc["description"], list)
        assert all(isinstance(line, str) for line in doc["description"])
        assert {"api_url", "api_token", "timeout_seconds"} <= set(doc["options"])


class TestUuid7:
    def test_returns_uuid_with_version_7(self) -> None:
        u = uuid7()
        assert isinstance(u, UUID)
        assert u.version == 7

    def test_unique_across_calls(self) -> None:
        ids = {uuid7() for _ in range(100)}
        assert len(ids) == 100

    def test_time_ordered_prefix(self) -> None:
        # The first 48 bits encode unix_ts_ms. UUIDs generated later
        # should sort >= those generated earlier when bytes-compared.
        first = uuid7()
        second = uuid7()
        assert first.bytes[:6] <= second.bytes[:6]


class TestResolveFqdn:
    def test_prefers_ansible_fqdn(self) -> None:
        f = {"ansible_fqdn": "host.example.com", "ansible_nodename": "host"}
        assert _resolve_fqdn(f, "inv-name") == "host.example.com"

    def test_falls_back_to_nodename(self) -> None:
        f = {"ansible_nodename": "host"}
        assert _resolve_fqdn(f, "inv-name") == "host"

    def test_falls_back_to_inventory_hostname(self) -> None:
        assert _resolve_fqdn({}, "inv-name") == "inv-name"


class TestTruncate:
    def test_under_cap_returns_unchanged(self) -> None:
        assert _truncate("abc", 10) == ("abc", False)

    def test_at_cap_returns_unchanged(self) -> None:
        assert _truncate("abc", 3) == ("abc", False)

    def test_over_cap_truncates(self) -> None:
        assert _truncate("abcdef", 3) == ("abc", True)

    def test_none_or_empty_returns_empty(self) -> None:
        assert _truncate(None, 10) == ("", False)
        assert _truncate("", 10) == ("", False)


class TestDeltaToMs:
    def test_parses_seconds(self) -> None:
        assert _delta_to_ms("0:00:01.000000") == 1000

    def test_parses_minutes(self) -> None:
        assert _delta_to_ms("0:01:30.500000") == 90500

    def test_parses_hours(self) -> None:
        assert _delta_to_ms("1:02:03.000000") == 3723000

    def test_handles_none(self) -> None:
        assert _delta_to_ms(None) == 0

    def test_handles_garbage(self) -> None:
        assert _delta_to_ms("not a delta") == 0


class TestIsoUtc:
    def test_aware_utc_passes_through(self) -> None:
        dt = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
        assert _iso_utc(dt) == "2026-05-08T12:00:00+00:00"

    def test_naive_assumed_utc(self) -> None:
        dt = datetime(2026, 5, 8, 12, 0)
        assert _iso_utc(dt) == "2026-05-08T12:00:00+00:00"


# ---------------------------------------------------------- plugin


def _make_module(
    api_url: str = "http://johnny-api:8001",
    api_token: str = "test-token",
) -> CallbackModule:
    """Construct a CallbackModule with options set, bypassing
    ansible's set_options machinery (which depends on a loaded
    DOCUMENTATION block)."""
    m = CallbackModule()
    m.api_url = api_url.rstrip("/")
    m.api_token = api_token
    m.timeout = 30
    m._display = MagicMock()  # the warning() target
    return m


def _fake_host(name: str = "web1.example.com", groups: list[str] | None = None) -> MagicMock:
    h = MagicMock()
    h.get_name.return_value = name
    h.get_groups.return_value = [
        MagicMock(get_name=MagicMock(return_value=g))
        for g in (groups or [])
    ]
    return h


def _fake_task(action: str = "apt", name: str = "install nginx") -> MagicMock:
    t = MagicMock()
    t.action = action
    t.get_name.return_value = name
    return t


def _fake_result(
    *,
    host: MagicMock | None = None,
    task: MagicMock | None = None,
    rdata: dict[str, Any] | None = None,
) -> MagicMock:
    r = MagicMock()
    r._host = host or _fake_host()
    r._task = task or _fake_task()
    r._result = rdata or {}
    return r


class TestRecordFacts:
    def test_captures_fact_dict_and_groups(self) -> None:
        m = _make_module()
        m._record_facts(_fake_result(
            host=_fake_host("web1.example.com", ["webservers", "linux"]),
            task=_fake_task(action="setup"),
            rdata={
                "ansible_facts": {
                    "ansible_fqdn": "web1.example.com",
                    "ansible_uptime_seconds": 3600,
                }
            },
        ))
        assert len(m._facts) == 1
        rec = m._facts[0]
        assert rec["fqdn"] == "web1.example.com"
        assert rec["inventory_hostname"] == "web1.example.com"
        assert rec["groups"] == ["webservers", "linux"]
        assert rec["ansible_facts"]["ansible_uptime_seconds"] == 3600

    def test_skips_when_no_facts(self) -> None:
        m = _make_module()
        m._record_facts(_fake_result(rdata={"ansible_facts": {}}))
        assert m._facts == []


class TestRecordEvent:
    def test_basic_ok_event(self) -> None:
        m = _make_module()
        m._record_event(_fake_result(
            host=_fake_host("h.example.com"),
            task=_fake_task(action="apt", name="install nginx"),
            rdata={"changed": False, "delta": "0:00:00.250000"},
        ), base_status="ok")
        assert len(m._events) == 1
        e = m._events[0]
        assert e["fqdn"] == "h.example.com"
        assert e["task_name"] == "install nginx"
        assert e["task_action"] == "apt"
        assert e["status"] == "ok"
        assert e["duration_ms"] == 250
        assert e["stdout"] == ""
        assert e["stdout_truncated"] is False
        assert e["diff"] is None
        UUID(e["event_uuid"])  # parses

    def test_changed_status_when_ok_and_changed(self) -> None:
        m = _make_module()
        m._record_event(
            _fake_result(rdata={"changed": True}),
            base_status="ok",
        )
        assert m._events[0]["status"] == "changed"

    def test_truncates_stdout_at_cap(self) -> None:
        m = _make_module()
        long_stdout = "x" * (STDOUT_MAX + 100)
        m._record_event(
            _fake_result(rdata={"stdout": long_stdout}),
            base_status="ok",
        )
        e = m._events[0]
        assert len(e["stdout"]) == STDOUT_MAX
        assert e["stdout_truncated"] is True

    def test_truncates_diff_at_cap(self) -> None:
        m = _make_module()
        long_diff = "y" * (DIFF_MAX + 100)
        m._record_event(
            _fake_result(rdata={"diff": long_diff, "changed": True}),
            base_status="ok",
        )
        assert len(m._events[0]["diff"]) == DIFF_MAX

    def test_serialises_dict_diff_as_json(self) -> None:
        m = _make_module()
        m._record_event(
            _fake_result(rdata={"diff": {"before": "a", "after": "b"}, "changed": True}),
            base_status="ok",
        )
        diff = m._events[0]["diff"]
        assert diff is not None
        assert json.loads(diff) == {"before": "a", "after": "b"}


# ---------------------------------------------------------- HTTP boundary


@pytest.fixture
def captured_posts():
    """Patch urlopen, capture (url, body, headers) for every POST."""
    captured: list[dict[str, Any]] = []

    class _FakeResponse:
        status = 202
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout=None):
        captured.append({
            "url": req.full_url,
            "method": req.get_method(),
            "headers": dict(req.headers),
            "body": json.loads(req.data.decode()) if req.data else None,
            "timeout": timeout,
        })
        return _FakeResponse()

    with patch(
        "plugins.callback.callback._urlrequest.urlopen",
        side_effect=_fake_urlopen,
    ):
        yield captured


class TestHttpBoundary:
    def test_post_includes_bearer_auth(self, captured_posts) -> None:
        m = _make_module(api_token="my-token")
        m._post("/api/v1/playbooks", {"id": "abc"})
        assert captured_posts[0]["headers"]["Authorization"] == "Bearer my-token"
        assert captured_posts[0]["headers"]["Content-type"] == "application/json"

    def test_post_skips_when_url_missing(self, captured_posts) -> None:
        m = _make_module(api_url="")
        m._post("/api/v1/playbooks", {})
        assert captured_posts == []
        m._display.warning.assert_called_once()

    def test_post_skips_when_token_missing(self, captured_posts) -> None:
        m = _make_module(api_token="")
        m._post("/api/v1/playbooks", {})
        assert captured_posts == []
        m._display.warning.assert_called_once()

    def test_post_swallows_http_error(self, captured_posts) -> None:
        # Replace the patched urlopen with one that raises.
        m = _make_module()
        from urllib.error import URLError
        with patch(
            "plugins.callback.callback._urlrequest.urlopen",
            side_effect=URLError("connection refused"),
        ):
            m._post("/api/v1/playbooks", {})
        m._display.warning.assert_called_once()

    def test_post_swallows_arbitrary_exception(self) -> None:
        m = _make_module()
        with patch(
            "plugins.callback.callback._urlrequest.urlopen",
            side_effect=RuntimeError("boom"),
        ):
            m._post("/api/v1/playbooks", {})  # must not raise
        m._display.warning.assert_called_once()


class TestPlaybookLifecyclePayloads:
    """Sanity-check the wire shape against johnny's contracts/v1.py."""

    def test_post_start_body(self, captured_posts) -> None:
        m = _make_module()
        m.playbook_id = uuid7()
        m.playbook_started_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
        m.playbook_name = "deploy.yml"
        m.inventory_sources = ["inventory.yml"]
        m.user = "ansible"
        m._post_start()
        assert len(captured_posts) == 1
        body = captured_posts[0]["body"]
        assert body["id"] == str(m.playbook_id)
        assert body["name"] == "deploy.yml"
        assert body["inventory_sources"] == ["inventory.yml"]
        assert body["user"] == "ansible"
        assert body["check_mode"] is False
        assert body["tags"] == []
        assert body["skip_tags"] == []

    def test_post_start_is_idempotent_per_playbook(self, captured_posts) -> None:
        m = _make_module()
        m.playbook_id = uuid7()
        m.playbook_started_at = datetime.now(timezone.utc)
        m._post_start()
        m._post_start()
        assert len(captured_posts) == 1

    def test_post_facts_body(self, captured_posts) -> None:
        m = _make_module()
        m.playbook_id = uuid7()
        m._facts = [{
            "fqdn": "h.example.com",
            "inventory_hostname": "h",
            "groups": ["webservers"],
            "ansible_facts": {"ansible_uptime_seconds": 1},
        }]
        m._post_facts()
        body = captured_posts[0]["body"]
        assert "captured_at" in body
        assert body["hosts"] == m._facts
        assert captured_posts[0]["url"].endswith(f"/api/v1/playbooks/{m.playbook_id}/facts")

    def test_post_events_body(self, captured_posts) -> None:
        m = _make_module()
        m.playbook_id = uuid7()
        m._events = [{
            "event_uuid": str(uuid7()),
            "fqdn": "h.example.com",
            "task_name": "install",
            "task_action": "apt",
            "status": "ok",
            "started_at": _iso_utc(datetime.now(timezone.utc)),
            "duration_ms": 100,
            "stdout": "",
            "stdout_truncated": False,
            "diff": None,
        }]
        m._post_events()
        body = captured_posts[0]["body"]
        assert body == {"events": m._events}
        assert captured_posts[0]["url"].endswith(f"/api/v1/playbooks/{m.playbook_id}/events")

    def test_post_finish_body_shape(self, captured_posts) -> None:
        m = _make_module()
        m.playbook_id = uuid7()
        # Fake an ansible AggregateStats object
        stats = MagicMock()
        stats.processed = {"h.example.com": 1}
        stats.summarize.return_value = {
            "ok": 5, "changed": 1, "failures": 0, "unreachable": 0,
            "skipped": 2, "rescued": 0, "ignored": 0,
        }
        m._post_finish(stats)
        body = captured_posts[0]["body"]
        assert "finished_at" in body
        assert "h.example.com" in body["stats"]
        assert body["stats"]["h.example.com"] == {
            "ok": 5, "changed": 1, "failed": 0, "unreachable": 0,
            "skipped": 2, "rescued": 0, "ignored": 0,
        }
        assert captured_posts[0]["url"].endswith(f"/api/v1/playbooks/{m.playbook_id}/finish")


# ---------------------------------------------------------- v2_* hooks


class TestV2Hooks:
    """Verify the ansible v2_* callback hooks dispatch correctly."""

    def test_playbook_on_start_initialises_state(self) -> None:
        m = _make_module()
        playbook = MagicMock(_file_name="/path/to/deploy.yml")
        m.v2_playbook_on_start(playbook)
        assert m.playbook_id is not None
        assert m.playbook_id.version == 7
        assert m.playbook_started_at is not None
        assert m.playbook_name == "deploy.yml"

    def test_runner_on_ok_records_event(self) -> None:
        m = _make_module()
        m.v2_runner_on_ok(_fake_result(rdata={"changed": False}))
        assert len(m._events) == 1
        assert m._events[0]["status"] == "ok"

    def test_runner_on_ok_with_setup_task_also_records_facts(self) -> None:
        m = _make_module()
        result = _fake_result(
            host=_fake_host("h.example.com", ["webservers"]),
            task=_fake_task(action="setup"),
            rdata={"ansible_facts": {"ansible_uptime_seconds": 1}},
        )
        m.v2_runner_on_ok(result)
        assert len(m._events) == 1
        assert len(m._facts) == 1

    def test_runner_on_failed_records_failed_event(self) -> None:
        m = _make_module()
        m.v2_runner_on_failed(_fake_result())
        assert m._events[0]["status"] == "failed"

    def test_runner_on_unreachable_records_unreachable_event(self) -> None:
        m = _make_module()
        m.v2_runner_on_unreachable(_fake_result())
        assert m._events[0]["status"] == "unreachable"

    def test_runner_on_skipped_records_skipped_event(self) -> None:
        m = _make_module()
        m.v2_runner_on_skipped(_fake_result())
        assert m._events[0]["status"] == "skipped"

    def test_playbook_on_stats_flushes_in_order(self, captured_posts) -> None:
        # facts -> events -> finish ordering matters: johnny's
        # ingest_facts auto-creates host rows, but ordering keeps
        # foreign-key joins predictable in the read tier.
        m = _make_module()
        m.playbook_id = uuid7()
        m._facts = [{
            "fqdn": "h.example.com",
            "inventory_hostname": "h",
            "groups": [],
            "ansible_facts": {"ansible_uptime_seconds": 1},
        }]
        m._events = [{
            "event_uuid": str(uuid7()),
            "fqdn": "h.example.com",
            "task_name": "t",
            "task_action": "apt",
            "status": "ok",
            "started_at": _iso_utc(datetime.now(timezone.utc)),
            "duration_ms": 1,
            "stdout": "",
            "stdout_truncated": False,
            "diff": None,
        }]
        stats = MagicMock()
        stats.processed = {"h.example.com": 1}
        stats.summarize.return_value = {
            "ok": 1, "changed": 0, "failures": 0, "unreachable": 0,
            "skipped": 0, "rescued": 0, "ignored": 0,
        }
        m.v2_playbook_on_stats(stats)
        urls = [p["url"] for p in captured_posts]
        assert urls[0].endswith("/facts")
        assert urls[1].endswith("/events")
        assert urls[2].endswith("/finish")

    def test_playbook_on_stats_skips_empty_buffers(self, captured_posts) -> None:
        # No facts and no events recorded — only /finish should fire.
        m = _make_module()
        m.playbook_id = uuid7()
        stats = MagicMock()
        stats.processed = {}
        m.v2_playbook_on_stats(stats)
        assert len(captured_posts) == 1
        assert captured_posts[0]["url"].endswith("/finish")

    def test_playbook_on_play_start_captures_inventory_sources(
        self, captured_posts
    ) -> None:
        m = _make_module()
        m.playbook_id = uuid7()
        m.playbook_started_at = datetime.now(timezone.utc)
        play = MagicMock()
        vm = MagicMock()
        vm._inventory._sources = ["inventory.yml", "extra.yml"]
        play.get_variable_manager.return_value = vm
        m.v2_playbook_on_play_start(play)
        assert m.inventory_sources == ["inventory.yml", "extra.yml"]
        # And it triggers the start POST (idempotent on subsequent calls).
        assert any(p["url"].endswith("/api/v1/playbooks") for p in captured_posts)
        m.v2_playbook_on_play_start(play)  # second play; should NOT re-POST
        starts = [p for p in captured_posts if p["url"].endswith("/api/v1/playbooks")]
        assert len(starts) == 1


class TestIsSetupTask:
    def test_recognises_short_form(self) -> None:
        assert CallbackModule._is_setup_task(_fake_task(action="setup"))

    def test_recognises_fqcn(self) -> None:
        assert CallbackModule._is_setup_task(
            _fake_task(action="ansible.builtin.setup")
        )

    def test_recognises_gather_facts(self) -> None:
        assert CallbackModule._is_setup_task(_fake_task(action="gather_facts"))

    def test_rejects_other_actions(self) -> None:
        assert not CallbackModule._is_setup_task(_fake_task(action="apt"))
