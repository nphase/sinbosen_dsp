# DSP Studio UI, Phase 1 (Tune screen) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Gradio tuning screen with the compact Tune screen from the approved mockup, served by a small local web server on top of the existing engine.

**Architecture:** `DSPService` stays the single owner of the device and of drafts; it gains one method for immediate writes. Three new pure-Python modules sit beside it: an app-side store (nicknames, link groups), link logic, and a JSON view-model builder that also computes plot curves with `response.py`. A Starlette app exposes these as a small JSON API and serves a static, build-free front end (`web/`), which only renders the view model and posts user actions.

**Tech Stack:** Python 3.12, Starlette + uvicorn (already present through the `ui` extra), numpy/scipy via `response.py`, vanilla JS ES modules + CSS, pytest, Starlette `TestClient`, the repo's opt-in Playwright group.

**Spec:** `docs/superpowers/specs/2026-11-14-studio-ui-design.md`. Mockup source: `docs/superpowers/specs/assets/studio-tune-mockup.dc.html` (copied in by Task 6; open it for exact colours, sizes and markup).

## Global Constraints

- Immediate controls are exactly the blocks `gain`, `polarity`, `mute`. They always write the requested value (no conflict check), are bounds-checked, refused on a locked unit, verified by readback, and never counted as unapplied.
- Everything else is a draft until Apply, unless Live is on. Apply/Live keep today's guarantees unchanged.
- Links: inputs link only with inputs, outputs with outputs. Tie-able sections are `crossover`, `eq`, `gain`, `delay`, `limiter`; a new group ties `crossover` and `eq`. A joining channel takes the group's values; nothing is rewritten silently when members differ.
- A band is "in use" when `enabled or gain_db != 0 or type != 0`. Removing a band sets `enabled=False, gain_db=0.0, type=0, slope=0` and keeps frequency and Q.
- Nicknames and links live in one JSON file under `~/.config/sinbosen-la548/`, keyed by `usb:<serial>` or the network address. Nothing is written to the device for them.
- The server binds `127.0.0.1` by default. No network fetches from the page: fonts, scripts and styles are served from the package.
- Dark theme only; density as in the mockup (controls about 24 px tall, 4 px gaps, 12-13 px text).
- Base install stays dependency-free; everything here is under the `ui` extra. The Gradio UI keeps working behind `--classic`.
- Never run anything against the attached unit except where Task 9 says so. Commits only when the user asks.

## Review Focus

- A failed immediate write (device unplugged mid-click): the control shows the device's last known value, an error is visible, and other pending edits are still pending. (Task 1)
- A corrupt or hand-edited store file: the app starts with empty nicknames and links, keeps the bad file aside, and says so; it never crashes on launch. (Task 2)
- A link whose members differ on connect: flagged out of sync, and an edit to a tied section still propagates only the edited control, not a wholesale overwrite. (Task 3)
- Input channels: no crossover cards, 7 bands, no limiter row; linking an input and an output is refused. (Tasks 3, 4)
- All 15 bands in use: "Add band" is disabled with `15 of 15 used`, not an error dialog. (Tasks 4, 7)

---

### Task 1: Immediate writes in `DSPService`

**Files:** Modify `src/sinbosen_la548/service.py`; Test `tests/test_service.py` (reuse its `rig` fixture and `Device` fake).

**Interfaces (produces):**
- `IMMEDIATE_BLOCKS: frozenset[str] = frozenset({"gain", "polarity", "mute"})`
- `DSPService.write_now(self, token: str, values: dict[ControlKey, object]) -> ApplyResult`

Behaviour: reject any key whose block is not in `IMMEDIATE_BLOCKS` with `ValueError` before any device call. Normalize with `normalize_control`. Demo: set the keys in both `baseline` and `draft`. Online: lock check; write each key with `_write` (skipping none: no conflict check); `read_settings()`; `baseline = readback`; `draft` = readback with every *other* previously pending change re-applied; keys whose readback differs go in `errors` and take the readback value in the draft. A write exception stops the loop, still attempts the readback, and reports the error. Offline (not demo, not connected): `ConnectionError`, state unchanged. Never changes `state.live`; bumps `revision`.

