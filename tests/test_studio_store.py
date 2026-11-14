from __future__ import annotations

import json
from pathlib import Path

from sinbosen_la548.studio_store import SECTIONS, DeviceNotes, LinkGroup, StudioStore, default_path

ALL = frozenset(SECTIONS)


def group(letter: str = "A", output: bool = False, members: tuple[int, ...] = (0, 1),
          sections: frozenset[str] = ALL) -> LinkGroup:
    return LinkGroup(letter, output, members, sections)


def write(path: Path, data: object) -> None:
    path.write_text(json.dumps(data))


def test_default_path_is_under_config_dir() -> None:
    assert default_path() == Path.home() / ".config" / "sinbosen-la548" / "studio.json"


def test_round_trip_per_device_key(tmp_path: Path) -> None:
    store = StudioStore(tmp_path / "studio.json")
    notes = DeviceNotes({(False, 0): "Kick", (True, 3): "Tweeter"},
                        (group("A", False, (0, 1)), group("B", True, (2, 3), frozenset({"gain", "eq"}))))
    store.save("usb:123", notes)
    loaded = StudioStore(tmp_path / "studio.json").load("usb:123")
    assert loaded == notes
    assert store.warning is None


def test_devices_do_not_share_notes(tmp_path: Path) -> None:
    store = StudioStore(tmp_path / "studio.json")
    store.save("usb:1", DeviceNotes({(False, 0): "One"}, ()))
    store.save("192.168.0.5", DeviceNotes({(True, 1): "Two"}, (group(),)))
    assert store.load("usb:1") == DeviceNotes({(False, 0): "One"}, ())
    assert store.load("192.168.0.5") == DeviceNotes({(True, 1): "Two"}, (group(),))


def test_unknown_device_and_missing_file_give_empty_notes(tmp_path: Path) -> None:
    store = StudioStore(tmp_path / "studio.json")
    assert store.load("usb:nope") == DeviceNotes({}, ())
    store.save("usb:1", DeviceNotes({(False, 0): "x"}, ()))
    assert store.load("usb:nope") == DeviceNotes({}, ())
    assert store.warning is None


def test_save_creates_directory(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "studio.json"
    StudioStore(path).save("usb:1", DeviceNotes({(False, 0): "x"}, ()))
    assert path.is_file()
    assert [p.name for p in path.parent.iterdir()] == ["studio.json"]


def test_corrupt_json_is_set_aside_with_warning(tmp_path: Path) -> None:
    path = tmp_path / "studio.json"
    path.write_text("{not json")
    store = StudioStore(path)
    assert store.load("usb:1") == DeviceNotes({}, ())
    assert store.warning is not None and "studio.json.bad" in store.warning
    assert not path.exists()
    assert (tmp_path / "studio.json.bad").read_text() == "{not json"


def test_wrong_shape_is_set_aside(tmp_path: Path) -> None:
    path = tmp_path / "studio.json"
    write(path, ["not", "an", "object"])
    store = StudioStore(path)
    assert store.load("usb:1") == DeviceNotes({}, ())
    assert store.warning is not None
    assert (tmp_path / "studio.json.bad").exists()


def test_save_after_set_aside_starts_fresh(tmp_path: Path) -> None:
    path = tmp_path / "studio.json"
    path.write_text("garbage")
    store = StudioStore(path)
    store.save("usb:1", DeviceNotes({(False, 0): "x"}, ()))
    assert store.load("usb:1").nicknames == {(False, 0): "x"}
    assert (tmp_path / "studio.json.bad").read_text() == "garbage"


def raw_group(letter: object = "A", output: object = False, members: object = (0, 1),
              sections: object = ("eq",)) -> dict[str, object]:
    return {"letter": letter, "output": output, "members": members, "sections": sections}


def test_invalid_groups_are_dropped_individually(tmp_path: Path) -> None:
    path = tmp_path / "studio.json"
    write(path, {"version": 1, "devices": {"usb:1": {"nicknames": {}, "links": [
        raw_group("A", False, [0, 1]),                    # valid
        raw_group("B", False, [2]),                       # fewer than 2 members
        raw_group("C", False, [3, 4], ["eq", "bogus"]),   # unknown section
        raw_group("D", False, [1, 5]),                    # channel 1 already in A (same side)
        raw_group("E", True, [1, 5]),                     # outputs are a separate side: valid
        raw_group("ab", False, [6, 7]),                   # bad letter
        raw_group("F", False, [8, 8]),                    # duplicates collapse to one member
        raw_group("G", False, [-1, 9]),                   # negative channel
        "junk",
    ]}}})
    store = StudioStore(path)
    notes = store.load("usb:1")
    assert [g.letter for g in notes.links] == ["A", "E"]
    assert store.warning is None
    assert not (tmp_path / "studio.json.bad").exists()


def test_members_are_sorted_and_unique(tmp_path: Path) -> None:
    path = tmp_path / "studio.json"
    write(path, {"version": 1, "devices": {"usb:1": {"nicknames": {}, "links": [
        raw_group("A", False, [3, 1, 3, 2])]}}})
    assert StudioStore(path).load("usb:1").links == (group("A", False, (1, 2, 3), frozenset({"eq"})),)


def test_save_normalizes_members_and_drops_invalid_groups(tmp_path: Path) -> None:
    store = StudioStore(tmp_path / "studio.json")
    store.save("usb:1", DeviceNotes({}, (group("A", False, (2, 0, 2)), group("B", False, (1,)),
                                         group("C", False, (0, 4)))))
    assert store.load("usb:1").links == (group("A", False, (0, 2)),)


def test_nicknames_are_trimmed_capped_and_removed_when_empty(tmp_path: Path) -> None:
    store = StudioStore(tmp_path / "studio.json")
    store.save("usb:1", DeviceNotes({(False, 0): "  Kick  ", (False, 1): "   ", (True, 0): "x" * 40,
                                     (True, 1): ""}, ()))
    assert store.load("usb:1").nicknames == {(False, 0): "Kick", (True, 0): "x" * 24}


def test_nicknames_trimmed_on_load(tmp_path: Path) -> None:
    path = tmp_path / "studio.json"
    write(path, {"version": 1, "devices": {"usb:1": {"nicknames": {
        "in:0": " Snare ", "out:2": "", "in:x": "bad", "out:1": 5, "in:-1": "neg"}, "links": []}}})
    assert StudioStore(path).load("usb:1").nicknames == {(False, 0): "Snare"}
