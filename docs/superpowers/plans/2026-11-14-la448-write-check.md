# LA448 Write Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove on the user's LA448 over USB that each kind of setting the REW tuning loop will write (mix route, output gain, delay, polarity, mute, crossover, an EQ band) changes on the unit, reads back as written, disturbs nothing else, and can be restored.

**Architecture:** One new module, `hardware_test/write_check.py`, with a pure step runner that takes any connected `Client` and a command-line entry point. It writes through the same path the app uses (`hardware_test.restore.write_control`, which calls `DSPService._write`), reads the whole settings snapshot back after every write, and compares with `service.changes`. No audio is played or measured: this rig has no loopback. Offline tests drive the runner against a fake unit.

**Tech Stack:** Python 3 standard library, the existing `sinbosen_la548` package, `hidapi` through the existing `usb` extra, pytest.

**Spec:** `docs/superpowers/specs/2026-11-14-rew-guided-tuning-design.md` (Phases, item 0).

## Global Constraints

- The check must run on macOS with the LA448 on USB. It must not import `hardware_test.rig`, `hardware_test.loopback`, `hardware_test.audio_io` or `hardware_test.analysis` (Linux/PipeWire, numpy).
- No new dependency.
- Nothing is written unless the operator passes `--amps-off`. The check plays no audio, but it unmutes, re-routes and re-filters an output.
- Never send a request the client does not already implement. No FIR writes (USB cannot carry them). No program save/load, no channel copy, no name writes.
- Limiters, compressors, dynamic EQ, FIR and all input-side processing are never written.
- All writes go through `hardware_test.restore.write_control`; the check never builds frames itself.
- The default test gate (`uv run pytest -q`) stays offline: the new tests use a fake unit only.
- A control that fails stops the run. No later step is attempted after a failure.

## Review Focus

1. The unit acknowledges a write but the value does not change: reported as `no change`, the run stops, restore is still attempted. (Task 1, `test_ack_without_change_stops_the_run`)
2. A write changes a control other than the one addressed: reported as `other controls changed` naming them, the run stops. (Task 1, `test_write_that_touches_another_control_is_reported`)
3. The link drops or a write raises mid-run: restore is still attempted, and if restore fails the report says the unit is NOT restored and where the baseline file is. (Task 1, `test_error_mid_run_still_restores`; Task 2, `test_failed_restore_exits_2_and_names_the_backup`)
4. The unit is locked: refused before any write. (Task 1, `test_locked_unit_is_refused_before_any_write`)
5. A current value already sits at the edge of its range or equals the candidate: the step picks a different valid value rather than writing the same value or an invalid one. (Task 1, `test_candidates_always_differ_from_current`)

---

### Task 1: The step runner

**Files:**
- Create: `hardware_test/write_check.py`
- Test: `tests/test_write_check.py`

**Interfaces:**
- Consumes: `sinbosen_la548.service.ControlKey`, `controls`, `changes`, `replace_control`, `normalize_control`; `sinbosen_la548.settings.DeviceSettings`; `hardware_test.restore.write_control`, `restore_settings` (call them as `restore.write_control` / `restore.restore_settings` through the module so tests can replace them); `Client.read_settings()`, `Client.read_group()`, `Client.read_lock_state()`.
- Produces:

```python
@dataclass(frozen=True)
class Step:
    name: str            # e.g. "Out 8 high-pass frequency"
    key: ControlKey
    value: object        # the changed value to write

@dataclass(frozen=True)
class StepResult:
    step: Step
    outcome: str         # "ok" | "no change" | "wrong value" | "other controls changed" | "error" | "not run"
    detail: str = ""

@dataclass(frozen=True)
class Report:
    results: tuple[StepResult, ...]
    restored: bool           # settings equal the baseline after restore
    byte_identical: bool     # read_group() bytes equal before and after
    restore_error: str = ""

    @property
    def passed(self) -> bool: ...   # every outcome "ok", restored, byte_identical

def candidates(settings: DeviceSettings, *, output: int = 7, route_input: int = 3, eq_band: int = 14) -> tuple[Step, ...]
def run(client: Client, steps: Sequence[Step] | None = None, *, output: int = 7, route_input: int = 3,
        eq_band: int = 14, on_baseline: Callable[[bytes], None] | None = None) -> Report
def markdown(report: Report) -> str
```

`output`, `route_input` and `eq_band` are 0-based. Defaults are Out 8, In D and EQ band 15: Out 8 is unused on the user's rig and In D to Out 8 is not a live music route.

- [ ] **Step 1: Write the fake unit and the failing tests**

