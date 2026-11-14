"""Browser checks for the studio page, served by uvicorn in offline demo mode.

The studio app is started without a service, so it runs the offline demo and never opens a device; nothing here
posts to `/api/connect`. These fixtures are separate from the Gradio ones in conftest.py.
"""

from __future__ import annotations

import re
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

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


@pytest.fixture
def studio(tmp_path: Path) -> Iterator[Studio]:
    pytest.importorskip("playwright.sync_api", reason="install the e2e dependency group")
    import uvicorn
    from playwright.sync_api import sync_playwright

    from sinbosen_la548.studio_app import create_studio_app

    app = create_studio_app(store=StudioStore(tmp_path / "studio.json"))
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
    assert cards.all_inner_texts() == ["High-pass", "Low-pass"]
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
    nickname = studio.field("Out 3 nickname")
    nickname.fill("Mid L")
    with studio.page.expect_response("**/api/nickname"):
        nickname.press("Tab")
    studio.load()
    assert studio.field("Out 3 nickname").input_value() == "Mid L"
    studio.select("Out 3")
    assert studio.page.locator("#selected-bar").get_by_text("Mid L", exact=True).is_visible()


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
    assert not bar.get_by_label("Gain", exact=True).is_checked()
    with studio.page.expect_response("**/api/link/sections"):
        bar.get_by_label("Gain", exact=True).check()
    state = studio.page.evaluate("fetch('/api/state').then(response => response.json())")
    assert state["selected"]["link"]["sections"]["gain"] is True
    assert bar.get_by_label("Gain", exact=True).is_checked()
    studio.field("Out 4 link").click()
    studio.page.get_by_role("menuitem", name="Unlink").click()
    studio.until("Out 4 link", "textContent", "Link")


def test_add_band_adds_a_card(studio: Studio) -> None:
    studio.select("Out 3")
    add = studio.page.get_by_role("button", name=re.compile("^Add band"))
    assert "0 of 15 used" in add.inner_text()
    add.click()
    studio.page.locator("#cards .card-title").get_by_text("EQ 1", exact=True).wait_for()
    assert "1 of 15 used" in add.inner_text()
    studio.unapplied().get_by_text("1 unapplied", exact=True).wait_for()


def test_no_request_leaves_loopback_and_inter_is_served(studio: Studio) -> None:
    studio.select("Out 3")
    studio.field("Out 3 mute").click()
    studio.page.wait_for_timeout(1500)
    assert studio.page.evaluate("document.fonts.ready.then(() => document.fonts.check('13px Inter'))")
    assert any(url.endswith("/static/fonts/InterVariable.woff2") for url in studio.requests)
    hosts = {urlsplit(url).hostname for url in studio.requests}
    assert hosts == {"127.0.0.1"}, studio.requests
