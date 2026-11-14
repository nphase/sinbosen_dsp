from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from sinbosen_la548 import codecs, rew, tuning
from sinbosen_la548.response import crossover_response
from sinbosen_la548.service import ControlKey, DSPService, normalize_control, replace_control, same_value
from sinbosen_la548.settings import CrossoverSettings, decode_settings

FIXTURE = decode_settings((Path(__file__).parent / "fixtures" / "read_group_sample.bin").read_bytes())

# Layout of REW's "Export filter settings as text" for the Generic equaliser
# (REW help, File menu; the filter lines match a real V5.18 export).
GENERIC = """Filter Settings file

Room EQ V5.18
Dated: 14-Nov-2026 15:10:02

Notes:

Equaliser: Generic
Sub L Nov 14
Filter  1: ON  PK       Fc    50.4 Hz  Gain  -6.0 dB  Q  2.58
Filter  2: ON  PK       Fc    1911 Hz  Gain  -3.1 dB  Q  1.00
Filter  3: OFF PK       Fc     352 Hz  Gain  -2.9 dB  Q  2.98
Filter  4: ON  None
Filter  5: ON  None
"""


def _lines(*filters: str, equaliser: str = "Generic") -> str:
    return f"Equaliser: {equaliser}\n" + "\n".join(filters) + "\n"


def _quantised_q(q: float) -> float:
    return codecs.eq_q_u16.decode(codecs.eq_q_u16.encode(q))


def test_parse_reads_the_equaliser_and_every_filter_line():
    export = rew.parse(GENERIC)
    assert export.equaliser == "Generic"
    assert [(f.number, f.enabled, f.kind, f.freq_hz, f.gain_db, f.q) for f in export.filters] == [
        (1, True, "PK", 50.4, -6.0, 2.58),
        (2, True, "PK", 1911.0, -3.1, 1.0),
        (3, False, "PK", 352.0, -2.9, 2.98),
        (4, True, "None", None, None, None),
        (5, True, "None", None, None, None),
    ]


def test_parse_accepts_decimal_commas():
    export = rew.parse(_lines("Filter  1: ON  PK       Fc    50,4 Hz  Gain  -6,0 dB  Q  2,580"))
    f = export.filters[0]
    assert (f.freq_hz, f.gain_db, f.q) == (50.4, -6.0, 2.58)


def test_parse_keeps_multi_word_filter_types_and_missing_q():
    export = rew.parse(_lines(
        "Filter  1: ON  LS 12dB  Fc     120 Hz  Gain   3.0 dB",
        "Filter  2: ON  HSC      Fc    8000 Hz  Gain  -2.0 dB  Q  0.700",
        "Filter  3: ON  HP       Fc      30 Hz",
        "Filter  4: ON  PK       Fc     100 Hz  Gain  -3.0 dB  BW Oct  0.50",
    ))
    assert [(f.kind, f.freq_hz, f.gain_db, f.q) for f in export.filters] == [
        ("LS 12dB", 120.0, 3.0, None),
        ("HSC", 8000.0, -2.0, 0.7),
        ("HP", 30.0, None, None),
        ("PK", 100.0, -3.0, None),
    ]


def test_parse_without_a_header_has_no_equaliser():
    assert rew.parse("Filter  1: ON  PK       Fc     100 Hz  Gain  -3.0 dB  Q  2.000\n").equaliser is None


def test_parse_rejects_text_with_no_filter_lines():
    with pytest.raises(ValueError, match="no filter lines"):
        rew.parse("Number,Enabled,Control,Type,Frequency,Gain,Q\n1,True,Auto,PK,100,-3,2\n")


def test_parse_rejects_a_filter_line_without_a_frequency():
    with pytest.raises(ValueError, match="line 2"):
        rew.parse(_lines("Filter  1: ON  PK       Gain  -3.0 dB  Q  2.000"))


def test_peaking_filter_maps_to_a_device_peaking_band_with_converted_q():
    mapping = rew.map_filters(rew.parse(GENERIC), band_count=15)
    first, second = mapping.bands
    # REW's Generic PK is the RBJ peaking filter; the device applies RBJ
    # after dividing the stored Q by 10**(|gain|/40) (docs/PROTOCOL.md).
    assert (first.band.freq_hz, first.band.type, first.band.slope, first.band.enabled) == (50, 0, 0, True)
    assert first.band.gain_db == pytest.approx(-6.0)
    assert first.band.q == _quantised_q(2.58 * 10 ** (6.0 / 40))
    assert first.source.number == 1
    assert second.band.freq_hz == 1911
    assert second.band.gain_db == pytest.approx(-3.1)
    assert second.band.q == _quantised_q(1.0 * 10 ** (3.1 / 40))
    assert mapping.unsupported == ()


