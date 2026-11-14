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

import argparse
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from pathlib import Path

from sinbosen_la548.client import Client
from sinbosen_la548.service import ControlKey, changes, controls, normalize_control, replace_control
from sinbosen_la548.settings import DeviceSettings

from . import restore

OK = "ok"
NOT_RUN = "not run"
INPUTS = "ABCD"
DEFAULT_BACKUP = "ignore/write-check-baseline.bin"
USB_HINT = "USB support needs the usb extra: uv run --extra usb python -m hardware_test.write_check usb --amps-off"


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
        Step(f"In {INPUTS[route_input]} to {out} route", route, not now[route]),
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


def _byte_diff(a: bytes, b: bytes) -> str:
    offsets = [i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y]
    count = len(offsets) + abs(len(a) - len(b))
    first = offsets[0] if offsets else min(len(a), len(b))
    return f"{count} byte{'' if count == 1 else 's'} of the snapshot differ, first at offset {first}"


def _attempt(client: Client, baseline: DeviceSettings, raw_before: bytes, step: Step) -> StepResult:
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
        # Compared as raw bytes, not decoded controls: a write that disturbed
        # bytes no control decodes cannot be put back by any write we have.
        raw = client.read_group()
        if raw != raw_before:
            return StepResult(step, "error", f"could not be undone: {_byte_diff(raw_before, raw)}")
    except Exception as exc:  # a dropped link or a refused write is a result, not a crash
        return StepResult(step, "error", str(exc) or type(exc).__name__)
    return StepResult(step, OK)


def run(client: Client, steps: Sequence[Step] | None = None, *, output: int = 7, route_input: int = 3,
        eq_band: int = 14, on_baseline: Callable[[bytes], None] | None = None) -> Report:
    if client.read_lock_state().locked:
        raise RuntimeError("the unit is locked; unlock it on the front panel and run again")
    raw_before = client.read_group()
    if client.read_group() != raw_before:
        raise RuntimeError("the unit's snapshot is not stable: two reads with no write between them differ, "
                           "so a write could not be told from the unit changing on its own")
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
            if stopped:
                results.append(StepResult(step, NOT_RUN))
                continue
            try:
                results.append(_attempt(client, baseline, raw_before, step))
            except KeyboardInterrupt:
                results.append(StepResult(step, "error", "interrupted"))
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hardware_test.write_check",
        description="Write each setting the tuning loop uses with a changed value, read it back and undo it. "
                    "Plays no audio. Turn the amplifiers off first: one output is briefly unmuted, re-routed "
                    "and re-filtered.")
    parser.add_argument("address", nargs="?", default="usb",
                        help="`usb`, `usb:<serial>` or an IP address (default: usb)")
    parser.add_argument("--amps-off", action="store_true",
                        help="confirm the amplifiers are off or the unit's outputs are unplugged (required)")
    parser.add_argument("--output", type=int, choices=range(1, 9), default=8, metavar="1-8",
                        help="output to write to (default: 8)")
    parser.add_argument("--input", choices=tuple(INPUTS), default="D",
                        help="input whose route to that output is toggled (default: D)")
    parser.add_argument("--eq-band", type=int, choices=range(1, 16), default=15, metavar="1-15",
                        help="EQ band on that output to write to (default: 15)")
    parser.add_argument("--backup", type=Path, default=Path(DEFAULT_BACKUP),
                        help=f"where to save the unit's snapshot before the first write (default: {DEFAULT_BACKUP})")
    return parser


def main(argv: Sequence[str] | None = None, *,
         connect: Callable[[str], AbstractContextManager[Client]] = Client) -> int:
    """Exit 0: every step ok and the unit restored byte-identical. 1: a step
    failed but the unit was restored. 2: nothing was run, or the unit was
    not restored."""
    args = _parser().parse_args(argv)
    if not args.amps_off:
        print("Nothing was written. Turn the amplifiers off (or unplug the unit's outputs), then run again "
              "with --amps-off: the check briefly unmutes, re-routes and re-filters one output.", file=sys.stderr)
        return 2

    if args.backup.exists():
        print(f"Nothing was written. {args.backup} already exists and may be the only record of how the unit "
              "was before an earlier run. Move it away, or name another file with --backup.", file=sys.stderr)
        return 2

    def save(raw: bytes) -> None:
        args.backup.parent.mkdir(parents=True, exist_ok=True)
        args.backup.write_bytes(raw)

    try:
        with connect(args.address) as client:
            report = run(client, output=args.output - 1, route_input=INPUTS.index(args.input),
                         eq_band=args.eq_band - 1, on_baseline=save)
    except Exception as exc:
        print(f"The write check did not run: {exc}", file=sys.stderr)
        if "hidapi" in str(exc):
            print(USB_HINT, file=sys.stderr)
        return 2

    print(markdown(report))
    for result in report.results:
        if result.outcome not in (OK, NOT_RUN):
            print(f"FAILED: {result.step.name}: {result.outcome}. {result.detail}".rstrip(), file=sys.stderr)
    if not (report.restored and report.byte_identical):
        print(f"The unit was NOT restored to how it was found. {report.restore_error}".rstrip(), file=sys.stderr)
        print(f"Its snapshot from before the run is in {args.backup}. Do not write to the unit again until "
              "this is understood.", file=sys.stderr)
        return 2
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
