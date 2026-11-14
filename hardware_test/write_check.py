"""Write-and-readback check for the settings the REW tuning loop will write.

Unlike the rest of this directory it needs no audio loopback, so it can run
against an LA448 on USB from any host: each step writes one control with a
changed value, reads the whole settings snapshot back, checks that exactly
that control changed and to the value sent, then undoes it. It plays and
measures nothing, so it says nothing about what a setting does to the audio.

Writes go through `restore.write_control`, the same path the app's Apply
uses, and the unit is put back with `restore.restore_settings` whatever
happens. Limiters, dynamics, FIR and input-side processing are never written.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from sinbosen_la548.client import Client
from sinbosen_la548.service import ControlKey, changes, controls, normalize_control, replace_control
from sinbosen_la548.settings import DeviceSettings

from . import restore

OK = "ok"
NOT_RUN = "not run"


@dataclass(frozen=True)
class Step:
    name: str
    key: ControlKey
    value: object


@dataclass(frozen=True)
class StepResult:
    step: Step
    outcome: str
    detail: str = ""


@dataclass(frozen=True)
class Report:
    results: tuple[StepResult, ...]
    restored: bool
    byte_identical: bool
    restore_error: str = ""

    @property
    def passed(self) -> bool:
        return self.restored and self.byte_identical and all(r.outcome == OK for r in self.results)


def _changes_only(settings: DeviceSettings, key: ControlKey, value: object) -> bool:
    try:
        after = replace_control(settings, key, normalize_control(key, value))
    except ValueError:
        return False
    return [c.key for c in changes(settings, after)] == [key]


def _nudged(settings: DeviceSettings, key: ControlKey, delta: float) -> float:
    """`current + delta`, or `current - delta` at the end of the range."""
    current = controls(settings)[key]
    assert isinstance(current, (int, float))
    return current + delta if _changes_only(settings, key, current + delta) else current - delta


def _other(current: object, first: object, second: object) -> object:
    return first if current != first else second


def candidates(settings: DeviceSettings, *, output: int = 7, route_input: int = 3,
               eq_band: int = 14) -> tuple[Step, ...]:
    """One changed value per kind of setting, each different from what the
    unit holds now. Level only ever goes down, except gain at its minimum."""
    now = controls(settings)
    out = f"Out {output + 1}"

    def key(block: str, index: int = -1) -> ControlKey:
        return ControlKey(True, output, block, index)

    route = ControlKey(False, route_input, "route", output)
    hp, lp, eq = now[key("hp")], now[key("lp")], now[key("eq", eq_band)]
    return (
        Step(f"{out} mute", key("mute"), not now[key("mute")]),
        Step(f"{out} gain", key("gain"), _nudged(settings, key("gain"), -1.0)),
        Step(f"{out} polarity", key("polarity"), not now[key("polarity")]),
        Step(f"{out} delay", key("delay"), _nudged(settings, key("delay"), 1.0)),
        Step(f"In {'ABCD'[route_input]} to {out} route", route, not now[route]),
        Step(f"{out} high-pass frequency", key("hp"), replace(hp, freq_hz=_other(hp.freq_hz, 120, 150))),
        Step(f"{out} high-pass type", key("hp"), replace(hp, filter_type=(hp.filter_type + 1) % 3)),
        Step(f"{out} high-pass slope", key("hp"),
             replace(hp, slope_db_per_oct=_other(hp.slope_db_per_oct, 24, 12))),
        Step(f"{out} high-pass enable", key("hp"), replace(hp, enabled=not hp.enabled)),
        Step(f"{out} low-pass frequency", key("lp"), replace(lp, freq_hz=_other(lp.freq_hz, 8000, 6000))),
        Step(f"{out} low-pass enable", key("lp"), replace(lp, enabled=not lp.enabled)),
        Step(f"{out} EQ band {eq_band + 1}", key("eq", eq_band), replace(
            eq, freq_hz=_other(eq.freq_hz, 1000, 1250), gain_db=_other(eq.gain_db, -3.0, -4.0),
            q=_other(eq.q, 2.0, 3.0), enabled=not eq.enabled)),
    )


def _attempt(client: Client, baseline: DeviceSettings, step: Step) -> StepResult:
    try:
        expected = normalize_control(step.key, step.value)
        restore.write_control(client, step.key, step.value)
        after = client.read_settings()
        diff = changes(baseline, after)
        if not diff:
            return StepResult(step, "no change", "acknowledged, but the readback is unchanged")
        others = [c.key.label for c in diff if c.key != step.key]
        if others:
            return StepResult(step, "other controls changed", ", ".join(others))
        if changes(replace_control(baseline, step.key, expected), after):
            return StepResult(step, "wrong value", f"wrote {expected!r}, read back {controls(after)[step.key]!r}")
        restore.write_control(client, step.key, controls(baseline)[step.key])
        if changes(baseline, client.read_settings()):
            return StepResult(step, "error", "could not be undone")
    except Exception as exc:  # a dropped link or a refused write is a result, not a crash
        return StepResult(step, "error", str(exc) or type(exc).__name__)
    return StepResult(step, OK)


def run(client: Client, steps: Sequence[Step] | None = None, *, output: int = 7, route_input: int = 3,
        eq_band: int = 14, on_baseline: Callable[[bytes], None] | None = None) -> Report:
    if client.read_lock_state().locked:
        raise RuntimeError("the unit is locked; unlock it on the front panel and run again")
    raw_before = client.read_group()
    if on_baseline is not None:
        on_baseline(raw_before)
    baseline = client.read_settings()
    if steps is None:
        steps = candidates(baseline, output=output, route_input=route_input, eq_band=eq_band)

    results: list[StepResult] = []
    try:
        for step in steps:
            # After a failure the unit's write behaviour is unknown: send nothing more.
            stopped = any(r.outcome != OK for r in results)
            results.append(StepResult(step, NOT_RUN) if stopped else _attempt(client, baseline, step))
    finally:
        restore_error = ""
        try:
            restore.restore_settings(client, baseline)
        except Exception as exc:
            restore_error = str(exc) or type(exc).__name__
        try:
            byte_identical = client.read_group() == raw_before
        except Exception:
            byte_identical = False
    return Report(tuple(results), not restore_error, byte_identical, restore_error)


def _shown(value: object) -> str:
    raw = getattr(value, "raw", None)
    text = repr(replace(value, raw=b"") if raw is not None else value)  # type: ignore[type-var]
    return text.replace(", raw=b''", "").replace("|", "\\|")


def markdown(report: Report) -> str:
    def yes(flag: bool) -> str:
        return "yes" if flag else "no"

    lines = ["| Control | Wrote | Result | Detail |", "|---|---|---|---|"]
    for result in report.results:
        detail = result.detail.replace("|", "\\|")
        lines.append(f"| {result.step.name} | `{_shown(result.step.value)}` | {result.outcome} | {detail} |")
    summary = (f"Settings restored: {yes(report.restored)}. "
               f"Snapshot byte-identical before and after: {yes(report.byte_identical)}.")
    if report.restore_error:
        summary += f" Restore error: {report.restore_error}"
    return "\n".join([*lines, "", summary])