def test_off_and_none_filters_are_skipped_not_mapped():
    mapping = rew.map_filters(rew.parse(GENERIC), band_count=15)
    assert [f.number for f in mapping.skipped] == [3, 4, 5]
    assert len(mapping.bands) == 2


def test_mapped_bands_are_exactly_what_the_device_codec_stores():
    band = rew.map_filters(rew.parse(GENERIC), band_count=15).bands[0].band
    assert codecs.eq_struct.decode(band.raw) == (band.freq_hz, band.gain_db, band.q, band.slope, band.type, True)


def test_modal_filter_maps_like_a_peaking_filter():
    mapping = rew.map_filters(
        rew.parse(_lines("Filter  1: ON  Modal    Fc    42.0 Hz  Gain  -8.0 dB  Q  6.000  T60 target   400 ms")),
        band_count=7,
    )
    assert (mapping.bands[0].band.type, mapping.bands[0].band.freq_hz) == (0, 42)


def test_all_pass_maps_to_the_second_order_allpass_with_zero_gain():
    mapping = rew.map_filters(rew.parse(_lines("Filter  1: ON  AP       Fc    80.0 Hz  Q  0.700")), band_count=7)
    band = mapping.bands[0].band
    assert (band.type, band.freq_hz, band.gain_db) == (4, 80, 0.0)
    assert band.q == _quantised_q(0.7)


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("Filter  1: ON  LS       Fc     120 Hz  Gain   3.0 dB", "shelf"),
        ("Filter  1: ON  HS 6dB   Fc    8000 Hz  Gain   3.0 dB", "shelf"),
        ("Filter  1: ON  LSC      Fc     120 Hz  Gain   3.0 dB  Q  0.700", "shelf"),
        ("Filter  1: ON  HPQ      Fc      30 Hz  Q  0.900", "Q 0.9"),
        ("Filter  1: ON  LP 24dB  Fc    2000 Hz", "no exact device equivalent"),
        ("Filter  1: ON  NO       Fc      60 Hz", "notch"),
        ("Filter  1: ON  BP       Fc     500 Hz  Q  1.000", "not available"),
        ("Filter  1: ON  PK       Fc     100 Hz  Gain  -3.0 dB  BW Oct  0.50", "unrecognised text 'BW Oct  0.50'"),
        ("Filter  1: ON  PK       Fc     5.0 Hz  Gain  -3.0 dB  Q  2.000", "10-20000 Hz"),
        ("Filter  1: ON  PK       Fc   21000 Hz  Gain  -3.0 dB  Q  2.000", "10-20000 Hz"),
        ("Filter  1: ON  PK       Fc     100 Hz  Gain -24.0 dB  Q  2.000", "20 dB"),
        ("Filter  1: ON  PK       Fc     100 Hz  Gain  -3.0 dB  Q  40.00", "Q"),
        ("Filter  1: ON  PK       Fc     100 Hz  Gain  -3.0 dB  Q  0.100", "Q"),
    ],
)
def test_filters_the_device_cannot_represent_are_reported(line, reason):
    mapping = rew.map_filters(rew.parse(_lines(line)), band_count=15)
    assert mapping.bands == ()
    assert len(mapping.unsupported) == 1
    assert mapping.unsupported[0].source.number == 1
    assert reason in mapping.unsupported[0].reason


def test_a_non_generic_equaliser_is_refused():
    text = _lines("Filter 1: ON  PA   Fc   129.1Hz (  125 +2 )   Gain -18.5dB  BW/60  4.0", equaliser="DSP1124P")
    with pytest.raises(ValueError, match="Generic"):
        rew.map_filters(rew.parse(text), band_count=15)


def test_more_filters_than_bands_is_an_error_naming_both_counts():
    lines = [f"Filter {i:2d}: ON  PK       Fc     {100 * i} Hz  Gain  -3.0 dB  Q  2.000" for i in range(1, 9)]
    with pytest.raises(ValueError, match=r"8 filters.*7 EQ bands"):
        rew.map_filters(rew.parse(_lines(*lines)), band_count=7)


def test_staged_controls_enable_mapped_bands_in_order():
    staged = rew.staged_controls(FIXTURE, GENERIC, output=True, channel=2)
    assert list(staged) == [ControlKey(True, 2, "eq", 0), ControlKey(True, 2, "eq", 1)]
    first = staged[ControlKey(True, 2, "eq", 0)]
    assert (first.freq_hz, first.type, first.enabled) == (50, 0, True)


