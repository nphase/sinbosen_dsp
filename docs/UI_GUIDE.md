# LA548 DSP Studio

`sinbosen-la548-ui` starts the new Tune screen. The older Gradio UI is still
there behind `--classic` and is documented under "Classic UI" below, after the
new screen.

The new screen has only been exercised in offline demo and against fakes in
tests. It has not been checked on a real unit.

## Tune screen

### Launch

```sh
uv sync --extra ui
uv run --extra ui sinbosen-la548-ui
```

Open the URL it prints, <http://127.0.0.1:7860> by default. `--port` changes
the port. `--demo` is accepted but the app always starts in offline demo, using
the bundled fixture, with Out 1 selected. Ctrl-C stops it and closes the device
connection.

The server binds to `127.0.0.1`. It only accepts requests whose Host is a
loopback name (or the one given to `--host`), refuses requests carrying another
site's Origin, requires POSTs to be JSON, and sends no CORS headers, so a page
on another site cannot drive it. Fonts, scripts and styles are served from the
package; the page fetches nothing from the network. `--host` binds another
address, which makes the UI reachable from the network; it has no
authentication and can change the device's output levels, so the launcher
prints a warning.

### Layout

- **Top bar.** The connection chip shows `USB` or the unit's address and its
  state ("Offline demo", "Not connected"); clicking it opens the connect box
  (an IP address, `usb` or `usb:<serial>`) with **Connect**, **Refresh**,
  **Disconnect** and **Offline demo**. Then the Tune tab, the selected
  channel's latency (see "Latency" below), the **Live** toggle, the count of
  unapplied changes, **Revert**, **Apply** and **Save** (see "Saving" below).
- **Channel strips.** All 4 inputs (`In A` to `In D`) and 8 outputs (`Out 1` to
  `Out 8`) in one row. Each strip has a header with its label and nickname, gain
  (dB), delay (ms), polarity (Ø), mute (M) and a link button. Clicking the
  header (its label or nickname) selects that channel.
- **Selected-channel bar.** The selected channel's nickname and label, its link
  group with the members and the sections it ties, and the plot options.
- **Plot**, full width.
- **Filter cards** for the selected channel, wrapping in one row.

### Immediate controls and drafts

Gain, polarity and mute are written to the unit as soon as you change them. A
strip's gain is sent when you press **Enter**: while a typed value has not been
sent, the field is outlined in blue and its tooltip says "Press Enter to send".
Leaving the field (clicking elsewhere or Tab) or pressing Escape discards the
typed value and shows the unit's gain again, so a half-typed "-1" on the way to
"-12" never reaches the unit. Delay and the filter-card fields are drafts and
are kept when you leave the field. Gain, polarity and mute always send the value you asked for, even if the unit's value was changed
elsewhere since the last readback, so mute is never refused over a conflict.
They are still bounds-checked, refused on a locked unit and verified by readback;
if that fails the control shows the device's value and an error. They never
count as unapplied.

Everything else (delay, crossovers, EQ bands) is a draft. Edited fields are
outlined and counted in the top bar. **Revert** discards the drafts. **Apply**
keeps the guarantees of the classic UI: values are bounds-checked, the lock state
is checked, the unit is read back and a conflicting outside change is reported
rather than overwritten, only changed blocks are written, and the readback is
verified. With **Live** on, drafts are written as you make them instead, and
Live needs a connected unit.

**Refresh** in the connection box reads the unit again and keeps your drafts. If
a control you have a draft for was changed on the unit in the meantime, it
reports the conflict and changes nothing. **Disconnect** and **Offline demo**
discard the drafts; with unapplied edits the box asks before doing either.

### Saving

Writes, Apply and Live change only the unit's live settings. When the unit is
switched off it forgets them and starts again from its current program slot
(see "A power cycle discards live changes" in [PROTOCOL.md](PROTOCOL.md)). To
keep a tuning, save it to a program slot:

1. Click **Save** in the top bar. The popover lists the unit's 30 program slots
   by number and stored name (an empty name shows as a dim "empty name"). The
   unit's current slot is marked "current" and preselected; if the unit has not
   reported it, nothing is preselected.
2. Choose a slot and press the button that names it, for example **Overwrite
   slot 3 (user3)**.
