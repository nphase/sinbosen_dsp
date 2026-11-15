from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import time
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")
pytest.importorskip("scipy")

with warnings.catch_warnings():
    # Starlette's TestClient warns at import that it prefers httpx2 and uses a deprecated anyio alias.
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from sinbosen_la548.client import Client, LockState  # noqa: E402
from sinbosen_la548.service import ControlKey, DSPService, controls, demo_settings, replace_control  # noqa: E402
from sinbosen_la548.studio_app import create_studio_app  # noqa: E402
from sinbosen_la548.studio_store import StudioStore  # noqa: E402


@pytest.fixture
def store(tmp_path: Path) -> StudioStore:
    return StudioStore(tmp_path / "studio.json")


def make_client(service: DSPService, store: StudioStore, **options: Any) -> TestClient:
    return TestClient(create_studio_app(service, store=store, **options), base_url="http://127.0.0.1")


@pytest.fixture
def client(store: StudioStore) -> Any:
    with make_client(DSPService(demo_settings()), store) as test_client:
        yield test_client


JSON = {"content-type": "application/json"}


def post(client: TestClient, path: str, **body: Any) -> Any:
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def channel(state: dict[str, Any], channel_id: str) -> dict[str, Any]:
    return next(c for c in state["channels"] if c["id"] == channel_id)


def test_state_after_start_is_the_demo(client: TestClient) -> None:
    state = client.get("/api/state").json()
    assert state["status"]["demo"] is True
    assert state["status"]["connected"] is False
    assert state["status"]["device"] is None
    assert state["status"]["pending_count"] == 0
    assert state["selected"]["id"] == "out:0"
    assert channel(state, "out:0")["selected"] is True
    assert len(state["channels"]) == 12


def test_edit_eq_field_is_pending_and_revert_clears_it(client: TestClient) -> None:
    post(client, "/api/select", id="out:0")
    state = post(client, "/api/band/add", id="out:0")
    assert state["selected"]["eq_used"] == 1
    post(client, "/api/apply")
    state = post(client, "/api/edit", id="out:0", kind="eq", index=0, field="gain_db", value=4.5)
    assert state["status"]["pending_count"] == 1
    card = next(c for c in state["selected"]["filters"] if c["key"] == "eq:0")
    assert next(f for f in card["fields"] if f["key"] == "gain_db")["value"] == 4.5
    assert card["pending"] is True
    state = post(client, "/api/revert")
    assert state["status"]["pending_count"] == 0
    card = next(c for c in state["selected"]["filters"] if c["key"] == "eq:0")
    assert next(f for f in card["fields"] if f["key"] == "gain_db")["value"] == 0


def test_edit_delay_and_crossover(client: TestClient) -> None:
    state = post(client, "/api/edit", id="out:2", kind="delay", field="delay_ms", value=1.5)
    assert channel(state, "out:2")["delay_ms"] == 1.5
    assert channel(state, "out:2")["delay_pending"] is True
    post(client, "/api/select", id="out:2")
    state = post(client, "/api/edit", id="out:2", kind="hp", field="freq_hz", value=80)
    hp = next(c for c in state["selected"]["filters"] if c["key"] == "hp")
    assert next(f for f in hp["fields"] if f["key"] == "freq_hz")["value"] == 80
    assert state["status"]["pending_count"] == 2


def test_edit_takes_several_fields_of_one_block_at_once(client: TestClient) -> None:
    post(client, "/api/select", id="out:0")
    post(client, "/api/band/add", id="out:0")
    post(client, "/api/apply")
    state = post(client, "/api/edit", id="out:0", kind="eq", index=0, values={"freq_hz": 480, "gain_db": -4.0})
    card = next(c for c in state["selected"]["filters"] if c["key"] == "eq:0")
    assert {f["key"]: f["value"] for f in card["fields"] if f["key"] != "q"} == {"type": 0, "freq_hz": 480,
                                                                                  "gain_db": -4.0}
    assert state["status"]["pending_count"] == 1
    state = post(client, "/api/edit", id="out:0", kind="hp", values={"freq_hz": 90, "enabled": True})
    hp = next(c for c in state["selected"]["filters"] if c["key"] == "hp")
    assert hp["enabled"] is True
    assert next(f for f in hp["fields"] if f["key"] == "freq_hz")["value"] == 90


@pytest.mark.parametrize("values", [{}, [], {"freq_hz": 480, "gain_db": 99}, {"freq_hz": 480, "bogus": 1}])
def test_invalid_multi_field_edit_stages_nothing(client: TestClient, values: object) -> None:
    post(client, "/api/band/add", id="out:0")
    post(client, "/api/apply")
    response = client.post("/api/edit", json={"id": "out:0", "kind": "eq", "index": 0, "values": values})
    assert response.status_code == 400
    assert response.json()["error"]
    assert client.get("/api/state").json()["status"]["pending_count"] == 0


@pytest.mark.parametrize("body", [
    {"id": "out:0", "kind": "eq", "index": 0, "field": "gain_db", "value": 99},
    {"id": "out:0", "kind": "eq", "index": 0, "field": "bogus", "value": 1},
    {"id": "out:0", "kind": "eq", "index": 99, "field": "gain_db", "value": 1},
    {"id": "out:0", "kind": "eq", "index": 0, "field": "gain_db", "value": "loud"},
    {"id": "out:0", "kind": "eq", "index": 0, "field": "enabled", "value": 1},
    {"id": "out:0", "kind": "delay", "field": "gain_db", "value": 1},
    {"id": "out:0", "kind": "delay", "field": "delay_ms", "value": 99999},
    {"id": "out:0", "kind": "hp", "field": "freq_hz", "value": 5},
    {"id": "in:0", "kind": "hp", "field": "freq_hz", "value": 80},
    {"id": "out:0", "kind": "nope", "field": "freq_hz", "value": 80},
    {"id": "out:99", "kind": "delay", "field": "delay_ms", "value": 1},
    {"id": "out:0", "kind": "delay", "field": "delay_ms"},
])
def test_invalid_edit_is_400_with_message_and_stages_nothing(client: TestClient, body: dict[str, Any]) -> None:
    response = client.post("/api/edit", json=body)
    assert response.status_code == 400
    assert response.json()["error"]
    assert client.get("/api/state").json()["status"]["pending_count"] == 0


@pytest.mark.parametrize("body", [
    {"id": "out:0", "kind": "delay", "field": "delay_ms", "value": 10 ** 400},
    {"id": "out:0", "kind": "hp", "field": "freq_hz", "value": 10 ** 400},
])
def test_a_huge_integer_edit_is_400_and_stages_nothing(client: TestClient, body: dict[str, Any]) -> None:
    response = client.post("/api/edit", json=body)
    assert response.status_code == 400
    assert response.json()["error"]
    assert client.get("/api/state").json()["status"]["pending_count"] == 0


def test_a_huge_integer_immediate_write_is_400_and_writes_nothing(store: StudioStore) -> None:
    device = FakeDevice()
    with connected(store, device) as client:
        response = client.post("/api/now", json={"id": "out:1", "block": "gain", "value": 10 ** 400})
        assert response.status_code == 400
        assert "out of range" in response.json()["error"]
    device.client.set_volume.assert_not_called()


