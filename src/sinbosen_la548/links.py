"""Link groups: an edit to a tied section on one member is made on every member."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from string import ascii_uppercase

from .service import ControlKey, controls, same_value
from .settings import DeviceSettings
from .studio_store import SECTIONS, LinkGroup

# Gain, mute and polarity have no section: a link never ties them.
_SECTION_OF_BLOCK = {"hp": "crossover", "lp": "crossover", "eq": "eq", "delay": "delay", "limiter": "limiter"}
NEW_GROUP_SECTIONS = frozenset({"crossover", "eq"})


def section_of(key: ControlKey) -> str | None:
    return _SECTION_OF_BLOCK.get(key.block)


def _group_of(groups: Sequence[LinkGroup], output: bool, channel: int) -> LinkGroup | None:
    return next((g for g in groups if g.output == output and channel in g.members), None)


def expand(groups: Sequence[LinkGroup], settings: DeviceSettings,
           edits: dict[ControlKey, object]) -> dict[ControlKey, object]:
    """The edits plus the same value on every other member of a group tying the edit's section.

    An explicit edit wins over a propagated one, and the first propagated value wins over a later one.
    """
    available = controls(settings)
    result = dict(edits)
    for key, value in edits.items():
        section = section_of(key)
        group = _group_of(groups, key.output, key.channel)
        if section is None or group is None or section not in group.sections:
            continue
        for member in group.members:
            target = replace(key, channel=member)
            if member != key.channel and target in available and target not in edits:
                result.setdefault(target, value)
    return result


def tied_group(groups: Sequence[LinkGroup], key: ControlKey) -> LinkGroup | None:
    """The group the key's channel is in when it ties the key's section, else None."""
    section = section_of(key)
    group = _group_of(groups, key.output, key.channel)
    return group if section is not None and group is not None and section in group.sections else None


def _tied_keys(group: LinkGroup, channel: int, available: dict[ControlKey, object]) -> list[ControlKey]:
    return [key for key in available
            if key.output == group.output and key.channel == channel and section_of(key) in group.sections]


def join(group: LinkGroup, channel: int, settings: DeviceSettings) -> dict[ControlKey, object]:
    """Edits that give `channel` the tied sections of the group's first member."""
    available = controls(settings)
    source = group.members[0]
    if channel == source:
        return {}
    return {replace(key, channel=channel): available[key] for key in _tied_keys(group, source, available)
            if replace(key, channel=channel) in available}


def in_sync(group: LinkGroup, settings: DeviceSettings) -> bool:
    """True when every member holds the same value as the first member in every tied control."""
    available = controls(settings)
    first, *others = group.members
    missing = object()
    return all(same_value(available[key], available.get(replace(key, channel=member), missing))
               for key in _tied_keys(group, first, available) for member in others)


def _letter_taken(groups: Sequence[LinkGroup], letter: str) -> LinkGroup | None:
    return next((g for g in groups if g.letter == letter), None)


def with_member(groups: Sequence[LinkGroup], output: bool, channel: int, letter: str | None) -> tuple[LinkGroup, ...]:
    """Add `channel` to group `letter`, or to a new group when `letter` is None; a channel leaves its group first."""
    current = _group_of(groups, output, channel)
    if letter is None:
        if current is not None:
            raise ValueError(f"channel is already in group {current.letter}")
        free = next((c for c in ascii_uppercase if _letter_taken(groups, c) is None), None)
        if free is None:
            raise ValueError("all group letters are in use")
        return (*groups, LinkGroup(free, output, (channel,), NEW_GROUP_SECTIONS))
    target = _letter_taken(groups, letter)
    if target is None:
        raise ValueError(f"no group {letter}")
    if target.output != output:
        raise ValueError("inputs link only with inputs and outputs only with outputs")
    if current is not None and current != target:
        raise ValueError(f"channel is already in group {current.letter}")
    members = tuple(sorted({*target.members, channel}))
    return tuple(replace(g, members=members) if g == target else g for g in groups)


def without_member(groups: Sequence[LinkGroup], output: bool, channel: int) -> tuple[LinkGroup, ...]:
    """Remove `channel` from its group; a group left with one member is dropped."""
    result = []
    for group in groups:
        if group.output == output and channel in group.members:
            members = tuple(m for m in group.members if m != channel)
            if len(members) < 2:
                continue
            group = replace(group, members=members)
        result.append(group)
    return tuple(result)


def with_sections(groups: Sequence[LinkGroup], letter: str, sections: frozenset[str]) -> tuple[LinkGroup, ...]:
    if _letter_taken(groups, letter) is None:
        raise ValueError(f"no group {letter}")
    if not sections <= set(SECTIONS):
        raise ValueError("unknown section")
    return tuple(replace(g, sections=sections) if g.letter == letter else g for g in groups)