3. The popover asks again: "Slot 3 (user3) will be replaced with the unit's
   current live settings. This cannot be undone." **Cancel** has the focus, so
   Enter does not confirm; press **Overwrite** to save (it can be pressed a
   moment after the step appears, so the click that opened it cannot confirm
   it too). Escape closes the popover without saving.

After a save the popover closes, the status line says "Saved to slot 3
(user3).", and that slot is the unit's current one, the one it starts in.

The unit saves what is live on it, not your drafts. With unapplied edits the
popover says so and the button reads **Apply and overwrite slot 3 (user3)**. The
second step then says "Your 3 unapplied edits will be applied to the unit first.
Then slot 3 (user3) will be replaced with the unit's live settings. This cannot
be undone." and its button is **Apply and overwrite**. It runs Apply first and
saves only if Apply reports no errors or conflicts and the readback verified;
otherwise nothing is saved and Apply's result is shown. If Apply wrote your
edits but the save then failed, the error bar says "Your edits were applied, but
saving to slot 3 failed: ..." and the edits are no longer counted as unapplied.
If the slot's name on the unit changed since the popover read it, nothing is
saved and the error bar says so.
Saving switches Live off, and the confirmation says so when Live is on. A
locked or disconnected unit, or a failed write or readback, is reported in the
error bar, and nothing is reported as saved. In the offline demo the slots are
named user1 to user30 and saving is simulated. Loading and renaming slots are
not in the new UI yet.

### Links

The link button on a strip joins its channel to a group (A, B, ...), each with a
colour. Inputs link only with inputs and outputs only with outputs. A group
ties some of the sections crossover, EQ, delay and limiter; a new group ties
crossover and EQ, and the tick boxes in the selected-channel bar change that.
While tied, an edit to one of those sections on one member is made on every
member, as a draft: only the fields you edited (a frequency, an enable tick, a
gain) are set on the other members, each on its own block, so members that differ
keep their other values. A field a member's own block would refuse (a gain on an
allpass band) is skipped for that member, and a member whose stored values the
unit would refuse is left as it is and shows as out of sync. Actions that set a whole block (Add band, removing a
band, REW import, joining and Copy from) set the whole block on every member. Links never tie gain, mute or polarity: each channel's level
is its own, and joining a group or copying from a member never writes anything
to the unit at once. A group stored by an earlier version that tied gain keeps
its other sections.

A channel that joins takes the group's current values for the tied sections as
ordinary edits; it never overwrites the group. If a readback shows members' tied
sections differ (for example after a change made elsewhere) the group is marked
out of sync and offers "Copy from ..." a member. Nothing is rewritten silently.
**Unlink** removes the channel from its group.

### Plot

The plot shows the magnitude of the selected channel, with the draft curve drawn
over the last readback. The options in the selected-channel bar are **Phase**
and **Group delay** (extra panels under the magnitude, off by default), **Each
filter** and **On device**. The curves are computed on the server from the same
filter models as the classic UI (see [FILTER_MODELS.md](FILTER_MODELS.md)); the
page only draws them. The same limits apply: they model the unit's processing
only, not speakers, room or amplifier.

Each enabled crossover and each EQ band in use has a handle. Drag it sideways to
change frequency. For bands with gain, drag up or down to change gain. For a
high-pass or low-pass, drag down for a steeper slope and up for a gentler one,
one 6 dB/oct step per about 18 px, stopping at 6 and 48 dB/oct; frequency and
slope can change in the same drag, and the card's slope follows. Scroll the
wheel, or two fingers on a trackpad, over a handle to change Q, by about 10%
per 100 px of scroll; one hard flick moves it at most about 22%, and the page
does not scroll under the handle. With a handle focused, the arrow keys move
it: left and right change frequency by 1/24 octave, up and down change gain
by 0.1 dB (1 dB with Shift), or step a crossover's slope. The values appear in
the filter card as a draft, to be applied like any other.

A band that is in use but disabled keeps a greyed, dashed handle with its band
number, where the band would put the curve if it were on: at its frequency, and
the curve's level there plus its own gain. It adds nothing to the curve, and
dragging it edits the disabled band. Unused bands have no handle.

### Filter cards

High-pass and low-pass cards come first (outputs only; inputs have no crossover),
then one card per EQ band in use, with a checkbox to enable it, fields for
frequency, gain, Q, type and slope as the band allows, and an **×** that
removes the band. A band is in use when it is enabled, has non-zero gain or is not
a parametric band; a configured but disabled band keeps a dimmed card. Removing
a band disables it and resets it to a 0 dB parametric band, keeping its
frequency and Q. **Add band** enables the first band not in use and shows
`n of N used`.

