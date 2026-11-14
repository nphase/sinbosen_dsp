"""Offline checks of hardware_test.write_check against a fake unit - no
device. The fake applies writes to an in-memory settings snapshot the way a
well-behaved unit would; each test then breaks it in one specific way."""

from collections.abc import Callable
from dataclasses import fields, is_dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from hardware_test import restore, write_check
from sinbosen_la548.service import ControlKey, changes, controls, normalize_control, replace_control
from sinbosen_la548.settings import DeviceSettings, decode_settings

FIXTURE = Path(__file__).parent / "fixtures" / "read_group_sample.bin"

Hook = Callable[["FakeUnit", ControlKey, object], bool]


def _plain(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return tuple(getattr(value, f.name) for f in fields(value) if f.name != "raw")
    return value


class FakeUnit:
    def __init__(self, *, locked: bool = False, hook: Hook | None = None) -> None:
        self.settings: DeviceSettings = decode_settings(FIXTURE.read_bytes())
        self.locked = locked
        self.hook = hook
        self.writes: list[tuple[ControlKey, object]] = []

    def read_settings(self) -> DeviceSettings:
        return self.settings

    def read_group(self) -> bytes:
        # Changes exactly when a control's value does.
        return repr([(key, _plain(value)) for key, value in controls(self.settings).items()]).encode()

    def read_lock_state(self) -> SimpleNamespace:
        return SimpleNamespace(locked=self.locked)

    def store(self, key: ControlKey, value: object) -> None:
        self.settings = replace_control(self.settings, key, normalize_control(key, value))


def fake_write(client: FakeUnit, key: ControlKey, value: object) -> None:
    client.writes.append((key, value))
    if client.hook is None or not client.hook(client, key, value):
        client.store(key, value)


@pytest.fixture(autouse=True)
def _writes_go_to_the_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(restore, "write_control", fake_write)


def _apply(settings: DeviceSettings, step: write_check.Step) -> DeviceSettings:
    return replace_control(settings, step.key, normalize_control(step.key, step.value))


def test_candidates_cover_every_kind_of_setting():
    steps = write_check.candidates(FakeUnit().settings)
    assert [(s.key.block, s.key.output, s.key.channel, s.key.index) for s in steps] == [
        ("mute", True, 7, -1), ("gain", True, 7, -1), ("polarity", True, 7, -1), ("delay", True, 7, -1),
        ("route", False, 3, 7),
        ("hp", True, 7, -1), ("hp", True, 7, -1), ("hp", True, 7, -1), ("hp", True, 7, -1),
        ("lp", True, 7, -1), ("lp", True, 7, -1),
        ("eq", True, 7, 14),
    ]


def test_candidates_are_named_for_the_operator():
    names = [s.name for s in write_check.candidates(FakeUnit().settings)]
    assert names[3] == "Out 8 delay"
    assert names[4] == "In D to Out 8 route"
    assert names[5] == "Out 8 high-pass frequency"
    assert names[11] == "Out 8 EQ band 15"


def test_candidates_always_differ_from_current():
    base = FakeUnit().settings
    shifted = base
    for step in write_check.candidates(base):
        shifted = _apply(shifted, step)
    for settings in (base, shifted):
        for step in write_check.candidates(settings):
            assert [c.key for c in changes(settings, _apply(settings, step))] == [step.key], step.name


def test_gain_candidate_rises_only_when_it_cannot_fall():
    base = FakeUnit().settings
    key = ControlKey(True, 7, "gain")
    current = controls(base)[key]
    assert write_check.candidates(base)[1].value == pytest.approx(current - 1.0)
    # Walk down to the bottom of the range, where lowering is no longer possible.
    floor = base
    while True:
        value = controls(floor)[key]
        try:
            lower = replace_control(floor, key, normalize_control(key, value - 1.0))
        except ValueError:
            break
        if controls(lower)[key] == value:
            break
        floor = lower
    assert write_check.candidates(floor)[1].value > controls(floor)[key]


def test_clean_run_passes_and_leaves_the_unit_as_found():
    unit = FakeUnit()
    before = unit.read_group()
    report = write_check.run(unit)
    assert report.passed and report.restored and report.byte_identical
    assert [r.outcome for r in report.results] == ["ok"] * 12
    assert unit.read_group() == before


def test_each_step_is_undone_before_the_next():
    unit = FakeUnit()
    base = unit.settings
    write_check.run(unit)
    keys = [key for key, _ in unit.writes]
    assert keys[0::2] == keys[1::2] == [s.key for s in write_check.candidates(base)]


def test_ack_without_change_stops_the_run():
    unit = FakeUnit(hook=lambda u, key, value: key.block == "delay")
    report = write_check.run(unit)
    assert [r.outcome for r in report.results] == ["ok", "ok", "ok", "no change"] + ["not run"] * 8
    assert not report.passed and report.restored


def test_wrong_value_is_reported_with_what_was_read():
    def hook(unit: FakeUnit, key: ControlKey, value: object) -> bool:
        if key.block != "gain" or len(unit.writes) != 3:
            return False
        unit.store(key, value + 0.5)
        return True

    report = write_check.run(FakeUnit(hook=hook))
    assert report.results[1].outcome == "wrong value"
    assert "read back" in report.results[1].detail
    assert report.results[2].outcome == "not run"
    assert not report.passed and report.restored


def test_write_that_touches_another_control_is_reported():
    neighbour = ControlKey(True, 6, "polarity")

    def hook(unit: FakeUnit, key: ControlKey, value: object) -> bool:
        if key == ControlKey(True, 7, "polarity") and len(unit.writes) == 5:
            unit.store(neighbour, not controls(unit.settings)[neighbour])
        return False

    unit = FakeUnit(hook=hook)
    before = unit.read_group()
    report = write_check.run(unit)
    assert report.results[2].outcome == "other controls changed"
    assert "Output 7: polarity" in report.results[2].detail
    assert not report.passed and report.restored
    assert unit.read_group() == before


def test_error_mid_run_still_restores():
    seen: list[ControlKey] = []

    def hook(unit: FakeUnit, key: ControlKey, value: object) -> bool:
        if key.block == "route" and not seen:
            seen.append(key)
            raise OSError("unplugged")
        return False

    unit = FakeUnit(hook=hook)
    before = unit.read_group()
    report = write_check.run(unit)
    assert report.results[4].outcome == "error" and "unplugged" in report.results[4].detail
    assert report.results[5].outcome == "not run"
    assert report.restored and unit.read_group() == before


def test_failed_restore_is_reported_not_raised():
    def hook(unit: FakeUnit, key: ControlKey, value: object) -> bool:
        if len(unit.writes) > 1:
            raise OSError("link down")
        return False

    report = write_check.run(FakeUnit(hook=hook))
    assert report.restored is False and "link down" in report.restore_error
    assert not report.passed


def test_step_that_cannot_be_undone_is_an_error():
    # The second mute write (the undo) is acknowledged but ignored.
    unit = FakeUnit(hook=lambda u, key, value: key.block == "mute" and len(u.writes) == 2)
    report = write_check.run(unit)
    assert report.results[0].outcome == "error" and "could not be undone" in report.results[0].detail
    assert report.results[1].outcome == "not run"


def test_locked_unit_is_refused_before_any_write():
    unit = FakeUnit(locked=True)
    with pytest.raises(RuntimeError, match="locked"):
        write_check.run(unit)
    assert unit.writes == []


def test_on_baseline_gets_the_snapshot_before_the_first_write():
    unit = FakeUnit()
    before = unit.read_group()
    got: list[tuple[bytes, int]] = []
    write_check.run(unit, on_baseline=lambda raw: got.append((raw, len(unit.writes))))
    assert got == [(before, 0)]


def test_markdown_has_one_row_per_step_and_a_restore_line():
    text = write_check.markdown(write_check.run(FakeUnit()))
    assert sum(line.startswith("| Out 8") or line.startswith("| In D") for line in text.splitlines()) == 12
    assert "| Out 8 delay |" in text
    assert "Settings restored: yes. Snapshot byte-identical before and after: yes." in text


def test_markdown_says_when_the_unit_was_not_restored():
    def hook(unit: FakeUnit, key: ControlKey, value: object) -> bool:
        if len(unit.writes) > 1:
            raise OSError("link down")
        return False

    text = write_check.markdown(write_check.run(FakeUnit(hook=hook)))
    assert "Settings restored: no." in text and "link down" in text


class _Connected:
    """Stands in for `Client(address)` used as a context manager."""

    def __init__(self, unit: FakeUnit, opened: list[str]) -> None:
        self.unit, self.opened = unit, opened

    def __call__(self, address: str) -> "_Connected":
        self.opened.append(address)
        return self

    def __enter__(self) -> FakeUnit:
        return self.unit

    def __exit__(self, *exc: object) -> None:
        return None


def _main(unit: FakeUnit, tmp_path: Path, *extra: str) -> tuple[int, Path]:
    backup = tmp_path / "deep" / "b.bin"
    code = write_check.main(["usb", "--amps-off", "--backup", str(backup), *extra], connect=_Connected(unit, []))
    return code, backup


def test_main_refuses_without_amps_off(capsys):
    opened: list[str] = []
    code = write_check.main(["usb"], connect=_Connected(FakeUnit(), opened))
    assert code == 2 and opened == []
    assert "--amps-off" in capsys.readouterr().err


def test_main_clean_run_prints_the_table_and_exits_0(tmp_path, capsys):
    unit = FakeUnit()
    before = unit.read_group()
    code, backup = _main(unit, tmp_path)
    assert code == 0
    assert "| Out 8 delay |" in capsys.readouterr().out
    assert backup.read_bytes() == before


def test_main_defaults_to_the_usb_address(tmp_path):
    opened: list[str] = []
    write_check.main(["--amps-off", "--backup", str(tmp_path / "b.bin")], connect=_Connected(FakeUnit(), opened))
    assert opened == ["usb"]


def test_main_backup_is_written_before_the_first_write(tmp_path):
    backup = tmp_path / "deep" / "b.bin"
    seen: list[bool] = []

    def hook(unit: FakeUnit, key: ControlKey, value: object) -> bool:
        seen.append(backup.exists())
        return False

    _main(FakeUnit(hook=hook), tmp_path)
    assert seen and all(seen)


def test_main_step_failure_exits_1(tmp_path, capsys):
    code, _ = _main(FakeUnit(hook=lambda u, key, value: key.block == "delay"), tmp_path)
    captured = capsys.readouterr()
    assert code == 1
    assert "| Out 8 delay |" in captured.out and "no change" in captured.out
    assert "Out 8 delay" in captured.err


def test_failed_restore_exits_2_and_names_the_backup(tmp_path, capsys):
    def hook(unit: FakeUnit, key: ControlKey, value: object) -> bool:
        if len(unit.writes) > 1:
            raise OSError("link down")
        return False

    code, backup = _main(FakeUnit(hook=hook), tmp_path)
    err = capsys.readouterr().err
    assert code == 2
    assert "NOT restored" in err and str(backup) in err


def test_main_locked_unit_exits_2(tmp_path, capsys):
    unit = FakeUnit(locked=True)
    code, _ = _main(unit, tmp_path)
    assert code == 2 and unit.writes == []
    assert "locked" in capsys.readouterr().err


def test_main_uses_the_chosen_channel(tmp_path, capsys):
    code, _ = _main(FakeUnit(), tmp_path, "--output", "7", "--input", "C", "--eq-band", "14")
    out = capsys.readouterr().out
    assert code == 0
    assert "| In C to Out 7 route |" in out and "| Out 7 EQ band 14 |" in out


@pytest.mark.parametrize("bad", [("--output", "9"), ("--output", "0"), ("--input", "E"), ("--eq-band", "16")])
def test_main_rejects_out_of_range_choices(tmp_path, bad):
    with pytest.raises(SystemExit) as raised:
        _main(FakeUnit(), tmp_path, *bad)
    assert raised.value.code == 2


def test_main_explains_a_missing_usb_extra(tmp_path, capsys):
    def connect(address: str):
        raise RuntimeError("USB support needs the 'hidapi' package")

    code = write_check.main(["usb", "--amps-off", "--backup", str(tmp_path / "b.bin")], connect=connect)
    err = capsys.readouterr().err
    assert code == 2
    assert "uv run --extra usb python -m hardware_test.write_check usb --amps-off" in err