In `tests/test_write_check.py`, build `FakeUnit` from the same fixture and decoding call `tests/test_hardware_helpers.py` uses for `read_group_sample.bin`. It holds a `DeviceSettings`, and exposes `read_settings()`, `read_group()` (returns `repr(controls(settings)).encode()`, so bytes change exactly when settings do), `read_lock_state()` (object with `.locked`), and a `writes: list[tuple[ControlKey, object]]`. A `fake_write(client, key, value)` function sets `client.settings = replace_control(client.settings, key, normalize_control(key, value))` and appends to `writes`. An autouse fixture monkeypatches `hardware_test.restore.write_control` with `fake_write`.

Tests, each asserting exactly this:

```python
def test_candidates_cover_every_kind_of_setting():
    steps = write_check.candidates(FakeUnit().settings)
    assert [(s.key.block, s.key.output, s.key.channel, s.key.index) for s in steps] == [
        ("mute", True, 7, -1), ("gain", True, 7, -1), ("polarity", True, 7, -1), ("delay", True, 7, -1),
        ("route", False, 3, 7),
        ("hp", True, 7, -1), ("hp", True, 7, -1), ("hp", True, 7, -1), ("hp", True, 7, -1),
        ("lp", True, 7, -1), ("lp", True, 7, -1),
        ("eq", True, 7, 14),
    ]

def test_candidates_always_differ_from_current():
    # for the fixture as-is, and again after applying every candidate once
    # (so each "current" equals the first-choice candidate), every step's
    # value normalizes without error and changes exactly its own key
    for settings in (base, base_with_all_candidates_applied):
        for step in write_check.candidates(settings):
            after = replace_control(settings, step.key, normalize_control(step.key, step.value))
            assert [c.key for c in changes(settings, after)] == [step.key]

def test_clean_run_passes_and_leaves_the_unit_as_found():
    unit = FakeUnit(); before = unit.read_group()
    report = write_check.run(unit)
    assert report.passed and report.restored and report.byte_identical
    assert [r.outcome for r in report.results] == ["ok"] * 12
    assert unit.read_group() == before

def test_each_step_is_undone_before_the_next():
    # after a clean run, writes alternate: step value, then baseline value, for the same key
    keys = [k for k, _ in unit.writes]
    assert keys[0::2] == keys[1::2] == [s.key for s in write_check.candidates(base)]

def test_ack_without_change_stops_the_run():
    # fake_write ignores writes to block "delay"
    assert [r.outcome for r in report.results] == ["ok", "ok", "ok", "no change"] + ["not run"] * 8
    assert not report.passed and report.restored

def test_wrong_value_is_reported_with_what_was_read():
    # fake_write stores gain + 0.5 dB instead of the requested gain
    assert report.results[1].outcome == "wrong value"
    assert "read back" in report.results[1].detail

def test_write_that_touches_another_control_is_reported():
    # fake_write for Out 8 polarity also flips Out 7 polarity
    assert report.results[2].outcome == "other controls changed"
    assert "Output 7: polarity" in report.results[2].detail
    assert report.restored   # restore_settings puts Out 7 back too

def test_error_mid_run_still_restores():
    # fake_write raises OSError("unplugged") on the first route write only
    assert report.results[4].outcome == "error" and "unplugged" in report.results[4].detail
    assert report.results[5].outcome == "not run"
    assert report.restored

def test_failed_restore_is_reported_not_raised():
    # fake_write raises on every write after the first
    assert report.restored is False and report.restore_error

def test_locked_unit_is_refused_before_any_write():
    unit = FakeUnit(locked=True)
    with pytest.raises(RuntimeError, match="locked"):
        write_check.run(unit)
    assert unit.writes == []

def test_on_baseline_gets_the_snapshot_before_the_first_write():
    # the callback records len(unit.writes) when called; it must be 0, and
    # it receives exactly unit.read_group() as it was at start

def test_markdown_has_one_row_per_step_and_a_restore_line():
    text = write_check.markdown(report)
    assert text.count("\n| ") >= 12 and "Out 8 delay" in text
    assert "byte-identical" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_write_check.py -q`
Expected: FAIL at import, `hardware_test.write_check` does not exist.

- [ ] **Step 3: Implement `candidates`**

Values, each built from the control's current value (`controls(settings)[key]`); struct values use `dataclasses.replace` on the current struct. The second value is used when the current value equals the first.