**Import from REW...** on the selected-channel bar is described under "Importing
REW filters" below.

At the right of an output's cards sit the **Limiter** and **Compressor** cards,
laid out like the filter cards: a tick in the title enables the block and each
field is a draft, shown unapplied until **Apply** (or sent by **Live**) and
undone by **Revert**. The limiter has Threshold (dB), Attack (ms), Release (ms)
and Detection (Average or Peak); the compressor has Threshold, Ratio (shown as
`4.0 : 1`), Attack and Release. The ranges are the unit's: a value outside one
is refused with the allowed range, and an accepted value is shown as the unit
will store it (thresholds in 0.5 dB steps, the ratio in 0.1 steps, release in
whole ms, attack in 0.1 ms up to 1 ms and whole ms beyond). A disabled
compressor on a factory unit stores values the unit would refuse (a ratio of 0
and a threshold of +340 dB); the card shows them as stored, and enabling or
editing it moves each stored value that is outside its range to the nearest
allowed one and shows those as unapplied; a factory limiter, which also stores
a +340 dB threshold, is edited the same way (a limiter that has never been used
shows a threshold of +20 dB when first enabled, which does not limit: set the
threshold before Apply). A limiter write sends the unit's unused ratio slot as 0. If the unit's stored bytes for a block cannot be
preserved by a write (a compressor with a non-zero unused detection byte), its
card is read-only and says so. A limiter edit follows a link
group that ties **Limiter**. The compressor is never linked. Neither block is
drawn on the plot, as their effect depends on the signal level, and neither
changes the latency readout. The FIR stays a row below, showing On or Off (an
input has only the FIR, and no limiter or compressor).

### Importing REW filters

**Import from REW...** (selected-channel bar, inputs and outputs) loads a Room
EQ Wizard filter file into the selected channel. In REW use the **Generic**
equaliser and File > Export > "Export filter settings as text", then choose
that `.txt` file. The file is read in the browser and sent only to this local
server. **This overrides the channel's current settings**, and the dialog that
opens says exactly what:

- **All EQ on the channel is replaced.** The file's filters take bands 1, 2, ...;
  every other band that is in use is reset (off, 0 dB parametric, frequency and Q
  kept), as the card's **×** does. The dialog says how many bands in use will be
  overwritten or cleared.
- **The high-pass and low-pass are replaced only if the file has them.** REW's
  `HP`/`LP` (12 dB/oct Butterworth), `HP1`/`LP1` (6 dB/oct Butterworth) and
  `HPQ`/`LPQ` with Q 0.707 (Butterworth 12 dB/oct) or Q 0.5 (Linkwitz-Riley 12
  dB/oct) go to the crossover, outputs only. A file with no HP keeps the channel's
  high-pass exactly as it is; likewise the low-pass. The dialog shows the current
  setting and what replaces it.
- **Linked channels change too.** The edits go through the channel's link group
  like a hand edit, and the dialog names each member that will change.
- A table shows what will be loaded (band, type, Hz, dB, Q, notes), and **Not
  imported** lists each filter the device cannot reproduce exactly, with the
  reason: shelves, notches, other kinds of HP/LP (for example 24 dB/oct or a Q
  that is neither 0.707 nor 0.5), a second HP or LP, an HP or LP on an input,
  and values outside the device's range. Nothing is approximated. REW's Q for a
  peaking filter is converted to the device's stored Q (shown on the band card);
  that conversion follows the documented model in docs/PROTOCOL.md and has not been
  checked by measurement on a unit.

Nothing is sent to the unit until you press **Apply**; with **Live** on, the
dialog says it will be sent straight away. **Cancel** (the default) and Escape
leave everything as it was. After **Replace EQ** (or **Replace EQ and
crossover**) the cards and plot show the drafts as unapplied, and **Revert**
undoes the import. A file that is not a REW export, was made with another
equaliser, has more filters than the channel has bands (7 on an input, 15 on an
output) or has nothing importable shows the reason and stages nothing.

### Latency

