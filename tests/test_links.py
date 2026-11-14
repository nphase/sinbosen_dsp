from __future__ import annotations

from dataclasses import replace

import pytest

from sinbosen_la548 import links
from sinbosen_la548.service import ControlKey, controls, demo_settings, replace_control
from sinbosen_la548.studio_store import SECTIONS, LinkGroup

ALL = frozenset(SECTIONS)


def group(letter: str = "A", output: bool = True, members: tuple[int, ...] = (3, 4),
          sections: frozenset[str] = frozenset({"crossover", "eq"})) -> LinkGroup:
    return LinkGroup(letter, output, members, sections)


def eq_key(channel: int, index: int = 0, output: bool = True) -> ControlKey:
    return ControlKey(output, channel, "eq", index)


def test_section_of_maps_blocks_to_sections() -> None:
    assert links.section_of(ControlKey(True, 0, "hp")) == "crossover"
    assert links.section_of(ControlKey(True, 0, "lp")) == "crossover"
    assert links.section_of(eq_key(0, 2)) == "eq"
    for block in ("delay", "limiter"):
        assert links.section_of(ControlKey(True, 0, block)) == block
    for block in ("gain", "mute", "polarity", "dyneq", "compressor", "route", "fir"):
        assert links.section_of(ControlKey(True, 0, block)) is None


def test_expand_copies_an_eq_edit_to_the_other_member() -> None:
    settings = demo_settings()
    band = replace(controls(settings)[eq_key(3, 5)], gain_db=3.0, enabled=True)
    result = links.expand([group()], settings, {eq_key(3, 5): band})
    assert result == {eq_key(3, 5): band, eq_key(4, 5): band}


def test_expand_ignores_untied_sections_and_other_groups() -> None:
    settings = demo_settings()
    delay = ControlKey(True, 3, "delay")
    assert links.expand([group()], settings, {delay: 2.0}) == {delay: 2.0}
    tied = links.expand([group(sections=ALL)], settings, {delay: 2.0})
    assert tied == {delay: 2.0, ControlKey(True, 4, "delay"): 2.0}
    # An input group with the same channel numbers does not tie outputs, and a non-member is not propagated.
    assert links.expand([group(output=False)], settings, {eq_key(3, 0): 1}) == {eq_key(3, 0): 1}
    assert links.expand([group(members=(0, 1))], settings, {eq_key(3, 0): 1}) == {eq_key(3, 0): 1}


def test_expand_reaches_every_member_of_a_larger_group() -> None:
    settings = demo_settings()
    hp = controls(settings)[ControlKey(True, 1, "hp")]
    result = links.expand([group(members=(1, 2, 5))], settings, {ControlKey(True, 2, "hp"): hp})
    assert set(result) == {ControlKey(True, ch, "hp") for ch in (1, 2, 5)}


def test_expand_skips_keys_the_device_does_not_have() -> None:
    settings = demo_settings()
    result = links.expand([group(members=(3, 4))], settings, {ControlKey(True, 3, "limiter"): "x"})
    assert result == {ControlKey(True, 3, "limiter"): "x"}
    ghost = group(members=(3, 40), sections=ALL)
    assert links.expand([ghost], settings, {ControlKey(True, 3, "delay"): 1.0}) == {ControlKey(True, 3, "delay"): 1.0}


def test_gain_mute_and_polarity_never_propagate() -> None:
    settings = demo_settings()
    for block, value in (("gain", -6.0), ("mute", True), ("polarity", True)):
        edit = {ControlKey(True, 3, block): value}
        assert links.expand([group(sections=ALL)], settings, edit) == edit
    assert not any(key.block in ("gain", "mute", "polarity") for key in links.join(group(sections=ALL), 6, settings))


def test_input_groups_never_produce_crossover_or_limiter_keys() -> None:
    settings = demo_settings()
    edits = {ControlKey(False, 0, "delay"): 0.5, eq_key(0, 1, False): 1}
    result = links.expand([group(output=False, members=(0, 1), sections=ALL)], settings, edits)
    assert {key.block for key in result} == {"delay", "eq"}
    assert all(not key.output for key in result)
    join = links.join(group(output=False, members=(0, 1), sections=ALL), 2, settings)
    assert {key.block for key in join} <= {"delay", "eq"}


def test_expand_propagates_only_the_edited_control_when_out_of_sync() -> None:
    settings = demo_settings()
    hp = ControlKey(True, 4, "hp")
    settings = replace_control(settings, hp, replace(controls(settings)[hp], freq_hz=500))
    assert not links.in_sync(group(), settings)
    band = replace(controls(settings)[eq_key(3, 2)], gain_db=2.0, enabled=True)
    result = links.expand([group()], settings, {eq_key(3, 2): band})
    assert set(result) == {eq_key(3, 2), eq_key(4, 2)}


