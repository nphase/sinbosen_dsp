from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from sinbosen_la548.fir_adapter import FIRParameters
from sinbosen_la548.service import ControlKey, DSPService, controls, demo_settings
from sinbosen_la548.studio_store import SECTIONS, DeviceNotes, LinkGroup
from sinbosen_la548.studio_view import LINK_COLOURS, Stage, build_state, fir_latency_ms, path_latency
from sinbosen_la548.tuning import band_in_use

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
    assert result["selected"]["more"] == [{"name": "FIR", "state": "Off"}]


OUT1 = (True, 0)


def dyn_fields(block: dict[str, Any]) -> dict[str, tuple[Any, ...]]:
    return {name: tuple(f[k] for k in ("value", "min", "max", "step") if k in f) for name, f in block["fields"].items()}


def test_an_output_has_limiter_and_compressor_with_the_engines_ranges(service: DSPService, token: str) -> None:
    selected = state(service, token, selected=OUT1)["selected"]
    assert dyn_fields(selected["limiter"]) == {
        "threshold_db": (0.0, -60.0, 20.0, 0.5), "start_time_ms": (0.3, 0.3, 200.0, 0.1),
        "release_time_ms": (50.0, 50.0, 5000.0, 1.0), "detection_type": (0,)}
    assert dyn_fields(selected["compressor"]) == {
        "threshold_db": (0.0, -60.0, 20.0, 0.5), "ratio": (1.0, 1.0, 128.0, 0.1),
        "start_time_ms": (0.3, 0.3, 200.0, 0.1), "release_time_ms": (50.0, 50.0, 5000.0, 1.0)}
    assert selected["limiter"]["fields"]["detection_type"]["options"] == [
        {"value": 0, "label": "Average"}, {"value": 1, "label": "Peak"}]
    for block in (selected["limiter"], selected["compressor"]):
        assert (block["enabled"], block["enabled_pending"], block["pending"], block["readonly_reason"]) == (
            False, False, False, None)
        assert not any(f["pending"] for f in block["fields"].values())


def test_an_input_has_neither_dynamics_block(service: DSPService, token: str) -> None:
    selected = state(service, token, selected=IN_A)["selected"]
    assert selected["limiter"] is None
    assert selected["compressor"] is None


def test_a_staged_limiter_threshold_marks_its_field_and_card(service: DSPService, token: str) -> None:
    key = ControlKey(True, 0, "limiter")
    service.stage(token, key, replace(controls(service.view(token).draft)[key], threshold_db=-6.0))
    result = state(service, token, selected=OUT1)
    limiter = result["selected"]["limiter"]
    assert result["status"]["pending_count"] == 1
    assert limiter["pending"] is True
    assert limiter["enabled_pending"] is False
    assert {n: f["pending"] for n, f in limiter["fields"].items()} == {
        "threshold_db": True, "start_time_ms": False, "release_time_ms": False, "detection_type": False}
    assert limiter["fields"]["threshold_db"]["value"] == -6.0
    assert result["selected"]["compressor"]["pending"] is False


def test_a_factory_disabled_compressor_renders_with_its_stored_values(service: DSPService, token: str) -> None:
    compressor = state(service, token)["selected"]["compressor"]  # Out 3
    assert dyn_fields(compressor)["ratio"][0] == 0.0
    assert dyn_fields(compressor)["threshold_db"][0] == 340.0
    assert compressor["readonly_reason"] is None


def test_a_block_the_engine_refuses_to_write_is_read_only_with_the_reason(service: DSPService, token: str) -> None:
    limiter = state(service, token)["selected"]["limiter"]  # Out 3's factory limiter keeps a stored ratio
    assert limiter["readonly_reason"] == ("This limiter holds data the app cannot write back safely, so it is "
                                          "shown read-only.")
    assert limiter["fields"]["threshold_db"]["value"] == 340.0


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
        "sections": {"crossover": True, "eq": True, "delay": False, "limiter": False},
    }


def test_link_sections_only_list_those_the_channel_kind_has(service: DSPService, token: str) -> None:
    group = LinkGroup("B", False, (0, 1), frozenset({"eq", "delay"}))
    result = state(service, token, DeviceNotes({}, (group,)), selected=IN_A)
    assert result["selected"]["link"]["sections"] == {"eq": True, "delay": True}
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



def handle(result: dict[str, Any], key: str) -> dict[str, Any]:
    return next(h for h in result["plot"]["handles"] if h["key"] == key)


def test_enabled_only_change_marks_the_card_but_no_field(service: DSPService, token: str) -> None:
    stage_band(service, token, 2, 0, gain_db=3.0, enabled=False)
    service.stage(token, ControlKey(True, 2, "hp"),
                  replace(controls(service.view(token).draft)[ControlKey(True, 2, "hp")], enabled=True))
    result = state(service, token)
    assert card(result, "eq:0")["enabled_pending"] is False
    hp = card(result, "hp")
    assert hp["pending"] is True and hp["enabled_pending"] is True
    assert not any(f["pending"] for f in hp["fields"])
    assert handle(result, "hp")["pending"] is True