The top bar shows an estimate of the selected channel's latency, labelled with
its nickname or, without one, its label, such as "Mid L latency 4.39 ms"
(a long name is cut short before the bar would need a second line). It is
computed from the draft settings and the figures measured in
[PROTOCOL.md](PROTOCOL.md) ("Latency"). For an output it adds up the base
latency (1.732 ms, analog input to analog output with flat processing), the
output's delay, its FIR if enabled, and the delay and enabled FIR of an input
the mixer routes to it. When several inputs are routed and give different
totals it shows a range ("3.73-4.23 ms"); with none routed it shows the output's
own figure and says so. An enabled FIR adds 2.662 ms with 256 taps and 3.995 ms
with 512. For an input it shows only what that input adds. Its tooltip lists the
parts line by line and the limits: EQ and crossover delay varies with frequency
and is not included (see Group delay); the unit can be 0.667 ms slower depending
on its most recent Kaiser-window FIR write, which cannot be read back; and the
figures were measured on one LA548 from input A to output 1.

### Mixer tab

The **Mixer** tab, next to **Tune** in the top bar, shows the unit's routing
matrix: one row per input (In A-D) and one column per output (Out 1-8). Each
header shows the channel's nickname over its fixed label, or the label alone.
An output fed by no input says "no input" under its header, and a muted channel
has a small red **M** in its header. The top bar and the channel strips are the
same on both tabs; the latency readout shows only on Tune.

Each crossing is a toggle, filled when that input feeds that output; the unit
routes on or off and has no level per crossing. Clicking one is a draft like a
filter edit: it gets the orange unapplied outline, counts in the unapplied
total and reaches the unit on **Apply**, or as you edit with **Live** on. It is
never written at once, because a wrong click could send a full-range signal to
a tweeter. Links do not tie routing. Screen readers announce a crossing as, for
example, "Left to Sub L, connected".

With a crossing focused, the arrow keys move to the next one and Space or Enter
toggles it; only one crossing is in the Tab order. The Tune tab's latency
readout follows the drafted routing. The chosen tab is remembered for the
browser tab, so a reload opens the same one.

### Nicknames

The nickname is shown in the strip's header. A long one covers the fixed label
(`Out 3`) and, beyond the strip's width, is cut short with an ellipsis; the
label stays as the header's tooltip, and screen readers announce both ("Out 3,
Mid L"). Double-click the nickname, or press Enter or F2 with the header
focused, to edit it in place: Enter or leaving the field saves it, Escape
cancels. A strip without a nickname shows a dim "Name" there while the pointer
is over the header or it has focus.

### Nicknames and links are stored on this computer

Nicknames and links are not written to the device. They are kept in one JSON
file, `~/.config/sinbosen-la548/studio.json`, under a key for the unit: its
`usb:<serial>` or its network address.

### Not in the new UI yet

These need `--classic` for now:

- loading programs, presets and channel copy (saving is on the Tune screen, see
  "Saving");
- measured traces, the Measured overlay and alignment;
- FIR coefficient backup and restore;
- editing the FIR and the other advanced blocks (dynamic EQ, expander, input
  source, frequency shifter).

Start the classic UI with `uv run --extra ui sinbosen-la548-ui --classic`. It is a
separate launch, not a screen in the same process, and the two UIs cannot be open
on the same device at once: stop one before starting the other. Unapplied
drafts do not carry over.

## Classic UI (`--classic`)

The rest of this guide describes the Gradio UI, started with
`sinbosen-la548-ui --classic`.


### Install and launch (classic UI)

Requires Python 3.12 or later. From the checkout:

```sh
uv sync --extra ui
uv run --extra ui sinbosen-la548-ui --classic --demo
```

Open <http://127.0.0.1:7860>. `--port` changes the port. The classic application binds
to `127.0.0.1`, disables public sharing and Gradio analytics, and starts offline.
The optional UI extra adds Gradio, NumPy, SciPy, and Plotly; the core client has
no runtime dependencies. `--demo` documents the default behavior.

### Connect, preview, Apply, Live

Enter the DSP IP address, or `usb` for a unit on USB (LA448; needs
`uv sync --extra usb`), and **Connect**. Connection reads the lock state and
actual snapshot without changing settings. Unsupported or malformed snapshots
are refused. The current layout is DA0408X: 4 inputs, 8 outputs, 7 input EQ
bands, and 15 output EQ bands. This is a validated snapshot layout rather than
automatic firmware identification.

