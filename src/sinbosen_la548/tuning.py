"""Core tuning actions shared by UI callbacks and offline tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from . import codecs
from .codecs import Codec
from .service import ControlKey, DSPService, same_value
from .settings import ChannelSettings, CompressorSettings, CrossoverSettings, EQBandSettings, LimiterSettings

EQ_TYPES = {0: "Parametric EQ", 1: "Low shelf", 2: "High shelf", 3: "Allpass (1st order)",
           4: "Allpass (2nd order)", 5: "Notch"}
XOVER_TYPES = {0: "Butterworth", 1: "Linkwitz–Riley", 2: "Bessel"}
# Both allpass types ignore gain on hardware (response.py), so the UI pins it to 0 dB.
ALLPASS_TYPES = frozenset({3, 4})


LIMITER_DETECTION = {0: "Average", 1: "Peak"}


def _step(codec: Codec[float]) -> float:
    """The finest change of a u16 codec's value: what one raw count is worth at the bottom of its range."""
    return round(codec.decode((1).to_bytes(2, "little")) - codec.decode((0).to_bytes(2, "little")), 6)


@dataclass(frozen=True)
class DynamicsField:
    """A number the Limiter and Compressor cards edit: its name in the block, its label, the engine's range, its
    finest step and its unit."""
    name: str
    label: str
    low: float
    high: float
    step: float
    unit: str = ""


_THRESHOLD = DynamicsField("threshold_db", "Threshold", *codecs.DYN_THRESHOLD_DB,
                           _step(codecs.dyneq_threshold_u16), "dB")
_ATTACK = DynamicsField("start_time_ms", "Attack", *codecs.DYN_START_MS, _step(codecs.dyneq_starttime_u16), "ms")
_RELEASE = DynamicsField("release_time_ms", "Release", *codecs.DYN_RELEASE_MS, _step(codecs.dyneq_release_time_u16),
                         "ms")
_RATIO = DynamicsField("ratio", "Ratio", *codecs.COMPRESSOR_RATIO, _step(codecs.compressor_ratio_u16))
# The numeric fields of each block, in card order. The limiter also has a detection choice and both an enable flag.
DYNAMICS_FIELDS: dict[str, tuple[DynamicsField, ...]] = {
    "limiter": (_THRESHOLD, _ATTACK, _RELEASE),
    "compressor": (_THRESHOLD, _RATIO, _ATTACK, _RELEASE),
}


def clamp_dynamics(kind: str, block: Any, *, keep: set[str]) -> Any:
    """The block with each numeric field outside the engine's range, except those in `keep`, moved to the nearest
    allowed value."""
    return replace(block, **{f.name: min(max(getattr(block, f.name), f.low), f.high)
                             for f in DYNAMICS_FIELDS[kind] if f.name not in keep})


def _range_text(field: DynamicsField) -> str:
    return f"{field.label} must be from {field.low:g} to {field.high:g}{' ' + field.unit if field.unit else ''}"


def band_in_use(band: EQBandSettings) -> bool:
    return band.enabled or band.gain_db != 0 or band.type != 0


def cleared_band(band: EQBandSettings) -> EQBandSettings:
    """The band as the Tune screen's remove action leaves it: off and flat, with frequency and Q kept."""
    return replace(band, enabled=False, gain_db=0.0, type=0, slope=0)