def test_enabled_only_change_on_an_eq_band(service: DSPService, token: str) -> None:
    stage_band(service, token, 2, 0, enabled=True)
    result = state(service, token)
    band = card(result, "eq:0")
    assert band["pending"] is True and band["enabled_pending"] is True
    assert not any(f["pending"] for f in band["fields"])
    assert handle(result, "eq:0")["pending"] is True


def test_field_only_change_leaves_enabled_not_pending(service: DSPService, token: str) -> None:
    stage_band(service, token, 2, 0, gain_db=2.0)
    result = state(service, token)
    band = card(result, "eq:0")
    assert band["pending"] is True and band["enabled_pending"] is False
    assert [f["key"] for f in band["fields"] if f["pending"]] == ["gain_db"]
    assert handle(result, "eq:0")["pending"] is True
    assert card(result, "hp")["enabled_pending"] is False


def test_labels_tolerate_channels_beyond_the_alphabet(service: DSPService, token: str) -> None:
    group = LinkGroup("A", False, (0, 99), frozenset({"eq"}))
    result = state(service, token, DeviceNotes({}, (group,)), selected=IN_A)
    assert [m["label"] for m in result["selected"]["link"]["members"]] == ["In A", "In 100"]


# Disabled bands keep a handle

def test_a_disabled_band_in_use_keeps_a_greyed_handle_at_its_own_gain(service: DSPService, token: str) -> None:
    stage_band(service, token, 2, 1, freq_hz=200, gain_db=4.0, enabled=False)
    stage_band(service, token, 2, 3, freq_hz=2000, gain_db=-3.0, enabled=True)
    result = state(service, token)
    disabled, enabled = handle(result, "eq:1"), handle(result, "eq:3")
    assert disabled["enabled"] is False and enabled["enabled"] is True
    plot = result["plot"]
    curve_at_200 = plot["draft_db"][plot["freq"].index(200.0)]
    assert curve_at_200 == pytest.approx(0.0, abs=0.1)  # the disabled band adds nothing to the curve
    assert disabled["db"] == pytest.approx(curve_at_200 + 4.0, abs=0.01)
    assert enabled["db"] == pytest.approx(-3.0, abs=0.1)
    assert card(result, "eq:1")["enabled"] is False


def test_a_disabled_allpass_handle_sits_on_the_curve(service: DSPService, token: str) -> None:
    stage_band(service, token, 2, 0, freq_hz=300, type=4, enabled=False)
    plot = state(service, token)["plot"]
    assert plot["handles"][0]["enabled"] is False
    assert plot["handles"][0]["db"] == pytest.approx(plot["draft_db"][plot["freq"].index(300.0)], abs=0.01)


def test_crossover_handles_are_enabled_and_slopes_mark_unresolved_values(service: DSPService, token: str) -> None:
    key = ControlKey(True, 2, "hp")
    service.stage(token, key, replace(controls(service.view(token).draft)[key], enabled=True))
    result = state(service, token)
    assert handle(result, "hp")["enabled"] is True
    slope = field(card(result, "hp"), "slope_db_per_oct")
    assert not any(o.get("unresolved") for o in slope["options"])


# Latency

def test_latency_of_a_flat_path_is_the_base() -> None:
    assert path_latency(Stage("Out 1", 0.0), [Stage("In A", 0.0)]) == (1.732, 1.732)


def test_latency_adds_the_output_delay_and_a_routed_inputs_delay() -> None:
    assert path_latency(Stage("Out 1", 2.5), [Stage("In A", 0.0)]) == pytest.approx((4.232, 4.232))
    assert path_latency(Stage("Out 1", 2.5), [Stage("In A", 1.0)]) == pytest.approx((5.232, 5.232))


def test_latency_of_a_256_tap_fir_on_the_output() -> None:
    assert fir_latency_ms(256) == pytest.approx(2.662, abs=0.0005)
    assert fir_latency_ms(512) == pytest.approx(3.995, abs=0.0005)
    low, high = path_latency(Stage("Out 1", 0.0, fir_latency_ms(256)), [Stage("In A", 0.0)])
    assert low == high == pytest.approx(1.732 + 2.662, abs=0.0005)


def test_latency_of_two_routed_inputs_with_different_delays_is_a_range() -> None:
    assert path_latency(Stage("Out 1", 2.0), [Stage("In A", 0.0), Stage("In B", 0.5)]) == pytest.approx((3.732, 4.232))


def test_latency_with_no_routed_input_is_the_output_alone() -> None:
    assert path_latency(Stage("Out 3", 2.5), []) == pytest.approx((4.232, 4.232))