Choose a channel in the sidebar. Edits change a browser-specific draft and its
plots. Channel switching retains drafts. **Pending changes** shows the original
readback and draft values. **Revert draft** restores the last readback. **Refresh**
merges unrelated external changes while retaining your edits; a conflicting
external edit is reported rather than silently overwritten. Revert a conflicting
draft and refresh to load the external version before editing again.

EQ/allpass frequency and Q, crossover frequencies, and channel delay settle
after 250 ms of typing. **Enter** or leaving a field commits it immediately.
Blank or invalid values stay in the field with a red border and an explanation
below it; the response continues to use the last valid setting. Correct the
highlighted value or use **Revert draft**. Invalid numeric edits block Apply and
enabling Live, and never reach the device.

**Apply changes** validates edited controls, checks device locking, reads current
settings for conflicts, writes only changed blocks, then verifies readback. An
ACK alone is insufficient. A partial failure reports verified controls and keeps
unapplied edits pending. A missing verification readback leaves writes unverified.
Retry Apply only after resolving the reported connection or conflict issue.

**Live tuning** requires a clean draft. Apply or revert existing edits first.
Sliders submit on release; typed numeric edits settle after 250 ms without a newer
edit, while Enter or leaving the field commits explicitly. Repeated edits to a
block coalesce. Live is disabled on connection loss,
conflict, write/verification failure, program operations, and preset import.
Reconnect retains pending drafts for the same device without sending them.
Connecting to a different device requires reverting pending edits first.

One server-owned client serializes all device operations, preserving the client's
transport pacing. Connecting from another browser replaces that connection;
earlier sessions retain their drafts but must reconnect before writing. Use one
browser for tuning a device. Browser drafts expire after an hour; export a preset
to keep them. Stop the application with Ctrl-C to close its device transport.

**Offline demo** disconnects and resets that browser's DSP draft to the bundled
hardware fixture. Apply, Live, programs, and copy are simulated locally. Snapshot
download returns the original demo fixture; it is not an encoder for simulated
settings. Separate coefficient data is unavailable in demo mode.

### Crossovers and allpass

Output HP/LP cards are adjacent. Select enable, corner frequency, family, and
slope. All three families allow 6–48 dB/oct. The odd-order Linkwitz–Riley modes
use the LA548's nonstandard measured sections rather than textbook LR filters.

Select an EQ row or band, then edit enable, frequency, gain, Q, and filter type.
**Add allpass** consumes the first disabled EQ slot and initializes a second-order
allpass at 80 Hz, Q approximately 1. The actual Q is quantized through the device
codec. Allpass gain is fixed; frequency and Q control its phase transition and
group-delay peak. A full channel reports that no slot is available.

**Bypass all EQ** stages each band disabled and remembers the original enable
mask. **Restore EQ** restores that mask while retaining parameter edits. Restore
before allocating another allpass. Saved bypass masks are discarded on revert,
connect, demo reset, preset import, and successful program/copy operations.
The firmware's nonfunctional global bypass command is not used.

Sidebar controls manage gain, mute, polarity, and delay. The **Mixer** tab is
binary input-to-output routing; it has no continuous crosspoint gain.

### Response plots and export

Magnitude, phase, and group delay share a logarithmic frequency axis. Hover
shows numerical values; zoom/pan and legend visibility use Plotly controls.
Toggle individual filters, readback overlay, phase unwrap, and subwoofer zoom.
**Export response plots** downloads a ZIP containing standalone interactive HTML,
curve CSV, and the displayed model notes. The Plotly mode bar also exports an image.

The channel plot models that channel's processing. The alignment view composes
the selected input and each routed output. Neither includes loudspeaker, room,
amplifier, or the unit's fixed input-to-output latency (1.732 ms on input A to
output 1, or 2.399 ms after a Kaiser 21 dB FIR write; see
[PROTOCOL.md](PROTOCOL.md) "Latency").

Coefficients are estimates: RBJ peaking (Q-corrected against hardware), measured shelf shapes,
first- and second-order allpass/a negative-gain notch (with its measured
bandwidth switch at -6 dB), digital Butterworth, doubled Butterworth for
standard even-order Linkwitz–Riley, measured firmware-specific odd-order
Linkwitz–Riley sections, magnitude-normalized Bessel low-pass, and Bessel
high-pass sections calibrated against the LA548. See
[FILTER_MODELS.md](FILTER_MODELS.md) for measurement coverage and limits.
The models run at 96 kHz, the DSP rate confirmed on hardware (see
[PROTOCOL.md](PROTOCOL.md) "Sample rate and clock offset"). Delay phase
and pure-delay group delay are calculated analytically; per-section
continuous phase prevents long-delay/cascade unwrap errors.