- [ ] **Step 1: failing tests**

```python
def test_write_now_writes_without_touching_other_pending_edits(rig): ...
    # stage delay on output 2 -> write_now mute on output 0 -> device got exactly one set_mute;
    # view.pending is still just the delay; view.baseline shows the mute
def test_write_now_rejects_non_immediate_blocks_before_any_device_call(rig): ...   # delay -> ValueError, no calls
def test_write_now_writes_even_when_the_device_value_changed_elsewhere(rig): ...   # no conflict refusal
def test_write_now_refuses_a_locked_device(rig): ...                               # PermissionError, no write
def test_write_now_failure_shows_the_device_value_and_keeps_other_drafts(rig): ... # set_mute raises
def test_write_now_readback_mismatch_is_reported(rig): ...
def test_write_now_in_demo_changes_baseline_and_draft(rig): ...
def test_write_now_out_of_range_gain_is_refused(rig): ...                          # +13 dB -> ValueError, no write
```

- [ ] **Step 2:** `uv run --extra ui pytest tests/test_service.py -q -k write_now`; expect failures (`AttributeError: write_now`).
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run --extra ui pytest -q && uv run --extra ui --extra usb mypy && uv run ruff check`; all green.

### Task 2: App-side store

**Files:** Create `src/sinbosen_la548/studio_store.py`; Test `tests/test_studio_store.py`.

**Interfaces (produces):**
- `SECTIONS: tuple[str, ...] = ("crossover", "eq", "gain", "delay", "limiter")`
- `@dataclass(frozen=True) class LinkGroup: letter: str; output: bool; members: tuple[int, ...]; sections: frozenset[str]`
- `@dataclass(frozen=True) class DeviceNotes: nicknames: dict[tuple[bool, int], str]; links: tuple[LinkGroup, ...]` (key = `(output, channel)`)
- `default_path() -> Path` (`~/.config/sinbosen-la548/studio.json`)
- `class StudioStore(path: Path)` with `load(device_key: str) -> DeviceNotes`, `save(device_key: str, notes: DeviceNotes) -> None`, and `warning: str | None` (set when a bad file was set aside).

Atomic save (temp file + `os.replace`). Unknown device key -> empty notes. Unreadable or invalid file -> renamed to `studio.json.bad`, `warning` set, empty notes. Nicknames trimmed, max 24 characters, empty string removes the entry. Validation on load drops groups with fewer than 2 members, unknown sections, or a channel in two groups.

- [ ] **Step 1: failing tests** — round trip per device key; two devices do not share notes; corrupt JSON is set aside with a warning; invalid groups dropped; nickname trimming and removal; save creates the directory.
- [ ] **Step 2:** run, expect import failure. **Step 3:** implement. **Step 4:** full suite, mypy, ruff.

### Task 3: Link logic

**Files:** Create `src/sinbosen_la548/links.py`; Test `tests/test_links.py` (settings from `ui.demo_settings()`).

**Interfaces:** consumes `LinkGroup`, `SECTIONS`, `service.ControlKey`, `service.controls`. Produces:
- `section_of(key: ControlKey) -> str | None` — `hp`/`lp` -> `crossover`; `eq` -> `eq`; `gain`, `delay`, `limiter` -> themselves; anything else `None`.
- `expand(groups: Sequence[LinkGroup], settings: DeviceSettings, edits: dict[ControlKey, object]) -> dict[ControlKey, object]` — each edit plus the same value for the same block/index on every other member of a group that ties its section. Keys absent from `controls(settings)` are skipped.
- `join(group: LinkGroup, channel: int, settings: DeviceSettings) -> dict[ControlKey, object]` — edits that set `channel`'s tied sections to the values of `group.members[0]`.
- `in_sync(group: LinkGroup, settings: DeviceSettings) -> bool` — compares tied sections across members with `service._semantic`-equal values (expose a public `service.same_value(a, b)` wrapper for this).
- `with_member(groups, output, channel, letter | None) -> tuple[LinkGroup, ...]` — add to an existing group, start a new one (first free letter, sections `{"crossover", "eq"}`) when `letter` is `None`; raises `ValueError` for a group of the other kind. `without_member(groups, output, channel)` removes it and drops groups left with one member. `with_sections(groups, letter, sections)`.

- [ ] **Step 1: failing tests** — an EQ edit on Out 3 in group A (Out 3, Out 4; eq tied) yields the same band on Out 4; a gain edit does not propagate unless `gain` is tied; `join` copies hp, lp and all 15 bands and nothing else; input groups never produce crossover or limiter keys; `in_sync` false after changing one member's hp; an out-of-sync group still propagates only the edited control; mixing input and output raises; leaving a two-member group deletes it.
- [ ] **Steps 2-4:** as Task 2.

### Task 4: View model

**Files:** Create `src/sinbosen_la548/studio_view.py`; Test `tests/test_studio_view.py`.

**Interfaces:** consumes `ServiceView`, `DeviceNotes`, `links.in_sync`, `response.channel_response`, `response.frequency_grid`, `tuning.EQ_TYPES`, `tuning.XOVER_TYPES`. Produces:
- `band_in_use(band: EQBandSettings) -> bool`
- `LINK_COLOURS: dict[str, str]` (`A` `#C58BFF`, `B` `#F2A33A`, then builder's choice, each differing in lightness)
- `build_state(view: ServiceView, notes: DeviceNotes, *, selected: tuple[bool, int], device: str | None, phase: bool = False, group_delay: bool = False, each_filter: bool = True, on_device: bool = True, live_errors: str = "") -> dict[str, Any]` returning JSON-ready data:
  - `status`: `connected`, `demo`, `live`, `device`, `message`, `revision`, `pending_count` (`len(view.pending)`; immediate blocks never appear in it because `write_now` moves the baseline), `warning`.
  - `channels`: 12 entries in order In A-D, Out 1-8: `id` (`"in:0"`/`"out:2"`), `label`, `nickname`, `gain_db`, `delay_ms`, `muted`, `inverted`, `delay_pending`, `link` (`letter`, `colour`, `in_sync`) or `null`, `selected`.
  - `selected`: `id`, `label`, `nickname`, `eq_slots`, `eq_used`, `link` (letter, member labels/nicknames, `sections` as `{name: bool}` for the sections that exist on this kind of channel), `filters`: list of cards `{key, kind: "hp"|"lp"|"eq", index, title, enabled, pending, fields}` where `fields` carry value, bounds, options and a per-field `pending` flag. `hp`/`lp` only for outputs. EQ cards only for bands in use, in band order. `more`: `[{name, state}]` for limiter / compressor / FIR where the block exists.
  - `plot`: `freq` (log-spaced, at most 400 points, 20 Hz-20 kHz), `draft_db`, `device_db` (when `on_device`), `filters` (`[{key, db}]` when `each_filter`), `phase_deg` / `group_delay_ms` when asked, and `handles`: `[{key, label, freq_hz, db, has_gain, pending}]` for enabled crossovers and bands in use. Non-finite values become `null`.