class TuningController:
    def __init__(self, service: DSPService):
        self.service = service
        self._bypass_masks: dict[tuple[str, bool, int], tuple[bool, ...]] = {}

    def reset(self, token: str) -> None:
        for key in list(self._bypass_masks):
            if key[0] == token:
                self._bypass_masks.pop(key)

    def channel(self, token: str, output: bool, channel: int) -> ChannelSettings:
        settings = self.service.view(token).draft
        channels = settings.outputs if output else settings.inputs
        if not 0 <= channel < len(channels):
            raise ValueError("channel out of range")
        return channels[channel]

    def eq_edit(self, token: str, output: bool, channel: int, index: int, field: str,
                value: Any) -> tuple[ControlKey, EQBandSettings]:
        """The band an EQ field edit would stage, without staging it."""
        ch = self.channel(token, output, channel)
        if not 0 <= index < len(ch.eq_bands):
            raise ValueError("EQ band out of range")
        if field not in {"freq_hz", "gain_db", "q", "type", "enabled"}:
            raise ValueError("unsupported EQ field")
        band = ch.eq_bands[index]
        if field in {"freq_hz", "type"}:
            value = int(value)
        updates: dict[str, Any] = {field: value}
        if field == "type" and value != band.type:
            if value not in EQ_TYPES:
                raise ValueError("unresolved EQ types cannot be selected as new filters")
            updates["slope"] = 0
            if value in ALLPASS_TYPES:
                updates["gain_db"] = 0.0
        if field == "gain_db" and band.type in ALLPASS_TYPES:
            raise ValueError("allpass gain is fixed; adjust frequency and Q")
        return ControlKey(output, channel, "eq", index), replace(band, **updates)

    def eq(self, token: str, output: bool, channel: int, index: int, field: str, value: Any,
           *, immediate: bool = False) -> None:
        key, band = self.eq_edit(token, output, channel, index, field, value)
        self.service.stage(token, key, band, immediate=immediate)

    def crossover_edit(self, token: str, channel: int, band: str, field: str,
                       value: Any) -> tuple[ControlKey, CrossoverSettings]:
        """The crossover block a field edit would stage, without staging it."""
        if band not in {"hp", "lp"}:
            raise ValueError("unknown crossover band")
        ch = self.channel(token, True, channel)
        block = ch.hp if band == "hp" else ch.lp
        if block is None:
            raise ValueError("crossover unavailable")
        if field not in {"freq_hz", "filter_type", "slope_db_per_oct", "enabled"}:
            raise ValueError("unsupported crossover field")
        if field != "enabled":
            value = int(value)
        return ControlKey(True, channel, band), replace(block, **{field: value})

    def crossover(self, token: str, channel: int, band: str, field: str, value: Any,
                  *, immediate: bool = False) -> None:
        key, block = self.crossover_edit(token, channel, band, field, value)
        self.service.stage(token, key, block, immediate=immediate)

    def dynamics_edit(self, token: str, channel: int, kind: str,
                      values: dict[str, Any]) -> tuple[ControlKey, LimiterSettings | CompressorSettings]:
        """The limiter or compressor block an edit of `values` (field: value) would stage, without staging it.

        A typed number outside the engine's range is refused. The factory compressor of a disabled output stores
        values the encoder rejects (a ratio of 0, a threshold of 340 dB); editing such a block, or enabling it,
        clamps each stored field that was not itself typed to the nearest allowed value, as the app's own controls
        would have to before the unit takes the block.
        """
        ch = self.channel(token, True, channel)
        block = ch.limiter if kind == "limiter" else ch.compressor
        if kind not in DYNAMICS_FIELDS or block is None:
            raise ValueError(f"{kind} unavailable")
        numbers = {f.name: f for f in DYNAMICS_FIELDS[kind]}
        allowed = {*numbers, "enabled", *(("detection_type",) if kind == "limiter" else ())}
        unknown = sorted(values.keys() - allowed)
        if unknown:
            raise ValueError(f"unsupported {kind} field: {', '.join(unknown)}")
        updates: dict[str, Any] = {}
        for name, value in values.items():
            if name in numbers:
                if not numbers[name].low <= value <= numbers[name].high:
                    raise ValueError(_range_text(numbers[name]))
            elif name == "detection_type":
                if value not in LIMITER_DETECTION:
                    raise ValueError("Detection must be Average or Peak")
                value = int(value)
            updates[name] = value
        edited = replace(block, **updates)
        if same_value(edited, block):
            return ControlKey(True, channel, kind), block
        return ControlKey(True, channel, kind), clamp_dynamics(kind, edited, keep=set(updates))

    def add_allpass(self, token: str, output: bool, channel: int) -> int:
        ch = self.channel(token, output, channel)
        if (token, output, channel) in self._bypass_masks:
            raise ValueError("restore EQ before allocating an allpass slot")
        for index, band in enumerate(ch.eq_bands):
            if not band.enabled:
                self.service.stage(token, ControlKey(output, channel, "eq", index),
                                   replace(band, freq_hz=80, gain_db=0.0, q=1.0, slope=0, type=4, enabled=True))
                return index
        raise ValueError("no disabled EQ slot is available for an allpass")

    def bypass(self, token: str, output: bool, channel: int, *, restore: bool = False) -> None:
        ch = self.channel(token, output, channel)
        key = (token, output, channel)
        mask = self._bypass_masks.get(key)
        if restore and mask is None:
            raise ValueError("there is no saved EQ bypass state to restore")
        if not restore and mask is None:
            mask = tuple(b.enabled for b in ch.eq_bands)
        self.service.stage_many(token, {
            ControlKey(output, channel, "eq", i): replace(band, enabled=mask[i] if restore and mask else False)
            for i, band in enumerate(ch.eq_bands)
        })
        if restore:
            self._bypass_masks.pop(key, None)
        elif mask is not None:
            self._bypass_masks[key] = mask
