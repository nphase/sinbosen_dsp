from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from sinbosen_la548.service import ControlKey, DSPService, controls, demo_settings
from sinbosen_la548.studio_store import SECTIONS, DeviceNotes, LinkGroup
from sinbosen_la548.studio_view import LINK_COLOURS, band_in_use, build_state

NO_NOTES = DeviceNotes({}, ())
OUT3 = (True, 2)
IN_A = (False, 0)


@pytest.fixture
def service() -> DSPService:
    return DSPService(demo_settings())


@pytest.fixture
def token(service: DSPService) -> str:
    return service.new_session()


def eq_key(channel: int, index: int, output: bool = True) -> ControlKey:
    return ControlKey(output, channel, "eq", index)


def stage_band(service: DSPService, token: str, channel: int, index: int, output: bool = True, **updates: Any) -> None:
    band = controls(service.view(token).draft)[eq_key(channel, index, output)]
    service.stage(token, eq_key(channel, index, output), replace(band, **updates))


def state(service: DSPService, token: str, notes: DeviceNotes = NO_NOTES, selected: tuple[bool, int] = OUT3,
          **kwargs: Any) -> dict[str, Any]:
    return build_state(service.view(token), notes, selected=selected, device=None, **kwargs)


def card(result: dict[str, Any], key: str) -> dict[str, Any]:
    return next(c for c in result["selected"]["filters"] if c["key"] == key)


def field(c: dict[str, Any], key: str) -> dict[str, Any]:
    return next(f for f in c["fields"] if f["key"] == key)


def test_band_in_use_rule(service: DSPService, token: str) -> None:
    band = controls(service.view(token).draft)[eq_key(2, 0)]
    assert not band_in_use(band)
    assert band_in_use(replace(band, enabled=True))
    assert band_in_use(replace(band, gain_db=1.5))
    assert band_in_use(replace(band, type=2))


def test_demo_state_has_twelve_labelled_channels(service: DSPService, token: str) -> None:
    result = state(service, token)
    labels = ["In A", "In B", "In C", "In D"] + [f"Out {i}" for i in range(1, 9)]
    assert [c["label"] for c in result["channels"]] == labels
    assert [c["id"] for c in result["channels"]][:5] == ["in:0", "in:1", "in:2", "in:3", "out:0"]
    assert [c["selected"] for c in result["channels"]].count(True) == 1
    assert result["channels"][6]["selected"] is True
    assert result["status"]["demo"] is True
    assert result["status"]["pending_count"] == 0
    assert result["status"]["warning"] is None


def test_status_passes_device_warning_and_live_errors(service: DSPService, token: str) -> None:
    result = build_state(service.view(token), NO_NOTES, selected=OUT3, device="usb:123", warning="notes reset",
                         live_errors="write failed")
    assert result["status"]["device"] == "usb:123"
    assert result["status"]["warning"] == "notes reset"
    assert result["status"]["live_errors"] == "write failed"


def test_input_selection_has_no_crossover_cards_and_seven_slots(service: DSPService, token: str) -> None:
    result = state(service, token, selected=IN_A)
    assert result["selected"]["id"] == "in:0"
    assert result["selected"]["label"] == "In A"
    assert result["selected"]["eq_slots"] == 7
    assert result["selected"]["eq_used"] == 0
    assert result["selected"]["filters"] == []
    assert [row["name"] for row in result["selected"]["more"]] == ["FIR"]


def test_output_selection_has_crossover_cards_and_more_rows(service: DSPService, token: str) -> None:
    result = state(service, token)
    assert result["selected"]["eq_slots"] == 15
    assert [c["key"] for c in result["selected"]["filters"]] == ["hp", "lp"]
    assert [c["kind"] for c in result["selected"]["filters"]] == ["hp", "lp"]
    assert [c["title"] for c in result["selected"]["filters"]] == ["High-pass", "Low-pass"]
    assert result["selected"]["more"] == [{"name": "Limiter", "state": "Off"}, {"name": "Compressor", "state": "Off"},
                                          {"name": "FIR", "state": "Off"}]


def test_crossover_fields_carry_bounds_and_options(service: DSPService, token: str) -> None:
    hp = card(state(service, token), "hp")
    assert [f["label"] for f in hp["fields"]] == ["Hz", "Type", "Slope"]
    freq, kind, slope = hp["fields"]
    assert (freq["key"], freq["min"], freq["max"]) == ("freq_hz", 10, 20000)
    assert [o["label"] for o in kind["options"]] == ["Butterworth", "Linkwitz–Riley", "Bessel"]
    assert [o["value"] for o in slope["options"]] == [6, 12, 18, 24, 30, 36, 42, 48]
    assert not hp["pending"]
    assert not any(f["pending"] for f in hp["fields"])


