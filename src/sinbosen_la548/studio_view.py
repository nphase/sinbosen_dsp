"""Everything the studio screen shows, as JSON-ready data built from the engine's state."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from string import ascii_uppercase
from typing import Any

import numpy as np
from numpy.typing import NDArray

from . import codecs
from .fir_adapter import FIRParameters
from .links import in_sync
from .response import Response, channel_response, frequency_grid
from .service import Change, ControlKey, ServiceView
from .settings import ChannelSettings, CrossoverSettings, DeviceSettings, EQBandSettings
from .studio_store import SECTIONS, DeviceNotes, LinkGroup
from .tuning import ALLPASS_TYPES, EQ_TYPES, XOVER_TYPES

_PALETTE = ("#C58BFF", "#F2A33A", "#5FD0A5", "#FF7A9C", "#6BC7F0", "#D9D35A", "#E8825A", "#9AA8FF")
LINK_COLOURS: dict[str, str] = {letter: _PALETTE[i % len(_PALETTE)] for i, letter in enumerate(ascii_uppercase)}

PLOT_LOW_HZ = 20
PLOT_HIGH_HZ = 20000
PLOT_POINTS = 400
FREQ_BOUNDS = (10, 20000)
GAIN_BOUNDS = (-20, 20)
# eq_q_u16 maps raw 0 to the highest Q and raw 295 to the lowest.
Q_BOUNDS = (round(codecs.eq_q_u16.decode((295).to_bytes(2, "little")), 3),
            round(codecs.eq_q_u16.decode((0).to_bytes(2, "little")), 3))
SLOPES = tuple(range(6, 49, 6))
# The sections a channel kind can tie that are not present on every channel.
_OPTIONAL_SECTIONS = {"crossover": "hp", "limiter": "limiter"}
# Measured latency (docs/PROTOCOL.md, "Latency"): input A to output 1 with flat processing and no delay; an enabled
# FIR adds (N - 1) / 2 + 128 samples at 96 kHz, measured as 2.662 and 3.995 ms; a Kaiser-window FIR write at 21 dB
# can make every path 64 samples slower.
BASE_LATENCY_MS = 1.732
FIR_FIXED_SAMPLES = 128
LATENCY_SAMPLES_PER_MS = 96
FIR_TAPS = {0: 256, 1: 512}  # by order index
FIR_LATENCY_MS = {256: 2.662, 512: 3.995}
LINEAR_PHASE_FIR_TYPES = frozenset({0, 1, 2})
LATENCY_CAVEATS = (
    "EQ and crossover delay varies with frequency and is not included (see Group delay).",
    "The unit can be 0.667 ms slower, depending on its most recent Kaiser-window FIR write, which cannot be read back.",
    "Figures were measured on an LA548 from input A to output 1.",
)


def band_in_use(band: EQBandSettings) -> bool:
    return band.enabled or band.gain_db != 0 or band.type != 0


def _channel_id(output: bool, channel: int) -> str:
    return f"{'out' if output else 'in'}:{channel}"


def _channel_label(output: bool, channel: int) -> str:
    if output or channel >= len(ascii_uppercase):
        return f"{'Out' if output else 'In'} {channel + 1}"
    return f"In {ascii_uppercase[channel]}"


def _series(values: NDArray[np.float64]) -> list[float | None]:
    """Plot values rounded for the wire; non-finite ones become null."""
    return [round(float(v), 3) if np.isfinite(v) else None for v in values]


def _option(value: int, label: str) -> dict[str, Any]:
    return {"value": value, "label": label}


def _number(key: str, label: str, value: float, bounds: tuple[float, float], *, pending: bool,
            editable: bool = True) -> dict[str, Any]:
    return {"key": key, "label": label, "kind": "number", "value": value, "min": bounds[0], "max": bounds[1],
            "editable": editable, "pending": pending}


def _select(key: str, label: str, value: int, options: list[dict[str, Any]], *, pending: bool) -> dict[str, Any]:
    if all(o["value"] != value for o in options):
        options = [*options, {**_option(value, f"Unresolved ({value})"), "unresolved": True}]
    return {"key": key, "label": label, "kind": "select", "value": value, "options": options, "editable": True,
            "pending": pending}


def _differs(base: object, now: object, attr: str) -> bool:
    return base is not None and getattr(base, attr) != getattr(now, attr)


def _channel_of(settings: DeviceSettings, selected: tuple[bool, int]) -> ChannelSettings:
    output, index = selected
    return (settings.outputs if output else settings.inputs)[index]


def _crossover_card(kind: str, block: CrossoverSettings, base: CrossoverSettings | None,
                    key: ControlKey, pending_keys: set[ControlKey]) -> dict[str, Any]:
    def differs(attr: str) -> bool:
        return _differs(base, block, attr)

    slopes = [_option(s, f"{s} dB/oct") for s in SLOPES]
    return {"key": kind, "kind": kind, "index": 0, "title": "High-pass" if kind == "hp" else "Low-pass",
            "enabled": block.enabled, "enabled_pending": differs("enabled"),
            "pending": key in pending_keys,
            "fields": [_number("freq_hz", "Hz", block.freq_hz, FREQ_BOUNDS, pending=differs("freq_hz")),
                       _select("filter_type", "Type", block.filter_type,
                               [_option(v, n) for v, n in XOVER_TYPES.items()], pending=differs("filter_type")),
                       _select("slope_db_per_oct", "Slope", block.slope_db_per_oct, slopes,
                               pending=differs("slope_db_per_oct"))]}


def _eq_card(index: int, band: EQBandSettings, base: EQBandSettings | None,
             key: ControlKey, pending_keys: set[ControlKey]) -> dict[str, Any]:
    def differs(attr: str) -> bool:
        return _differs(base, band, attr)

    return {"key": f"eq:{index}", "kind": "eq", "index": index, "title": f"EQ {index + 1}", "enabled": band.enabled,
            "enabled_pending": differs("enabled"), "pending": key in pending_keys,
            "fields": [_select("type", "Type", band.type, [_option(v, n) for v, n in EQ_TYPES.items()],
                               pending=differs("type")),
                       _number("freq_hz", "Hz", band.freq_hz, FREQ_BOUNDS, pending=differs("freq_hz")),
                       _number("gain_db", "dB", band.gain_db, GAIN_BOUNDS, pending=differs("gain_db"),
                               editable=band.type not in ALLPASS_TYPES),
                       _number("q", "Q", round(band.q, 4), Q_BOUNDS, pending=differs("q"))]}


def _filter_cards(draft: ChannelSettings, baseline: ChannelSettings,
                  pending_keys: set[ControlKey]) -> list[dict[str, Any]]:
    cards = []
    for kind in ("hp", "lp"):
        block = getattr(draft, kind)
        if block is not None:
            cards.append(_crossover_card(kind, block, getattr(baseline, kind),
                                         ControlKey(draft.output, draft.channel_id, kind), pending_keys))
    for i, band in enumerate(draft.eq_bands):
        if band_in_use(band):
            cards.append(_eq_card(i, band, baseline.eq_bands[i], ControlKey(draft.output, draft.channel_id, "eq", i),
                                  pending_keys))
    return cards


def _more_rows(ch: ChannelSettings) -> list[dict[str, str]]:
    blocks = (("Limiter", ch.limiter), ("Compressor", ch.compressor), ("FIR", ch.fir))
    return [{"name": name, "state": "On" if block.enabled else "Off"} for name, block in blocks if block is not None]


def _group_of(notes: DeviceNotes, output: bool, channel: int) -> LinkGroup | None:
    return next((g for g in notes.links if g.output == output and channel in g.members), None)


def _member(notes: DeviceNotes, output: bool, channel: int) -> dict[str, str]:
    return {"id": _channel_id(output, channel), "label": _channel_label(output, channel),
            "nickname": notes.nicknames.get((output, channel), "")}


def _link_section_names(ch: ChannelSettings) -> list[str]:
    return [s for s in SECTIONS if s not in _OPTIONAL_SECTIONS or getattr(ch, _OPTIONAL_SECTIONS[s]) is not None]


@dataclass(frozen=True)
class _Handle:
    key: str
    label: str
    freq_hz: int
    has_gain: bool
    enabled: bool = True
    gain_db: float = 0.0


def _plot_handles(draft: ChannelSettings) -> list[_Handle]:
    """Enabled crossovers and bands in use, enabled or not."""
    handles = [_Handle(kind, "High-pass" if kind == "hp" else "Low-pass", getattr(draft, kind).freq_hz, False)
               for kind in ("hp", "lp") if getattr(draft, kind) is not None and getattr(draft, kind).enabled]
    handles += [_Handle(f"eq:{i}", f"EQ {i + 1}", band.freq_hz, band.type not in ALLPASS_TYPES, band.enabled,
                        band.gain_db) for i, band in enumerate(draft.eq_bands) if band_in_use(band)]
    return handles


def _control_key(output: bool, channel: int, key: str) -> ControlKey:
    """The ControlKey behind a card or handle key (`hp`, `lp`, `eq:3`)."""
    block, _, number = key.partition(":")
    return ControlKey(output, channel, block, int(number) if number else -1)


def _contribution_key(name: str) -> str:
    """Key of a `channel_response` contribution; the names come from response.py."""
    if name.startswith("HP"):
        return "hp"
    if name.startswith("LP"):
        return "lp"
    return f"eq:{int(name.rsplit(' ', 1)[1]) - 1}"


def _plot(view: ServiceView, selected: tuple[bool, int], pending_keys: set[ControlKey], *, phase: bool,
          group_delay: bool, each_filter: bool, on_device: bool) -> dict[str, Any]:
    output, index = selected
    draft = _channel_of(view.draft, selected)
    baseline = _channel_of(view.baseline, selected)
    rate = view.draft.profile.sample_rate_hz
    handles = _plot_handles(draft)
    # Each handle's own frequency is a grid point, so its dot lands exactly on the curve.
    on_grid = [float(h.freq_hz) for h in handles if PLOT_LOW_HZ <= h.freq_hz <= PLOT_HIGH_HZ]
    base_grid = frequency_grid(sample_rate_hz=rate, low=PLOT_LOW_HZ, high=PLOT_HIGH_HZ,
                               points=PLOT_POINTS - len(on_grid))
    freq = np.unique(np.concatenate([base_grid, on_grid]))
    response = channel_response(draft, freq, sample_rate_hz=rate)
    total = response.total
    plot: dict[str, Any] = {"freq": [float(f) for f in freq], "draft_db": _series(total.magnitude_db)}
    if on_device:
        plot["device_db"] = _series(channel_response(baseline, freq, sample_rate_hz=rate).total.magnitude_db)
    if each_filter:
        plot["filters"] = [{"key": _contribution_key(name), "db": _series(r.magnitude_db)}
                           for name, r in response.contributions[1:] if r.complete]
    if phase:
        plot["phase_deg"] = _series(total.phase_deg)
    if group_delay:
        plot["group_delay_ms"] = _series(total.group_delay_ms)
    # A disabled band adds nothing to the curve; its handle sits where the band would put it: the curve plus its gain.
    plot["handles"] = [
        {"key": h.key, "label": h.label, "freq_hz": h.freq_hz,
         "db": _handle_db(freq, total, h.freq_hz, 0.0 if h.enabled or not h.has_gain else h.gain_db),
         "has_gain": h.has_gain, "enabled": h.enabled, "pending": _control_key(output, index, h.key) in pending_keys}
        for h in handles]
    return plot


def _handle_db(freq: NDArray[np.float64], total: Response, hz: float, offset_db: float) -> float | None:
    value = float(np.interp(hz, freq, total.magnitude_db)) + offset_db
    return round(value, 3) if np.isfinite(value) else None


# Latency

@dataclass(frozen=True)
class Stage:
    """One channel's own part of a path's latency: its delay, plus its FIR's when the FIR is on."""
    label: str
    delay_ms: float
    fir_ms: float = 0.0

    @property
    def ms(self) -> float:
        return self.delay_ms + self.fir_ms