- [ ] **Step 1: failing tests** — demo state has 12 channels with the right labels; selecting an input gives no hp/lp cards, `eq_slots == 7` and no limiter row; only in-use bands get cards and handles; a staged EQ change marks that card and field pending and raises `pending_count`; `draft_db` differs from `device_db` only after a staged change; phase and group delay are absent unless asked; nickname and link data pass through; the whole result survives `json.dumps(..., allow_nan=False)`.
- [ ] **Steps 2-4:** as Task 2.

### Task 5: HTTP API

**Files:** Create `src/sinbosen_la548/studio_app.py`; Test `tests/test_studio_app.py` (Starlette `TestClient`, demo mode, temp store path).

**Interfaces:** `create_studio_app(service: DSPService | None = None, *, store: StudioStore | None = None) -> Starlette`. One server-side session (single-user tool): the app holds one service token, the selected channel, plot options, and the current device key. A background task calls `service.flush_live(token)` every 0.25 s while Live is on, as `ui.py` does today. `service.close()` runs on shutdown. Errors return `{"error": "<message>"}` with status 400 (`ValueError`, `PermissionError`) or 503 (`ConnectionError`, `TimeoutError`, `OSError`, `RuntimeError`), and never a traceback. Every successful POST returns the fresh state from `build_state`.

