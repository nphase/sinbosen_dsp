"""Local JSON API behind the studio page: one server-side session over `DSPService`, independent of Gradio."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from . import links
from .service import PROGRAM_SLOTS, ApplyResult, ControlKey, DSPService, demo_settings
from .settings import CrossoverSettings, EQBandSettings
from .studio_store import SECTIONS, DeviceNotes, LinkGroup, StudioStore, default_path
from .studio_view import band_in_use, build_state
from .tuning import TuningController

LOG = logging.getLogger(__name__)
WEB_DIR = Path(__file__).with_name("web")
DEMO_DEVICE_KEY = "demo"
FLUSH_INTERVAL_S = 0.25

Handler = Callable[[dict[str, Any]], ApplyResult | None]


def _text(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _flag(body: dict[str, Any], name: str) -> bool:
    value = body.get(name)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _index(body: dict[str, Any], name: str = "index") -> int:
    value = body.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    return value


def _number(value: object) -> float:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
    except OverflowError:
        pass
    raise ValueError("value must be a finite number")


class SlotRenamed(Exception):
    """The slot the user confirmed has another name on the unit now: answered with 409, nothing saved."""


def _parse_channel(text: object) -> tuple[bool, int]:
    side, _, number = text.partition(":") if isinstance(text, str) else ("", "", "")
    if side not in ("in", "out") or not number.isascii() or not number.isdigit():
        raise ValueError("id must look like in:0 or out:3")
    return side == "out", int(number)


class _Studio:
    """The single-user session: the service token, the selected channel, the plot options and the device key."""

    def __init__(self, service: DSPService, store: StudioStore) -> None:
        self.service = service
        self.store = store
        self.tuning = TuningController(service)
        self.token = service.new_session()
        self.lock = threading.RLock()
        self.selected = (True, 0)  # Out 1
        self.plot: dict[str, bool] = {"phase": False, "group_delay": False, "each_filter": True, "on_device": True}
        self.device_key = DEMO_DEVICE_KEY
        self.live_errors = ""

    # State

    def state(self) -> dict[str, Any]:
        view = self.service.view(self.token)
        channels = view.draft.outputs if self.selected[0] else view.draft.inputs
        if self.selected[1] >= len(channels):
            self.selected = (True, 0)
        return build_state(view, self._notes(), selected=self.selected,
                           device=None if view.demo else self.device_key, live_errors=self.live_errors,
                           warning=self.store.warning, **self.plot)

    def _channel(self, body: dict[str, Any]) -> tuple[bool, int]:
        output, channel = _parse_channel(body.get("id"))
        draft = self.service.view(self.token).draft
        if channel >= len(draft.outputs if output else draft.inputs):
            raise ValueError("channel out of range")
        return output, channel

    def _notes(self) -> DeviceNotes:
        """The notes of the device in use, without link members beyond its channel counts."""
        profile = self.service.view(self.token).draft.profile
        return self.store.load(self.device_key, channel_counts=(profile.input_count, profile.output_count))

    def _save(self, notes: DeviceNotes) -> None:
        self.store.save(self.device_key, notes)

    def _group(self, letter: str) -> LinkGroup:
        group = next((g for g in self._notes().links if g.letter == letter), None)
        if group is None:
            raise ValueError(f"no group {letter}")
        return group

    # Device

    def connect(self, body: dict[str, Any]) -> None:
        address = _text(body, "address").strip()
        if not address:
            raise ValueError("address must not be empty")
        self.service.connect(self.token, address)
        self.device_key = self.service.device_address(self.token) or address
        self.live_errors = ""

    def demo(self, body: dict[str, Any]) -> None:
        self.service.demo(self.token)
        self.device_key = DEMO_DEVICE_KEY
        self.live_errors = ""

    def refresh(self, body: dict[str, Any]) -> None:
        self.service.refresh(self.token)

    def apply(self, body: dict[str, Any]) -> ApplyResult:
        result = self.service.apply(self.token)
        self.live_errors = ""
        return result

    def revert(self, body: dict[str, Any]) -> None:
        self.service.revert(self.token)

    def live(self, body: dict[str, Any]) -> None:
        self.service.set_live(self.token, _flag(body, "enabled"))
        self.live_errors = ""

    def flush(self) -> None:
        """One Live tick, as `ui.py` does: write what is due, and switch Live off after a failure."""
        with self.lock:
            try:
                if not self.service.view(self.token).live:
                    return
                result = self.service.flush_live(self.token)
                if result is not None and (result.errors or result.conflicts):
                    self.live_errors = result.summary
            except Exception as error:
                LOG.warning("Live write failed: %s", error)
                self.live_errors = str(error)
                with contextlib.suppress(Exception):
                    self.service.set_live(self.token, False)

    def programs(self) -> dict[str, Any]:
        slots = self.service.programs(self.token)
        return {"slots": [{"index": i, "number": i + 1, "name": name, "current": i == slots.current}
                          for i, name in enumerate(slots.names)], "current": slots.current}

    def save(self, body: dict[str, Any]) -> tuple[ApplyResult | None, dict[str, Any] | None, str | None]:
        """Save the unit's live settings to the slot the user confirmed by number and `name`; a slot now named
        otherwise on the unit is refused. With `apply_first`, Apply runs first and the save happens only when it
        reports ok; otherwise nothing is saved and its result is returned. A save that fails after Apply wrote
        something is returned as the third value, so the page learns of the Apply too."""
        slot = _index(body, "slot")
        if not 0 <= slot < PROGRAM_SLOTS:
            raise ValueError(f"slot must be 0-{PROGRAM_SLOTS - 1}")
        name = _text(body, "name")
        apply_first = _flag(body, "apply_first") if "apply_first" in body else False
        names = self.service.programs(self.token).names
        if names[slot] != name:
            raise SlotRenamed(f"slot {slot + 1} is now named {names[slot]!r} on the unit, not {name!r}; "
                              "nothing was saved")
        result = None
        if apply_first:
            result = self.apply(body)
            if not _result_json(result)["ok"]:
                return result, None, None
        try:
            self.service.operation(self.token, "save", slot=slot)
        except Exception as error:
            if result is None or not result.applied:
                raise
            if _status_for(error) is None:
                LOG.exception("Unhandled error saving after Apply")
            return result, None, f"Your edits were applied, but saving to slot {slot + 1} failed: {error}"
        return result, {"slot": slot, "name": names[slot]}, None

    # View

    def select(self, body: dict[str, Any]) -> None:
        self.selected = self._channel(body)

    def set_plot(self, body: dict[str, Any]) -> None:
        options = {name: _flag(body, name) for name in self.plot if name in body}
        self.plot.update(options)

    # Edits

    def now(self, body: dict[str, Any]) -> ApplyResult | None:
        output, channel = self._channel(body)
        key = ControlKey(output, channel, _text(body, "block"))
        # Links never tie gain, mute or polarity: only this channel is written.
        return self.service.write_now(self.token, {key: body.get("value")})

    def edit(self, body: dict[str, Any]) -> None:
        """One field (`field`, `value`) or several fields of the same block at once (`values`: {field: value})."""
        output, channel = self._channel(body)
        kind = body.get("kind")
        values = body["values"] if "values" in body else {body.get("field"): body.get("value")}
        if not isinstance(values, dict) or not values:
            raise ValueError("values must be an object of field: value")
        key: ControlKey | None = None
        block: Any = None
        edited: CrossoverSettings | EQBandSettings
        for field, value in values.items():
            if not isinstance(field, str):
                raise ValueError("field must be a string")
            if kind == "delay":
                if field != "delay_ms":
                    raise ValueError("unsupported delay field")
                key, block = ControlKey(output, channel, "delay"), _number(value)
                continue
            if kind in ("hp", "lp"):
                if not output:
                    raise ValueError("crossovers exist on outputs only")
                key, edited = self.tuning.crossover_edit(self.token, channel, kind, field,
                                                         self._field_value(field, value))
            elif kind == "eq":
                key, edited = self.tuning.eq_edit(self.token, output, channel, _index(body), field,
                                                  self._field_value(field, value))
            else:
                raise ValueError("kind must be delay, hp, lp or eq")
            # Each field is checked against the draft on its own; what it changed is carried into the combined block.
            draft_channel = self.tuning.channel(self.token, output, channel)
            current = draft_channel.eq_bands[key.index] if kind == "eq" else getattr(draft_channel, kind)
            block = replace(block if block is not None else edited, **{
                f.name: getattr(edited, f.name) for f in fields(edited)
                if getattr(edited, f.name) != getattr(current, f.name)})
        assert key is not None
        draft = self.service.view(self.token).draft
        self.service.stage_many(self.token, links.expand(self._notes().links, draft, {key: block}))

    @staticmethod
    def _field_value(field: str, value: object) -> object:
        if field == "enabled":
            if not isinstance(value, bool):
                raise ValueError("enabled must be true or false")
            return value
        return _number(value)

    def band_add(self, body: dict[str, Any]) -> None:
        output, channel = self._channel(body)
        draft = self.service.view(self.token).draft
        bands = (draft.outputs if output else draft.inputs)[channel].eq_bands
        index = next((i for i, band in enumerate(bands) if not band_in_use(band)), None)
        if index is None:
            raise ValueError("no free EQ band")
        band = replace(bands[index], enabled=True, type=0, gain_db=0.0, q=1.0, freq_hz=1000, slope=0)
        key = ControlKey(output, channel, "eq", index)
        self.service.stage_many(self.token, links.expand(self._notes().links, draft, {key: band}))

    def band_remove(self, body: dict[str, Any]) -> None:
        output, channel = self._channel(body)
        index = _index(body)
        draft = self.service.view(self.token).draft
        bands = (draft.outputs if output else draft.inputs)[channel].eq_bands
        if not 0 <= index < len(bands):
            raise ValueError("EQ band out of range")
        band = replace(bands[index], enabled=False, gain_db=0.0, type=0, slope=0)
        key = ControlKey(output, channel, "eq", index)
        self.service.stage_many(self.token, links.expand(self._notes().links, draft, {key: band}))

    # Notes

    def nickname(self, body: dict[str, Any]) -> None:
        output, channel = self._channel(body)
        notes = self._notes()
        self._save(replace(notes, nicknames={**notes.nicknames, (output, channel): _text(body, "name")}))

    def link(self, body: dict[str, Any]) -> None:
        """Join or create a group. A joining channel takes the group's tied sections as ordinary pending edits; no
        tie-able section is written immediately, so joining never writes to the unit."""
        output, channel = self._channel(body)
        letter = body.get("letter")
        if letter is not None and not isinstance(letter, str):
            raise ValueError("letter must be a group letter or null")
        notes = self._notes()
        groups = links.with_member(notes.links, output, channel, letter)
        if letter is not None:
            # The group as it was before joining: the joiner must not be taken as its source.
            before = next(g for g in notes.links if g.letter == letter)
            if channel not in before.members:
                draft = self.service.view(self.token).draft
                self.service.stage_many(self.token, links.join(before, channel, draft))
        self._save(replace(notes, links=groups))

    def unlink(self, body: dict[str, Any]) -> None:
        output, channel = self._channel(body)
        notes = self._notes()
        self._save(replace(notes, links=links.without_member(notes.links, output, channel)))

    def link_sections(self, body: dict[str, Any]) -> None:
        sections = body.get("sections")
        if not isinstance(sections, list) or not all(isinstance(s, str) for s in sections):
            raise ValueError(f"sections must be a list drawn from {', '.join(SECTIONS)}")
        notes = self._notes()
        self._save(replace(notes, links=links.with_sections(notes.links, _text(body, "letter"),
                                                            frozenset(sections))))

    def link_sync(self, body: dict[str, Any]) -> None:
        group = self._group(_text(body, "letter"))
        output, source = self._channel({"id": body.get("from")})
        if output != group.output or source not in group.members:
            raise ValueError(f"{body.get('from')} is not in group {group.letter}")
        draft = self.service.view(self.token).draft
        ordered = replace(group, members=(source, *(m for m in group.members if m != source)))
        edits: dict[ControlKey, object] = {}
        for member in ordered.members[1:]:
            edits.update(links.join(ordered, member, draft))
        self.service.stage_many(self.token, edits)


def _result_json(result: ApplyResult) -> dict[str, Any]:
    return {"ok": not result.errors and not result.conflicts and result.verified, "errors": list(result.errors),
            "conflicts": [key.label for key in result.conflicts], "pending": [key.label for key in result.pending],
            "verified": result.verified}


def _hostname(host: str) -> str:
    """The host part of a Host header value, keeping the brackets of an IPv6 literal."""
    host = host.strip().lower()
    return host[:host.index("]") + 1] if host.startswith("[") and "]" in host else host.partition(":")[0]


class _LocalOnly:
    """Refuses requests a page on another site could send: foreign Host or Origin, and non-JSON POSTs to the API."""

    def __init__(self, app: ASGIApp, allowed_hosts: Sequence[str]) -> None:
        self.app = app
        extra = (f"[{h}]" if ":" in h and not h.startswith("[") else h for h in allowed_hosts)
        self.allowed = {"127.0.0.1", "localhost", "[::1]", *(h.strip().lower() for h in extra)}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        origin = headers.get("origin")
        refusal: tuple[int, str] | None = None
        if _hostname(host) not in self.allowed:
            refusal = 400, "unexpected Host header"
        elif origin is not None and urlsplit(origin).netloc.lower() != host.strip().lower():
            refusal = 403, "cross-origin requests are not allowed"
        elif scope["method"] == "POST" and scope["path"].startswith("/api/"):
            if headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
                refusal = 415, "requests must be sent as application/json"
        if refusal is not None:
            await JSONResponse({"error": refusal[1]}, status_code=refusal[0])(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _status_for(error: Exception) -> int | None:
    if isinstance(error, SlotRenamed):
        return 409
    if isinstance(error, (ValueError, PermissionError, OverflowError)):
        return 400
    if isinstance(error, (ConnectionError, TimeoutError, OSError, RuntimeError)):
        return 503
    return None


def create_studio_app(service: DSPService | None = None, *, store: StudioStore | None = None,
                      allowed_hosts: Sequence[str] = ()) -> Starlette:
    """The studio API and page. `service` defaults to an offline demo; it is closed when the app shuts down.

    Only loopback Host headers are served, plus `allowed_hosts`; no CORS headers are ever sent.
    """""
    studio = _Studio(service or DSPService(demo_settings()), store or StudioStore(default_path()))

    def respond(call: Callable[[], dict[str, Any]]) -> Response:
        try:
            return JSONResponse(call())
        except Exception as error:
            status = _status_for(error)
            if status is None:
                LOG.exception("Unhandled error in the studio API")
                return JSONResponse({"error": f"unexpected error: {error}"}, status_code=500)
            return JSONResponse({"error": str(error)}, status_code=status)

    def run(handler: Handler, body: dict[str, Any]) -> dict[str, Any]:
        with studio.lock:
            result = handler(body)
            return {**studio.state(), "result": None if result is None else _result_json(result)}

    async def read_body(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if not raw.strip():
            return {}
        try:
            body = await request.json()
        except ValueError:
            raise ValueError("request body must be JSON") from None
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

    def post_route(path: str, handler: Handler) -> Route:
        async def endpoint(request: Request) -> Response:
            try:
                body = await read_body(request)
            except ValueError as error:
                return JSONResponse({"error": str(error)}, status_code=400)
            return await run_in_threadpool(respond, lambda: run(handler, body))

        return Route(path, endpoint, methods=["POST"])

    async def get_state(request: Request) -> Response:
        def current() -> dict[str, Any]:
            with studio.lock:
                return studio.state()

        return await run_in_threadpool(respond, current)

    async def get_programs(request: Request) -> Response:
        def current() -> dict[str, Any]:
            with studio.lock:
                return studio.programs()

        return await run_in_threadpool(respond, current)

    async def save_program(request: Request) -> Response:
        try:
            body = await read_body(request)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)

        def save() -> dict[str, Any]:
            with studio.lock:
                result, saved, save_error = studio.save(body)
                return {**studio.state(), "result": None if result is None else _result_json(result), "saved": saved,
                        "save_error": save_error}

        return await run_in_threadpool(respond, save)

    async def index(request: Request) -> Response:
        return FileResponse(WEB_DIR / "index.html")

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async def flush_live() -> None:
            while True:
                await asyncio.sleep(FLUSH_INTERVAL_S)
                await asyncio.to_thread(studio.flush)

        task = asyncio.create_task(flush_live())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await asyncio.to_thread(studio.service.close)

    routes: list[Route | Mount] = [
        Route("/api/state", get_state, methods=["GET"]),
        post_route("/api/connect", studio.connect),
        post_route("/api/disconnect", studio.demo),
        post_route("/api/demo", studio.demo),
        post_route("/api/refresh", studio.refresh),
        post_route("/api/apply", studio.apply),
        post_route("/api/revert", studio.revert),
        post_route("/api/live", studio.live),
        post_route("/api/select", studio.select),
        post_route("/api/plot", studio.set_plot),
        post_route("/api/now", studio.now),
        post_route("/api/edit", studio.edit),
        post_route("/api/band/add", studio.band_add),
        post_route("/api/band/remove", studio.band_remove),
        post_route("/api/nickname", studio.nickname),
        post_route("/api/link", studio.link),
        post_route("/api/unlink", studio.unlink),
        post_route("/api/link/sections", studio.link_sections),
        post_route("/api/link/sync", studio.link_sync),
        Route("/api/programs", get_programs, methods=["GET"]),
        Route("/api/programs/save", save_program, methods=["POST"]),
    ]
    if WEB_DIR.is_dir():
        routes += [Route("/", index, methods=["GET"]), Mount("/static", StaticFiles(directory=WEB_DIR))]
    app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(_LocalOnly, allowed_hosts=allowed_hosts)])
    app.state.service = studio.service
    return app