def test_a_stored_link_beyond_the_profile_does_not_break_the_state(store: StudioStore) -> None:
    store.path.write_text(json.dumps({"version": 1, "devices": {"demo": {"nicknames": {}, "links": [
        {"letter": "A", "output": False, "members": [0, 99], "sections": ["eq"]},
        {"letter": "B", "output": True, "members": [42], "sections": ["eq"]}]}}}))
    with make_client(DSPService(demo_settings()), store) as client:
        response = client.get("/api/state")
        assert response.status_code == 200, response.text
        post(client, "/api/select", id="in:0")
        state = post(client, "/api/link/sections", letter="A", sections=["eq", "delay"])
        assert [m["id"] for m in state["selected"]["link"]["members"]] == ["in:0"]
        assert client.post("/api/link/sections", json={"letter": "B", "sections": []}).status_code == 400


def test_now_mute_changes_the_channel_without_pending(client: TestClient) -> None:
    state = post(client, "/api/now", id="out:1", block="mute", value=True)
    assert channel(state, "out:1")["muted"] is True
    assert state["status"]["pending_count"] == 0


def test_now_refuses_blocks_that_need_apply(client: TestClient) -> None:
    response = client.post("/api/now", json={"id": "out:1", "block": "delay", "value": 1.0})
    assert response.status_code == 400
    assert "immediate" in response.json()["error"]


def test_gain_mute_and_polarity_never_reach_link_members(client: TestClient) -> None:
    post(client, "/api/link", id="out:3", letter=None)
    before = channel(post(client, "/api/link", id="out:4", letter="A"), "out:4")
    post(client, "/api/link/sections", letter="A", sections=["crossover", "eq", "delay", "limiter"])
    post(client, "/api/now", id="out:3", block="polarity", value=not before["inverted"])
    post(client, "/api/now", id="out:3", block="mute", value=True)
    state = post(client, "/api/now", id="out:3", block="gain", value=-6.0)
    assert (channel(state, "out:3")["gain_db"], channel(state, "out:3")["muted"]) == (-6.0, True)
    after = channel(state, "out:4")
    assert {k: after[k] for k in ("gain_db", "muted", "inverted")} == {k: before[k] for k in ("gain_db", "muted",
                                                                                              "inverted")}
    assert state["status"]["pending_count"] == 0


def test_gain_is_not_a_section_a_group_can_tie(client: TestClient) -> None:
    post(client, "/api/link", id="out:0", letter=None)
    response = client.post("/api/link/sections", json={"letter": "A", "sections": ["gain"]})
    assert response.status_code == 400
    assert "gain" not in post(client, "/api/select", id="out:0")["selected"]["link"]["sections"]


def test_linked_eq_edit_changes_both_members(client: TestClient) -> None:
    post(client, "/api/link", id="out:3", letter=None)
    state = post(client, "/api/link", id="out:4", letter="A")
    assert channel(state, "out:4")["link"]["letter"] == "A"
    state = post(client, "/api/edit", id="out:3", kind="eq", index=2, field="enabled", value=True)
    assert state["status"]["pending_count"] == 2
    state = post(client, "/api/select", id="out:4")
    assert state["selected"]["eq_used"] == 1


def test_link_across_input_and_output_is_400(client: TestClient) -> None:
    post(client, "/api/link", id="in:0", letter=None)
    response = client.post("/api/link", json={"id": "out:0", "letter": "A"})
    assert response.status_code == 400
    assert "inputs link only with inputs" in response.json()["error"]


def test_link_unknown_letter_and_missing_fields_are_400(client: TestClient) -> None:
    assert client.post("/api/link", json={"id": "out:0", "letter": "Q"}).status_code == 400
    assert client.post("/api/link", json={"letter": None}).status_code == 400
    assert client.post("/api/link", content=b"not json", headers=JSON).status_code == 400


def test_creating_a_group_adds_a_one_member_group(client: TestClient) -> None:
    state = post(client, "/api/link", id="out:0", letter=None)
    link = channel(state, "out:0")["link"]
    assert (link["letter"], link["in_sync"]) == ("A", True)
    assert state["selected"]["link"]["members"][0]["id"] == "out:0"  # out:0 is selected from the start
    state = post(client, "/api/select", id="out:1")
    assert state["selected"]["link"] is None


def test_joining_takes_the_groups_values_from_the_pre_join_source(client: TestClient) -> None:
    # Channel 0 joins group A whose source is channel 3; sorting members must not make the joiner the source.
    post(client, "/api/link", id="out:3", letter=None)
    post(client, "/api/edit", id="out:3", kind="delay", field="delay_ms", value=2.0)
    post(client, "/api/edit", id="out:3", kind="eq", index=0, field="enabled", value=True)
    post(client, "/api/edit", id="out:0", kind="eq", index=0, field="gain_db", value=3.0)
    post(client, "/api/select", id="out:0")
    state = post(client, "/api/link", id="out:0", letter="A")
    eq0 = next(c for c in state["selected"]["filters"] if c["key"] == "eq:0")
    assert eq0["enabled"] is True
    assert next(f for f in eq0["fields"] if f["key"] == "gain_db")["value"] == 0
    assert channel(state, "out:0")["link"]["in_sync"] is True


def test_joining_a_group_never_changes_the_joiners_gain(client: TestClient) -> None:
    post(client, "/api/link", id="out:3", letter=None)
    post(client, "/api/now", id="out:3", block="gain", value=-4.0)
    state = post(client, "/api/link", id="out:5", letter="A")
    assert channel(state, "out:5")["gain_db"] == 0
    assert state["result"] is None


def test_link_sections_can_be_changed(client: TestClient) -> None:
    post(client, "/api/link", id="out:0", letter=None)
    post(client, "/api/link", id="out:1", letter="A")
    post(client, "/api/select", id="out:0")
    state = post(client, "/api/link/sections", letter="A", sections=["eq", "delay"])
    assert [s for s, on in state["selected"]["link"]["sections"].items() if on] == ["eq", "delay"]
    assert client.post("/api/link/sections", json={"letter": "Q", "sections": []}).status_code == 400
    assert client.post("/api/link/sections", json={"letter": "A", "sections": ["bogus"]}).status_code == 400
    assert client.post("/api/link/sections", json={"letter": "A", "sections": "eq"}).status_code == 400


def test_sync_stages_the_source_members_tied_sections(client: TestClient) -> None:
    post(client, "/api/link", id="out:0", letter=None)
    post(client, "/api/link", id="out:1", letter="A")
    post(client, "/api/link", id="out:2", letter="A")
    post(client, "/api/link/sections", letter="A", sections=[])
    post(client, "/api/edit", id="out:2", kind="delay", field="delay_ms", value=5.0)
    post(client, "/api/apply")
    post(client, "/api/link/sections", letter="A", sections=["delay"])
    state = post(client, "/api/link/sync", letter="A", **{"from": "out:2"})
    assert [channel(state, f"out:{i}")["delay_ms"] for i in range(3)] == [5.0, 5.0, 5.0]
    assert state["status"]["pending_count"] == 2
    assert client.post("/api/link/sync", json={"letter": "A", "from": "out:7"}).status_code == 400
    assert client.post("/api/link/sync", json={"letter": "Q", "from": "out:2"}).status_code == 400


def test_unlink_removes_the_channel_from_its_group(client: TestClient) -> None:
    post(client, "/api/link", id="out:0", letter=None)
    post(client, "/api/link", id="out:1", letter="A")
    state = post(client, "/api/unlink", id="out:1")
    assert channel(state, "out:1")["link"] is None


