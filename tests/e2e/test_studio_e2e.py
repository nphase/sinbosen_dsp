"""Browser checks for the studio page, served by uvicorn.

The `studio` app is started without a service, so it runs the offline demo and never opens a device. The
`connected_studio` app's service hands every address to a mock client (`FakeUnit`), so its `/api/connect` never
reaches hardware either. These fixtures are separate from the Gradio ones in conftest.py.
"""

from __future__ import annotations

import io
import json
import re
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from sinbosen_la548.service import ControlKey, replace_control
from sinbosen_la548.studio_store import StudioStore

pytestmark = pytest.mark.e2e

VIEWPORT = {"width": 1440, "height": 900}
PENDING_BORDER = "rgb(242, 163, 58)"
ERROR_BORDER = "rgb(255, 107, 94)"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Studio:
    def __init__(self, page: Any, url: str, requests: list[str], errors: list[str]) -> None:
        self.page = page
        self.url = url
        self.requests = requests
        self.errors = errors

    def load(self) -> None:
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.page.locator(".strip").first.wait_for(timeout=10000)

    def select(self, label: str) -> None:
        self.page.get_by_role("button", name=label, exact=True).click()
        self.page.locator("#selected-bar").get_by_text(label, exact=True).wait_for(timeout=5000)

    def field(self, name: str) -> Any:
        return self.page.get_by_label(name, exact=True)

    def border(self, name: str) -> str:
        return str(self.field(name).evaluate("el => getComputedStyle(el).borderTopColor"))

    def unapplied(self) -> Any:
        return self.page.locator("#unapplied")

    def until(self, name: str, prop: str, expected: str) -> None:
        """Wait until the element labelled `name` has `expected` in its DOM property `prop`."""
        self.page.wait_for_function("([name, prop, expected]) => document.querySelector("
                                    "`[aria-label=\"${name}\"]`)?.[prop] === expected", arg=[name, prop, expected])

    def handles(self) -> Any:
        return self.page.locator("#plot [role=slider]")

    def handle(self, name: str) -> Any:
        return self.page.locator("#plot").get_by_role("slider", name=name, exact=True)

    def add_band(self) -> None:
        """Select Out 3 and add one EQ band (1000 Hz, 0 dB, Q 1)."""
        self.select("Out 3")
        self.page.get_by_role("button", name=re.compile("^Add band")).click()
        self.handle("EQ 1").wait_for()

    def name(self, label: str, name: str) -> None:
        """Give a strip a nickname: double-click its name, type, and leave the field."""
        self.page.locator(".strip", has=self.page.get_by_role("button", name=label, exact=True)) \
            .locator(".strip-name").dblclick()
        field = self.field(f"{label} nickname")
        field.fill(name)
        with self.page.expect_response("**/api/nickname"):
            field.press("Enter")

    def server_state(self) -> dict[str, Any]:
        return dict(self.page.evaluate("fetch('/api/state').then(response => response.json())"))

    def centre(self, locator: Any) -> tuple[float, float]:
        box = locator.bounding_box()
        assert box is not None
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


class FakeUnit:
    """A connected unit made of a mock client: mute, polarity, gain and delay writes change its settings."""

    def __init__(self) -> None:
        from sinbosen_la548.client import Client, LockState
        from sinbosen_la548.service import demo_settings

        self.settings = demo_settings()
        self.client = Mock(spec=Client)
        self.client.link_up = True
        self.client.address = "usb:FAKE1"
        self.client.read_settings.side_effect = lambda: self.settings
        self.client.read_lock_state.return_value = LockState(False, "")
        for name, block in [("set_mute", "mute"), ("set_phase", "polarity"), ("set_volume", "gain"),
                            ("set_delay", "delay")]:
            getattr(self.client, name).side_effect = self._setter(block)

    def _setter(self, block: str) -> Any:
        def write(channel: int, value: Any, *, output: bool = False, **kwargs: Any) -> None:
            self.set(ControlKey(output, channel, block), value)
        return write

    def set(self, key: Any, value: Any) -> None:
        self.settings = replace_control(self.settings, key, value)


@pytest.fixture
def unit() -> FakeUnit:
    return FakeUnit()


@pytest.fixture
def studio(tmp_path: Path) -> Iterator[Studio]:
    yield from _serve(tmp_path, None)


@pytest.fixture
def connected_studio(tmp_path: Path, unit: FakeUnit) -> Iterator[Studio]:
    """The page connected to `unit`, a fake: the service's client factory hands it out for any address."""
    from sinbosen_la548.service import DSPService, demo_settings

    for run in _serve(tmp_path, DSPService(demo_settings(), client_factory=lambda address: unit.client)):
        run.page.evaluate("fetch('/api/connect', {method: 'POST', headers: {'Content-Type': 'application/json'},"
                          "body: JSON.stringify({address: 'usb'})})")
        run.page.locator("#connection").get_by_text("USB · connected").wait_for()
        yield run


def _serve(tmp_path: Path, service: Any) -> Iterator[Studio]:
    pytest.importorskip("playwright.sync_api", reason="install the e2e dependency group")
    import uvicorn
    from playwright.sync_api import sync_playwright

    from sinbosen_la548.studio_app import create_studio_app

    app = create_studio_app(service, store=StudioStore(tmp_path / "studio.json"))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    requests: list[str] = []
    errors: list[str] = []
    playwright = sync_playwright().start()
    browser = None
    try:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport=VIEWPORT)
        page = context.new_page()
        page.on("request", lambda request: requests.append(request.url))
        page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
        page.on("console", lambda message: errors.append(f"console: {message.text}")
                if message.type == "error" else None)
        page.set_default_timeout(5000)
        run = Studio(page, f"http://127.0.0.1:{port}/", requests, errors)
        run.load()
        yield run
        assert not errors, "Unexpected browser errors:\n" + "\n".join(errors)
    finally:
        if browser is not None:
            browser.close()
        playwright.stop()
        server.should_exit = True
        thread.join(timeout=10)


def test_strips_show_twelve_channels(studio: Studio) -> None:
    strips = studio.page.locator(".strip")
    assert strips.count() == 12
    labels = [studio.page.locator(".strip .strip-label").nth(i).inner_text() for i in range(12)]
    assert labels == ["In A", "In B", "In C", "In D", *(f"Out {n}" for n in range(1, 9))]


def test_label_selects_channel_and_shows_its_cards(studio: Studio) -> None:
    studio.select("Out 3")
    strip = studio.page.locator(".strip", has=studio.page.get_by_role("button", name="Out 3", exact=True))
    assert "selected" in (strip.get_attribute("class") or "")
    cards = studio.page.locator("#cards .card-title")
    assert cards.all_inner_texts() == ["High-pass", "Low-pass", "Limiter", "Compressor"]
    assert studio.field("High-pass frequency").input_value() == "20"
    studio.select("In A")
    assert studio.page.locator("#cards .card-title").count() == 0


def test_high_pass_edit_is_unapplied_until_revert(studio: Studio) -> None:
    studio.select("Out 3")
    field = studio.field("High-pass frequency")
    field.fill("140")
    field.press("Enter")
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for()
    assert studio.border("High-pass frequency") == PENDING_BORDER
    assert field.input_value() == "140"
    studio.page.get_by_role("button", name="Revert", exact=True).click()
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for(state="detached")
    assert field.input_value() == "20"
    assert studio.border("High-pass frequency") != PENDING_BORDER


def test_mute_toggles_without_unapplied_count(studio: Studio) -> None:
    mute = studio.field("Out 3 mute")
    assert mute.get_attribute("aria-pressed") == "false"
    mute.click()
    studio.until("Out 3 mute", "ariaPressed", "true")
    assert studio.unapplied().inner_text() == ""
    mute.click()
    studio.until("Out 3 mute", "ariaPressed", "false")
    assert studio.unapplied().inner_text() == ""


def test_gain_accepts_a_unit_and_is_immediate(studio: Studio) -> None:
    gain = studio.field("Out 3 gain in dB")
    assert gain.input_value() == "0.0"
    gain.fill("-3 dB")
    gain.press("Enter")
    studio.until("Out 3 gain in dB", "value", "-3.0")
    assert studio.unapplied().inner_text() == ""


def test_refused_entry_keeps_focus_and_shows_the_reason(studio: Studio) -> None:
    studio.select("Out 3")
    field = studio.field("High-pass frequency")
    field.fill("5")
    field.press("Enter")
    message = studio.page.locator(".field-error")
    message.get_by_text(re.compile("out of range")).wait_for()
    assert message.inner_text().endswith("Esc to cancel")
    assert studio.border("High-pass frequency") == ERROR_BORDER
    assert field.evaluate("el => el === document.activeElement")
    assert studio.unapplied().inner_text() == ""
    field.press("Escape")
    assert field.input_value() == "20"
    assert message.count() == 0
    # Chromium logs the refused request itself; that one is expected.
    studio.errors[:] = [e for e in studio.errors if "status of 400" not in e]