def test_only_bands_in_use_get_cards_and_handles(service: DSPService, token: str) -> None:
    stage_band(service, token, 2, 3, freq_hz=1000, gain_db=-3.0, enabled=True)
    stage_band(service, token, 2, 1, freq_hz=200, gain_db=2.0, enabled=False)
    result = state(service, token)
    assert [c["key"] for c in result["selected"]["filters"]] == ["hp", "lp", "eq:1", "eq:3"]
    assert [c["title"] for c in result["selected"]["filters"]][2:] == ["EQ 2", "EQ 4"]
    assert result["selected"]["eq_used"] == 2
    assert [h["key"] for h in result["plot"]["handles"]] == ["eq:1", "eq:3"]
    assert [c["enabled"] for c in result["selected"]["filters"]][2:] == [False, True]


def test_enabled_crossover_gets_a_handle_without_gain(service: DSPService, token: str) -> None:
    service.stage(token, ControlKey(True, 2, "hp"),
                  replace(controls(service.view(token).draft)[ControlKey(True, 2, "hp")], enabled=True, freq_hz=140))
    handles = state(service, token)["plot"]["handles"]
    assert [h["key"] for h in handles] == ["hp"]
    assert handles[0]["label"] == "High-pass"
    assert handles[0]["freq_hz"] == 140
    assert handles[0]["has_gain"] is False


def test_handle_sits_on_the_draft_curve_and_allpass_has_no_gain(service: DSPService, token: str) -> None:
    stage_band(service, token, 2, 0, freq_hz=1000, gain_db=-6.0, q=2.0, enabled=True)
    stage_band(service, token, 2, 1, freq_hz=300, type=4, enabled=True)
    plot = state(service, token)["plot"]
    peak, allpass = plot["handles"]
    assert peak["has_gain"] is True and allpass["has_gain"] is False
    assert peak["db"] == pytest.approx(-6.0, abs=0.1)
    assert peak["freq_hz"] in plot["freq"]
    assert peak["pending"] is True and allpass["pending"] is True


def test_staged_eq_change_marks_card_and_field_pending(service: DSPService, token: str) -> None:
    stage_band(service, token, 2, 0, freq_hz=2500, gain_db=0.0, enabled=True)
    before = state(service, token)
    assert before["status"]["pending_count"] == 1
    assert card(before, "eq:0")["pending"] is True
    stage_band(service, token, 2, 0, gain_db=-4.0)
    after = state(service, token)
    assert after["status"]["pending_count"] == 1
    band = card(after, "eq:0")
    assert band["pending"] is True
    assert field(band, "gain_db")["pending"] is True
    assert field(band, "gain_db")["value"] == -4.0
    assert [f["label"] for f in band["fields"]] == ["Type", "Hz", "dB", "Q"]
    assert (field(band, "gain_db")["min"], field(band, "gain_db")["max"]) == (-20, 20)
    assert field(band, "q")["min"] == pytest.approx(0.404, abs=0.001)
    assert field(band, "q")["max"] == pytest.approx(28.86, abs=0.01)
    assert [o["label"] for o in field(band, "type")["options"]][0] == "Parametric EQ"
    assert card(after, "hp")["pending"] is False


def test_unchanged_field_of_a_pending_band_is_not_pending(service: DSPService, token: str) -> None:
    stage_band(service, token, 2, 0, freq_hz=2500, gain_db=-4.0, enabled=True)
    band = card(state(service, token), "eq:0")
    assert field(band, "freq_hz")["pending"] is True
    assert field(band, "q")["pending"] is False


def test_allpass_gain_is_marked_fixed(service: DSPService, token: str) -> None:
    stage_band(service, token, 2, 0, type=4, enabled=True)
    band = card(state(service, token), "eq:0")
    assert field(band, "gain_db")["editable"] is False
    assert field(band, "freq_hz")["editable"] is True


def test_draft_differs_from_device_only_after_a_staged_change(service: DSPService, token: str) -> None:
    clean = state(service, token)["plot"]
    assert clean["draft_db"] == clean["device_db"]
    stage_band(service, token, 2, 0, freq_hz=1000, gain_db=6.0, enabled=True)
    plot = state(service, token)["plot"]
    assert plot["draft_db"] != plot["device_db"]
    assert max(plot["draft_db"]) > max(plot["device_db"]) + 3