def test_band_add_then_remove_round_trips_eq_used(client: TestClient) -> None:
    post(client, "/api/select", id="out:0")
    state = post(client, "/api/band/add", id="out:0")
    assert state["selected"]["eq_used"] == 1
    card = state["selected"]["filters"][-1]
    assert card["enabled"] is True
    fields = {f["key"]: f["value"] for f in card["fields"]}
    assert (fields["type"], fields["freq_hz"], fields["gain_db"], round(fields["q"], 2)) == (0, 1000, 0, 1.0)
    state = post(client, "/api/band/add", id="out:0")
    assert state["selected"]["eq_used"] == 2
    state = post(client, "/api/band/remove", id="out:0", index=0)
    assert state["selected"]["eq_used"] == 1
    state = post(client, "/api/band/remove", id="out:0", index=1)
    assert state["selected"]["eq_used"] == 0


def test_band_add_with_no_free_slot_and_bad_remove_are_400(client: TestClient) -> None:
    for _ in range(7):
        post(client, "/api/band/add", id="in:0")
    response = client.post("/api/band/add", json={"id": "in:0"})
    assert response.status_code == 400
    assert client.post("/api/band/remove", json={"id": "in:0", "index": 7}).status_code == 400
    assert client.post("/api/band/remove", json={"id": "in:0"}).status_code == 400


def test_nickname_persists_across_a_new_app_instance(store: StudioStore) -> None:
    with make_client(DSPService(demo_settings()), store) as first:
        state = post(first, "/api/nickname", id="out:2", name="  Tweeter ")
        assert channel(state, "out:2")["nickname"] == "Tweeter"
    assert "demo" in json.loads(store.path.read_text())["devices"]
    with make_client(DSPService(demo_settings()), StudioStore(store.path)) as second:
        assert channel(second.get("/api/state").json(), "out:2")["nickname"] == "Tweeter"
        state = post(second, "/api/nickname", id="out:2", name="")
        assert channel(state, "out:2")["nickname"] == ""


def test_plot_options_and_select_shape_the_state(client: TestClient) -> None:
    state = post(client, "/api/select", id="out:5")
    assert state["selected"]["id"] == "out:5"
    assert channel(state, "out:5")["selected"] is True
    before = client.get("/api/state").json()["plot"]
    after = post(client, "/api/plot", phase=True, each_filter=False)["plot"]
    assert before != after
    assert client.post("/api/plot", json={"phase": "yes"}).status_code == 400
    assert client.post("/api/select", json={"id": "sideways"}).status_code == 400


def test_live_flushes_edits_in_the_background(client: TestClient) -> None:
    state = post(client, "/api/live", enabled=True)
    assert state["status"]["live"] is True
    post(client, "/api/edit", id="out:0", kind="delay", field="delay_ms", value=1.0)
    deadline = time.monotonic() + 3
    state = client.get("/api/state").json()
    while state["status"]["pending_count"] and time.monotonic() < deadline:
        time.sleep(0.1)
        state = client.get("/api/state").json()
    assert state["status"]["pending_count"] == 0
    assert channel(state, "out:0")["delay_ms"] == 1.0


def test_apply_and_demo_reset(client: TestClient) -> None:
    post(client, "/api/edit", id="out:0", kind="delay", field="delay_ms", value=1.0)
    state = post(client, "/api/apply")
    assert state["status"]["pending_count"] == 0
    state = post(client, "/api/demo")
    assert channel(state, "out:0")["delay_ms"] == 0
    assert post(client, "/api/refresh")["status"]["demo"] is True


def test_connection_error_becomes_a_503_json_error(store: StudioStore) -> None:
    def refuse(address: str) -> Client:
        raise ConnectionError(f"no unit at {address}")

    with make_client(DSPService(demo_settings(), client_factory=refuse), store) as client:
        response = client.post("/api/connect", json={"address": "10.1.2.3"})
        assert response.status_code == 503
        assert response.json() == {"error": "no unit at 10.1.2.3"}
        assert client.get("/api/state").json()["status"]["demo"] is True


def test_other_errors_map_to_400_and_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    service = client.app.state.service
    for error, status in [(PermissionError("locked"), 400), (TimeoutError("slow"), 503), (OSError("io"), 503),
                          (RuntimeError("usb needs hidapi"), 503), (ValueError("bad"), 400)]:
        monkeypatch.setattr(service, "refresh", Mock(side_effect=error))
        response = client.post("/api/refresh", json={})
        assert response.status_code == status
        assert response.json() == {"error": str(error)}
    assert client.post("/api/connect", json={}).status_code == 400
    assert client.post("/api/connect", json={"address": "  "}).status_code == 400


class FakeDevice:
    def __init__(self, *, ignore_writes: bool = False) -> None:
        self.settings = demo_settings()
        self.client = Mock(spec=Client)
        self.client.link_up = True
        self.client.address = "usb:ABC123"
        self.client.read_settings.side_effect = lambda: self.settings
        self.client.read_lock_state.return_value = LockState(False, "")
        self.writes: list[tuple[str, int, object]] = []

        def set_mute(channel: int, muted: bool, *, output: bool = False, **kwargs: Any) -> None:
            self.writes.append(("mute", channel, muted))
            if ignore_writes:
                return
            self.settings = replace_control(self.settings, ControlKey(output, channel, "mute"), muted)

        self.client.set_mute.side_effect = set_mute


def test_connected_device_key_is_the_links_address_and_writes_go_to_the_fake(store: StudioStore) -> None:
    device = FakeDevice()
    service = DSPService(demo_settings(), client_factory=lambda address: device.client)
    with make_client(service, store) as client:
        state = post(client, "/api/connect", address="usb")
        assert state["status"]["connected"] is True
        assert state["status"]["device"] == "usb:ABC123"
        state = post(client, "/api/now", id="out:1", block="mute", value=True)
        assert device.writes == [("mute", 1, True)]
        assert channel(state, "out:1")["muted"] is True
        post(client, "/api/nickname", id="out:1", name="Sub")
        assert list(json.loads(store.path.read_text())["devices"]) == ["usb:ABC123"]
        state = post(client, "/api/disconnect")
        assert state["status"]["demo"] is True
        assert channel(state, "out:1")["nickname"] == ""
    assert device.client.close.called


