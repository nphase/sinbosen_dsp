"""App-side channel nicknames and link groups, kept per device on this computer and never written to the device."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SECTIONS: tuple[str, ...] = ("crossover", "eq", "gain", "delay", "limiter")
NICKNAME_MAX = 24
_VERSION = 1
_LETTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


@dataclass(frozen=True)
class LinkGroup:
    letter: str
    output: bool
    members: tuple[int, ...]
    sections: frozenset[str]


@dataclass(frozen=True)
class DeviceNotes:
    nicknames: dict[tuple[bool, int], str]  # key = (output, channel)
    links: tuple[LinkGroup, ...]


def default_path() -> Path:
    return Path.home() / ".config" / "sinbosen-la548" / "studio.json"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nickname_key(output: bool, channel: int) -> str:
    return f"{'out' if output else 'in'}:{channel}"


def _parse_nickname_key(key: str) -> tuple[bool, int] | None:
    side, _, number = key.partition(":")
    if side not in ("in", "out") or not number.isascii() or not number.isdigit():
        return None
    return side == "out", int(number)


def _clean_nicknames(raw: dict[tuple[bool, int], str]) -> dict[tuple[bool, int], str]:
    cleaned = {}
    for key, name in raw.items():
        name = name.strip()[:NICKNAME_MAX].strip()
        if name:
            cleaned[key] = name
    return cleaned


def _clean_links(groups: list[LinkGroup]) -> tuple[LinkGroup, ...]:
    """Normalise each group and drop the invalid ones; a channel belongs to the first group that claims it."""
    kept: list[LinkGroup] = []
    taken: set[tuple[bool, int]] = set()
    for group in groups:
        members = tuple(sorted(set(group.members)))
        claimed = {(group.output, channel) for channel in members}
        if (group.letter not in _LETTERS or not members or any(channel < 0 for channel in members)
                or not group.sections <= set(SECTIONS) or claimed & taken):
            continue
        taken |= claimed
        kept.append(LinkGroup(group.letter, group.output, members, group.sections))
    return tuple(kept)


def _parse_group(raw: Any) -> LinkGroup | None:
    if not isinstance(raw, dict):
        return None
    letter, output, members, sections = (raw.get(k) for k in ("letter", "output", "members", "sections"))
    if (not isinstance(letter, str) or not isinstance(output, bool) or not isinstance(members, list)
            or not all(_is_int(m) for m in members) or not isinstance(sections, list)
            or not all(isinstance(s, str) for s in sections)):
        return None
    return LinkGroup(letter, output, tuple(members), frozenset(sections))


def _parse_notes(raw: Any) -> DeviceNotes:
    if not isinstance(raw, dict):
        raise ValueError("device entry is not an object")
    nicknames: dict[tuple[bool, int], str] = {}
    raw_nicknames = raw.get("nicknames", {})
    if not isinstance(raw_nicknames, dict):
        raise ValueError("nicknames is not an object")
    for key, name in raw_nicknames.items():
        parsed = _parse_nickname_key(key)
        if parsed is not None and isinstance(name, str):
            nicknames[parsed] = name
    raw_links = raw.get("links", [])
    if not isinstance(raw_links, list):
        raise ValueError("links is not a list")
    groups = [group for group in map(_parse_group, raw_links) if group is not None]
    return DeviceNotes(_clean_nicknames(nicknames), _clean_links(groups))


class StudioStore:
    """One JSON file holding the notes of every device, keyed by `usb:<serial>` or the network address."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.warning: str | None = None

    def load(self, device_key: str) -> DeviceNotes:
        raw = self._read_devices().get(device_key)
        if raw is None:
            return DeviceNotes({}, ())
        try:
            return _parse_notes(raw)
        except ValueError:
            return DeviceNotes({}, ())

    def save(self, device_key: str, notes: DeviceNotes) -> None:
        devices = self._read_devices()
        devices[device_key] = {
            "nicknames": {_nickname_key(*key): name for key, name in sorted(_clean_nicknames(notes.nicknames).items())},
            "links": [{"letter": g.letter, "output": g.output, "members": list(g.members),
                       "sections": [s for s in SECTIONS if s in g.sections]} for g in _clean_links(list(notes.links))],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({"version": _VERSION, "devices": devices}, handle, indent=2)
                handle.write("\n")
            os.replace(temp_name, self.path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def _read_devices(self) -> dict[str, Any]:
        """Raw per-device entries; an unreadable or wrong-shaped file is renamed to `.bad` and read as empty."""
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError) as error:
            return self._set_aside(str(error))
        try:
            data = json.loads(text)
            devices = data["devices"]
            if not isinstance(data, dict) or not isinstance(devices, dict):
                raise ValueError("unexpected structure")
        except (ValueError, KeyError, TypeError) as error:
            return self._set_aside(str(error))
        return dict(devices)

    def _set_aside(self, reason: str) -> dict[str, Any]:
        bad = self.path.with_name(self.path.name + ".bad")
        try:
            os.replace(self.path, bad)
            self.warning = f"Could not read {self.path} ({reason}); it was moved to {bad} and notes were reset."
        except OSError as error:
            self.warning = f"Could not read {self.path} ({reason}) and could not move it aside ({error})."
        return {}