def test_staged_controls_reset_in_use_bands_the_import_does_not_use():
    key = ControlKey(False, 1, "eq", 5)
    old = FIXTURE.inputs[1].eq_bands[5]
    settings = replace_control(FIXTURE, key, normalize_control(key, replace(old, gain_db=4.0, type=2, enabled=True)))
    staged = rew.staged_controls(settings, GENERIC, output=False, channel=1)
    assert set(staged) == {ControlKey(False, 1, "eq", 0), ControlKey(False, 1, "eq", 1), key}
    # The value the Tune screen's remove-band action stages: frequency and Q stay, the rest is cleared.
    assert same_value(staged[key], tuning.cleared_band(settings.inputs[1].eq_bands[5]))
    cleared = staged[key]
    assert (cleared.enabled, cleared.gain_db, cleared.type, cleared.slope) == (False, 0.0, 0, 0)
    assert (cleared.freq_hz, cleared.q) == (old.freq_hz, old.q)


def test_staged_controls_use_the_channel_kind_band_count():
    lines = [f"Filter {i:2d}: ON  PK       Fc     {100 * i} Hz  Gain  -3.0 dB  Q  2.000" for i in range(1, 9)]
    assert len(rew.staged_controls(FIXTURE, _lines(*lines), output=True, channel=0)) == 8
    with pytest.raises(ValueError, match=r"8 filters.*7 EQ bands"):
        rew.staged_controls(FIXTURE, _lines(*lines), output=False, channel=0)


def test_staged_controls_refuse_unsupported_filters_unless_told_to_skip():
    text = _lines(
        "Filter  1: ON  PK       Fc     100 Hz  Gain  -3.0 dB  Q  2.000",
        "Filter  2: ON  LS       Fc     120 Hz  Gain   3.0 dB",
    )
    with pytest.raises(ValueError, match=r"Filter 2 \(LS\)"):
        rew.staged_controls(FIXTURE, text, output=True, channel=0)
    staged = rew.staged_controls(FIXTURE, text, output=True, channel=0, skip_unsupported=True)
    assert list(staged) == [ControlKey(True, 0, "eq", 0)]


def test_staged_controls_reject_a_channel_that_does_not_exist():
    with pytest.raises(ValueError, match="channel"):
        rew.staged_controls(FIXTURE, GENERIC, output=False, channel=4)


def test_staged_controls_can_be_staged_as_a_service_draft():
    service = DSPService(FIXTURE)
    token = service.new_session()
    service.stage_many(token, rew.staged_controls(FIXTURE, GENERIC, output=True, channel=2))
    view = service.view(token)
    assert [change.key for change in view.pending] == [ControlKey(True, 2, "eq", 0), ControlKey(True, 2, "eq", 1)]
    assert view.draft.outputs[2].eq_bands[0].enabled is True


# HP / LP go to the crossover

XOVER_FIXTURE = FIXTURE.outputs[2]


def _xover(line: str) -> rew.MappedCrossover:
    mapping = rew.map_filters(rew.parse(_lines(line)), band_count=15)
    assert mapping.unsupported == ()
    assert mapping.bands == ()
    return mapping.high_pass or mapping.low_pass


def _textbook_db(kind: str, band: str, q: float, ratio: np.ndarray) -> np.ndarray:
    """Magnitude in dB of REW's filter at f/Fc = `ratio`: first order, or second order with Q `q`."""
    s = 1j * ratio
    if kind == "first":
        h = s / (1 + s) if band == "hp" else 1 / (1 + s)
    else:
        h = s * s / (s * s + s / q + 1) if band == "hp" else 1 / (s * s + s / q + 1)
    return 20 * np.log10(np.abs(h))