def fir_latency_ms(taps: int) -> float:
    """What an enabled linear-phase FIR of `taps` taps adds: the measured figure, else (N - 1) / 2 + 128 samples."""
    return FIR_LATENCY_MS.get(taps, ((taps - 1) / 2 + FIR_FIXED_SAMPLES) / LATENCY_SAMPLES_PER_MS)


def path_latency(channel: Stage, routed: Sequence[Stage] | None) -> tuple[float, float]:
    """The lowest and highest latency through `channel`, in ms.

    For an output, `routed` are the inputs the mix matrix sends to it: the base latency, the output's part and each
    input's part; with none routed, the base and the output's part. For an input, `routed` is None and the result
    is the input's own part only.
    """
    if routed is None:
        return channel.ms, channel.ms
    totals = [BASE_LATENCY_MS + channel.ms + stage.ms for stage in routed] or [BASE_LATENCY_MS + channel.ms]
    return min(totals), max(totals)


def _stage(ch: ChannelSettings, label: str) -> tuple[Stage, list[str], bool]:
    """The channel's stage, its breakdown lines, and False when its FIR's delay can only be partly counted."""
    lines = [f"{label} delay: {ch.base.delay_ms:.3f} ms"]
    fir = None if ch.fir is None else FIRParameters.read(ch.fir)
    if fir is None or not fir.enabled:
        return Stage(label, ch.base.delay_ms), lines, True
    taps = FIR_TAPS.get(fir.order_index)
    if taps is not None and fir.fir_type in LINEAR_PHASE_FIR_TYPES:
        lines.append(f"{label} FIR, {taps} taps: {fir_latency_ms(taps):.3f} ms")
        return Stage(label, ch.base.delay_ms, fir_latency_ms(taps)), lines, True
    fixed = FIR_FIXED_SAMPLES / LATENCY_SAMPLES_PER_MS
    lines.append(f"{label} FIR: {fixed:.3f} ms counted; its coefficients' own delay is not known")
    return Stage(label, ch.base.delay_ms, fixed), lines, False