def test_delay_is_a_draft(studio: Studio) -> None:
    delay = studio.field("Out 5 delay in ms")
    delay.fill("2.35ms")
    delay.press("Enter")
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for()
    assert delay.input_value() == "2.35"
    assert studio.border("Out 5 delay in ms") == PENDING_BORDER


def test_nickname_survives_reload(studio: Studio) -> None:
    studio.name("Out 3", "Mid L")
    studio.load()
    head = studio.page.get_by_role("button", name="Out 3, Mid L", exact=True)
    assert head.locator(".strip-name").inner_text() == "Mid L"
    head.click()
    studio.page.locator("#selected-bar").get_by_text("Mid L", exact=True).wait_for()


def test_link_menu_creates_and_joins_groups(studio: Studio) -> None:
    studio.field("Out 3 link").click()
    studio.page.get_by_role("menuitem", name="New link").click()
    studio.until("Out 3 link", "textContent", "Link A")
    studio.field("Out 4 link").click()
    studio.page.get_by_role("menuitem", name="Link A").click()
    studio.until("Out 4 link", "textContent", "Link A")
    studio.select("Out 3")
    bar = studio.page.locator("#selected-bar")
    assert bar.get_by_text("with Out 4", exact=False).is_visible()
    assert bar.get_by_label("Crossover", exact=True).is_checked()
    assert bar.get_by_label("Gain", exact=True).count() == 0  # gain is never tied
    assert not bar.get_by_label("Delay", exact=True).is_checked()
    with studio.page.expect_response("**/api/link/sections"):
        bar.get_by_label("Delay", exact=True).check()
    state = studio.page.evaluate("fetch('/api/state').then(response => response.json())")
    assert state["selected"]["link"]["sections"]["delay"] is True
    assert bar.get_by_label("Delay", exact=True).is_checked()
    studio.field("Out 4 link").click()
    studio.page.get_by_role("menuitem", name="Unlink").click()
    studio.until("Out 4 link", "textContent", "Link")


def test_add_band_adds_a_card_and_a_handle(studio: Studio) -> None:
    studio.select("Out 3")
    add = studio.page.get_by_role("button", name=re.compile("^Add band"))
    assert "0 of 15 used" in add.inner_text()
    assert studio.handles().count() == 0
    add.click()
    studio.page.locator("#cards .card-title").get_by_text("EQ 1", exact=True).wait_for()
    assert "1 of 15 used" in add.inner_text()
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for()
    studio.handle("EQ 1").wait_for()
    assert studio.handles().count() == 1


def test_card_selects_show_their_text_unclipped(studio: Studio) -> None:
    studio.add_band()
    for name in ("EQ 1 type", "High-pass family", "High-pass slope"):
        select = studio.field(name)
        # Every option's text, as drawn in the select's font, fits inside the padding and the chevron.
        room, text = select.evaluate("""async el => {
            await document.fonts.ready;
            const style = getComputedStyle(el);
            const context = document.createElement("canvas").getContext("2d");
            context.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
            const room = el.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
            return [room, Math.max(...[...el.options].map(o => context.measureText(o.text).width))];
        }""")
        assert text <= room, (name, text, room)
    option = studio.field("EQ 1 type").locator("option[value='0']")
    assert option.text_content() == "Parametric"
    assert option.get_attribute("title") == "Parametric EQ"


def test_plot_has_a_handle_per_enabled_crossover_and_band_in_use(studio: Studio) -> None:
    studio.select("Out 3")
    assert studio.page.locator("#plot svg").count() == 1
    assert studio.handles().count() == 0
    with studio.page.expect_response("**/api/edit"):
        studio.page.get_by_role("group", name="High-pass").get_by_role("checkbox").check()
    studio.handle("High-pass").wait_for()
    assert studio.handle("High-pass").text_content() == "HP"
    studio.page.get_by_role("button", name=re.compile("^Add band")).click()
    studio.handle("EQ 1").wait_for()
    assert studio.handle("EQ 1").text_content() == "1"
    assert studio.handles().count() == 2
    studio.select("In A")
    assert studio.handles().count() == 0


def test_dragging_an_eq_handle_left_lowers_its_frequency(studio: Studio) -> None:
    studio.add_band()
    studio.page.get_by_role("button", name="Apply", exact=True).click()
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for(state="detached")
    hz = studio.field("EQ 1 frequency")
    assert hz.input_value() == "1000"
    assert studio.border("EQ 1 frequency") != PENDING_BORDER
    x, y = studio.centre(studio.handle("EQ 1"))
    mouse = studio.page.mouse
    mouse.move(x, y)
    mouse.down()
    mouse.move(x - 150, y, steps=10)
    mouse.up()
    studio.page.wait_for_function("() => Number(document.querySelector('[aria-label=\"EQ 1 frequency\"]').value) < 600")
    studio.page.wait_for_function("() => getComputedStyle(document.querySelector("
                                  "'[aria-label=\"EQ 1 frequency\"]')).borderTopColor === 'rgb(242, 163, 58)'")
    studio.page.wait_for_timeout(300)
    band = studio.server_state()["selected"]["filters"][2]
    assert str(band["fields"][1]["value"]) == hz.input_value()
    assert band["fields"][2]["value"] == 0  # moved sideways only: the gain is unchanged
    handle_freq = studio.server_state()["plot"]["handles"][0]["freq_hz"]
    assert handle_freq == int(hz.input_value())


def test_dragging_up_raises_the_gain_and_crossovers_move_only_sideways(studio: Studio) -> None:
    studio.add_band()
    x, y = studio.centre(studio.handle("EQ 1"))
    mouse = studio.page.mouse
    mouse.move(x, y)
    mouse.down()
    mouse.move(x, y - 52, steps=5)
    mouse.up()
    studio.page.wait_for_function("() => Number(document.querySelector('[aria-label=\"EQ 1 gain\"]').value) >= 9")
    gain = float(studio.field("EQ 1 gain").input_value())
    assert 9 <= gain <= 11
    assert studio.field("EQ 1 frequency").input_value() == "1000"
    with studio.page.expect_response("**/api/edit"):
        studio.page.get_by_role("group", name="High-pass").get_by_role("checkbox").check()
    x, y = studio.centre(studio.handle("High-pass"))
    mouse.move(x, y)
    mouse.down()
    mouse.move(x + 100, y + 60, steps=5)
    mouse.up()
    studio.page.wait_for_function("() => Number(document.querySelector("
                                  "'[aria-label=\"High-pass frequency\"]').value) > 30")
    studio.page.wait_for_timeout(300)
    hp = studio.server_state()["selected"]["filters"][0]
    assert hp["fields"][0]["value"] > 30


def test_escape_during_a_drag_restores_the_value(studio: Studio) -> None:
    studio.add_band()
    x, y = studio.centre(studio.handle("EQ 1"))
    mouse = studio.page.mouse
    mouse.move(x, y)
    mouse.down()
    mouse.move(x - 150, y - 30, steps=10)
    studio.page.wait_for_function("() => Number(document.querySelector('[aria-label=\"EQ 1 frequency\"]').value) < 600")
    edits = []
    studio.page.on("request", lambda request: edits.append(request.post_data) if "/api/edit" in request.url else None)
    studio.page.wait_for_timeout(200)
    edits.clear()
    studio.page.keyboard.press("Escape")
    studio.page.wait_for_function("() => document.querySelector('[aria-label=\"EQ 1 frequency\"]').value === '1000'")
    mouse.up()
    studio.page.wait_for_timeout(300)
    assert len(edits) == 1, edits
    band = studio.server_state()["selected"]["filters"][2]
    assert [band["fields"][1]["value"], band["fields"][2]["value"]] == [1000, 0]
    assert studio.field("EQ 1 gain").input_value() == "0.0"


def test_a_poll_does_not_move_a_handle_mid_drag(studio: Studio) -> None:
    studio.add_band()
    handle = studio.handle("EQ 1")
    x, y = studio.centre(handle)
    mouse = studio.page.mouse
    mouse.move(x, y)
    mouse.down()
    mouse.move(x - 120, y, steps=6)
    studio.page.wait_for_timeout(200)
    before = studio.centre(handle)
    # Something else moves the band while the pointer is held; the polls that follow must not move the handle.
    studio.page.evaluate("fetch('/api/edit', {method: 'POST', headers: {'Content-Type': 'application/json'},"
                         "body: JSON.stringify({id: 'out:2', kind: 'eq', index: 0, field: 'freq_hz', value: 5000})})")
    studio.page.wait_for_timeout(2200)
    assert studio.centre(handle) == pytest.approx(before, abs=1)
    mouse.up()