@pytest.mark.parametrize("fc", [80.0, 1000.0])
@pytest.mark.parametrize(
    ("kind", "shape", "q", "wanted"),
    [
        ("{0}", "second", 0.7071, (0, 12)),  # Generic HP / LP
        ("{0}1", "first", 0.0, (0, 6)),
        ("{0}Q  Q  0.707", "second", 0.7071, (0, 12)),
        ("{0}Q  Q  0.500", "second", 0.5, (1, 12)),
    ],
)
@pytest.mark.parametrize("band", ["hp", "lp"])
def test_every_mapped_crossover_kind_matches_the_textbook_filter_within_a_tenth_of_a_db(band, fc, kind, shape, q,
                                                                                          wanted):
    name = band.upper()
    # REW writes the type, then Fc, then Q.
    head, _, tail = kind.format(name).partition("  ")
    line = f"Filter  1: ON  {head:<8}Fc  {fc:g} Hz" + (f"  {tail}" if tail else "")
    mapped = _xover(line)
    assert (mapped.band, mapped.block.freq_hz) == (band, int(fc))
    assert (mapped.block.filter_type, mapped.block.slope_db_per_oct, mapped.block.enabled) == (*wanted, True)
    ratios = np.array([0.25, 0.35, 0.5, 0.7, 1.0, 1.4, 2.0, 2.8, 4.0])
    device = crossover_response(mapped.block, band, ratios * mapped.block.freq_hz).magnitude_db
    np.testing.assert_allclose(device, _textbook_db(shape, band, q, ratios), atol=0.1)


def test_crossovers_are_reported_with_their_device_settings_in_words():
    mapping = rew.map_filters(rew.parse(_lines(
        "Filter  1: ON  HPQ      Fc     100 Hz  Q  0.500",
        "Filter  2: ON  LP1      Fc    5000 Hz",
    )), band_count=15)
    assert rew.crossover_summary(mapping.high_pass.block) == "100 Hz Linkwitz-Riley 12 dB/oct"
    assert rew.crossover_summary(mapping.low_pass.block) == "5000 Hz Butterworth 6 dB/oct"
    assert rew.crossover_summary(replace(mapping.low_pass.block, enabled=False)) == "off"
    assert mapping.high_pass.source.number == 1


def test_a_crossover_frequency_is_stored_in_whole_hertz_and_says_so():
    mapped = _xover("Filter  1: ON  HP       Fc    80.4 Hz")
    assert mapped.block.freq_hz == 80
    assert mapped.notes == ("80.4 Hz stored as 80 Hz",)


def test_a_crossover_frequency_outside_the_device_range_is_unsupported():
    mapping = rew.map_filters(rew.parse(_lines("Filter  1: ON  LP       Fc   22000 Hz")), band_count=15)
    assert mapping.low_pass is None
    assert "10-20000 Hz" in mapping.unsupported[0].reason


def test_a_generic_hp_with_another_q_is_unsupported_not_approximated():
    mapping = rew.map_filters(rew.parse(_lines("Filter  1: ON  HP       Fc    80 Hz  Q  1.000")), band_count=15)
    assert mapping.high_pass is None
    assert "Q 1" in mapping.unsupported[0].reason


def test_a_second_high_pass_or_low_pass_is_unsupported():
    mapping = rew.map_filters(rew.parse(_lines(
        "Filter  1: ON  HP       Fc      80 Hz",
        "Filter  2: ON  HP1      Fc      40 Hz",
        "Filter  3: ON  LP       Fc    5000 Hz",
        "Filter  4: ON  LPQ      Fc    8000 Hz  Q  0.500",
    )), band_count=15)
    assert (mapping.high_pass.source.number, mapping.low_pass.source.number) == (1, 3)
    assert [(u.source.number, u.reason) for u in mapping.unsupported] == [
        (2, "the channel has one high-pass"), (4, "the channel has one low-pass")]


def test_an_off_high_pass_is_skipped_and_does_not_take_the_slot():
    mapping = rew.map_filters(rew.parse(_lines(
        "Filter  1: OFF HP       Fc      80 Hz",
        "Filter  2: ON  HP1      Fc      40 Hz",
    )), band_count=15)
    assert mapping.high_pass.source.number == 2
    assert [f.number for f in mapping.skipped] == [1]


def test_an_input_channel_has_no_crossover_so_hp_and_lp_are_unsupported():
    mapping = rew.map_filters(
        rew.parse(_lines("Filter  1: ON  HP       Fc      80 Hz", "Filter  2: ON  LP1      Fc    5000 Hz")),
        band_count=7, crossover_unavailable="input channels have no crossover")
    assert (mapping.high_pass, mapping.low_pass) == (None, None)
    assert [u.reason for u in mapping.unsupported] == ["input channels have no crossover"] * 2


def test_staged_controls_replace_only_the_crossover_the_file_has():
    text = _lines("Filter  1: ON  HP       Fc     100 Hz", "Filter  2: ON  PK       Fc    1000 Hz  Gain  -3.0 dB  Q  2")
    staged = rew.staged_controls(FIXTURE, text, output=True, channel=2)
    assert set(staged) == {ControlKey(True, 2, "hp"), ControlKey(True, 2, "eq", 0)}
    hp = staged[ControlKey(True, 2, "hp")]
    assert (hp.freq_hz, hp.filter_type, hp.slope_db_per_oct, hp.enabled) == (100, 0, 12, True)
    # A file with no low-pass leaves the channel's low-pass as it is.
    assert ControlKey(True, 2, "lp") not in staged