def test_root_serves_the_page_and_static_files(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'src="static/studio.js"' in response.text
    assert client.get("/static/index.html").status_code == 200
    assert client.get("/static/fonts/InterVariable.woff2").headers["content-type"] == "font/woff2"
    assert client.get("/static/nope.js").status_code == 404


def test_the_app_does_not_import_gradio_or_plotly() -> None:
    code = ("import sys; import sinbosen_la548.studio_app; "
            "bad = [m for m in sys.modules if m.split('.')[0] in ('gradio', 'plotly')]; "
            "sys.exit(f'imported: {bad}' if bad else 0)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_unlinking_one_of_two_members_dissolves_the_group(client: TestClient) -> None:
    post(client, "/api/link", id="out:0", letter=None)
    post(client, "/api/link", id="out:1", letter="A")
    state = post(client, "/api/unlink", id="out:1")
    assert channel(state, "out:0")["link"] is None
    assert channel(state, "out:1")["link"] is None


# Write results

@contextlib.contextmanager
def connected(store: StudioStore, device: FakeDevice) -> Iterator[TestClient]:
    service = DSPService(demo_settings(), client_factory=lambda address: device.client)
    with make_client(service, store) as client:
        post(client, "/api/connect", address="usb")
        yield client


def test_successful_immediate_write_reports_ok(store: StudioStore) -> None:
    device = FakeDevice()
    with connected(store, device) as client:
        state = post(client, "/api/now", id="out:1", block="mute", value=True)
    assert state["result"] == {"ok": True, "errors": [], "conflicts": [], "pending": [], "verified": True}


def test_demo_write_reports_ok_and_operations_without_a_write_report_null(client: TestClient) -> None:
    assert post(client, "/api/now", id="out:1", block="mute", value=True)["result"]["ok"] is True
    assert post(client, "/api/select", id="out:1")["result"] is None
    assert post(client, "/api/link", id="out:0", letter=None)["result"] is None
    assert "result" not in client.get("/api/state").json()


def test_readback_disagreeing_with_a_write_is_visible(store: StudioStore) -> None:
    device = FakeDevice(ignore_writes=True)
    with connected(store, device) as client:
        response = client.post("/api/now", json={"id": "out:1", "block": "mute", "value": True})
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["ok"] is False
    assert body["result"]["errors"]
    assert channel(body, "out:1")["muted"] is False


def test_a_write_that_raises_is_visible(store: StudioStore) -> None:
    device = FakeDevice()
    device.client.set_mute.side_effect = OSError("usb write failed")
    with connected(store, device) as client:
        response = client.post("/api/now", json={"id": "out:1", "block": "mute", "value": True})
    assert response.status_code == 200
    assert response.json()["result"]["ok"] is False
    assert any("usb write failed" in e for e in response.json()["result"]["errors"])


def test_apply_with_an_external_conflict_reports_conflicts(store: StudioStore) -> None:
    device = FakeDevice()
    with connected(store, device) as client:
        post(client, "/api/edit", id="out:0", kind="delay", field="delay_ms", value=1.0)
        device.settings = replace_control(device.settings, ControlKey(True, 0, "delay"), 7.0)
        state = post(client, "/api/apply")
    assert state["result"]["ok"] is False
    assert state["result"]["conflicts"] == ["Output 1: delay"]
    assert state["result"]["pending"] == ["Output 1: delay"]
    assert state["status"]["pending_count"] == 1


def test_link_join_and_sync_never_write_immediately(store: StudioStore) -> None:
    device = FakeDevice()
    with connected(store, device) as client:
        post(client, "/api/link", id="out:3", letter=None)
        post(client, "/api/link/sections", letter="A", sections=["delay"])
        post(client, "/api/edit", id="out:3", kind="delay", field="delay_ms", value=2.0)
        state = post(client, "/api/link", id="out:5", letter="A")
        assert state["result"] is None
        state = post(client, "/api/link/sync", letter="A", **{"from": "out:3"})
        assert state["result"] is None
        assert channel(state, "out:5")["delay_ms"] == 2.0 and channel(state, "out:5")["delay_pending"] is True
    assert device.writes == []
    device.client.set_volume.assert_not_called()


def wait_for(client: TestClient, done: Any) -> dict[str, Any]:
    deadline = time.monotonic() + 3
    state = client.get("/api/state").json()
    while not done(state) and time.monotonic() < deadline:
        time.sleep(0.1)
        state = client.get("/api/state").json()
    return state


def test_live_flush_that_fails_to_verify_is_reported(store: StudioStore) -> None:
    device = FakeDevice()
    with connected(store, device) as client:
        post(client, "/api/live", enabled=True)
        post(client, "/api/edit", id="out:0", kind="delay", field="delay_ms", value=1.0)  # the fake ignores set_delay
        state = wait_for(client, lambda s: s["status"]["live_errors"])
    assert "did not match readback" in state["status"]["live_errors"]
    assert state["status"]["live"] is False


def test_live_flush_with_an_external_conflict_is_reported(store: StudioStore) -> None:
    device = FakeDevice()
    with connected(store, device) as client:
        post(client, "/api/live", enabled=True)
        device.settings = replace_control(device.settings, ControlKey(True, 0, "delay"), 7.0)
        post(client, "/api/edit", id="out:0", kind="delay", field="delay_ms", value=1.0)
        state = wait_for(client, lambda s: s["status"]["live_errors"])
    assert "External changes conflict" in state["status"]["live_errors"]


# Cross-site requests

def test_post_without_the_json_media_type_is_refused_and_does_nothing(client: TestClient) -> None:
    body = json.dumps({"id": "out:0", "kind": "delay", "field": "delay_ms", "value": 1.0})
    for headers in ({"content-type": "text/plain"}, {"content-type": "application/x-www-form-urlencoded"}, {}):
        response = client.post("/api/edit", content=body, headers=headers)
        assert response.status_code == 415
        assert "application/json" in response.json()["error"]
    assert client.post("/api/apply", content=b"", headers={"content-type": "text/plain"}).status_code == 415
    assert client.post("/api/apply").status_code == 415
    assert client.get("/api/state").json()["status"]["pending_count"] == 0


def test_json_media_type_with_parameters_is_accepted(client: TestClient) -> None:
    response = client.post("/api/select", content=b'{"id": "out:1"}',
                           headers={"content-type": "Application/JSON; charset=utf-8"})
    assert response.status_code == 200


def test_a_foreign_origin_is_refused(client: TestClient) -> None:
    for origin in ("http://evil.example", "http://127.0.0.1:9999", "null"):
        response = client.post("/api/select", json={"id": "out:1"}, headers={"origin": origin})
        assert response.status_code == 403
    assert client.get("/api/state").json()["selected"]["id"] == "out:0"
    assert client.get("/api/state", headers={"origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/select", json={"id": "out:1"}, headers={"origin": "http://127.0.0.1"}).status_code == 200


def test_a_foreign_host_is_refused_everywhere(client: TestClient) -> None:
    for path in ("/api/state", "/", "/static/index.html"):
        response = client.get(path, headers={"host": "evil.example"})
        assert response.status_code == 400
    assert client.post("/api/select", json={"id": "out:1"}, headers={"host": "evil.example:8000"}).status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8765", "localhost", "LOCALHOST:80", "[::1]", "[::1]:8765"])
def test_loopback_hosts_are_allowed(client: TestClient, host: str) -> None:
    assert client.get("/api/state", headers={"host": host}).status_code == 200


def test_extra_allowed_hosts_work_and_no_cors_headers_are_sent(store: StudioStore) -> None:
    with make_client(DSPService(demo_settings()), store, allowed_hosts=["studio.lan", "fe80::1"]) as client:
        assert client.get("/api/state", headers={"host": "studio.lan:8000"}).status_code == 200
        assert client.get("/api/state", headers={"host": "[fe80::1]:8000"}).status_code == 200
        assert client.get("/api/state", headers={"host": "other.lan"}).status_code == 400
        response = client.post("/api/select", json={"id": "out:1"}, headers={"origin": "http://127.0.0.1"})
        assert not any(h.lower().startswith("access-control-") for h in response.headers)


# Program slots

NAMES = tuple(f"User{n}" for n in range(1, 31))


def test_demo_programs_list_30_slots_with_the_current_one_flagged(client: TestClient) -> None:
    programs = client.get("/api/programs").json()
    assert programs["current"] == 0
    assert len(programs["slots"]) == 30
    assert programs["slots"][0] == {"index": 0, "number": 1, "name": "user1", "current": True}
    assert programs["slots"][29] == {"index": 29, "number": 30, "name": "user30", "current": False}
    assert sum(slot["current"] for slot in programs["slots"]) == 1


def test_connected_programs_come_from_the_unit(store: StudioStore) -> None:
    device = FakeDevice()
    device.client.get_program_names.return_value = ("", *NAMES[1:])
    device.client.current_program = 2
    with connected(store, device) as client:
        programs = client.get("/api/programs").json()
    assert programs["current"] == 2
    assert programs["slots"][0]["name"] == ""
    assert [slot["index"] for slot in programs["slots"] if slot["current"]] == [2]


def test_an_unknown_current_slot_flags_none(store: StudioStore) -> None:
    device = FakeDevice()
    device.client.get_program_names.return_value = NAMES
    device.client.current_program = None
    with connected(store, device) as client:
        programs = client.get("/api/programs").json()
    assert programs["current"] is None
    assert not any(slot["current"] for slot in programs["slots"])


def test_saving_in_the_demo_makes_the_slot_current(client: TestClient) -> None:
    state = post(client, "/api/programs/save", slot=2, name="user3", apply_first=False)
    assert state["saved"] == {"slot": 2, "name": "user3"}
    assert state["result"] is None
    assert state["status"]["demo"] is True
    assert client.get("/api/programs").json()["current"] == 2


def test_saving_to_a_connected_unit(store: StudioStore) -> None:
    device = FakeDevice()
    device.client.get_program_names.return_value = NAMES
    device.client.current_program = 0
    device.client.save_program.side_effect = lambda slot: setattr(device.client, "current_program", slot)
    with connected(store, device) as client:
        state = post(client, "/api/programs/save", slot=3, name="User4")
        assert client.get("/api/programs").json()["current"] == 3
    device.client.save_program.assert_called_once_with(3)
    assert state["saved"] == {"slot": 3, "name": "User4"}


def test_saving_with_pending_edits_and_no_apply_first_is_refused(client: TestClient) -> None:
    post(client, "/api/edit", id="out:0", kind="delay", field="delay_ms", value=1.0)
    response = client.post("/api/programs/save", json={"slot": 0, "name": "user1", "apply_first": False})
    assert response.status_code == 400
    assert "pending" in response.json()["error"]


def test_apply_first_applies_then_saves(store: StudioStore) -> None:
    device = FakeDevice()
    device.client.get_program_names.return_value = NAMES
    order: list[str] = []
    device.client.set_delay.side_effect = lambda channel, ms, **kwargs: (
        order.append("apply"), setattr(device, "settings", replace_control(
            device.settings, ControlKey(True, channel, "delay"), ms)))
    device.client.save_program.side_effect = lambda slot: order.append("save")
    with connected(store, device) as client:
        post(client, "/api/edit", id="out:0", kind="delay", field="delay_ms", value=1.0)
        state = post(client, "/api/programs/save", slot=1, name="User2", apply_first=True)
    assert order == ["apply", "save"]
    assert state["result"]["ok"] is True
    assert state["saved"] == {"slot": 1, "name": "User2"}
    assert state["status"]["pending_count"] == 0


def test_apply_first_saves_nothing_when_apply_reports_a_conflict(store: StudioStore) -> None:
    device = FakeDevice()
    device.client.get_program_names.return_value = NAMES
    with connected(store, device) as client:
        post(client, "/api/edit", id="out:0", kind="delay", field="delay_ms", value=1.0)
        device.settings = replace_control(device.settings, ControlKey(True, 0, "delay"), 7.0)
        state = post(client, "/api/programs/save", slot=1, name="User2", apply_first=True)
    device.client.save_program.assert_not_called()
    assert state["saved"] is None
    assert state["result"]["ok"] is False
    assert state["result"]["conflicts"] == ["Output 1: delay"]
    assert state["status"]["pending_count"] == 1


def test_a_locked_unit_refuses_to_save(store: StudioStore) -> None:
    device = FakeDevice()
    device.client.get_program_names.return_value = NAMES
    with connected(store, device) as client:
        device.client.read_lock_state.return_value = LockState(True, "1234")
        response = client.post("/api/programs/save", json={"slot": 0, "name": "User1"})
    assert response.status_code == 400
    assert "locked" in response.json()["error"]
    device.client.save_program.assert_not_called()


@pytest.mark.parametrize("slot", [-1, 30, 1.0, "1", True, None])
def test_an_invalid_slot_is_400(client: TestClient, slot: Any) -> None:
    response = client.post("/api/programs/save", json={"slot": slot, "name": "user1"})
    assert response.status_code == 400
    assert "error" in response.json()


def test_apply_first_must_be_a_bool(client: TestClient) -> None:
    assert client.post("/api/programs/save", json={"slot": 0, "name": "user1", "apply_first": "yes"}).status_code == 400


def test_programs_and_saving_while_disconnected_are_503(store: StudioStore) -> None:
    device = FakeDevice()
    device.client.get_program_names.return_value = NAMES
    service = DSPService(demo_settings(), client_factory=lambda address: device.client)
    with make_client(service, store) as client:
        post(client, "/api/connect", address="usb")
        service.close()
        assert client.get("/api/programs").status_code == 503
        response = client.post("/api/programs/save", json={"slot": 0, "name": "User1"})
        assert response.status_code == 503
        assert "error" in response.json()
    device.client.save_program.assert_not_called()


def test_programs_is_local_only(client: TestClient) -> None:
    assert client.get("/api/programs", headers={"host": "evil.example"}).status_code == 400
    assert client.post("/api/programs/save", content=b'{"slot": 0}',
                       headers={"content-type": "text/plain"}).status_code == 415


def test_the_save_names_the_slot_and_is_refused_when_the_unit_has_another_name(store: StudioStore) -> None:
    device = FakeDevice()
    device.client.get_program_names.return_value = NAMES
    with connected(store, device) as client:
        post(client, "/api/edit", id="out:0", kind="delay", field="delay_ms", value=1.0)
        response = client.post("/api/programs/save", json={"slot": 2, "name": "user3", "apply_first": True})
        assert response.status_code == 409
        assert response.json() == {"error": "slot 3 is now named 'User3' on the unit, not 'user3'; nothing was saved"}
        assert client.get("/api/state").json()["status"]["pending_count"] == 1
    device.client.save_program.assert_not_called()
    device.client.set_delay.assert_not_called()


@pytest.mark.parametrize("body", [{"slot": 0}, {"slot": 0, "name": None}, {"slot": 0, "name": 3}])
def test_the_save_must_carry_the_slot_name(client: TestClient, body: dict[str, Any]) -> None:
    response = client.post("/api/programs/save", json=body)
    assert response.status_code == 400
    assert "name" in response.json()["error"]


def test_a_save_that_fails_after_apply_first_reports_the_apply_and_the_failure(store: StudioStore) -> None:
    device = FakeDevice()
    device.client.get_program_names.return_value = NAMES
    device.client.set_delay.side_effect = lambda channel, ms, **kwargs: setattr(
        device, "settings", replace_control(device.settings, ControlKey(True, channel, "delay"), ms))
    device.client.save_program.side_effect = OSError("usb write failed")
    with connected(store, device) as client:
        post(client, "/api/edit", id="out:0", kind="delay", field="delay_ms", value=1.0)
        state = post(client, "/api/programs/save", slot=1, name="User2", apply_first=True)
    assert state["saved"] is None
    assert state["result"]["ok"] is True
    assert state["save_error"] == "Your edits were applied, but saving to slot 2 failed: usb write failed"
    assert state["status"]["pending_count"] == 0
    assert channel(state, "out:0")["delay_ms"] == 1.0


def test_a_plain_save_has_no_save_error(client: TestClient) -> None:
    assert post(client, "/api/programs/save", slot=0, name="user1")["save_error"] is None


# Routing

def test_a_route_toggle_is_staged_and_revert_clears_it(client: TestClient) -> None:
    state = post(client, "/api/route", input=0, output=2, on=True)
    assert state["mixer"]["cells"][0][2] == {"on": True, "pending": True}
    assert state["status"]["pending_count"] == 1
    state = post(client, "/api/route", input=0, output=0, on=False)
    assert state["mixer"]["cells"][0][0] == {"on": False, "pending": True}
    assert state["status"]["pending_count"] == 2
    state = post(client, "/api/revert")
    assert state["mixer"]["cells"][0][2] == {"on": False, "pending": False}
    assert state["mixer"]["cells"][0][0] == {"on": True, "pending": False}
    assert state["status"]["pending_count"] == 0


def test_a_route_is_applied_by_apply(client: TestClient) -> None:
    post(client, "/api/route", input=3, output=2, on=True)
    state = post(client, "/api/apply")
    assert state["mixer"]["cells"][3][2] == {"on": True, "pending": False}
    assert state["status"]["pending_count"] == 0


def test_a_route_ignores_links(client: TestClient) -> None:
    post(client, "/api/link", id="in:0", letter=None)
    post(client, "/api/link", id="in:1", letter="A")
    post(client, "/api/link/sections", letter="A", sections=["crossover", "eq", "delay", "limiter"])
    state = post(client, "/api/route", input=0, output=2, on=True)
    assert state["mixer"]["cells"][1][2] == {"on": False, "pending": False}
    assert state["status"]["pending_count"] == 1


@pytest.mark.parametrize("body", [
    {"input": 4, "output": 0, "on": True}, {"input": -1, "output": 0, "on": True},
    {"input": 0, "output": 8, "on": True}, {"input": 0, "output": -1, "on": True},
    {"input": "0", "output": 0, "on": True}, {"input": 0, "output": 1.0, "on": True},
    {"input": True, "output": 0, "on": True}, {"output": 0, "on": True},
    {"input": 0, "output": 2, "on": 1}, {"input": 0, "output": 2, "on": "true"}, {"input": 0, "output": 2},
])
def test_a_bad_route_is_refused_and_stages_nothing(client: TestClient, body: dict[str, Any]) -> None:
    response = client.post("/api/route", json=body)
    assert response.status_code == 400
    assert response.json()["error"]
    assert client.get("/api/state").json()["status"]["pending_count"] == 0


def test_with_live_on_a_route_is_staged_then_sent_by_live(store: StudioStore) -> None:
    device = FakeDevice()

    def set_mix_route(source: int, target: int, on: bool, **kwargs: Any) -> None:
        device.settings = replace_control(device.settings, ControlKey(False, source, "route", target), on)

    device.client.set_mix_route.side_effect = set_mix_route
    with connected(store, device) as client:
        post(client, "/api/live", enabled=True)
        state = post(client, "/api/route", input=0, output=2, on=True)
        assert state["mixer"]["cells"][0][2] == {"on": True, "pending": True}
        state = wait_for(client, lambda s: not s["status"]["pending_count"])
    device.client.set_mix_route.assert_called_once_with(0, 2, True)
    assert state["mixer"]["cells"][0][2] == {"on": True, "pending": False}
    assert state["status"]["live_errors"] == ""


# Import from REW

REW_PK = """Equaliser: Generic
Filter  1: ON  PK       Fc    50.4 Hz  Gain  -6.0 dB  Q  2.58
Filter  2: ON  PK       Fc    1911 Hz  Gain  -3.1 dB  Q  1.00
Filter  3: OFF PK       Fc     352 Hz  Gain  -2.9 dB  Q  2.98
Filter  4: ON  None
"""
REW_HP = REW_PK + "Filter  5: ON  HP       Fc     100 Hz\n"


def rew_post(client: TestClient, path: str, text: Any, channel_id: Any = "out:2") -> Any:
    return client.post(path, json={"channel": channel_id, "text": text})


def card(state: dict[str, Any], key: str) -> dict[str, Any]:
    return next(c for c in state["selected"]["filters"] if c["key"] == key)


def in_use_cards(state: dict[str, Any]) -> list[str]:
    return [c["key"] for c in state["selected"]["filters"] if c["kind"] == "eq" and (
        c["enabled"] or next(f for f in c["fields"] if f["key"] == "gain_db")["value"] != 0)]


def test_rew_preview_stages_nothing_and_describes_the_import(client: TestClient) -> None:
    before = client.get("/api/state").json()["status"]["pending_count"]
    response = rew_post(client, "/api/rew/preview", REW_HP)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"]["pending_count"] == before == 0
    rew = body["rew"]
    first = rew["bands"][0]
    assert (first["n"], first["source"], first["freq_hz"], first["gain_db"]) == (1, "Filter 1 (PK)", 50, -6.0)
    # The card shows the stored Q; the note says what REW wrote.
    assert first["q"] == pytest.approx(2.58 * 10 ** (6.0 / 40), abs=0.1)
    assert first["type"] == "Parametric EQ"
    assert any("REW Q 2.58" in note for note in first["notes"])
    assert [b["n"] for b in rew["bands"]] == [1, 2]
    assert rew["high_pass"] == {"source": "Filter 5 (HP)", "summary": "100 Hz Butterworth 12 dB/oct"}
    assert rew["low_pass"] is None
    assert rew["unsupported"] == []
    assert rew["skipped"] == 2
    assert rew["linked"] == []
    assert rew["nothing_to_import"] is False


def test_rew_preview_reports_what_it_replaces(client: TestClient) -> None:
    post(client, "/api/select", id="out:2")
    post(client, "/api/band/add", id="out:2")
    post(client, "/api/band/add", id="out:2")
    post(client, "/api/edit", id="out:2", kind="hp", values={"freq_hz": 80, "filter_type": 1,
                                                              "slope_db_per_oct": 24, "enabled": True})
    pending = client.get("/api/state").json()["status"]["pending_count"]
    rew = rew_post(client, "/api/rew/preview", REW_HP).json()["rew"]
    assert rew["replaces"] == {"eq_in_use": 2, "high_pass": "80 Hz Linkwitz-Riley 24 dB/oct", "low_pass": None}
    # A file with no HP does not say it replaces one.
    rew = rew_post(client, "/api/rew/preview", REW_PK).json()["rew"]
    assert rew["replaces"] == {"eq_in_use": 2, "high_pass": None, "low_pass": None}
    assert client.get("/api/state").json()["status"]["pending_count"] == pending


def test_rew_preview_lists_unsupported_filters_and_flags_an_empty_import(client: TestClient) -> None:
    text = "Filter  1: ON  LS       Fc     120 Hz  Gain   3.0 dB\nFilter  2: ON  HP       Fc     50 Hz\n" \
           "Filter  3: ON  HP1      Fc     40 Hz\n"
    rew = rew_post(client, "/api/rew/preview", text, "in:0").json()["rew"]
    assert [u["source"] for u in rew["unsupported"]] == ["Filter 1 (LS)", "Filter 2 (HP)", "Filter 3 (HP1)"]
    assert "no crossover" in rew["unsupported"][1]["reason"]
    assert rew["nothing_to_import"] is True


def test_rew_import_stages_eq_only_for_a_pk_only_file(client: TestClient) -> None:
    state = rew_post(client, "/api/rew/import", REW_PK).json()
    assert state["result"] is None
    assert state["status"]["pending_count"] == 2
    post(client, "/api/select", id="out:2")
    state = client.get("/api/state").json()
    assert [c["key"] for c in state["selected"]["filters"] if c["pending"]] == ["eq:0", "eq:1"]
    assert card(state, "hp")["pending"] is False and card(state, "lp")["pending"] is False
    assert in_use_cards(state) == ["eq:0", "eq:1"]
    assert post(client, "/api/revert")["status"]["pending_count"] == 0


def test_rew_import_with_a_high_pass_replaces_it_and_leaves_the_low_pass(client: TestClient) -> None:
    post(client, "/api/select", id="out:2")
    post(client, "/api/edit", id="out:2", kind="lp", values={"freq_hz": 3000, "enabled": True})
    post(client, "/api/apply")
    state = rew_post(client, "/api/rew/import", REW_HP).json()
    hp, lp = card(state, "hp"), card(state, "lp")
    assert hp["pending"] is True and hp["enabled"] is True
    assert {f["key"]: f["value"] for f in hp["fields"]} == {"freq_hz": 100, "filter_type": 0, "slope_db_per_oct": 12}
    assert lp["pending"] is False and lp["enabled"] is True
    assert next(f for f in lp["fields"] if f["key"] == "freq_hz")["value"] == 3000


def test_rew_import_resets_in_use_bands_the_file_does_not_use(client: TestClient) -> None:
    post(client, "/api/select", id="out:2")
    for _ in range(4):
        post(client, "/api/band/add", id="out:2")
    post(client, "/api/edit", id="out:2", kind="eq", index=3, values={"gain_db": 4.0, "type": 2})
    post(client, "/api/apply")
    state = rew_post(client, "/api/rew/import", REW_PK).json()
    # Bands 3 and 4 are reset (off, flat), so their cards are gone; bands 1 and 2 take the file's filters.
    assert state["status"]["pending_count"] == 4
    assert in_use_cards(state) == ["eq:0", "eq:1"]
    assert state["selected"]["eq_used"] == 2


def test_rew_import_changes_a_linked_member_the_same_way(client: TestClient) -> None:
    post(client, "/api/link", id="out:2", letter=None)
    post(client, "/api/link", id="out:3", letter="A")
    rew = rew_post(client, "/api/rew/preview", REW_HP).json()["rew"]
    assert rew["linked"] == ["Out 4"]
    state = rew_post(client, "/api/rew/import", REW_HP).json()
    assert state["status"]["pending_count"] == 6
    state = post(client, "/api/select", id="out:3")
    assert in_use_cards(state) == ["eq:0", "eq:1"]
    assert card(state, "hp")["pending"] is True


def test_rew_linked_lists_only_members_that_a_tied_section_changes(client: TestClient) -> None:
    post(client, "/api/link", id="out:2", letter=None)
    post(client, "/api/link", id="out:3", letter="A")
    post(client, "/api/link/sections", letter="A", sections=["crossover"])
    # The group ties only the crossover, so an EQ-only file changes nobody else...
    assert rew_post(client, "/api/rew/preview", REW_PK).json()["rew"]["linked"] == []
    # ...and a file with an HP changes the member through the crossover.
    assert rew_post(client, "/api/rew/preview", REW_HP).json()["rew"]["linked"] == ["Out 4"]


def test_rew_import_on_an_input_imports_the_eq_and_not_the_high_pass(client: TestClient) -> None:
    state = rew_post(client, "/api/rew/import", REW_HP, "in:0").json()
    assert state["status"]["pending_count"] == 2
    post(client, "/api/select", id="in:0")
    assert [c["key"] for c in client.get("/api/state").json()["selected"]["filters"]
            if c["kind"] == "eq" and c["pending"]] == ["eq:0", "eq:1"]


@pytest.mark.parametrize("path", ["/api/rew/preview", "/api/rew/import"])
@pytest.mark.parametrize(("text", "channel_id", "message"), [
    ("not a REW file", "out:2", "no filter lines"),
    ("Equaliser: DSP1124P\nFilter  1: ON  PK       Fc     100 Hz  Gain  -3.0 dB  Q  2\n", "out:2", "Generic"),
    ("".join(f"Filter {i:2d}: ON  PK       Fc     {100 * i} Hz  Gain  -3.0 dB  Q  2.000\n" for i in range(1, 9)),
     "in:0", "7 EQ bands"),
    ("Filter  1: ON  PK       Fc     100 Hz  Gain  -3.0 dB  Q  2.000\n" + "x" * 262144, "out:2", "256 kB"),
    (None, "out:2", "text must be a string"),
    (12, "out:2", "text must be a string"),
    (REW_PK, "out:99", "out of range"),
    (REW_PK, "elsewhere", "id must look like"),
    (REW_PK, None, "id must look like"),
])
def test_rew_errors_are_400_with_the_message_and_stage_nothing(client: TestClient, path: str, text: Any,
                                                                channel_id: Any, message: str) -> None:
    response = client.post(path, json={"channel": channel_id, "text": text})
    assert response.status_code == 400, response.text
    assert message in response.json()["error"]
    assert client.get("/api/state").json()["status"]["pending_count"] == 0


def test_rew_import_of_a_file_with_nothing_importable_is_400_and_stages_nothing(client: TestClient) -> None:
    text = ("Filter  1: ON  LS       Fc     120 Hz  Gain   3.0 dB\n"
            "Filter  2: OFF PK       Fc     100 Hz  Gain  -3 dB  Q  2\n")
    response = rew_post(client, "/api/rew/import", text)
    assert response.status_code == 400
    assert "nothing in this file can be imported" in response.json()["error"]
    assert client.get("/api/state").json()["status"]["pending_count"] == 0


def test_rew_import_is_a_draft_even_with_live_off_and_never_an_immediate_write(store: StudioStore) -> None:
    device = FakeDevice()
    with connected(store, device) as client:
        state = rew_post(client, "/api/rew/import", REW_HP).json()
        assert state["status"]["pending_count"] == 3
        assert device.writes == []
    device.client.set_eq_band.assert_not_called()
    device.client.set_xover.assert_not_called()


# Limiter and compressor editors

LIMITER_EDITS = [("threshold_db", -6.0), ("start_time_ms", 5.0), ("release_time_ms", 200.0), ("detection_type", 1),
                 ("enabled", True)]
COMPRESSOR_EDITS = [("threshold_db", -20.0), ("ratio", 4.0), ("start_time_ms", 10.0), ("release_time_ms", 300.0),
                    ("enabled", True)]


def dynamics(state: dict[str, Any], kind: str) -> dict[str, Any]:
    return state["selected"][kind]


def value_of(state: dict[str, Any], kind: str, name: str) -> Any:
    block = dynamics(state, kind)
    return block["enabled"] if name == "enabled" else block["fields"][name]["value"]


@pytest.mark.parametrize(("kind", "edits"), [("limiter", LIMITER_EDITS), ("compressor", COMPRESSOR_EDITS)])
def test_each_dynamics_field_is_an_unapplied_edit_that_revert_clears(client: TestClient, kind: str,
                                                                     edits: list[tuple[str, Any]]) -> None:
    for name, value in edits:
        state = post(client, "/api/edit", id="out:0", kind=kind, field=name, value=value)
        assert value_of(state, kind, name) == value
        assert dynamics(state, kind)["pending"] is True
        assert state["status"]["pending_count"] == 1
        state = post(client, "/api/revert")
        assert state["status"]["pending_count"] == 0
        assert dynamics(state, kind)["pending"] is False
        assert value_of(state, kind, name) != value


def test_a_dynamics_edit_takes_several_fields_at_once(client: TestClient) -> None:
    state = post(client, "/api/edit", id="out:0", kind="compressor",
                 values={"enabled": True, "threshold_db": -12.0, "ratio": 8.0})
    assert [value_of(state, "compressor", n) for n in ("enabled", "threshold_db", "ratio")] == [True, -12.0, 8.0]
    assert state["status"]["pending_count"] == 1


def test_a_typed_value_is_shown_as_the_value_the_device_stores(client: TestClient) -> None:
    state = post(client, "/api/edit", id="out:0", kind="compressor",
                 values={"threshold_db": -10.3, "ratio": 4.26, "start_time_ms": 2.4, "release_time_ms": 100.7})
    assert [value_of(state, "compressor", n) for n in ("threshold_db", "ratio", "start_time_ms",
                                                       "release_time_ms")] == [-10.5, 4.2, 2.0, 100.0]


def test_enabling_the_factory_compressor_stages_a_block_the_device_accepts(client: TestClient) -> None:
    state = post(client, "/api/edit", id="out:2", kind="compressor", field="enabled", value=True)
    block = dynamics(post(client, "/api/select", id="out:2"), "compressor")
    assert block["enabled"] is True and block["pending"] is True
    assert {n: f["value"] for n, f in block["fields"].items()} == {
        "threshold_db": 20.0, "ratio": 1.0, "start_time_ms": 3.0, "release_time_ms": 100.0}
    assert block["fields"]["threshold_db"]["pending"] is True
    assert block["fields"]["ratio"]["pending"] is True
    assert block["fields"]["release_time_ms"]["pending"] is False
    assert state["status"]["pending_count"] == 1
    assert post(client, "/api/apply")["result"]["ok"] is True


def test_editing_the_factory_compressor_clamps_the_fields_that_were_not_typed(client: TestClient) -> None:
    state = post(client, "/api/edit", id="out:2", kind="compressor", field="start_time_ms", value=7.0)
    block = dynamics(post(client, "/api/select", id="out:2"), "compressor")
    assert (block["enabled"], block["fields"]["start_time_ms"]["value"], block["fields"]["ratio"]["value"],
            block["fields"]["threshold_db"]["value"]) == (False, 7.0, 1.0, 20.0)
    assert state["status"]["pending_count"] == 1


def test_unticking_a_disabled_factory_compressor_stages_nothing(client: TestClient) -> None:
    state = post(client, "/api/edit", id="out:2", kind="compressor", field="enabled", value=False)
    assert state["status"]["pending_count"] == 0


@pytest.mark.parametrize(("kind", "values", "text"), [
    ("limiter", {"threshold_db": 25}, "Threshold must be from -60 to 20 dB"),
    ("limiter", {"threshold_db": -60.5}, "Threshold must be from -60 to 20 dB"),
    ("limiter", {"start_time_ms": 0.2}, "Attack must be from 0.3 to 200 ms"),
    ("limiter", {"release_time_ms": 5001}, "Release must be from 50 to 5000 ms"),
    ("limiter", {"detection_type": 2}, "Detection must be Average or Peak"),
    ("limiter", {"detection_type": 0.5}, "Detection must be Average or Peak"),
    ("compressor", {"ratio": 0.5}, "Ratio must be from 1 to 128"),
    ("compressor", {"ratio": 129}, "Ratio must be from 1 to 128"),
    ("compressor", {"threshold_db": -6, "ratio": 0}, "Ratio must be from 1 to 128"),
    ("compressor", {"enabled": 1}, "enabled must be true or false"),
    ("compressor", {"threshold_db": "loud"}, "finite number"),
    ("compressor", {"threshold_db": True}, "finite number"),
    ("compressor", {"detection_type": 1}, "unsupported compressor field"),
    ("limiter", {"ratio": 2}, "unsupported limiter field"),
    ("limiter", {"bogus": 1}, "unsupported limiter field"),
    ("limiter", {}, "values must be an object"),
])
def test_a_refused_dynamics_edit_names_the_range_and_stages_nothing(client: TestClient, kind: str,
                                                                    values: dict[str, Any], text: str) -> None:
    response = client.post("/api/edit", json={"id": "out:0", "kind": kind, "values": values})
    assert response.status_code == 400
    assert text in response.json()["error"]
    assert client.get("/api/state").json()["status"]["pending_count"] == 0


@pytest.mark.parametrize("kind", ["limiter", "compressor"])
def test_an_input_has_no_dynamics_block_to_edit(client: TestClient, kind: str) -> None:
    response = client.post("/api/edit", json={"id": "in:0", "kind": kind, "field": "enabled", "value": True})
    assert response.status_code == 400
    assert "outputs only" in response.json()["error"]
    assert client.get("/api/state").json()["status"]["pending_count"] == 0


def test_a_block_the_engine_refuses_to_write_cannot_be_edited(client: TestClient) -> None:
    response = client.post("/api/edit", json={"id": "out:2", "kind": "limiter", "field": "enabled", "value": True})
    assert response.status_code == 400
    assert "unused limiter bytes" in response.json()["error"]
    assert client.get("/api/state").json()["status"]["pending_count"] == 0


def _link_outputs(client: TestClient, sections: list[str]) -> None:
    post(client, "/api/link", id="out:0", letter=None)
    post(client, "/api/link", id="out:1", letter="A")
    post(client, "/api/link/sections", letter="A", sections=sections)


def test_a_limiter_edit_reaches_a_linked_output_only_when_the_group_ties_the_limiter(client: TestClient) -> None:
    _link_outputs(client, ["crossover", "eq", "delay"])
    state = post(client, "/api/edit", id="out:0", kind="limiter", field="threshold_db", value=-6.0)
    assert state["status"]["pending_count"] == 1
    assert value_of(post(client, "/api/select", id="out:1"), "limiter", "threshold_db") == 0.0
    post(client, "/api/revert")
    post(client, "/api/link/sections", letter="A", sections=["crossover", "eq", "delay", "limiter"])
    state = post(client, "/api/edit", id="out:0", kind="limiter", values={"threshold_db": -6.0, "enabled": True})
    assert state["status"]["pending_count"] == 2
    assert value_of(post(client, "/api/select", id="out:1"), "limiter", "threshold_db") == -6.0


def test_a_compressor_edit_never_reaches_a_linked_output(client: TestClient) -> None:
    _link_outputs(client, ["crossover", "eq", "delay", "limiter"])
    state = post(client, "/api/edit", id="out:0", kind="compressor", values={"threshold_db": -6.0, "enabled": True})
    assert state["status"]["pending_count"] == 1
    other = dynamics(post(client, "/api/select", id="out:1"), "compressor")
    assert (other["enabled"], other["pending"]) == (False, False)


def test_apply_writes_the_dynamics_blocks_to_the_device_and_clears_them(store: StudioStore) -> None:
    device = FakeDevice()
    written: list[tuple[str, int, dict[str, Any]]] = []

    def set_block(kind: str) -> Any:
        def write(channel: int, **kwargs: Any) -> None:
            written.append((kind, channel, kwargs))
            block = type(controls(device.settings)[ControlKey(True, channel, kind)])
            raw = b"\x00" * 10  # the fake keeps no wire bytes; only the fields matter to the readback
            kwargs.pop("output", None)
            key = ControlKey(True, channel, kind)
            device.settings = replace_control(device.settings, key, block(**kwargs, raw=raw))
        return write

    device.client.set_limiter.side_effect = set_block("limiter")
    device.client.set_compressor.side_effect = set_block("compressor")
    with connected(store, device) as client:
        post(client, "/api/edit", id="out:0", kind="limiter", values={"threshold_db": -6.0, "enabled": True})
        state = post(client, "/api/edit", id="out:0", kind="compressor", values={"ratio": 4.0, "enabled": True})
        assert state["status"]["pending_count"] == 2
        state = post(client, "/api/apply")
    assert state["result"]["ok"] is True
    assert state["status"]["pending_count"] == 0
    assert dynamics(state, "limiter")["enabled"] is True and dynamics(state, "compressor")["enabled"] is True
    sent = {kind: (channel, kwargs) for kind, channel, kwargs in written}
    assert len(written) == 2
    assert sent["limiter"][0] == sent["compressor"][0] == 0
    assert sent["limiter"][1]["threshold_db"] == -6.0
    assert sent["compressor"][1]["ratio"] == 4.0