def test_expand_prefers_an_explicit_edit_over_a_propagated_one() -> None:
    settings = demo_settings()
    first = replace(controls(settings)[eq_key(3, 0)], gain_db=1.0, enabled=True)
    second = replace(first, gain_db=-4.0)
    result = links.expand([group()], settings, {eq_key(3, 0): first, eq_key(4, 0): second})
    assert result == {eq_key(3, 0): first, eq_key(4, 0): second}


def test_join_copies_crossover_and_all_bands_from_the_first_member() -> None:
    settings = demo_settings()
    current = controls(settings)
    result = links.join(group(members=(3, 4)), 6, settings)
    expected = {ControlKey(True, 6, "hp"), ControlKey(True, 6, "lp"), *(eq_key(6, i) for i in range(15))}
    assert set(result) == expected
    assert result[ControlKey(True, 6, "hp")] == current[ControlKey(True, 3, "hp")]
    assert result[eq_key(6, 7)] == current[eq_key(3, 7)]


def test_join_follows_the_tied_sections_and_the_lowest_member() -> None:
    settings = demo_settings()
    settings = replace_control(settings, ControlKey(True, 4, "delay"), 3.0)  # members[0] is the source
    result = links.join(group(members=(4, 2), sections=frozenset({"delay"})), 6, settings)
    assert result == {ControlKey(True, 6, "delay"): 3.0}


def test_in_sync_notices_a_changed_tied_control() -> None:
    settings = demo_settings()
    assert links.in_sync(group(), settings)
    hp = ControlKey(True, 4, "hp")
    changed = replace_control(settings, hp, replace(controls(settings)[hp], freq_hz=500))
    assert not links.in_sync(group(), changed)
    assert links.in_sync(group(sections=frozenset({"eq"})), changed)
    delay = replace_control(settings, ControlKey(True, 4, "delay"), 3.0)
    assert links.in_sync(group(), delay)
    assert not links.in_sync(group(sections=ALL), delay)
    gain = replace_control(settings, ControlKey(True, 4, "gain"), -3.0)
    assert links.in_sync(group(sections=ALL), gain)  # gain is never tied


def test_with_member_adds_to_an_existing_group() -> None:
    groups = (group(),)
    result = links.with_member(groups, True, 6, "A")
    assert result == (group(members=(3, 4, 6)),)
    assert links.with_member(result, True, 6, "A") == result
    assert links.with_member((group(members=(4, 6)),), True, 3, "A") == (group(members=(3, 4, 6)),)


def test_with_member_starts_a_new_group_with_the_first_free_letter() -> None:
    groups = (group("A"), group("C", members=(0, 1), output=False))
    result = links.with_member(groups, True, 6, None)
    assert result[:2] == groups
    assert result[2] == LinkGroup("B", True, (6,), frozenset({"crossover", "eq"}))


def test_with_member_refuses_the_wrong_kind_a_second_group_or_an_unknown_letter() -> None:
    groups = (group("A"), group("B", members=(0, 1), output=False))
    with pytest.raises(ValueError):
        links.with_member(groups, False, 2, "A")
    with pytest.raises(ValueError):
        links.with_member(groups, True, 2, "B")
    with pytest.raises(ValueError):
        links.with_member(groups, True, 2, "Z")
    with pytest.raises(ValueError):
        links.with_member(groups, True, 3, None)
    other = (group("A"), group("B", members=(6, 7)))
    with pytest.raises(ValueError):
        links.with_member(other, True, 3, "B")


def test_with_member_raises_when_every_letter_is_taken() -> None:
    full = tuple(group(chr(ord("A") + i), members=(i * 2, i * 2 + 1)) for i in range(26))
    with pytest.raises(ValueError):
        links.with_member(full, False, 0, None)


def test_without_member_removes_it_and_drops_a_group_left_with_one() -> None:
    three = (group(members=(3, 4, 6)),)
    assert links.without_member(three, True, 4) == (group(members=(3, 6)),)
    assert links.without_member((group(),), True, 3) == ()
    assert links.without_member((group(),), False, 3) == (group(),)
    assert links.without_member((group(),), True, 9) == (group(),)


def test_leaving_then_joining_another_group_moves_the_channel() -> None:
    groups = (group("A", members=(0, 1)), group("B", members=(2, 3, 4)))
    moved = links.with_member(links.without_member(groups, True, 4), True, 4, "A")
    assert moved == (group("A", members=(0, 1, 4)), group("B", members=(2, 3)))


def test_with_sections_replaces_the_tied_sections() -> None:
    groups = (group("A"), group("B", members=(6, 7)))
    result = links.with_sections(groups, "B", frozenset({"delay"}))
    assert result == (group("A"), group("B", members=(6, 7), sections=frozenset({"delay"})))
    with pytest.raises(ValueError):
        links.with_sections(groups, "A", frozenset({"gain"}))
    with pytest.raises(ValueError):
        links.with_sections(groups, "C", ALL)
    with pytest.raises(ValueError):
        links.with_sections(groups, "A", frozenset({"mute"}))