def test_arrow_keys_move_a_focused_handle(studio: Studio) -> None:
    studio.add_band()
    handle = studio.handle("EQ 1")
    handle.focus()
    handle.press("ArrowLeft")
    studio.until("EQ 1 frequency", "value", "972")  # 1000 Hz down 1/24 octave
    handle.press("ArrowRight")
    studio.until("EQ 1 frequency", "value", "1000")  # 972 Hz up 1/24 octave, rounded as drag_frequency does
    handle.press("ArrowDown")
    studio.until("EQ 1 gain", "value", "-0.1")
    handle.press("Shift+ArrowUp")
    studio.until("EQ 1 gain", "value", "+0.9")
    assert handle.evaluate("el => el === document.activeElement")
    studio.page.wait_for_timeout(300)
    band = studio.server_state()["selected"]["filters"][2]
    assert [band["fields"][1]["value"], band["fields"][2]["value"]] == [1000, pytest.approx(0.9)]


def test_wheel_over_a_handle_changes_q_and_does_not_scroll(studio: Studio) -> None:
    studio.add_band()
    studio.page.evaluate("window.wheels = []; window.addEventListener('wheel', e => "
                         "window.wheels.push(e.defaultPrevented), {passive: false})")
    x, y = studio.centre(studio.handle("EQ 1"))
    studio.page.mouse.move(x, y)
    studio.page.mouse.wheel(0, -100)
    studio.until("EQ 1 Q", "value", "1.10")  # 10% per 100 px of scroll
    studio.page.mouse.wheel(0, 100)
    studio.until("EQ 1 Q", "value", "1.00")
    studio.page.mouse.wheel(0, -10)  # a two-finger nudge: in proportion to the distance
    studio.until("EQ 1 Q", "value", "1.01")
    studio.page.mouse.wheel(0, -5000)  # a hard flick moves Q by at most two 100 px steps
    studio.page.wait_for_function("() => document.querySelector('[aria-label=\"EQ 1 Q\"]').value !== '1.01'")
    studio.page.wait_for_timeout(300)
    assert 1.15 < float(studio.field("EQ 1 Q").input_value()) < 1.3
    studio.page.mouse.move(x - 300, y + 60)
    studio.page.mouse.wheel(0, 100)
    studio.page.wait_for_function("() => window.wheels.length === 5")
    assert studio.page.evaluate("window.wheels") == [True, True, True, True, False]


def test_clicking_a_handle_selects_its_card(studio: Studio) -> None:
    studio.add_band()
    edits: list[str] = []
    studio.page.on("request", lambda request: edits.append(request.url) if "/api/edit" in request.url else None)
    studio.handle("EQ 1").click()
    card = studio.page.get_by_role("group", name="EQ 1")
    studio.page.wait_for_function("() => document.querySelector('[data-key=\"card:eq:0\"]')"
                                  ".classList.contains('selected')")
    assert card.evaluate("el => el.contains(document.activeElement)")
    assert edits == []


def test_phase_and_group_delay_panels_follow_their_ticks(studio: Studio) -> None:
    studio.select("Out 3")
    bar = studio.page.locator("#selected-bar")
    panels = studio.page.locator("#plot [data-panel]")
    assert panels.evaluate_all("els => els.map(e => e.dataset.panel)") == ["magnitude"]
    with studio.page.expect_response("**/api/plot"):
        bar.get_by_label("Phase", exact=True).check()
    studio.page.locator("#plot [data-panel=phase]").wait_for()
    with studio.page.expect_response("**/api/plot"):
        bar.get_by_label("Group delay", exact=True).check()
    studio.page.locator("#plot [data-panel=group-delay]").wait_for()
    assert panels.evaluate_all("els => els.map(e => e.dataset.panel)") == ["magnitude", "phase", "group-delay"]
    with studio.page.expect_response("**/api/plot"):
        bar.get_by_label("Phase", exact=True).uncheck()
    studio.page.locator("#plot [data-panel=phase]").wait_for(state="detached")
    assert panels.evaluate_all("els => els.map(e => e.dataset.panel)") == ["magnitude", "group-delay"]


def _db_labels(studio: Studio) -> list[float]:
    texts = studio.page.locator("#plot [data-axis=db] text").all_text_contents()
    return [float(t.replace("−", "-")) for t in texts]


def _set_channel_gain(studio: Studio, label: str, db: float) -> None:
    gain = studio.field(f"{label} gain in dB")
    gain.fill(str(db))
    with studio.page.expect_response("**/api/now"):
        gain.press("Enter")


def _steep_high_pass(studio: Studio) -> None:
    """Out 3 at -10 dB with a 140 Hz Linkwitz-Riley 24 dB/oct high-pass: its skirt falls far below -40 dB."""
    studio.select("Out 3")
    _set_channel_gain(studio, "Out 3", -10)
    with studio.page.expect_response("**/api/edit"):
        studio.page.get_by_role("group", name="High-pass").get_by_role("checkbox").check()
    hz = studio.field("High-pass frequency")
    hz.fill("140")
    with studio.page.expect_response("**/api/edit"):
        hz.press("Enter")
    with studio.page.expect_response("**/api/edit"):
        studio.field("High-pass family").select_option("1")
    with studio.page.expect_response("**/api/edit"):
        studio.field("High-pass slope").select_option("24")
    hp = studio.server_state()["selected"]["filters"][0]
    assert [f["value"] for f in hp["fields"]] == [140, 1, 24]
    assert min(v for v in studio.server_state()["plot"]["draft_db"] if v is not None) < -60


def test_a_steep_high_pass_does_not_stretch_the_range(studio: Studio) -> None:
    _steep_high_pass(studio)
    studio.page.wait_for_timeout(200)
    labels = _db_labels(studio)
    assert (labels[0], labels[-1]) == (10, -40)


def test_a_handle_on_a_quiet_channel_stays_10_db_above_the_bottom(studio: Studio) -> None:
    studio.add_band()
    _set_channel_gain(studio, "Out 3", -50)
    handle_db = studio.server_state()["plot"]["handles"][0]["db"]
    assert handle_db == pytest.approx(-50, abs=0.5)
    studio.page.wait_for_function("() => [...document.querySelectorAll('#plot [data-axis=db] text')].at(-1)"
                                  ".textContent !== '−40'")
    labels = _db_labels(studio)
    assert labels[-1] <= handle_db - 10
    assert labels[-1] == -60
    assert labels[0] == 10


def test_a_peak_raises_the_top(studio: Studio) -> None:
    studio.add_band()
    assert _db_labels(studio)[0] == 10
    gain = studio.field("EQ 1 gain")
    gain.fill("18")
    gain.press("Enter")
    studio.page.wait_for_function("() => document.querySelector('#plot [data-axis=db] text').textContent === '+20'")
    assert _db_labels(studio)[-1] == -40


def test_curves_leaving_the_range_are_clipped_at_the_plot_border(studio: Studio) -> None:
    image_module = pytest.importorskip("PIL.Image")
    _steep_high_pass(studio)
    studio.page.mouse.move(0, 0)
    studio.page.wait_for_timeout(200)
    plot = studio.page.locator("#plot")
    origin = plot.bounding_box()
    frame = studio.page.locator("#plot [data-panel=magnitude] .frame").bounding_box()
    assert origin is not None and frame is not None
    scale = studio.page.evaluate("devicePixelRatio")
    image = image_module.open(io.BytesIO(plot.screenshot())).convert("RGB")
    left, top = (frame["x"] - origin["x"] - 2) * scale, (frame["y"] - origin["y"] - 2) * scale
    right = (frame["x"] + frame["width"] - origin["x"] + 2) * scale
    bottom = (frame["y"] + frame["height"] - origin["y"] + 2) * scale
    outside = []
    for x in range(image.width):
        for y in range(image.height):
            if left <= x <= right and top <= y <= bottom:
                continue
            r, g, b = image.getpixel((x, y))
            if b > 180 and r < 130 and 110 < g < 200:  # the accent blue of the draft curve
                outside.append((x, y))
    assert not outside, outside[:10]


def test_typing_survives_polls(studio: Studio) -> None:
    studio.select("Out 3")
    field = studio.field("High-pass frequency")
    field.click()
    field.fill("")
    field.type("15")
    with studio.page.expect_response("**/api/state"):
        pass
    with studio.page.expect_response("**/api/state"):
        pass
    assert field.input_value() == "15"
    assert field.evaluate("el => el === document.activeElement")
    field.type("0")
    assert field.input_value() == "150"


def test_hint_says_filters_wait_for_apply_while_live_is_off(studio: Studio) -> None:
    hint = studio.page.locator("#hint")
    assert "Filters, delay, limiter and compressor wait for Apply." in hint.inner_text()
    assert "Gain is sent when you press Enter; polarity and mute take effect immediately." in hint.inner_text()


def test_repeated_errors_are_listed_once(studio: Studio) -> None:
    studio.select("Out 3")
    studio.page.route("**/api/band/add", lambda route: route.fulfill(
        status=400, content_type="application/json", body='{"error": "no free EQ band"}'))
    add = studio.page.get_by_role("button", name=re.compile("^Add band"))
    for _ in range(2):
        with studio.page.expect_response("**/api/band/add"):
            add.click()
    studio.page.locator("#errors li").first.wait_for()
    assert studio.page.locator("#errors li").all_inner_texts() == ["no free EQ band"]
    studio.errors[:] = [e for e in studio.errors if "status of 400" not in e]