def _latency(settings: DeviceSettings, selected: tuple[bool, int], name: str) -> dict[str, str]:
    """The selected channel's latency readout, from the draft: the channel's name (nickname or label), a short text
    to follow it and a line-by-line breakdown."""
    output, index = selected
    own, lines, exact = _stage(_channel_of(settings, selected), _channel_label(output, index))
    if not output:
        low, high = path_latency(own, None)
        lines.append(f"The {BASE_LATENCY_MS:.3f} ms base latency and the output's delay and FIR come on top; "
                     "select an output for a whole path.")
        text = f"adds {'at least ' if not exact else ''}{low:.2f} ms"
        return {"channel": name, "text": text, "detail": "\n".join([*lines, *LATENCY_CAVEATS])}
    routed: list[Stage] = []
    for i, row in enumerate(settings.mix_matrix):
        if row[index]:
            stage, part, known = _stage(settings.inputs[i], _channel_label(False, i))
            routed.append(stage)
            lines += part
            exact = exact and known
    low, high = path_latency(own, routed)
    if len(routed) > 1:
        lines += [f"Through {stage.label}: {BASE_LATENCY_MS + own.ms + stage.ms:.3f} ms" for stage in routed]
    if not routed:
        lines.append("No input is routed to this output; an input's delay and FIR would add to this.")
    figure = f"{low:.2f}" if f"{low:.2f}" == f"{high:.2f}" else f"{low:.2f}-{high:.2f}"
    text = f"latency {'at least ' if not exact else ''}{figure} ms{'' if routed else ', no input routed'}"
    lines.insert(0, f"Base: {BASE_LATENCY_MS:.3f} ms (input to output with flat processing and no delay)")
    return {"channel": name, "text": text, "detail": "\n".join([*lines, *LATENCY_CAVEATS])}