Routes (JSON bodies):
- `GET /api/state`
- `POST /api/connect {address}`, `/api/disconnect`, `/api/demo`, `/api/refresh`, `/api/apply`, `/api/revert`, `/api/live {enabled}`
- `POST /api/select {id}`, `/api/plot {phase?, group_delay?, each_filter?, on_device?}`
- `POST /api/now {id, block, value}` -> `links.expand` then `service.write_now`
- `POST /api/edit {id, kind: "delay"|"hp"|"lp"|"eq", index?, field, value}` -> build the new block the way `TuningController.eq` / `.crossover` do (reuse them where they fit), then `links.expand`, then `service.stage_many`
- `POST /api/band/add {id}` (first band not in use: `enabled=True`, parametric, 0 dB, Q 1.0, 1000 Hz) and `/api/band/remove {id, index}`
- `POST /api/nickname {id, name}`
- `POST /api/link {id, letter | null}` (join or create; stages `links.join` edits), `/api/unlink {id}`, `/api/link/sections {letter, sections}`, `/api/link/sync {letter, from}` (stage that member's tied sections onto the others)
- Static: `/` -> `web/index.html`, `/static/*` -> `web/`.

The device key is the address as connected; for `usb` it is the `usb:<serial>` the link reports (add a read-only `Client.address` property returning the transport's resolved address for this).

- [ ] **Step 1: failing tests** — state after start is the demo; `/api/edit` on an EQ field shows up as pending and `/api/revert` clears it; `/api/now` mute changes the channel with `pending_count == 0`; a linked EQ edit changes both members; `/api/link` across input and output returns 400 with a message; `/api/band/add` then `/remove` round-trips `eq_used`; nickname persists across a new app instance with the same store; a service `ConnectionError` becomes a 503 JSON error; `/` serves HTML.
- [ ] **Steps 2-4:** as Task 2.

### Task 6: Front end shell, strips and cards

**Files:** Create `src/sinbosen_la548/web/index.html`, `web/studio.css`, `web/studio.js`, `web/api.js`, `web/fonts/InterVariable.woff2` (from the Inter release at github.com/rsms/inter, with its `LICENSE.txt` beside it), `docs/superpowers/specs/assets/studio-tune-mockup.dc.html` (copy of the mockup source from `<scratchpad>/design/project/Main.dc.html`, reference only). Confirm the wheel includes `web/` (build with `uv build` and list the archive); add package-data configuration if it does not.

`studio.js` renders the whole screen from one state object and re-renders on every response; it keeps no model of its own beyond the field being typed in. Structure and inline sizes follow the mockup section by section: top bar, 12-column strip grid, selected-channel bar, filter cards, "more" stack. Behaviour:
- Number fields commit on Enter or blur; Escape restores. Invalid entries keep focus with a red outline and the server's message beneath.
- Strip gain / polarity / mute post to `/api/now`; delay and all card fields post to `/api/edit`. Pending fields get the orange outline.
- The strip label selects the channel; the nickname field posts on blur.
- The link button opens a small menu: existing groups of the same kind, "New link", "Unlink". Section tick boxes post `/api/link/sections`. An out-of-sync group shows a warning chip with "Copy from…" choices.
- The connection chip opens a popover with an address field (default `usb`), Connect, Disconnect and Offline demo.
- Errors appear in a dismissible bar under the top bar. State is re-fetched every second to track the link.
- "Add band" is disabled at the slot limit.

- [ ] **Step 1: failing e2e tests** in `tests/e2e/test_studio_e2e.py` (opt-in `--run-e2e`, server started in demo mode on a free port): strips show 12 channels; clicking `Out 3`'s label selects it and shows its cards; typing a new high-pass frequency shows `1 unapplied` and an outlined field, and Revert clears it; clicking mute toggles it with no unapplied count; a nickname survives reload; no request leaves `127.0.0.1` (assert on Playwright's request log).
- [ ] **Step 2:** run with `uv run --group e2e --extra ui pytest tests/e2e/test_studio_e2e.py --run-e2e -q`; expect failures. **Step 3:** implement. **Step 4:** e2e green, then the full suite, mypy, ruff.

### Task 7: Plot and dragging

**Files:** Create `src/sinbosen_la548/web/plot.js`; Modify `web/studio.js`, `web/studio.css`; Test `tests/e2e/test_studio_e2e.py`.

SVG plot drawn from `state.plot`: log frequency axis 20 Hz-20 kHz with the mockup's gridlines and labels, magnitude range +10 to -40 dB, draft curve in the accent colour over the device curve in grey, dashed per-filter curves, legend inside the plot. Phase and group-delay panels appear under it only when their options are ticked. Handles: pointer drag maps x to frequency (rounded as `dragging.drag_frequency` does; port that rule) and, when `has_gain`, y to gain in 0.1 dB steps; wheel over a handle changes Q by 5% per notch. Drags post `/api/edit` at most every 40 ms and once more on release; the curve is redrawn from each response. Keyboard: a focused handle moves with arrow keys.

- [ ] **Step 1: failing e2e tests** — the plot has one handle per enabled crossover and in-use band for the selected channel; dragging an EQ handle left lowers that card's Hz and marks it pending; ticking Phase adds the phase panel and unticking removes it; "Add band" adds a card and a handle.
- [ ] **Steps 2-4:** as Task 6.

### Task 8: Launch, packaging, docs

**Files:** Modify `src/sinbosen_la548/ui.py` (`main`), `pyproject.toml` (list `starlette` and `uvicorn` in the `ui` extra), `README.md`, `docs/UI_GUIDE.md`; Test `tests/test_ui.py`.

`sinbosen-la548-ui [--host 127.0.0.1] [--port 7860] [--classic] [--demo]`: without `--classic`, run `create_studio_app()` under uvicorn and print the URL; with it, today's Gradio launch unchanged. Gradio is imported only on the `--classic` path.

- [ ] **Step 1: failing tests** — `main(["--classic"])` calls the Gradio launcher and `main([])` calls uvicorn with the studio app (both patched); importing `ui` without touching `--classic` does not import `gradio`.
- [ ] **Step 2-4:** implement; update README and UI_GUIDE (new screen first, classic described as the fallback for Mixer, programs, presets, measurements and the remaining editors); full suite, mypy, ruff.

### Task 9: See it working

- [ ] **Step 1:** Start `uv run --extra ui sinbosen-la548-ui --demo`, load it in a 1440 x 900 browser via Playwright, save a screenshot to the scratchpad, and compare it against the mockup section by section. Fix differences in layout, density and colour, re-running Tasks 6-7's tests.
- [ ] **Step 2 (hardware, read-only plus what the user already approved):** with the user's UI closed, connect to `usb`, confirm the strips show the unit's real values and that Disconnect releases the device. Do not change any control unless the user says to.
- [ ] **Step 3:** Report what was and was not exercised on hardware.