def test_a_result_without_error_lists_is_reported(studio: Studio) -> None:
    state = studio.server_state()

    def answer(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({**state, "result": {"ok": False, "verified": False}}))

    studio.page.route("**/api/now", answer)
    with studio.page.expect_response("**/api/now"):
        studio.field("Out 3 mute").click()
    studio.page.locator("#errors").get_by_text("The device did not confirm the write.").wait_for()


def test_no_request_leaves_loopback_and_inter_is_served(studio: Studio) -> None:
    studio.select("Out 3")
    studio.field("Out 3 mute").click()
    studio.page.wait_for_timeout(1500)
    assert studio.page.evaluate("document.fonts.ready.then(() => document.fonts.check('13px Inter'))")
    assert any(url.endswith("/static/fonts/InterVariable.woff2") for url in studio.requests)
    hosts = {urlsplit(url).hostname for url in studio.requests}
    assert hosts == {"127.0.0.1"}, studio.requests


# Errors, refresh and leaving the unit

def test_a_failed_immediate_write_reaches_the_error_bar(connected_studio: Studio, unit: FakeUnit) -> None:
    unit.client.set_mute.side_effect = OSError("usb write failed")
    with connected_studio.page.expect_response("**/api/now"):
        connected_studio.field("Out 2 mute").click()
    connected_studio.page.locator("#errors").get_by_text("Output 2: mute: usb write failed").wait_for()
    assert connected_studio.field("Out 2 mute").get_attribute("aria-pressed") == "false"


def test_a_dismissed_error_shows_again_when_it_recurs(studio: Studio) -> None:
    errors = studio.page.locator("#errors li")
    message = "The studio server is not reachable."
    for _ in range(2):
        studio.page.route("**/api/state", lambda route: route.abort())
        errors.get_by_text(message).wait_for()
        studio.page.get_by_role("button", name="Dismiss errors").click()
        errors.get_by_text(message).wait_for(state="detached")
        studio.page.unroute("**/api/state")
        with studio.page.expect_response("**/api/state"):
            pass
        studio.page.wait_for_timeout(100)
    studio.errors[:] = [e for e in studio.errors if "net::ERR_FAILED" not in e]


def test_refresh_reads_the_unit_again(connected_studio: Studio, unit: FakeUnit) -> None:
    unit.set(ControlKey(True, 0, "mute"), True)  # changed on the unit, not through the page
    with connected_studio.page.expect_response("**/api/edit"):
        connected_studio.field("Out 5 delay in ms").fill("1.5")
        connected_studio.field("Out 5 delay in ms").press("Enter")
    connected_studio.page.locator("#connection").click()
    with connected_studio.page.expect_response("**/api/refresh"):
        connected_studio.page.get_by_role("button", name="Refresh", exact=True).click()
    assert connected_studio.field("Out 1 mute").get_attribute("aria-pressed") == "true"
    assert connected_studio.unapplied().inner_text() == "1 unapplied"


def test_disconnecting_with_unapplied_edits_asks_first(connected_studio: Studio) -> None:
    page = connected_studio.page
    leaving: list[str] = []
    page.on("request", lambda request: leaving.append(request.url)
            if request.url.endswith(("/api/disconnect", "/api/demo")) else None)
    with page.expect_response("**/api/edit"):
        connected_studio.field("Out 5 delay in ms").fill("1.5")
        connected_studio.field("Out 5 delay in ms").press("Enter")
    page.locator("#connection").click()
    popover = page.get_by_role("dialog", name="Connection")
    for button, confirm in (("Disconnect", "Discard and disconnect"), ("Offline demo", "Discard and use demo")):
        popover.get_by_role("button", name=button, exact=True).click()
        popover.get_by_text("Discard 1 unapplied edit?", exact=False).wait_for()
        assert popover.get_by_role("button", name=confirm).is_visible()
        popover.get_by_role("button", name="Keep edits").click()
        popover.get_by_text("Discard 1 unapplied edit?", exact=False).wait_for(state="detached")
    page.wait_for_timeout(200)
    assert leaving == []
    assert connected_studio.unapplied().inner_text() == "1 unapplied"
    popover.get_by_role("button", name="Disconnect", exact=True).click()
    with page.expect_response("**/api/disconnect"):
        popover.get_by_role("button", name="Discard and disconnect").click()
    page.locator("#connection").get_by_text("Offline demo").wait_for()
    assert connected_studio.unapplied().inner_text() == ""


def test_disconnecting_without_unapplied_edits_does_not_ask(connected_studio: Studio) -> None:
    connected_studio.page.locator("#connection").click()
    with connected_studio.page.expect_response("**/api/disconnect"):
        connected_studio.page.get_by_role("button", name="Disconnect", exact=True).click()


# The owner's requests after the first demo

def test_the_page_opens_with_out_1_selected(studio: Studio) -> None:
    strip = studio.page.locator(".strip", has=studio.page.get_by_role("button", name="Out 1", exact=True))
    assert "selected" in (strip.get_attribute("class") or "")
    assert studio.page.locator("#selected-bar .selected-name").inner_text() == "Out 1"


def test_a_disabled_band_keeps_a_greyed_handle_that_can_be_dragged(studio: Studio) -> None:
    studio.add_band()
    gain = studio.field("EQ 1 gain")
    gain.fill("6")
    with studio.page.expect_response("**/api/edit"):
        gain.press("Enter")
    with studio.page.expect_response("**/api/edit"):
        studio.page.get_by_role("group", name="EQ 1").get_by_role("checkbox").uncheck()
    handle = studio.handle("EQ 1")
    studio.page.wait_for_function("() => document.querySelector('#plot [data-key=\"eq:0\"]')"
                                  ".classList.contains('disabled')")
    assert handle.text_content() == "1"
    assert "off" in (studio.page.locator("[data-key='card:eq:0']").get_attribute("class") or "")
    plot = studio.server_state()["plot"]
    assert plot["handles"][0]["enabled"] is False
    assert plot["handles"][0]["db"] == pytest.approx(6.0, abs=0.2)
    assert max(v for v in plot["draft_db"] if v is not None) < 0.5  # the disabled band is not in the curve
    x, y = studio.centre(handle)
    mouse = studio.page.mouse
    mouse.move(x, y)
    mouse.down()
    mouse.move(x - 150, y, steps=10)
    mouse.up()
    studio.page.wait_for_function("() => Number(document.querySelector('[aria-label=\"EQ 1 frequency\"]').value) < 600")
    studio.page.wait_for_timeout(300)
    band = studio.server_state()["selected"]["filters"][2]
    assert band["enabled"] is False and band["fields"][1]["value"] < 600


def test_a_long_nickname_covers_the_label_up_to_the_strip_width(studio: Studio) -> None:
    studio.name("Out 3", "Midrange Left Upper Horn")
    head = studio.page.get_by_role("button", name="Out 3, Midrange Left Upper Horn", exact=True)
    head.wait_for()
    assert head.get_attribute("title") == "Out 3"
    box = head.evaluate("""el => {
        const r = (e) => e.getBoundingClientRect();
        const name = el.querySelector('.strip-name'), label = el.querySelector('.strip-label');
        return {head: [r(el).left, r(el).right], name: [r(name).left, r(name).right],
                label: r(label).right, clipped: name.scrollWidth > name.clientWidth,
                ellipsis: getComputedStyle(name).textOverflow};
    }""")
    assert box["name"][0] == pytest.approx(box["head"][0], abs=1)
    assert box["name"][1] == pytest.approx(box["head"][1], abs=1)
    assert box["name"][0] < box["label"]  # over the fixed label
    assert box["clipped"] and box["ellipsis"] == "ellipsis"
    studio.name("Out 4", "Sub")
    short = studio.page.get_by_role("button", name="Out 4, Sub", exact=True).evaluate(
        "el => el.querySelector('.strip-name').getBoundingClientRect().left"
        " >= el.querySelector('.strip-label').getBoundingClientRect().right")
    assert short


def test_the_strip_name_selects_on_click_and_edits_on_double_click(studio: Studio) -> None:
    page = studio.page
    head = page.get_by_role("button", name="Out 3", exact=True)
    name = head.locator(".strip-name")
    assert name.evaluate("el => getComputedStyle(el).opacity") == "0"  # the "Name" affordance is hidden
    head.hover()
    page.wait_for_function("() => [...document.querySelectorAll('.strip-name')].some("
                           "el => getComputedStyle(el).opacity !== '0')")
    name.click()
    page.locator("#selected-bar .selected-name").get_by_text("Out 3", exact=True).wait_for()
    assert studio.field("Out 3 nickname").count() == 0
    name.dblclick()
    field = studio.field("Out 3 nickname")
    assert field.evaluate("el => el === document.activeElement")
    field.fill("Scratch")
    field.press("Escape")
    field.wait_for(state="detached")
    assert page.get_by_role("button", name="Out 3", exact=True).locator(".strip-name").inner_text() == "Name"
    head.focus()
    head.press("F2")
    studio.field("Out 3 nickname").fill("Mid L")
    with page.expect_response("**/api/nickname"):
        studio.field("Out 3 nickname").press("Enter")
    page.get_by_role("button", name="Out 3, Mid L", exact=True).wait_for()
    studio.field("Out 3 nickname").wait_for(state="detached")
    page.get_by_role("button", name="Out 3, Mid L", exact=True).press("Enter")
    studio.field("Out 3 nickname").fill("Mid R")
    with page.expect_response("**/api/nickname"):
        page.locator("#plot").click(position={"x": 5, "y": 5})  # leaving the field commits it
    page.get_by_role("button", name="Out 3, Mid R", exact=True).wait_for()