def _channel_entry(view: ServiceView, notes: DeviceNotes, output: bool, index: int, selected: tuple[bool, int],
                   pending_keys: set[ControlKey]) -> dict[str, Any]:
    ch = _channel_of(view.draft, (output, index))
    group = _group_of(notes, output, index)
    return {"id": _channel_id(output, index), "label": _channel_label(output, index),
            "nickname": notes.nicknames.get((output, index), ""), "gain_db": ch.base.gain_db,
            "delay_ms": ch.base.delay_ms, "muted": ch.base.muted, "inverted": ch.base.inverted,
            "delay_pending": ControlKey(output, index, "delay") in pending_keys,
            "link": None if group is None else {"letter": group.letter, "colour": LINK_COLOURS[group.letter],
                                                 "in_sync": in_sync(group, view.draft)},
            "selected": (output, index) == selected}


def _selected_entry(view: ServiceView, notes: DeviceNotes, selected: tuple[bool, int],
                    pending_keys: set[ControlKey]) -> dict[str, Any]:
    output, index = selected
    draft = _channel_of(view.draft, selected)
    baseline = _channel_of(view.baseline, selected)
    group = _group_of(notes, output, index)
    link = None
    if group is not None:
        link = {"letter": group.letter, "members": [_member(notes, output, m) for m in group.members],
                "sections": {s: s in group.sections for s in _link_section_names(draft)}}
    return {"id": _channel_id(output, index), "label": _channel_label(output, index),
            "nickname": notes.nicknames.get(selected, ""), "eq_slots": len(draft.eq_bands),
            "eq_used": sum(band_in_use(b) for b in draft.eq_bands), "link": link,
            "filters": _filter_cards(draft, baseline, pending_keys), "more": _more_rows(draft),
            "latency": _latency(view.draft, selected, notes.nicknames.get(selected) or _channel_label(output, index))}