| # | Name | Key | Value |
|---|---|---|---|
| 1 | Out N mute | `(True, output, "mute")` | not current |
| 2 | Out N gain | `(True, output, "gain")` | current - 1.0 dB; if `normalize_control` rejects that or it normalizes to the current value, current + 1.0 |
| 3 | Out N polarity | `(True, output, "polarity")` | not current |
| 4 | Out N delay | `(True, output, "delay")` | current + 1.0 ms; same fallback rule, current - 1.0 |
| 5 | In X to Out N route | `(False, route_input, "route", output)` | not current |
| 6 | Out N high-pass frequency | `(True, output, "hp")` | `freq_hz` 120, else 150 |
| 7 | Out N high-pass type | same | `filter_type` (current + 1) % 3 |
| 8 | Out N high-pass slope | same | `slope_db_per_oct` 24, else 12 |
| 9 | Out N high-pass enable | same | `enabled` not current |
| 10 | Out N low-pass frequency | `(True, output, "lp")` | `freq_hz` 8000, else 6000 |
| 11 | Out N low-pass enable | same | `enabled` not current |
| 12 | Out N EQ band B | `(True, output, "eq", eq_band)` | `freq_hz` 1000 else 1250, `gain_db` -3.0 else -4.0, `q` 2.0 else 3.0, `enabled` not current; type and slope unchanged |

Names use 1-based numbers and input letters A-D (`"Out 8 delay"`, `"In D to Out 8 route"`, `"Out 8 EQ band 15"`). Every gain and EQ candidate lowers level; none raises it except the gain fallback.

- [ ] **Step 4: Implement `run` and `markdown`**

`run` in this order:
1. `client.read_lock_state().locked` true: raise `RuntimeError("the unit is locked; unlock it on the front panel and run again")`.
2. `raw_before = client.read_group()`, call `on_baseline(raw_before)` if given, then `baseline = client.read_settings()`. `steps = steps or candidates(baseline, ...)`.
3. For each step: `restore.write_control(client, step.key, step.value)`, then `after = client.read_settings()`, then classify:
   - `diff = changes(baseline, after)`; empty: `no change`.
   - any key in `diff` other than `step.key`: `other controls changed`, detail lists their `label`s.
   - `changes(replace_control(baseline, step.key, normalize_control(step.key, step.value)), after)` non-empty: `wrong value`, detail `f"wrote {expected!r}, read back {actual!r}"`.
   - else `ok`.
   An exception from the write or the readback is outcome `error` with `str(exc)` as detail.
   After an `ok` step, undo it with `restore.write_control(client, step.key, controls(baseline)[step.key])` and confirm `changes(baseline, client.read_settings()) == ()`; if that fails the step becomes `error` with detail `"could not be undone"`.
   After the first non-`ok` outcome, every remaining step is recorded as `not run`.
4. Always, in a `finally`-style path: `restore.restore_settings(client, baseline)`; an exception there sets `restored=False`, `restore_error=str(exc)` and is not re-raised. Then `byte_identical = client.read_group() == raw_before` (False if that read raises).

`markdown(report)` returns a table with columns `Control | Wrote | Result | Detail`, one row per step in order, followed by one line: `Settings restored: yes/no. Snapshot byte-identical before and after: yes/no.` plus the restore error if any.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_write_check.py -q`
Expected: all pass. Then `uv run pytest -q` and `uv run ruff check hardware_test/write_check.py tests/test_write_check.py`: no failures, no findings.

- [ ] **Step 6: Commit**

```bash
git add hardware_test/write_check.py tests/test_write_check.py
git commit -m "Add a write-and-readback check for each setting the tuning loop uses"
```

---

### Task 2: Command line and operator instructions

**Files:**
- Modify: `hardware_test/write_check.py` (add `main`)
- Modify: `hardware_test/README.md` (new section "Write check without a loopback (LA448 over USB)")
- Test: `tests/test_write_check.py`

**Interfaces:**
- Consumes: `run`, `markdown`, `Report` from Task 1; `sinbosen_la548.client.Client` as a context manager (`Client("usb")`).
- Produces: `main(argv: Sequence[str] | None = None, *, connect: Callable[[str], AbstractContextManager[Client]] = Client) -> int`, run as `python -m hardware_test.write_check`.

Arguments: positional `address` (default `usb`; `usb:<serial>` or an IP also accepted); `--amps-off` (required acknowledgement); `--output N` (1-8, default 8); `--input X` (A-D, default D); `--eq-band B` (1-15, default 15); `--backup PATH` (default `ignore/write-check-baseline.bin`).

Exit codes: 0 all steps ok and unit restored byte-identical; 1 a step failed but the unit was restored; 2 the unit was not restored, or `--amps-off` was missing, or the unit is locked.

- [ ] **Step 1: Write the failing tests**

```python
def test_main_refuses_without_amps_off(capsys):
    opened = []
    code = write_check.main(["usb"], connect=lambda a: opened.append(a))
    assert code == 2 and opened == []
    assert "--amps-off" in capsys.readouterr().err