def test_dragging_a_crossover_handle_down_steepens_its_slope(studio: Studio) -> None:
    studio.select("Out 3")
    with studio.page.expect_response("**/api/edit"):
        studio.page.get_by_role("group", name="High-pass").get_by_role("checkbox").check()
    hp = studio.server_state()["selected"]["filters"][0]
    hz0, slope0 = hp["fields"][0]["value"], hp["fields"][2]["value"]
    assert slope0 <= 36
    posts: list[str] = []
    studio.page.on("request", lambda request: posts.append(request.post_data or "")
                   if "/api/edit" in request.url else None)
    x, y = studio.centre(studio.handle("High-pass"))
    mouse = studio.page.mouse
    mouse.move(x, y)
    mouse.down()
    mouse.move(x + 100, y + 40, steps=8)  # 40 px down: two 6 dB/oct steps steeper
    studio.until("High-pass slope", "value", str(slope0 + 12))
    mouse.up()
    studio.page.wait_for_timeout(400)
    hp = studio.server_state()["selected"]["filters"][0]
    assert hp["fields"][2]["value"] == slope0 + 12 and hp["fields"][0]["value"] > hz0
    both = [json.loads(p)["values"] for p in posts]
    assert any("freq_hz" in v and "slope_db_per_oct" in v for v in both), both
    handle = studio.handle("High-pass")
    handle.focus()
    handle.press("ArrowUp")
    studio.until("High-pass slope", "value", str(slope0 + 6))
    for _ in range(10):
        handle.press("ArrowDown")
    studio.until("High-pass slope", "value", "48")  # clamped at the steepest
    studio.page.wait_for_timeout(300)
    assert studio.server_state()["selected"]["filters"][0]["fields"][2]["value"] == 48


def test_the_top_bar_shows_the_selected_paths_latency(studio: Studio) -> None:
    latency = studio.page.locator("#topbar .latency")
    assert latency.text_content() == "Out 1 latency 1.73 ms"  # Out 1, fed by In A
    assert studio.page.locator("#selected-bar .latency").count() == 0
    studio.select("Out 3")
    latency.get_by_text("latency 1.73 ms, no input routed").wait_for()
    delay = studio.field("Out 3 delay in ms")
    delay.fill("2.5")
    with studio.page.expect_response("**/api/edit"):
        delay.press("Enter")
    latency.get_by_text("latency 4.23 ms, no input routed").wait_for()
    assert latency.locator(".latency-name").inner_text() == "Out 3"
    detail = latency.get_attribute("title") or ""
    assert "Out 3 delay: 2.500 ms" in detail and "Group delay" in detail and "0.667 ms" in detail
    assert latency.get_attribute("aria-description") == detail
    studio.select("In A")
    latency.get_by_text("adds 0.00 ms").wait_for()


def test_a_long_name_in_the_latency_readout_ellipsises_before_the_bar_wraps(studio: Studio) -> None:
    studio.select("Out 3")
    studio.name("Out 3", "Midrange Left Upper Horn")
    name = studio.page.locator("#topbar .latency-name")
    name.get_by_text("Midrange Left Upper Horn").wait_for()

    def one_row() -> bool:
        centres = studio.page.locator("#topbar > *").evaluate_all(
            "els => els.map(e => e.getBoundingClientRect()).map(r => r.top + r.height / 2)")
        return bool(max(centres) - min(centres) <= 4)

    assert one_row()  # at 1440 px
    # A bar too narrow for the whole name: the name gives way, the bar stays on one line.
    studio.page.evaluate("document.querySelector('#topbar').style.width = '820px'")
    studio.page.wait_for_timeout(100)
    assert one_row()
    assert name.evaluate("el => el.scrollWidth > el.clientWidth && getComputedStyle(el).textOverflow === 'ellipsis'")


def test_gain_is_sent_only_on_enter(studio: Studio) -> None:
    page = studio.page
    sent: list[str] = []
    page.on("request", lambda request: sent.append(request.post_data or "") if "/api/now" in request.url else None)
    gain = studio.field("Out 3 gain in dB")
    gain.fill("-1")  # half-way to -12
    assert "unsent" in (gain.get_attribute("class") or "")
    assert gain.get_attribute("title") == "Press Enter to send"
    page.locator("#plot").click(position={"x": 5, "y": 5})  # leaving the field discards it
    studio.until("Out 3 gain in dB", "value", "0.0")
    gain.fill("-1")
    gain.press("Tab")
    studio.until("Out 3 gain in dB", "value", "0.0")
    gain.fill("-12")
    gain.press("Escape")
    studio.until("Out 3 gain in dB", "value", "0.0")
    page.wait_for_timeout(300)
    assert sent == []
    gain.fill("-12")
    with page.expect_response("**/api/now"):
        gain.press("Enter")
    studio.until("Out 3 gain in dB", "value", "-12.0")
    assert [json.loads(body) for body in sent] == [{"id": "out:2", "block": "gain", "value": -12}]
    assert "unsent" not in (gain.get_attribute("class") or "")


def test_delay_still_commits_when_leaving_the_field(studio: Studio) -> None:
    delay = studio.field("Out 5 delay in ms")
    delay.fill("1.5")
    with studio.page.expect_response("**/api/edit"):
        delay.press("Tab")
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for()


def test_a_sideways_drag_keeps_an_unresolved_crossover_slope(connected_studio: Studio, unit: FakeUnit) -> None:
    # 54 dB/oct decodes from the unit's raw slope 8, which no option offers. The engine refuses to write such a block,
    # so the drag is refused; it must not be "fixed" by posting the nearest option, 48.
    from dataclasses import replace

    hp = unit.settings.outputs[2].hp
    assert hp is not None
    unit.set(ControlKey(True, 2, "hp"), replace(hp, enabled=True, slope_db_per_oct=54))
    page = connected_studio.page
    with page.expect_response("**/api/refresh"):
        page.evaluate("fetch('/api/refresh', {method: 'POST', headers: {'Content-Type': 'application/json'}, "
                      "body: '{}'})")
    connected_studio.select("Out 3")
    connected_studio.until("High-pass slope", "value", "54")
    posts: list[dict[str, Any]] = []
    page.on("request", lambda request: posts.append(json.loads(request.post_data or "{}"))
            if "/api/edit" in request.url else None)
    x, y = connected_studio.centre(connected_studio.handle("High-pass"))
    mouse = page.mouse
    mouse.move(x, y)
    mouse.down()
    mouse.move(x + 100, y + 3, steps=8)  # sideways, well under one 20 px slope step
    mouse.up()
    page.wait_for_timeout(400)
    assert posts and all(set(p["values"]) == {"freq_hz"} for p in posts), posts
    page.locator("#errors").get_by_text("xover slope out of range: 54 dB/oct (6-48)", exact=False).first.wait_for()
    assert connected_studio.server_state()["selected"]["filters"][0]["fields"][2]["value"] == 54
    assert unit.client.set_xover.call_count == 0
    connected_studio.errors[:] = [e for e in connected_studio.errors if "status of 400" not in e]


# Saving to a program slot

def _open_save(studio: Studio) -> Any:
    studio.page.get_by_role("button", name="Save", exact=True).click()
    popover = studio.page.get_by_role("dialog", name="Save to program slot")
    popover.get_by_role("button", name=re.compile("^Slot 30, ")).wait_for()
    return popover


def _saves(studio: Studio) -> list[str]:
    saves: list[str] = []
    studio.page.on("request", lambda request: saves.append(request.post_data or "")
                   if request.url.endswith("/api/programs/save") else None)
    return saves


def test_save_opens_with_the_current_slot_preselected(studio: Studio) -> None:
    popover = _open_save(studio)
    current = popover.get_by_role("button", name="Slot 1, user1, current")
    assert current.get_attribute("aria-pressed") == "true"
    assert "current" in current.inner_text()
    assert popover.get_by_role("button", name="Overwrite slot 1 (user1)").is_enabled()
    popover.get_by_role("button", name="Slot 3, user3").click()
    assert popover.get_by_role("button", name="Slot 3, user3").get_attribute("aria-pressed") == "true"
    popover.get_by_role("button", name="Overwrite slot 3 (user3)").wait_for()