def _mixer(view: ServiceView, notes: DeviceNotes, pending_keys: set[ControlKey]) -> dict[str, Any]:
    """The routing matrix from the draft: a header per input and output and, per input, a cell per output."""
    draft = view.draft

    def header(ch: ChannelSettings) -> dict[str, Any]:
        return {**_member(notes, ch.output, ch.channel_id), "muted": ch.base.muted}

    matrix = draft.mix_matrix
    return {"inputs": [header(ch) for ch in draft.inputs],
            "outputs": [{**header(ch), "no_input": not any(row[ch.channel_id] for row in matrix)}
                        for ch in draft.outputs],
            "cells": [[{"on": on, "pending": ControlKey(False, i, "route", o) in pending_keys}
                       for o, on in enumerate(row)] for i, row in enumerate(matrix)]}


def build_state(view: ServiceView, notes: DeviceNotes, *, selected: tuple[bool, int], device: str | None,
                phase: bool = False, group_delay: bool = False, each_filter: bool = True, on_device: bool = True,
                live_errors: str = "", warning: str | None = None) -> dict[str, Any]:
    """The whole screen as JSON-ready data: status, the channel strip, the selected channel and its plot, and the
    routing matrix."""
    pending: Sequence[Change] = view.pending
    pending_keys = {change.key for change in pending}
    channels = [_channel_entry(view, notes, output, i, selected, pending_keys)
                for output, count in ((False, len(view.draft.inputs)), (True, len(view.draft.outputs)))
                for i in range(count)]
    return {
        "status": {"connected": view.connected, "demo": view.demo, "live": view.live, "device": device,
                   "message": view.message, "revision": view.revision, "pending_count": len(pending),
                   "warning": warning, "live_errors": live_errors},
        "channels": channels,
        "selected": _selected_entry(view, notes, selected, pending_keys),
        "plot": _plot(view, selected, pending_keys, phase=phase, group_delay=group_delay, each_filter=each_filter,
                      on_device=on_device),
        "mixer": _mixer(view, notes, pending_keys),
    }