def test_a_pk_only_file_never_touches_either_crossover():
    staged = rew.staged_controls(FIXTURE, GENERIC, output=True, channel=2)
    assert {k.block for k in staged} == {"eq"}


def test_a_crossover_that_already_holds_the_value_is_not_staged():
    key = ControlKey(True, 2, "hp")
    settings = replace_control(FIXTURE, key, normalize_control(key, CrossoverSettings(100, 0, 12, True, raw=b"")))
    staged = rew.staged_controls(settings, _lines("Filter  1: ON  HP       Fc     100 Hz"), output=True, channel=2)
    assert staged == {}


def test_staged_controls_skip_hp_on_an_input_as_unsupported():
    text = _lines("Filter  1: ON  HP       Fc     100 Hz", "Filter  2: ON  PK       Fc    1000 Hz  Gain  -3.0 dB  Q  2")
    with pytest.raises(ValueError, match="no crossover"):
        rew.staged_controls(FIXTURE, text, output=False, channel=0)
    staged = rew.staged_controls(FIXTURE, text, output=False, channel=0, skip_unsupported=True)
    assert list(staged) == [ControlKey(False, 0, "eq", 0)]


@pytest.mark.parametrize(
    ("line", "leftover"),
    [
        ("Filter  1: ON  HP       Fc    80.0 Hz  LR 24dB", "LR 24dB"),
        ("Filter  1: ON  LP       Fc    2000 Hz  Slope 48 dB/oct", "Slope 48 dB/oct"),
        ("Filter  1: ON  HP       Fc      80 Hz  BW Oct  1.0", "BW Oct  1.0"),
        ("Filter  1: ON  PK       Fc     100 Hz  Gain  -3.0 dB  Q  2.00  Order 4", "Order 4"),
    ],
)
def test_a_filter_line_with_text_the_parser_does_not_know_is_unsupported_not_fitted(line, leftover):
    mapping = rew.map_filters(rew.parse(_lines(line)), band_count=15)
    assert (mapping.bands, mapping.high_pass, mapping.low_pass) == ((), None, None)
    assert mapping.unsupported[0].reason == (
        f"unrecognised text '{leftover}'; not imported so that a different filter is not fitted in its place")


def test_unrecognised_text_on_one_filter_does_not_stop_the_others():
    mapping = rew.map_filters(rew.parse(_lines(
        "Filter  1: ON  HP       Fc    80.0 Hz  LR 24dB",
        "Filter  2: ON  PK       Fc     100 Hz  Gain  -3.0 dB  Q  2.00",
    )), band_count=15)
    assert [b.source.number for b in mapping.bands] == [2]
    assert [u.source.number for u in mapping.unsupported] == [1]


def test_the_modal_t60_field_and_the_real_export_layout_are_recognised():
    modal = "Filter  6: ON  Modal    Fc    42.0 Hz  Gain  -8.0 dB  Q  6.000  T60 target   400 ms\n"
    export = rew.parse(GENERIC + modal)
    assert [f.extra for f in export.filters] == [""] * 6
    assert rew.map_filters(export, band_count=15).unsupported == ()


@pytest.mark.parametrize(("q", "wanted"), [("0.71", (0, 12)), ("0.707", (0, 12)), ("0.7071", (0, 12)),
                                           ("0.5", (1, 12)), ("0.50", (1, 12)), ("0.504", (1, 12)),
                                           ("0.496", (1, 12))])
def test_hpq_q_within_five_thousandths_maps(q, wanted):
    mapped = _xover(f"Filter  1: ON  HPQ      Fc     100 Hz  Q  {q}")
    assert (mapped.block.filter_type, mapped.block.slope_db_per_oct) == wanted


@pytest.mark.parametrize("q", ["0.72", "0.49", "0.52", "0.6", "1.0"])
def test_hpq_q_outside_the_two_importable_values_says_what_it_found_and_what_is_importable(q):
    mapping = rew.map_filters(rew.parse(_lines(f"Filter  1: ON  LPQ      Fc     100 Hz  Q  {q}")), band_count=15)
    assert mapping.low_pass is None
    reason = mapping.unsupported[0].reason
    assert f"Q {float(q):g}" in reason and "0.707" in reason and "0.5" in reason
