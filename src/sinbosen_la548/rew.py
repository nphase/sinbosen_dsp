"""Import Room EQ Wizard (REW) filter settings into a channel's EQ bands and crossover.

Reads the file REW writes with File > Export > "Export filter settings as
text" for its **Generic** equaliser:

    Equaliser: Generic
    Filter  1: ON  PK       Fc    50.4 Hz  Gain  -6.0 dB  Q  2.58
    Filter  2: ON  None

`parse` only reads the text. `map_filters` turns the filters into device EQ
bands and crossover settings and reports every filter the device cannot
reproduce; nothing is approximated. `staged_controls` returns the result as
draft edits for
`DSPService.stage_many`, like `presets.import_preset`. See docs/PROTOCOL.md,
"Importing REW filters".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import codecs
from .service import ControlKey, same_value
from .settings import CrossoverSettings, DeviceSettings, EQBandSettings
from .tuning import XOVER_TYPES, band_in_use, cleared_band

_FILTER = re.compile(r"^\s*Filter\s*(\d+)?\s*:\s*(ON|OFF)\b\s*(.*?)\s*$", re.IGNORECASE)
_EQUALISER = re.compile(r"^\s*Equali[sz]er\s*:\s*(.*?)\s*$", re.IGNORECASE)
_NUMBER = r"([-+]?\d[\d.,]*)"
_FC = re.compile(rf"\bFc\s+{_NUMBER}\s*Hz", re.IGNORECASE)
_GAIN = re.compile(rf"\bGain\s+{_NUMBER}\s*dB", re.IGNORECASE)
_Q = re.compile(rf"\bQ\s+{_NUMBER}")

EQ_FREQ_HZ = (10, 20000)
EQ_GAIN_DB = 20.0
_PEAKING, _ALLPASS2 = 0, 4
_BUTTERWORTH, _LINKWITZ_RILEY = 0, 1
_BUTTERWORTH_Q = 0.5**0.5
_LINKWITZ_RILEY_Q = 0.5
_Q_TOLERANCE = 0.001  # REW writes Q to three decimals
# REW kinds that are an HP or LP: (crossover band, form). Generic HP/LP are 12 dB/oct Butterworth, HP1/LP1 are first
# order, HPQ/LPQ are 12 dB/oct with the Q in the file.
_CROSSOVER_KINDS = {
    "HP": ("hp", "generic"), "HP1": ("hp", "first"), "HPQ": ("hp", "q"),
    "LP": ("lp", "generic"), "LP1": ("lp", "first"), "LPQ": ("lp", "q"),
}
_BAND_NAMES = {"hp": "high-pass", "lp": "low-pass"}


@dataclass(frozen=True)
class RewFilter:
    """One `Filter N:` line. `kind` is REW's type text as written ("PK",
    "LS 12dB", "None"); a parameter the line does not carry is None."""

    number: int
    enabled: bool
    kind: str
    freq_hz: float | None = None
    gain_db: float | None = None
    q: float | None = None

    @property
    def label(self) -> str:
        return f"Filter {self.number} ({self.kind})"


@dataclass(frozen=True)
class RewExport:
    equaliser: str | None
    filters: tuple[RewFilter, ...]


@dataclass(frozen=True)
class MappedBand:
    """A REW filter and the device EQ band that reproduces it. `band` holds
    the values the device will store, after its frequency, gain and Q
    quantisation; `notes` says where those differ from the REW values."""

    source: RewFilter
    band: EQBandSettings
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MappedCrossover:
    """A REW HP or LP filter and the device crossover block (`band` "hp" or "lp") that equals it."""

    source: RewFilter
    band: str
    block: CrossoverSettings
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Unsupported:
    source: RewFilter
    reason: str


@dataclass(frozen=True)
class Mapping:
    bands: tuple[MappedBand, ...]
    unsupported: tuple[Unsupported, ...]
    skipped: tuple[RewFilter, ...]  # OFF filters and empty ("None") slots
    high_pass: MappedCrossover | None = None
    low_pass: MappedCrossover | None = None


def _number(text: str) -> float:
    # REW writes a decimal point by default, or the computer's number format
    # (decimal comma) when so configured; it does not group digits by
    # default. With both separators present the last one is the decimal.
    text = text.rstrip(".,")
    if "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        text = text.replace("." if decimal == "," else ",", "")
    return float(text.replace(",", "."))


def parse(text: str) -> RewExport:
    """Read a REW filter settings text export. Raises ValueError when it has
    no `Filter N:` lines or a filter line cannot be read."""
    equaliser: str | None = None
    filters: list[RewFilter] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if (named := _EQUALISER.match(line)) is not None:
            equaliser = named.group(1)
            continue
        if (found := _FILTER.match(line)) is None:
            continue
        number = int(found.group(1)) if found.group(1) else len(filters) + 1
        enabled = found.group(2).upper() == "ON"
        rest = found.group(3)
        if not rest or rest.split()[0].lower() == "none":
            filters.append(RewFilter(number, enabled, "None"))
            continue
        fc = _FC.search(rest)
        if fc is None:
            raise ValueError(f"line {line_number}: filter has no 'Fc ... Hz' frequency: {line.strip()!r}")
        kind = " ".join(rest[: fc.start()].split())
        if not kind:
            raise ValueError(f"line {line_number}: filter has no type: {line.strip()!r}")
        gain, q = _GAIN.search(rest), _Q.search(rest)
        try:
            filters.append(RewFilter(
                number, enabled, kind,
                freq_hz=_number(fc.group(1)),
                gain_db=_number(gain.group(1)) if gain else None,
                q=_number(q.group(1)) if q else None,
            ))
        except ValueError:
            raise ValueError(f"line {line_number}: unreadable number in {line.strip()!r}") from None
    if not filters:
        raise ValueError(
            "no filter lines found; expected REW's 'Export filter settings as text' file "
            "(lines such as 'Filter  1: ON  PK  Fc  100 Hz  Gain  -3.0 dB  Q  2.000')"
        )
    return RewExport(equaliser, tuple(filters))


def _unsupported_kind(kind: str) -> str | None:
    """Why a REW filter type has no device EQ band, or None if it has one."""
    name = kind.upper()
    if name in ("PK", "PEQ", "MODAL", "AP"):
        return None
    if name.startswith(("LS", "HS")):
        return ("shelf filters are not imported: the device's shelves have fixed shapes that differ from REW's "
                "(docs/FILTER_MODELS.md)")
    if name.startswith(("LP", "HP")):
        return (f"{kind} has no exact device equivalent: the device's crossover has Butterworth and "
                "Linkwitz-Riley filters in 6 dB/oct steps, which match only REW's HP, LP, HP1, LP1, HPQ and LPQ")
    if name == "NO":
        return ("REW's notch is a full-depth notch; the device's notch type is a finite cut of at most 20 dB "
                "with its own bandwidth rule")
    return f"filter type {kind} is not available on this device"


def _map_one(source: RewFilter) -> MappedBand | Unsupported:
    reason = _unsupported_kind(source.kind)
    if reason is not None:
        return Unsupported(source, reason)
    assert source.freq_hz is not None
    allpass = source.kind.upper() == "AP"
    notes: list[str] = []
    freq_hz = int(source.freq_hz + 0.5)
    if not EQ_FREQ_HZ[0] <= freq_hz <= EQ_FREQ_HZ[1]:
        return Unsupported(source, f"frequency {source.freq_hz:g} Hz is outside the device's 10-20000 Hz")
    if freq_hz != source.freq_hz:
        notes.append(f"{source.freq_hz:g} Hz stored as {freq_hz} Hz")
    if source.q is None:
        return Unsupported(source, "has no Q (export with REW's Generic equaliser, which writes Q)")
    if allpass:
        gain_db, wire_q = 0.0, source.q
    else:
        if source.gain_db is None:
            return Unsupported(source, "has no gain")
        gain_db = round(source.gain_db, 1)
        if abs(gain_db) > EQ_GAIN_DB:
            return Unsupported(source, f"gain {source.gain_db:g} dB is outside the device's +-20 dB")
        if gain_db != source.gain_db:
            notes.append(f"{source.gain_db:g} dB stored as {gain_db:g} dB")
        # REW's Generic PK is the RBJ peaking filter (bandwidth fc/Q between
        # the half-gain points). The device applies RBJ after dividing the
        # stored Q by 10**(|gain|/40) (docs/PROTOCOL.md, "EQ bands"), so the
        # stored Q is scaled up by the same factor.
        wire_q = source.q * 10 ** (abs(gain_db) / 40)
    try:
        raw = codecs.eq_struct.encode((freq_hz, gain_db, wire_q, 0, _ALLPASS2 if allpass else _PEAKING, True))
    except ValueError:
        return Unsupported(
            source, f"Q {source.q:g} needs a stored Q of {wire_q:.2f}, outside the device's 0.40-28.85")
    band = EQBandSettings(*codecs.eq_struct.decode(raw), raw=raw)
    equivalent = band.q if allpass else band.q / 10 ** (abs(gain_db) / 40)
    if abs(equivalent - source.q) > 0.0005:
        notes.append(f"Q {source.q:g} stored as the nearest step, equivalent to {equivalent:.3f}")
    return MappedBand(source, band, tuple(notes))


def _map_crossover(source: RewFilter, band: str, form: str) -> MappedCrossover | Unsupported:
    """The device crossover equal to a REW HP or LP filter, or why there is none."""
    assert source.freq_hz is not None
    if form == "first":
        filter_type, slope = _BUTTERWORTH, 6
    else:
        if form == "q" and source.q is None:
            return Unsupported(source, "has no Q (export with REW's Generic equaliser, which writes Q)")
        q = _BUTTERWORTH_Q if source.q is None else source.q
        if abs(q - _BUTTERWORTH_Q) <= _Q_TOLERANCE:
            filter_type, slope = _BUTTERWORTH, 12
        elif abs(q - _LINKWITZ_RILEY_Q) <= _Q_TOLERANCE and form == "q":
            filter_type, slope = _LINKWITZ_RILEY, 12
        else:
            return Unsupported(source, f"Q {q:g} has no device equivalent: the device's 12 dB/oct crossovers are "
                                       "Butterworth (Q 0.707) and Linkwitz-Riley (Q 0.5)")
    notes: list[str] = []
    freq_hz = int(source.freq_hz + 0.5)
    if not EQ_FREQ_HZ[0] <= freq_hz <= EQ_FREQ_HZ[1]:
        return Unsupported(source, f"frequency {source.freq_hz:g} Hz is outside the device's 10-20000 Hz")
    if freq_hz != source.freq_hz:
        notes.append(f"{source.freq_hz:g} Hz stored as {freq_hz} Hz")
    raw = codecs.xover_struct.encode((freq_hz, filter_type, slope, True))
    return MappedCrossover(source, band, CrossoverSettings(*codecs.xover_struct.decode(raw), raw=raw), tuple(notes))


def crossover_summary(block: CrossoverSettings) -> str:
    """A crossover block in words, e.g. "80 Hz Linkwitz-Riley 24 dB/oct"; "off" when it is disabled."""
    if not block.enabled:
        return "off"
    family = XOVER_TYPES.get(block.filter_type, f"type {block.filter_type}").replace("\u2013", "-")
    return f"{block.freq_hz} Hz {family} {block.slope_db_per_oct} dB/oct"


def map_filters(export: RewExport, *, band_count: int, crossover_unavailable: str | None = None) -> Mapping:
    """Map an export's enabled filters onto device EQ bands and crossover, in file order.

    Filters the device cannot reproduce come back in `unsupported`, each
    with a reason. A channel has one high-pass and one low-pass: the first
    of each that maps is used and a later one is unsupported.
    `crossover_unavailable` is the reason to give every HP or LP when the
    channel cannot take them (an input has no crossover). Raises ValueError
    for an export made for another equaliser (their Q conventions differ)
    or with more filters than `band_count`.
    """
    if export.equaliser is not None and export.equaliser.lower() != "generic":
        raise ValueError(
            f"this file was exported for REW's {export.equaliser!r} equaliser, whose filter and Q conventions "
            "differ; choose the Generic equaliser in REW's EQ window and export again"
        )
    bands: list[MappedBand] = []
    unsupported: list[Unsupported] = []
    skipped: list[RewFilter] = []
    crossovers: dict[str, MappedCrossover] = {}
    for source in export.filters:
        if not source.enabled or source.kind == "None":
            skipped.append(source)
            continue
        if (crossover := _CROSSOVER_KINDS.get(" ".join(source.kind.upper().split()))) is not None:
            band, form = crossover
            if crossover_unavailable is not None:
                unsupported.append(Unsupported(source, crossover_unavailable))
            elif band in crossovers:
                unsupported.append(Unsupported(source, f"the channel has one {_BAND_NAMES[band]}"))
            elif isinstance(found := _map_crossover(source, band, form), MappedCrossover):
                crossovers[band] = found
            else:
                unsupported.append(found)
            continue
        mapped = _map_one(source)
        if isinstance(mapped, MappedBand):
            bands.append(mapped)
        else:
            unsupported.append(mapped)
    if len(bands) > band_count:
        raise ValueError(f"REW file has {len(bands)} filters to import, but the channel has only {band_count} EQ bands")
    return Mapping(tuple(bands), tuple(unsupported), tuple(skipped), crossovers.get("hp"), crossovers.get("lp"))


def describe_unsupported(mapping: Mapping) -> str:
    return "; ".join(f"{item.source.label}: {item.reason}" for item in mapping.unsupported)


def _check_channel(settings: DeviceSettings, *, output: bool, channel: int) -> None:
    count = len(settings.outputs if output else settings.inputs)
    if not 0 <= channel < count:
        raise ValueError(f"{'output' if output else 'input'} channel must be 0-{count - 1}, got {channel}")


def plan_bands(settings: DeviceSettings, mapping: Mapping, *, output: bool, channel: int) -> dict[
    ControlKey, EQBandSettings
]:
    """The EQ band writes that replace a channel's EQ with `mapping`.

    Mapped filters take the first bands in order; every other band that is
    in use is reset, as the Tune screen's remove action does. Bands that
    already hold the wanted values are left out.
    """
    _check_channel(settings, output=output, channel=channel)
    current = (settings.outputs if output else settings.inputs)[channel].eq_bands
    if len(mapping.bands) > len(current):
        raise ValueError(
            f"REW file has {len(mapping.bands)} filters to import, but the channel has only {len(current)} EQ bands"
        )
    plan: dict[ControlKey, EQBandSettings] = {}
    for index, old in enumerate(current):
        if index < len(mapping.bands):
            new = mapping.bands[index].band
        elif band_in_use(old):
            new = cleared_band(old)
        else:
            continue
        if not same_value(new, old):
            plan[ControlKey(output, channel, "eq", index)] = new
    return plan


def plan_crossovers(settings: DeviceSettings, mapping: Mapping, *, channel: int) -> dict[
    ControlKey, CrossoverSettings
]:
    """The crossover writes for the HP and LP the file has. A crossover the file does not have is not touched, and
    one that already holds the wanted values is left out."""
    _check_channel(settings, output=True, channel=channel)
    plan: dict[ControlKey, CrossoverSettings] = {}
    for mapped in (mapping.high_pass, mapping.low_pass):
        if mapped is not None and not same_value(mapped.block, getattr(settings.outputs[channel], mapped.band)):
            plan[ControlKey(True, channel, mapped.band)] = mapped.block
    return plan


def plan(settings: DeviceSettings, mapping: Mapping, *, output: bool, channel: int) -> dict[ControlKey, object]:
    """Every draft edit that applies `mapping` to one channel: its EQ bands, then the crossovers it has."""
    edits: dict[ControlKey, object] = dict(plan_bands(settings, mapping, output=output, channel=channel))
    if output:
        edits.update(plan_crossovers(settings, mapping, channel=channel))
    return edits


def map_for_channel(settings: DeviceSettings, text: str, *, output: bool, channel: int) -> Mapping:
    """Parse a REW export and map it for one channel of `settings`; an input channel takes no HP or LP."""
    _check_channel(settings, output=output, channel=channel)
    channels = settings.outputs if output else settings.inputs
    return map_filters(parse(text), band_count=len(channels[channel].eq_bands),
                       crossover_unavailable=None if output else "input channels have no crossover")


def staged_controls(settings: DeviceSettings, text: str, *, output: bool, channel: int,
                    skip_unsupported: bool = False) -> dict[ControlKey, object]:
    """Parse a REW export and return the draft edits that apply it to one
    channel, for `DSPService.stage_many`. Raises ValueError if any filter
    cannot be reproduced, unless `skip_unsupported` leaves those out."""
    mapping = map_for_channel(settings, text, output=output, channel=channel)
    if mapping.unsupported and not skip_unsupported:
        raise ValueError(f"cannot import: {describe_unsupported(mapping)}")
    return plan(settings, mapping, output=output, channel=channel)
