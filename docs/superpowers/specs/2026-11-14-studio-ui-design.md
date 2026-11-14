# DSP Studio UI redesign: design

Items marked **(call)** were decided by the author of this document, not by
the user, and are open to correction.

## Intent

The current web UI (`ui.py`, Gradio) does not follow the layout of any DSP
editor its user knows, so it is hard to make changes with. The channel list
is a sidebar, the first thing shown is a stack of magnitude, phase and
group-delay plots, the EQ area is a table of fifteen mostly disabled bands,
there is a lot of empty space, and the font is disliked. The user wants a
compact, conventional DSP tuning screen.

The agreed look is the mockup at
https://claude.ai/artifact/DScmrm9iEGu97bvFuraQyt (source:
the design canvas "DSP Studio redesign", artboard "Tune screen, compact").
References the user named: miniDSP's plugins and, more so, Dayton Audio's
AXIS-8 software (one dense screen, full-width plot, every channel's basics
visible at once, colour-coded link groups).

## The Tune screen, top to bottom

1. **Top bar** (one slim row): app name; connection chip (model, `USB` or
   IP, state) that opens connect / disconnect / offline demo; tabs; the
   selected channel's latency estimate ("Mid L latency 4.39 ms"); Live
   toggle; count of unapplied changes; Revert; Apply.
2. **Channel strips**: all 4 inputs and 8 outputs in one row. Each strip has
   - its fixed label (`In A`..`In D`, `Out 1`..`Out 8`), which selects the
     channel when clicked;
   - an editable nickname;
   - gain (dB) and delay (ms) fields;
   - polarity and mute buttons;
   - a link button showing the strip's link group, if any.
3. **Selected-channel bar**: nickname and label; the selected channel's link
   group, its members and tick boxes for which sections the link ties; plot
   options (Phase, Group delay, Each filter, On device, Measured).
4. **Plot**, full width: magnitude by default. Phase and group delay are
   optional extra panels under it, off by default. The draft curve is drawn
   over the last device readback. There is a draggable handle for each
   enabled crossover and each EQ band in use, and none for unused bands.
5. **Filter cards** in one wrapping row: High-pass, Low-pass (outputs only;
   inputs have no crossover), then one card per EQ band in use, then
   "Add band" with an `n of N used` count. At the right, a small stack for
   Limiter, Compressor and FIR. (The mockup also shows "Copy channel…" there;
   it is not built, see the calls below.)

The plot is never placed beside the controls. Density follows the mockup:
controls about 24 px tall, 4 px gaps, 12-13 px text, dark theme only.

## Behaviour the user decided

- **Immediate controls.** Gain, polarity and mute are written to the device
  as soon as they change, without Apply. Everything else (delay, crossover,
  EQ, the rest) stays a draft until Apply, unless Live is on.
- **Lasting links.** Linking channels ties them until unlinked: a later edit
  to a tied section on one member is made on every member.
- **Nicknames** are stored by the app on this computer, per unit. They are
  not written to the device (its channel-name write format is unknown and
  the comparable program-name write corrupts data; see docs/PROTOCOL.md).
- **Build approach.** A hand-built HTML/CSS/JS front end on a small local
  web server, reusing the existing engine. See Approaches.

## Behaviour decided by the author (call)

- **Links.**
  - A link group is app-side state: a letter (A, B, …), a colour, member
    channels and the set of tied sections. Inputs link only with inputs,
    outputs with outputs.
  - Sections that can be tied: crossover, EQ, delay, limiter. A new group
    ties crossover and EQ. Gain, mute and polarity are never tied (owner's
    decision after the first demo, for safety): each channel's level stays
    its own. A stored group that still lists gain loads with it stripped.
  - When a channel joins a group, its tied sections are set to the group's
    existing values as ordinary pending edits; joining never writes to the
    unit at once. The joining channel never overwrites the group.
  - If a readback shows members' tied sections differ (changed outside the
    app), the group is marked out of sync and offers "copy from <member>".
    Nothing is rewritten silently.
- **Immediate writes** always send the value the user asked for, even if
  the unit's value was changed elsewhere since the last readback: mute is
  the control reached for in a hurry and must never be refused, and gain and
  polarity follow the same rule so the three behave alike. There is no
  conflict check for them. They are still bounds-checked, refused on a
  locked unit, and verified by readback. On failure the control shows the
  device's value and an error. Other pending edits are untouched. A typed
  strip gain is sent only on Enter; leaving the field or Escape discards it.
  In offline demo they are simulated.
- **No channel copy on the Tune screen.** Links cover "make these channels
  the same". The device's own whole-channel copy (which also copies gain,
  mute and delay, writes at once and refuses to run with drafts pending)
  stays in `--classic` only.