Shelf Q is stored by the device but has no measured effect. Shelf slope 0
is first order and slope 1 is second order. A first-order shelf at exactly
zero gain is bypassed; even a ±0.1 dB setting introduces substantial phase
rotation. The preview includes this device behavior. Shelf frequency is a
prototype corner rather than the usual half-gain midpoint.

Phase and group delay are masked near nulls (below -120 dB relative to peak or
near numerical zero). Magnitude remains visible. Enabled unknown EQ types,
unresolved EQ slopes, unsupported LR orders, FIR, dynamic EQ, dynamics, shifting,
or unmodeled source selection mark totals incomplete and list omitted stages.
An incomplete curve predicts only the stages explicitly modeled. Exact device
coefficient agreement requires hardware comparison.

### Open Sound Meter traces

Import a single native OSM stored-trace `.osm`, or a `.txt`, `.csv`, or `.frd`
export. Native projects and live streaming are deferred. Text accepts ordered
columns: **frequency in Hz, magnitude in dB, optional phase in degrees, optional
coherence from 0 to 1**. Whitespace, comma, and semicolon delimiters are supported,
as are REW headers/comments and OSM export headers. Frequencies must increase;
DC bins are omitted from logarithmic plots. At least three positive samples are
required. `*`, empty CSV fields, `NaN`, and `null` preserve missing values as gaps.
Magnitude-only files stay magnitude-only and cannot participate in complex sums.