def test_main_clean_run_prints_the_table_and_exits_0(tmp_path, capsys):
    code = write_check.main(["usb", "--amps-off", "--backup", str(tmp_path / "b.bin")], connect=fake_connect)
    assert code == 0
    assert "| Out 8 delay |" in capsys.readouterr().out
    assert (tmp_path / "b.bin").read_bytes() == unit_snapshot_before

def test_main_backup_is_written_before_the_first_write(tmp_path): ...
    # the fake's first write asserts the backup file already exists

def test_main_step_failure_exits_1(tmp_path): ...       # delay writes ignored
def test_failed_restore_exits_2_and_names_the_backup(tmp_path, capsys): ...
    # stderr contains "NOT restored" and the backup path
def test_main_locked_unit_exits_2(tmp_path, capsys): ...
def test_main_uses_the_chosen_channel(tmp_path, capsys):
    # --output 7 --input C --eq-band 14 -> output contains "In C to Out 7 route" and "Out 7 EQ band 14"
def test_main_rejects_out_of_range_choices():
    # --output 9, --input E, --eq-band 16 each raise SystemExit(2) from argparse
```

`fake_connect` returns a context manager yielding the Task 1 `FakeUnit`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_write_check.py -q`
Expected: the new tests FAIL, `write_check.main` does not exist.

- [ ] **Step 3: Implement `main`**

Check `--amps-off` before connecting. Create the backup file's parent directory. Pass `on_baseline` to `run` to write the raw snapshot to `--backup`. Print `markdown(report)` to stdout and failures to stderr. A hidapi import failure from the client is reported as: `USB support needs the usb extra: uv run --extra usb python -m hardware_test.write_check usb --amps-off`. Add the `if __name__ == "__main__": raise SystemExit(main())` guard.

- [ ] **Step 4: Write the README section**

Cover, in this order: what the check does and that it plays no audio; turn the amplifiers off or unplug the unit's outputs first, because it unmutes, re-routes and re-filters one output for a moment; the command `uv run --extra usb python -m hardware_test.write_check usb --amps-off`; how to pick a different output, input or band; what the three exit codes mean; that the pre-run snapshot is saved to the `--backup` path; that it does not measure audio, so filter shapes on the LA448 remain unverified.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest -q` and `uv run ruff check hardware_test tests/test_write_check.py`
Expected: all pass, no findings.

- [ ] **Step 6: Commit**

```bash
git add hardware_test/write_check.py hardware_test/README.md tests/test_write_check.py
git commit -m "Run the write check from the command line and document it"
```

---

### Task 3: Run it on the LA448 and record the result

This task touches the real unit. It needs the user present: do not start it until they confirm the amplifiers are off (or the outputs unplugged) and the LA448 is on USB.

**Files:**
- Modify: `docs/PROTOCOL.md` (section "USB HID transport (LA448)")
- Modify: `docs/superpowers/specs/2026-11-14-rew-guided-tuning-design.md` (Phases, item 0) only if a control failed

**Interfaces:**
- Consumes: the command from Task 2.

- [ ] **Step 1: Confirm the unit is visible, read-only**

Run: `uv run --extra usb sinbosen-la548 search` (it prints `usb:<serial>\tUSB` for each unit on USB).
Expected: one `usb:` line. If none, stop and tell the user. Then `uv run --extra usb sinbosen-la548 status usb` to confirm the unit answers and is not locked.

- [ ] **Step 2: Get the user's go-ahead, then run the check**

Run: `uv run --extra usb python -m hardware_test.write_check usb --amps-off`
Expected: a 12-row table and exit code 0. Keep the full output.

- [ ] **Step 3: If the exit code is 2, stop**

Tell the user the unit was not restored, give them the backup path and the table, and do nothing further to the unit without their say.

- [ ] **Step 4: Record the outcome in `docs/PROTOCOL.md`**

In "USB HID transport (LA448)": add a dated paragraph and the table under "Established on one unit", listing each control that passed as written with a changed value, read back, undone, with the snapshot byte-identical afterwards. In "Not established over USB", replace "A write that changes a value, and each control other than mute and gain" with what is still unestablished: any control not in the table (limiter, compressor, expander, dynamic EQ, input source, frequency shifter, FIR enable, input-side EQ), any control that failed with what was observed, and that the audible effect and filter shapes have not been measured on the LA448. State facts only as observed in this run.

- [ ] **Step 5: If any control failed, note it in the design doc**

Under Phases item 0, add one line per failed control with the observed outcome, and name the later phase it blocks (route: phase 1; gain or EQ: phase 2; delay, polarity or crossover: phase 3).

- [ ] **Step 6: Commit**

```bash
git add docs/PROTOCOL.md docs/superpowers/specs/2026-11-14-rew-guided-tuning-design.md
git commit -m "Record which LA448 writes are established over USB"
```