- **`--classic` is a separate launch**, not a second screen in the same
  process. Until phases 2 and 3 land, reaching the mixer, programs, presets
  or measurements means stopping the new UI and starting the old one, which
  drops unapplied drafts.
- **Bands "in use".** An EQ band has a card and a handle when it is enabled,
  or has non-zero gain, or is not a parametric band. "Add band" enables the
  first band not in use. Removing a band disables it and resets it to a
  0 dB parametric band. A configured but disabled band keeps a dimmed card.
- **Plot editing.** Dragging a handle sideways changes frequency and, for
  bands with gain, up and down changes gain; the scroll wheel over a handle
  changes Q. Curves come from the existing `response.py` models on the
  server (they include device-specific filter shapes); the browser only
  draws them.
- **App-side storage.** One JSON file under `~/.config/sinbosen-la548/`,
  keyed by USB serial or, for a network unit, its address. It holds
  nicknames and link groups.
- **Launch.** `sinbosen-la548-ui` starts the new UI. `--classic` starts the
  Gradio UI, which stays until every screen is ported. The server binds to
  127.0.0.1 only.
- **Stack.** Starlette and uvicorn for the server (already installed as
  dependencies of the `ui` extra's Gradio); vanilla JS modules and CSS with
  no build step, shipped inside the package; Inter bundled as a font file so
  the UI works offline. Plots are drawn by the app's own SVG code; Plotly is
  not used by the new UI.

## Phases

Each phase is its own plan and leaves a working app.

1. **Tune screen.** Everything in "The Tune screen" except the Measured
   overlay and the Limiter / Compressor / FIR editors. Connect, demo, Apply,
   Revert, Live, immediate controls, nicknames, links, plot with dragging,
   crossover and EQ cards. The Limiter / Compressor / FIR rows
   show on/off state only; editing them stays in `--classic`. Only the Tune
   tab is shown.
   After phase 1 lands, **REW import** is added to the Tune screen: an
   "Import from REW" action on the selected channel that loads a Room EQ
   Wizard filter export into its EQ bands as ordinary pending edits, shown on
   the plot before Apply. The parser and band mapping are built separately
   (`rew.py`) and do not depend on the UI.
2. **Mixer tab**: the 4-by-8 routing matrix, using nicknames.
3. **Measurements and Programs tabs**, the Measured overlay, alignment
   tools, presets, device programs, backups, and editors for the remaining
   blocks (limiter, compressor, FIR, dynamic EQ, expander, input source,
   frequency shifter). Then remove Gradio and `--classic`.

## Goals

- A change the user makes most often (level, mute, polarity, a crossover or
  EQ move on one or several channels) takes one or two interactions and no
  scrolling on a 1440 x 900 window.
- Works identically for a unit on UDP or USB, and in offline demo.
- Every Apply and Live write keeps today's guarantees: value bounds, lock
  check, conflict check against a fresh readback, readback verification.
  Immediate controls keep all but the conflict check (see the calls).

## Non-goals

- Touch or phone layouts. Light theme.
- Writing nicknames to the device.
- Level meters on the strips (the keepalive reply's meter payload is not
  understood well enough).
- Multi-user use. One browser tunes one unit.

## Anti-goals

- Hiding which changes have and have not reached the device. Unapplied
  fields are outlined; immediate controls never appear in the unapplied
  count.
- A link that silently overwrites a channel's settings.

## Constraints

- The engine stays as it is: `service.DSPService` (sessions, drafts,
  `stage` / `apply` / `revert` / `refresh` / Live), `response.py`,
  `tuning.py`, `presets.py`, `traces.py`. The new server is another caller
  of it. `DSPService` has no way yet to apply a chosen subset of controls
  while leaving other drafts pending; immediate controls need that added.
- One server-owned client per unit; all device operations stay serialized.
- Base install stays dependency-free; the UI remains an optional extra.
- FIR coefficient restore is unavailable over USB.

## Approaches

1. **Hand-built front end on the existing engine (chosen by the user).**
   Exact control over density and layout, immediate controls are simple, and
   interactions do not round-trip a whole component tree. Most work: every
   screen is rebuilt, hence the phases.
2. **Restyle the Gradio app.** Fast to start and keeps all features working,
   but twelve compact strips, per-band cards and a draggable full-width plot
   fight Gradio's component model, and it stays slow.
3. **Native desktop app.** Best feel, but a new toolchain and packaging for
   something that works in a browser tab.

## Left to the builder

The HTTP API shape, front-end module structure, how drag previews are
throttled, the storage file's schema, link colours beyond the first two,
and the test split between Python API tests and browser tests (the repo has
an opt-in Playwright group).