def test_saving_asks_again_with_cancel_focused_and_enter_does_not_save(studio: Studio) -> None:
    saves = _saves(studio)
    page = studio.page
    popover = _open_save(studio)
    popover.get_by_role("button", name="Slot 3, user3").click()
    popover.get_by_role("button", name="Overwrite slot 3 (user3)").click()
    popover.get_by_text("Slot 3 (user3) will be replaced with the unit's current live settings. "
                        "This cannot be undone.").wait_for()
    cancel = popover.get_by_role("button", name="Cancel", exact=True)
    assert cancel.evaluate("el => el === document.activeElement")
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)
    assert saves == []
    popover.get_by_role("button", name="Overwrite slot 3 (user3)").click()
    with page.expect_response("**/api/programs/save"):
        popover.get_by_role("button", name="Overwrite", exact=True).click()
    popover.wait_for(state="detached")
    assert json.loads(saves[0]) == {"slot": 2, "name": "user3", "apply_first": False}
    page.locator("#hint").get_by_text("Saved to slot 3 (user3).").wait_for()
    assert page.locator("#errors").inner_text() == ""
    popover = _open_save(studio)
    assert popover.get_by_role("button", name="Slot 3, user3, current").get_attribute("aria-pressed") == "true"


def test_saving_with_an_unapplied_edit_applies_first(studio: Studio) -> None:
    page = studio.page
    with page.expect_response("**/api/edit"):
        studio.field("Out 5 delay in ms").fill("1.5")
        studio.field("Out 5 delay in ms").press("Enter")
    assert studio.unapplied().inner_text() == "1 unapplied"
    popover = _open_save(studio)
    popover.get_by_text("You have 1 unapplied edit. The unit saves what is live on it, not your drafts.").wait_for()
    popover.get_by_role("button", name="Apply and overwrite slot 1 (user1)").click()
    popover.get_by_text("Your 1 unapplied edit will be applied to the unit first. Then slot 1 (user1) will be "
                        "replaced with the unit's live settings. This cannot be undone.", exact=True).wait_for()
    assert popover.get_by_text("not your drafts", exact=False).count() == 0
    assert popover.get_by_role("button", name="Overwrite", exact=True).count() == 0
    with page.expect_response("**/api/programs/save") as response:
        popover.get_by_role("button", name="Apply and overwrite", exact=True).click()
    assert json.loads(response.value.request.post_data or "{}") == {"slot": 0, "name": "user1", "apply_first": True}
    page.locator("#hint").get_by_text("Saved to slot 1 (user1).").wait_for()
    assert studio.unapplied().inner_text() == ""
    assert studio.field("Out 5 delay in ms").input_value() == "1.50"


def test_escape_closes_the_save_popover_without_saving(studio: Studio) -> None:
    saves = _saves(studio)
    popover = _open_save(studio)
    popover.get_by_role("button", name="Overwrite slot 1 (user1)").click()
    studio.page.keyboard.press("Escape")
    popover.wait_for(state="detached")
    assert studio.page.get_by_role("button", name="Save", exact=True).evaluate("el => el === document.activeElement")
    studio.page.wait_for_timeout(200)
    assert saves == []


def test_saving_says_live_is_switched_off_and_a_locked_unit_reaches_the_error_bar(
        connected_studio: Studio, unit: FakeUnit) -> None:
    from sinbosen_la548.client import LockState

    unit.client.get_program_names.return_value = tuple(f"User{n}" for n in range(1, 30)) + ("",)
    unit.client.current_program = 4
    page = connected_studio.page
    popover = _open_save(connected_studio)
    popover.get_by_role("button", name="Overwrite slot 1 (User1)").wait_for(state="detached")
    popover.get_by_role("button", name="Overwrite slot 5 (User5)").click()
    with page.expect_response("**/api/programs/save"):
        popover.get_by_role("button", name="Overwrite", exact=True).click()
    page.locator("#hint").get_by_text("Saved to slot 5 (User5).").wait_for()
    page.get_by_role("checkbox", name="Live").check()
    page.get_by_role("button", name="Save", exact=True).click()
    assert popover.get_by_role("button", name="Slot 5, User5, current").get_attribute("aria-pressed") == "true"
    assert popover.get_by_role("button", name="Slot 30, empty name").locator(".slot-name.empty").inner_text() \
        == "empty name"
    popover.get_by_role("button", name="Overwrite slot 5 (User5)").click()
    popover.get_by_text("Live tuning will be switched off.", exact=False).wait_for()
    unit.client.read_lock_state.return_value = LockState(True, "1234")
    with page.expect_response("**/api/programs/save"):
        popover.get_by_role("button", name="Overwrite", exact=True).click()
    page.locator("#errors").get_by_text("device is locked; writes are disabled").wait_for()
    assert page.locator("#hint").get_by_text("Saved to").count() == 0  # the earlier success line is gone
    unit.client.save_program.assert_called_once_with(4)
    connected_studio.errors[:] = [e for e in connected_studio.errors if "status of 400" not in e]


def test_the_click_that_opens_the_confirmation_cannot_confirm_it(studio: Studio) -> None:
    saves = _saves(studio)
    page = studio.page
    popover = _open_save(studio)
    popover.get_by_role("button", name="Overwrite slot 1 (user1)").dblclick()
    page.wait_for_timeout(500)
    assert saves == []  # the second click lands outside the shorter confirmation step and closes it
    # A second activation straight after the step opens, by pointer or by key, is ignored too.
    if popover.count():
        page.keyboard.press("Escape")
    popover = _open_save(studio)
    page.evaluate("""() => {
        document.querySelector('#overlay .button.primary').click();
        const confirm = [...document.querySelectorAll('#overlay .button.danger')][0];
        confirm.click();
        confirm.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    }""")
    page.wait_for_timeout(500)
    assert saves == []
    with page.expect_response("**/api/programs/save"):
        popover.get_by_role("button", name="Overwrite", exact=True).click()
    page.locator("#hint").get_by_text("Saved to slot 1 (user1).").wait_for()


def test_a_save_that_fails_after_its_apply_says_the_edits_were_applied(connected_studio: Studio,
                                                                       unit: FakeUnit) -> None:
    unit.client.get_program_names.return_value = tuple(f"User{n}" for n in range(1, 31))
    unit.client.current_program = 0
    unit.client.save_program.side_effect = OSError("usb write failed")
    page = connected_studio.page
    with page.expect_response("**/api/edit"):
        connected_studio.field("Out 5 delay in ms").fill("1.5")
        connected_studio.field("Out 5 delay in ms").press("Enter")
    popover = _open_save(connected_studio)
    popover.get_by_role("button", name="Apply and overwrite slot 1 (User1)").click()
    with page.expect_response("**/api/programs/save"):
        popover.get_by_role("button", name="Apply and overwrite", exact=True).click()
    page.locator("#errors").get_by_text(
        "Your edits were applied, but saving to slot 1 failed: usb write failed").wait_for()
    assert connected_studio.unapplied().inner_text() == ""
    assert page.locator("#hint").get_by_text("Saved to").count() == 0


# Mixer

def _mixer(studio: Studio) -> Any:
    studio.page.get_by_role("button", name="Mixer", exact=True).click()
    mixer = studio.page.locator("#mixer")
    mixer.locator(".route").first.wait_for()
    return mixer


def _route(studio: Studio, name: str) -> Any:
    return studio.page.locator("#mixer").get_by_role("button", name=name, exact=True)


def test_the_mixer_tab_shows_a_cell_per_input_and_output(studio: Studio) -> None:
    mixer = _mixer(studio)
    assert studio.page.get_by_role("button", name="Mixer", exact=True).get_attribute("aria-current") == "page"
    assert studio.page.get_by_role("button", name="Tune", exact=True).get_attribute("aria-current") is None
    assert mixer.locator(".route").count() == 32
    assert mixer.locator("thead .mixer-label").all_inner_texts() == [f"Out {n}" for n in range(1, 9)]
    assert mixer.locator("tbody .mixer-label").all_inner_texts() == ["In A", "In B", "In C", "In D"]
    assert _route(studio, "In A to Out 1, connected").get_attribute("aria-pressed") == "true"
    assert _route(studio, "In A to Out 2, not connected").get_attribute("aria-pressed") == "false"
    # The demo feeds nothing to outputs 3 and 4.
    assert mixer.locator("thead th").filter(has_text="no input").all_inner_texts() == ["Out 3\nno input",
                                                                                         "Out 4\nno input"]
    assert studio.page.locator(".strip").count() == 12
    assert studio.page.locator("#topbar .latency").count() == 0
    for hidden in ("#selected-bar", "#plot", "#cards"):
        assert not studio.page.locator(hidden).is_visible()
    box = mixer.locator(".route").first.bounding_box()
    assert box is not None and (box["width"], box["height"]) == (44, 28)