def test_latency_of_an_input_is_its_own_part_only() -> None:
    assert path_latency(Stage("In A", 1.0, fir_latency_ms(512)), None) == pytest.approx((4.995, 4.995))


def test_selected_bar_latency_follows_the_draft(service: DSPService, token: str) -> None:
    service.stage(token, ControlKey(True, 0, "delay"), 2.5)
    service.stage(token, ControlKey(False, 0, "delay"), 1.0)
    latency = state(service, token, selected=(True, 0))["selected"]["latency"]
    assert (latency["channel"], latency["text"]) == ("Out 1", "latency 5.23 ms")
    lines = latency["detail"].splitlines()
    assert "Base: 1.732 ms" in lines[0]
    assert any("Out 1 delay: 2.500 ms" in line for line in lines)
    assert any("In A" in line and "1.000 ms" in line for line in lines)
    detail = latency["detail"]
    assert "not included" in detail and "Group delay" in detail and "0.667 ms" in detail
    assert "input A to output 1" in detail


def test_selected_bar_latency_ranges_and_unrouted_outputs(service: DSPService, token: str) -> None:
    service.stage(token, ControlKey(False, 2, "delay"), 0.5)  # In C feeds Out 5 and Out 6 in the demo
    service.stage(token, ControlKey(False, 0, "route", 4), True)  # In A now feeds Out 5 too
    assert state(service, token, selected=(True, 4))["selected"]["latency"]["text"] == "latency 1.73-2.23 ms"
    unrouted = state(service, token, selected=OUT3)["selected"]["latency"]
    assert unrouted["text"] == "latency 1.73 ms, no input routed"
    assert "No input is routed" in unrouted["detail"]


def test_selected_bar_latency_of_an_input_and_an_enabled_fir(service: DSPService, token: str) -> None:
    key = ControlKey(False, 0, "fir")
    fir = FIRParameters.read(controls(service.view(token).draft)[key])  # order index 1: 512 taps
    service.stage(token, key, replace(fir, enabled=True).snapshot(output=False))
    latency = state(service, token, selected=IN_A)["selected"]["latency"]
    assert (latency["channel"], latency["text"]) == ("In A", "adds 4.00 ms")
    assert "In A FIR, 512 taps: 3.995 ms" in latency["detail"]
    assert "1.732" not in latency["detail"].splitlines()[0]


def test_latency_is_labelled_with_the_nickname_when_there_is_one(service: DSPService, token: str) -> None:
    notes = DeviceNotes({(True, 2): "Mid L"}, ())
    assert state(service, token, notes)["selected"]["latency"]["channel"] == "Mid L"
    assert state(service, token, notes, selected=(True, 3))["selected"]["latency"]["channel"] == "Out 4"


# Mixer

def route_key(input_index: int, output_index: int) -> ControlKey:
    return ControlKey(False, input_index, "route", output_index)


def test_mixer_matches_the_draft_matrix(service: DSPService, token: str) -> None:
    mixer = state(service, token)["mixer"]
    matrix = service.view(token).draft.mix_matrix
    assert [[cell["on"] for cell in row] for row in mixer["cells"]] == [list(row) for row in matrix]
    assert all(not cell["pending"] for row in mixer["cells"] for cell in row)
    assert [i["label"] for i in mixer["inputs"]] == ["In A", "In B", "In C", "In D"]
    assert [o["label"] for o in mixer["outputs"]] == [f"Out {n}" for n in range(1, 9)]
    assert [o["id"] for o in mixer["outputs"]][:2] == ["out:0", "out:1"]
    # The demo routes A to 1, B to 2, C to 5 and 6 and D to 7 and 8: outputs 3 and 4 have no input.
    assert [o["no_input"] for o in mixer["outputs"]] == [False, False, True, True, False, False, False, False]


def test_a_staged_route_is_pending_and_counted(service: DSPService, token: str) -> None:
    service.stage(token, route_key(0, 2), True)
    result = state(service, token)
    cells = result["mixer"]["cells"]
    assert cells[0][2] == {"on": True, "pending": True}
    assert sum(cell["pending"] for row in cells for cell in row) == 1
    assert result["status"]["pending_count"] == 1
    assert result["mixer"]["outputs"][2]["no_input"] is False


def test_mixer_headers_carry_nicknames_and_mutes(service: DSPService, token: str) -> None:
    service.stage_many(token, {ControlKey(True, 1, "mute"): True, ControlKey(False, 3, "mute"): True}, immediate=True)
    notes = DeviceNotes({(False, 0): "Left", (True, 1): "Sub L"}, ())
    mixer = state(service, token, notes)["mixer"]
    assert mixer["inputs"][0] == {"id": "in:0", "label": "In A", "nickname": "Left", "muted": False}
    assert mixer["inputs"][3]["muted"] is True
    assert mixer["outputs"][1] == {"id": "out:1", "label": "Out 2", "nickname": "Sub L", "muted": True,
                                   "no_input": False}