Native OSM uses linear transfer magnitude and phase in radians. The importer
reproduces stored display gain, delay in ms, polarity, inverse magnitude, and
ignore-coherence semantics. Stored impulse data is retained with its gain,
polarity, and time adjustments. These follow OSM's
[Stored implementation](https://github.com/psmokotnin/osm/blob/master/src/source/stored.cpp).
OSM text exports may contain adjusted magnitude but omit stored delay/polarity
from phase; prefer native `.osm` when alignment depends on those adjustments.

Rename, show/hide, associate traces with an output, or set coherence masking.
Imported samples and original file bytes remain immutable. Filters configured in
the DSP are never applied again to imported measurements.

### Alignment

Select a **common input** and two distinct outputs in **Alignment**. The electrical
comparison shows both paths, their complex sum, and B-minus-A unwrapped phase.
An unrouted path has no response. Preview alignment delay/polarity changes with
the dedicated controls, then Apply (or use already-enabled Live). Refresh the
comparison after other tuning changes.

For measured sums, select two phase-bearing traces and confirm that they share
timing and level references. Different known sample rates are refused. The sum
uses overlapping frequency samples, interpolates magnitude in log frequency and
continuous phase in linear frequency, and preserves gaps. Confirming timing does
not align traces automatically. Imported phase resolution limits interpolation
accuracy; independent capture clocks and unknown reference offsets cannot be
repaired by the importer.

### Advanced controls, FIR, presets, and programs

**Advanced** offers only blocks present on the channel: output compressor/limiter,
input expander/dynamic EQ/source/shifter, and FIR parameters on both directions.
Edit the Value column and **Preview block changes**. Boolean values are `true`
or `false`; whole-block codec validation occurs before staging. Disabled hardware
readback may contain out-of-range dynamics values: replace those with valid values
before staging that block. Unused dynamics bytes are preserved by refusing writes
when the current client cannot preserve them.

Block descriptions explain verified enums and remaining uncertainty, including
which FIR frequency field is the low-pass and high-pass corner. On the DA0408X,
order index 0 selects 256 taps and index 1 selects 512 taps for both inputs and
outputs. The management tab can download or restore the selected channel's exact
2048-byte coefficient block. Restore validates all 512 float32 values locally,
requires a clean unlocked device, verifies exact coefficient readback and an
unchanged settings snapshot, and attempts rollback after a readable mismatch.

FIR type 5 runs the channel's coefficient block instead of a designed filter, but
only once a block has been written after selecting it: apply type 5, then restore
a coefficient block to that channel. It reads back as type 6, which can be written
back unchanged. Plots do not include an enabled FIR. The designed types are
linear phase and delay their channel by 2.662 ms (256 taps) or 3.995 ms (512
taps); type 5 delays by 1.333 ms plus the block's own delay. The alignment view
does not include either delay.

Local preset JSON is versioned and profile-specific. Export captures the draft's
supported controls; import stages changes with Live off. Presets omit names,
unknown fields, program contents, and separate FIR coefficient payloads. Import
refuses incompatible profiles, structure, and malformed values.

The DA0408X exposes 30 program slots with zero-based wire indices 0–29. Save records
current device settings, while load replaces all channels and routes. Applied changes
live only in the unit's working settings and are lost at power-off unless saved to a
slot. The unit powers on into its current program, shown on its front panel as
"Scene: UserN"; saving or loading a slot makes that slot current. After a load from this UI the
panel keeps showing the previous scene until the next power cycle. Channel copy
is within the selected input/output direction. Review
destinations with **Review destinations** and confirm before execution; pending drafts must be applied or
reverted. Each operation disables Live and reloads actual settings, including
after a missing ACK where readback is still possible. If readback fails the service
disconnects and requires reconnect. Program/copy actions do not read or decode
separate FIR coefficient buffers automatically.

### Verification and remaining acceptance

The unit suite covers packed snapshot boundaries, codecs, response mathematics,
OSM semantics, service writes/conflicts/partial failure, Live, presets, operation
destinations, and offline Gradio callbacks. Chromium exercises the real assembled
application through an injected fake client (see "Browser E2E and review videos"
below).

### Dragging EQ frequencies and seeing unapplied edits

The magnitude plot has circular handles for enabled EQ/shelf bands and diamond
handles for allpass. The selected disabled band has a hollow handle; moving it
does not enable the band. Click a handle to select its editor below, or choose
the band from the dropdown when handles overlap. Handle height is the band's
configured gain, rather than the combined response. Allpass handles stay at 0 dB.

Drag horizontally to change frequency. The magnitude, phase and group-delay
curves preview the movement. Frequency stops at the visible zoom bounds and
the device's 10–20,000 Hz limits. Gain and Q are still edited below, and the
frequency box remains available. Crossovers and measurements have no handles.

Starting a drag switches **Live tuning** off. Releasing stages the frequency
locally; use **Apply changes** to write and verify it. Live stays off until you
enable it again after Apply or Revert. Escape or pointer cancellation discards
the movement. Changing channel/band, refreshing, or changing the draft ends
the drag. Dragging replaces unfinished frequency input for that band; other
highlighted input errors still require correction.

The persistent **Unapplied changes** banner counts controls across every
channel, including drafts outside the current view. The Pending changes table
shows their before/draft values. Returning a control to its readback value
removes it from the count. Failed or partially completed Apply keeps remaining
edits visible, including after disconnect. Unfinished numeric input has its
own warning; offline demo changes are explicitly marked as local.

### Browser E2E and review videos

The browser suite starts a separate localhost Gradio server for every scenario.
It accepts only the fake hostname `e2e.fake.invalid`, records its fake-device
operation ledger, and never creates a production device client. Ordinary test
runs skip this suite unless `--run-e2e` is present.

```sh
uv sync --extra ui --group e2e
uv run --group e2e playwright install chromium
uv run --extra ui --group e2e pytest tests/e2e --run-e2e --review-videos
```

Each run creates a new `ignore/playwright/<timestamp>-sinbosen_la548/` directory.
Its `index.html` links each 1920×1080 silent WebM recording, Playwright trace,
final screenshot, client diagnostics, and inspected downloads. `--review-videos` pauses and scrolls
completed crossover/allpass, mixer, alignment, FIR, and management states; the
rapid Live numeric burst remains unpaced so it still tests coalescing. Browser
page errors and console errors fail the test and leave diagnostics in that run.
The scenarios include recorded drag, cancellation, editor synchronization and
global unapplied-status coverage.

`python scripts/check_ui_browser.py --review-videos` is a convenience wrapper
around the same scenarios. Pass `--artifacts /some/local/path` to change their
parent directory; no artifact is uploaded or overwritten.

UI hardware acceptance remains opt-in: compare readback to the manufacturer's
app, make reversible mixer/crossover/allpass changes through a hardware loopback,
and check reconnect without replay. Compare representative imported native/text
traces visually against OSM. Existing client HIL findings inform the UI but do
not establish that the UI has completed hardware acceptance.