def test_a_route_click_is_unapplied_until_apply(studio: Studio) -> None:
    _mixer(studio)
    with studio.page.expect_response("**/api/route") as response:
        _route(studio, "In A to Out 3, not connected").click()
    assert json.loads(response.value.request.post_data or "{}") == {"input": 0, "output": 2, "on": True}
    cell = _route(studio, "In A to Out 3, connected")
    cell.wait_for()
    assert cell.get_attribute("aria-pressed") == "true"
    assert cell.evaluate("el => getComputedStyle(el).borderTopColor") == PENDING_BORDER
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for()
    assert studio.page.locator("#mixer thead th").filter(has_text="no input").count() == 1
    with studio.page.expect_response("**/api/apply"):
        studio.page.get_by_role("button", name="Apply", exact=True).click()
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for(state="detached")
    assert cell.evaluate("el => getComputedStyle(el).borderTopColor") != PENDING_BORDER
    assert cell.get_attribute("aria-pressed") == "true"
    hosts = {urlsplit(url).hostname for url in studio.requests}
    assert hosts == {"127.0.0.1"}, studio.requests


def test_the_tab_choice_survives_a_reload(studio: Studio) -> None:
    _mixer(studio)
    studio.load()
    studio.page.locator("#mixer .route").first.wait_for()
    assert studio.page.get_by_role("button", name="Mixer", exact=True).get_attribute("aria-current") == "page"
    studio.page.get_by_role("button", name="Tune", exact=True).click()
    studio.page.locator("#plot").wait_for()
    studio.load()
    studio.page.locator("#topbar .latency").wait_for()
    assert not studio.page.locator("#mixer").is_visible()


def test_arrow_keys_move_between_cells_and_space_and_enter_toggle(studio: Studio) -> None:
    _mixer(studio)
    first = _route(studio, "In A to Out 1, connected")
    first.focus()
    studio.page.keyboard.press("ArrowRight")
    studio.page.keyboard.press("ArrowDown")
    focused = "document.activeElement.getAttribute('aria-label')"
    assert studio.page.evaluate(focused) == "In B to Out 2, connected"
    studio.page.keyboard.press("ArrowLeft")
    studio.page.keyboard.press("ArrowLeft")  # stays in the first column
    studio.page.keyboard.press("ArrowUp")
    studio.page.keyboard.press("ArrowUp")  # and the first row
    assert studio.page.evaluate(focused) == "In A to Out 1, connected"
    studio.page.keyboard.press("ArrowDown")
    studio.page.keyboard.press("ArrowRight")
    with studio.page.expect_response("**/api/route"):
        studio.page.keyboard.press("Space")
    studio.until("In B to Out 2, not connected", "ariaPressed", "false")
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for()
    assert studio.page.evaluate(focused) == "In B to Out 2, not connected"
    with studio.page.expect_response("**/api/route"):
        studio.page.keyboard.press("Enter")
    studio.until("In B to Out 2, connected", "ariaPressed", "true")
    assert studio.unapplied().inner_text() == ""
    # Only the focused cell is in the tab order.
    assert studio.page.locator("#mixer .route[tabindex='0']").count() == 1


def test_mixer_headers_show_nicknames_and_mutes(studio: Studio) -> None:
    studio.name("In A", "Left")
    studio.name("Out 2", "Sub L")
    studio.field("Out 2 mute").click()
    studio.until("Out 2 mute", "ariaPressed", "true")
    mixer = _mixer(studio)
    head = mixer.locator("thead th").nth(1)
    assert head.locator(".mixer-name").inner_text() == "Sub L"
    assert head.locator(".mixer-label").inner_text() == "Out 2"
    assert head.locator(".mixer-mute").inner_text() == "M"
    assert mixer.locator(".mixer-mute").count() == 1
    assert mixer.locator("thead th").nth(2).locator(".mixer-name").inner_text() == ""
    _route(studio, "Left to Sub L, not connected").wait_for()
    _route(studio, "Left to Out 1, connected").wait_for()


def test_a_staged_route_updates_the_tune_latency(studio: Studio) -> None:
    studio.select("Out 3")
    latency = studio.page.locator("#topbar .latency")
    latency.get_by_text("latency 1.73 ms, no input routed").wait_for()
    studio.field("In C delay in ms").fill("1")
    with studio.page.expect_response("**/api/edit"):
        studio.field("In C delay in ms").press("Enter")
    _mixer(studio)
    with studio.page.expect_response("**/api/route"):
        _route(studio, "In C to Out 3, not connected").click()
    studio.page.get_by_role("button", name="Tune", exact=True).click()
    latency.get_by_text("latency 2.73 ms").wait_for()


# Import from REW

REW_FIXTURE = Path(__file__).parents[1] / "fixtures" / "rew_filters.txt"


def _api(studio: Studio, path: str, body: dict[str, Any]) -> None:
    studio.page.evaluate("([path, body]) => fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},"
                         " body: JSON.stringify(body)})", [path, body])


def _choose_rew_file(studio: Studio, path: Path) -> Any:
    page = studio.page
    with page.expect_file_chooser() as chooser:
        page.get_by_role("button", name="Import from REW…", exact=True).click()
    chooser.value.set_files(path)
    dialog = page.get_by_role("dialog", name="Import REW filters")
    dialog.wait_for()
    return dialog


def _out3_with_bands_and_a_high_pass(studio: Studio) -> None:
    """Out 3 with 6 bands in use and a 80 Hz Linkwitz-Riley 24 dB/oct high-pass, all applied."""
    for _ in range(6):
        _api(studio, "/api/band/add", {"id": "out:2"})
    _api(studio, "/api/edit", {"id": "out:2", "kind": "hp", "values": {
        "freq_hz": 80, "filter_type": 1, "slope_db_per_oct": 24, "enabled": True}})
    _api(studio, "/api/apply", {})
    studio.select("Out 3")
    studio.page.locator("#cards .card-title").get_by_text("EQ 6", exact=True).wait_for()


def test_the_import_dialog_says_exactly_what_it_overrides(studio: Studio, tmp_path: Path) -> None:
    _out3_with_bands_and_a_high_pass(studio)
    studio.name("Out 3", "Mid L")
    dialog = _choose_rew_file(studio, REW_FIXTURE)
    assert dialog.locator(".popover-title").inner_text() == 'Import REW filters into Out 3 "Mid L"'
    warning = dialog.locator(".rew-warning").first
    assert warning.inner_text().splitlines() == [
        "This overrides the channel's current settings.",
        "This replaces all EQ on this channel. 6 bands now in use will be overwritten or cleared.",
        "The high-pass (now 80 Hz Linkwitz-Riley 24 dB/oct) will be replaced by 100 Hz Butterworth 12 dB/oct.",
        "The low-pass is kept: the file has none."]
    assert warning.evaluate("el => getComputedStyle(el).borderTopColor") == PENDING_BORDER
    rows = [row.split("\t") for row in dialog.locator(".rew-table tbody tr").all_inner_texts()]
    assert [row[:5] for row in rows[:2]] == [["1", "Parametric EQ", "50", "-6.0", "3.69"],
                                             ["2", "Parametric EQ", "1911", "-3.1", "1.20"]]
    assert "REW Q 2.58" in rows[0][5]
    assert rows[2][:2] == ["HP", "100 Hz Butterworth 12 dB/oct (Filter 4 (HP))"]
    skipped = dialog.locator(".rew-skipped")
    assert skipped.get_by_text("Not imported", exact=True).count() == 1
    assert skipped.locator("li").count() == 1 and skipped.locator("li").inner_text().startswith("Filter 5 (LS): ")
    dialog.get_by_text("Nothing is sent to the unit until you press Apply.", exact=True).wait_for()
    cancel = dialog.get_by_role("button", name="Cancel", exact=True)
    assert cancel.evaluate("el => el === document.activeElement")
    assert dialog.get_by_role("button", name="Replace EQ and crossover", exact=True).is_enabled()
    hosts = {urlsplit(url).hostname for url in studio.requests}
    assert hosts == {"127.0.0.1"}, studio.requests


def test_cancel_and_escape_stage_nothing_and_enter_does_not_replace(studio: Studio) -> None:
    studio.select("Out 3")
    page = studio.page
    stagings: list[str] = []
    page.on("request", lambda request: stagings.append(request.url) if request.url.endswith("/api/rew/import")
            else None)
    dialog = _choose_rew_file(studio, REW_FIXTURE)
    # Cancel has the focus, so Enter cancels.
    page.keyboard.press("Enter")
    dialog.wait_for(state="detached")
    dialog = _choose_rew_file(studio, REW_FIXTURE)
    page.keyboard.press("Escape")
    dialog.wait_for(state="detached")
    button = page.get_by_role("button", name="Import from REW…", exact=True)
    assert button.evaluate("el => el === document.activeElement")
    page.wait_for_timeout(200)
    assert stagings == [] and studio.unapplied().inner_text() == ""
    assert studio.server_state()["status"]["pending_count"] == 0