def test_plot_grid_and_optional_series(service: DSPService, token: str) -> None:
    stage_band(service, token, 2, 0, freq_hz=1000, gain_db=6.0, enabled=True)
    plot = state(service, token)["plot"]
    assert len(plot["freq"]) <= 400
    assert plot["freq"][0] == pytest.approx(20) and plot["freq"][-1] == pytest.approx(20000)
    assert plot["freq"] == sorted(plot["freq"])
    assert "phase_deg" not in plot and "group_delay_ms" not in plot
    assert [f["key"] for f in plot["filters"]] == ["eq:0"]
    assert len(plot["filters"][0]["db"]) == len(plot["freq"])
    full = state(service, token, phase=True, group_delay=True)["plot"]
    assert len(full["phase_deg"]) == len(full["group_delay_ms"]) == len(full["freq"])
    bare = state(service, token, each_filter=False, on_device=False)["plot"]
    assert "filters" not in bare and "device_db" not in bare


def test_nicknames_and_links_pass_through(service: DSPService, token: str) -> None:
    group = LinkGroup("A", True, (2, 3), frozenset({"crossover", "eq"}))
    notes = DeviceNotes({(True, 2): "Mid L", (True, 3): "Mid R", (False, 0): "Left"}, (group,))
    result = state(service, token, notes)
    chans = {c["id"]: c for c in result["channels"]}
    assert chans["in:0"]["nickname"] == "Left"
    assert chans["out:2"]["nickname"] == "Mid L"
    assert chans["out:2"]["link"] == {"letter": "A", "colour": LINK_COLOURS["A"], "in_sync": True}
    assert chans["out:0"]["link"] is None
    selected = result["selected"]
    assert selected["nickname"] == "Mid L"
    assert selected["link"] == {
        "letter": "A",
        "members": [{"id": "out:2", "label": "Out 3", "nickname": "Mid L"},
                    {"id": "out:3", "label": "Out 4", "nickname": "Mid R"}],
        "sections": {"crossover": True, "eq": True, "gain": False, "delay": False, "limiter": False},
    }


def test_link_sections_only_list_those_the_channel_kind_has(service: DSPService, token: str) -> None:
    group = LinkGroup("B", False, (0, 1), frozenset({"eq", "gain"}))
    result = state(service, token, DeviceNotes({}, (group,)), selected=IN_A)
    assert result["selected"]["link"]["sections"] == {"eq": True, "gain": True, "delay": False}
    assert set(result["selected"]["link"]["sections"]) <= set(SECTIONS)


def test_link_goes_out_of_sync_when_members_differ(service: DSPService, token: str) -> None:
    stage_band(service, token, 2, 0, gain_db=3.0, enabled=True)
    group = LinkGroup("A", True, (2, 3), frozenset({"eq"}))
    result = state(service, token, DeviceNotes({}, (group,)))
    assert {c["id"]: c["link"]["in_sync"] for c in result["channels"] if c["link"]} == {"out:2": False, "out:3": False}


def test_link_colours() -> None:
    assert LINK_COLOURS["A"] == "#C58BFF" and LINK_COLOURS["B"] == "#F2A33A"
    assert len(set(LINK_COLOURS.values())) >= 8


def test_delay_pending_follows_the_baseline(service: DSPService, token: str) -> None:
    service.stage(token, ControlKey(True, 2, "delay"), 2.35)
    chans = {c["id"]: c for c in state(service, token)["channels"]}
    assert chans["out:2"]["delay_pending"] is True and chans["out:2"]["delay_ms"] == 2.35
    assert chans["out:3"]["delay_pending"] is False
    assert state(service, token)["status"]["pending_count"] == 1


def test_immediate_blocks_do_not_count_as_pending(service: DSPService, token: str) -> None:
    service.stage(token, ControlKey(True, 2, "gain"), -10.0, immediate=True)
    result = state(service, token)
    assert {c["id"]: c for c in result["channels"]}["out:2"]["gain_db"] == -10.0


def test_state_survives_strict_json(service: DSPService, token: str) -> None:
    stage_band(service, token, 2, 0, freq_hz=1000, gain_db=6.0, enabled=True)
    stage_band(service, token, 2, 1, freq_hz=80, type=4, enabled=True)
    stage_band(service, token, 2, 2, freq_hz=12000, type=5, q=20.0, enabled=True)
    group = LinkGroup("A", True, (2, 3), frozenset({"eq"}))
    notes = DeviceNotes({(True, 2): "Mid L"}, (group,))
    for sel in (OUT3, IN_A):
        result = state(service, token, notes, selected=sel, phase=True, group_delay=True)
        json.dumps(result, allow_nan=False)


def test_non_finite_values_become_null(service: DSPService, token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import numpy as np

    from sinbosen_la548 import response, studio_view

    real = response.channel_response

    def broken(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        delay = result.total.group_delay_ms.copy()
        delay[0] = np.nan
        delay[1] = np.inf
        return replace(result, total=replace(result.total, group_delay_ms=delay))

    monkeypatch.setattr(studio_view, "channel_response", broken)
    plot = state(service, token, group_delay=True)["plot"]
    assert plot["group_delay_ms"][:2] == [None, None]
    json.dumps(plot, allow_nan=False)