def test_replace_stages_the_bands_as_unapplied_cards_and_revert_clears_them(studio: Studio) -> None:
    page = studio.page
    studio.select("Out 3")
    dialog = _choose_rew_file(studio, REW_FIXTURE)
    dialog.get_by_text("No EQ bands are in use now.", exact=False).wait_for()
    with page.expect_response("**/api/rew/import"):
        dialog.get_by_role("button", name="Replace EQ and crossover", exact=True).click()
    dialog.wait_for(state="detached")
    studio.unapplied().get_by_text("3 unapplied", exact=True).wait_for()
    cards = page.locator("#cards .card")
    assert cards.locator(".card-title").all_inner_texts() == ["High-pass", "Low-pass", "EQ 1", "EQ 2", "Limiter",
                                                              "Compressor"]
    assert all("pending" in (card.get_attribute("class") or "")
               for card in (cards.nth(0), cards.nth(2), cards.nth(3)))
    assert "pending" not in (cards.nth(1).get_attribute("class") or "")
    assert studio.field("High-pass frequency").input_value() == "100"
    page.get_by_role("button", name="Revert", exact=True).click()
    studio.unapplied().get_by_text("3 unapplied", exact=True).wait_for(state="detached")
    assert page.locator("#cards .card-title").all_inner_texts() == ["High-pass", "Low-pass", "Limiter", "Compressor"]
    assert studio.field("High-pass frequency").input_value() == "20"


def test_the_same_file_can_be_chosen_twice_in_a_row(studio: Studio) -> None:
    studio.select("Out 3")
    for _ in range(2):
        dialog = _choose_rew_file(studio, REW_FIXTURE)
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        dialog.wait_for(state="detached")


def test_a_pk_only_file_on_an_input_offers_replace_eq_and_says_nothing_of_the_crossover(
        studio: Studio, tmp_path: Path) -> None:
    source = tmp_path / "pk.txt"
    source.write_text("Equaliser: Generic\nFilter  1: ON  PK       Fc    1000 Hz  Gain  -3.0 dB  Q  2.00\n")
    studio.select("In A")
    dialog = _choose_rew_file(studio, source)
    assert dialog.locator(".popover-title").inner_text() == "Import REW filters into In A"
    assert "high-pass" not in dialog.inner_text() and "low-pass" not in dialog.inner_text()
    assert dialog.get_by_role("button", name="Replace EQ", exact=True).is_enabled()


def test_a_file_that_is_not_a_rew_export_shows_the_error_with_only_close(studio: Studio, tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("measurements, not filters\n")
    studio.select("Out 3")
    dialog = _choose_rew_file(studio, source)
    dialog.get_by_text("no filter lines found", exact=False).wait_for()
    assert [b.inner_text() for b in dialog.get_by_role("button").all()] == ["Close"]
    dialog.get_by_role("button", name="Close", exact=True).click()
    dialog.wait_for(state="detached")
    assert studio.unapplied().inner_text() == ""
    studio.errors[:] = [e for e in studio.errors if "status of 400" not in e]


def test_the_dialog_names_linked_channels_that_will_change(studio: Studio) -> None:
    studio.select("Out 3")
    _api(studio, "/api/link", {"id": "out:2", "letter": None})
    _api(studio, "/api/link", {"id": "out:3", "letter": "A"})
    studio.page.locator(".link-summary").wait_for()
    studio.name("Out 4", "Mid R")
    studio.select("Out 3")
    dialog = _choose_rew_file(studio, REW_FIXTURE)
    dialog.get_by_text('Out 4 "Mid R" is linked and will change too.', exact=True).wait_for()


def test_with_live_on_the_dialog_says_it_will_be_sent_straight_away(connected_studio: Studio) -> None:
    page = connected_studio.page
    page.get_by_role("checkbox", name="Live").check()
    connected_studio.select("Out 3")
    dialog = _choose_rew_file(connected_studio, REW_FIXTURE)
    dialog.get_by_text("Live is on: this will be sent to the unit straight away.", exact=True).wait_for()
    assert dialog.get_by_text("until you press Apply", exact=False).count() == 0
    dialog.get_by_role("button", name="Cancel", exact=True).click()


# Limiter and compressor cards

def _dynamics_card(studio: Studio, title: str) -> Any:
    return studio.page.locator("#cards .card", has=studio.page.get_by_text(title, exact=True))


def test_the_limiter_and_compressor_cards_are_on_outputs_only(studio: Studio) -> None:
    for title in ("Limiter", "Compressor"):
        card = _dynamics_card(studio, title)
        assert card.count() == 1
        assert card.get_by_role("checkbox").is_checked() is False
    assert _dynamics_card(studio, "Limiter").locator(".card-row > span").all_inner_texts() == [
        "Thr", "Atk", "Rel", "Det"]
    assert _dynamics_card(studio, "Compressor").locator(".card-row > span").all_inner_texts() == [
        "Thr", "Ratio", "Atk", "Rel"]
    assert studio.field("Compressor ratio").input_value() == "1.0 : 1"
    # FIR stays a read-only row of the right-hand stack.
    assert studio.page.locator("#cards .more-row .name").all_inner_texts() == ["FIR"]
    studio.select("In A")
    assert studio.page.locator("#cards .card").count() == 0
    assert studio.page.locator("#cards .more-row .name").all_inner_texts() == ["FIR"]


def test_a_limiter_threshold_is_unapplied_until_apply(studio: Studio) -> None:
    field = studio.field("Limiter threshold")
    assert field.input_value() == "0.0 dB"
    field.fill("-6")
    field.press("Enter")
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for()
    assert studio.border("Limiter threshold") == PENDING_BORDER
    assert field.input_value() == "-6.0 dB"
    assert "pending" in (_dynamics_card(studio, "Limiter").get_attribute("class") or "")
    assert studio.border("Limiter attack") != PENDING_BORDER
    studio.page.get_by_role("button", name="Apply", exact=True).click()
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for(state="detached")
    assert field.input_value() == "-6.0 dB"
    assert studio.border("Limiter threshold") != PENDING_BORDER


def test_a_typed_dynamics_value_is_shown_as_the_device_stores_it(studio: Studio) -> None:
    field = studio.field("Compressor ratio")
    field.fill("4:1")
    field.press("Enter")
    studio.until("Compressor ratio", "value", "4.0 : 1")
    field = studio.field("Compressor attack")
    field.fill("2.4 ms")
    field.press("Enter")
    studio.until("Compressor attack", "value", "2 ms")
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for()


def test_a_dynamics_value_out_of_range_is_refused_with_its_range(studio: Studio) -> None:
    field = studio.field("Limiter threshold")
    field.fill("25")
    field.press("Enter")
    message = studio.page.locator(".field-error")
    message.get_by_text(re.compile("Threshold must be from -60 to 20 dB")).wait_for()
    assert studio.border("Limiter threshold") == ERROR_BORDER
    assert studio.unapplied().inner_text() == ""
    field.press("Escape")
    assert field.input_value() == "0.0 dB"
    studio.errors[:] = [e for e in studio.errors if "status of 400" not in e]


def test_the_compressor_can_be_enabled_and_disabled(studio: Studio) -> None:
    tick = studio.page.get_by_role("checkbox", name="Compressor", exact=True)
    tick.check()
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for()
    assert "pending" in (studio.page.get_by_text("Compressor", exact=True).locator("..").get_attribute("class") or "")
    assert "off" not in (_dynamics_card(studio, "Compressor").get_attribute("class") or "").split()
    tick.uncheck()
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for(state="detached")


def test_limiter_detection_is_a_select(studio: Studio) -> None:
    select = studio.field("Limiter detection")
    assert select.evaluate("el => [...el.options].map(o => o.text)") == ["Average", "Peak"]
    select.select_option("Peak")
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for()
    assert studio.border("Limiter detection") == PENDING_BORDER
    studio.until("Limiter detection", "value", "1")


def test_a_factory_disabled_compressor_shows_its_values_and_enabling_it_stages_valid_ones(studio: Studio) -> None:
    studio.select("Out 3")
    assert studio.field("Compressor ratio").input_value() == "0.0 : 1"
    assert studio.field("Compressor threshold").input_value() == "340.0 dB"
    studio.page.get_by_role("checkbox", name="Compressor", exact=True).check()
    studio.until("Compressor ratio", "value", "1.0 : 1")
    assert studio.field("Compressor threshold").input_value() == "20.0 dB"
    assert studio.border("Compressor ratio") == PENDING_BORDER
    assert studio.border("Compressor release") != PENDING_BORDER
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for()
    studio.page.get_by_role("button", name="Apply", exact=True).click()
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for(state="detached")


def test_a_block_the_engine_refuses_to_write_is_read_only_with_its_reason(studio: Studio) -> None:
    studio.select("Out 3")
    card = _dynamics_card(studio, "Limiter")
    assert card.locator(".card-note").inner_text() == ("This limiter holds data the app cannot write back safely, "
                                                        "so it is shown read-only.")
    assert card.get_by_role("checkbox").is_disabled()
    assert studio.field("Limiter threshold").is_disabled()
    assert studio.field("Limiter detection").is_disabled()
    assert not _dynamics_card(studio, "Compressor").locator(".card-note").count()


def test_dynamics_edits_send_nothing_off_loopback(studio: Studio) -> None:
    studio.field("Limiter threshold").fill("-3")
    studio.field("Limiter threshold").press("Enter")
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for()
    assert {urlsplit(url).hostname for url in studio.requests} == {"127.0.0.1"}
