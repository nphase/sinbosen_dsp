# sinbosen-la548

An independent Linux client, command-line tool and local web UI for the
Sinbosen LA548 network audio DSP, so the unit can be controlled without the
manufacturer's Windows-only app.

It also drives the Sinbosen LA448 over its USB port: the LA448 speaks the same
command protocol over USB HID, so every command below accepts `usb` in place
of the IP address (see "LA448 over USB").

See [the protocol reference](docs/PROTOCOL.md) for the wire format and
control catalog, including what's confirmed live vs. still unverified.

## Status

Session handling and three read-only calls (`read_lock_state`, `get_user_name`,
`read_group`) are confirmed against real hardware. An earlier version of this
client sent a guessed "handshake" frame on connect. It was really opcode 0xA4,
the generic parameter write, and its default header fields address channel 1's
mute, so every connect silently muted channel 1. The client never sends that
frame; see "Unsafe opcodes" in docs/PROTOCOL.md.

The write path (`registry.py` + `codecs.py` + `Client._write_param`) is the
structural fix for that incident: every control is a `ParamSpec`, never
hand-typed header fields. Mute/Volume/Phase/Delay/Crossover/EQ
(including AllPass, for subwoofer time/phase alignment) - input and output -
plus input-only Dynamic EQ, Compressor/Limiter/Expander (Compressor is input
and output, Limiter is output-only, Expander is input-only), the mix matrix
(on/off per crosspoint - no continuous level path exists), Input Source/FIR
(input and output)/Frequency Shifter (input-only), and the whole-device
operations (program save/load, FIR coefficient backup/restore, channel copy -
**not** the raw whole-group write path, which stays deliberately unimplemented,
see docs/PROTOCOL.md) are implemented, unit-tested, and **confirmed on real
hardware** with the hardware-in-the-loop suite (`hardware_test/`, see its own
README) - every control the client implements
has been driven and measured on a real unit at least once. A few findings
from that process are real, documented hardware limitations rather than bugs
in this client - `eq_all_bypass`/`eq_all_reset` are confirmed non-functional
on this unit's firmware, and the input compressor has no effect on this
firmware - see docs/PROTOCOL.md, "Known device behaviour and open
questions" for the full list of what's confirmed vs. still genuinely open
(e.g. EQ types 3/5's true identity).

The web UI is separate from that. The new Tune screen (below) has so far only
been exercised in offline demo and against fakes in tests, not on a real unit.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
```

USB support (LA448) needs the optional `hidapi` dependency:

```sh
uv sync --extra usb
```

## Usage

The device's IP is shown on its own front panel - that's the reliable way to
find it (network discovery has not been made to work against this hardware;
see docs/PROTOCOL.md).

```sh
uv run sinbosen-la548 status <ip>
uv run sinbosen-la548 search                          # best-effort; may find nothing, see docs/PROTOCOL.md
uv run sinbosen-la548 dump-group <ip> backup.bin       # back up the active channel group to a file
uv run sinbosen-la548 set-mute <ip> 0 mute             # mute input channel 0 (--output for an output channel)
uv run sinbosen-la548 set-phase <ip> 0 invert
uv run sinbosen-la548 set-volume <ip> 0 -6.0
uv run sinbosen-la548 set-delay <ip> 0 12.5            # 12.5 ms
uv run sinbosen-la548 set-delay <ip> 0 12.5 --verify   # ...then read settings back and confirm the stored value
uv run sinbosen-la548 set-xover <ip> 0 hp --freq 80 --type 1 --slope 24 --enable   # output channel only
uv run sinbosen-la548 set-eq-band <ip> 0 0 --freq 45 --q 1.0 --type 4 --slope 0 --enable   # AllPass at 45 Hz, gain auto-zeroed
uv run sinbosen-la548 import-rew <ip> 0 filters.txt --output --dry-run   # show how a REW filter export maps onto output 0's EQ bands; sends nothing
uv run sinbosen-la548 import-rew <ip> 0 filters.txt --output --verify    # replace output 0's EQ with it, then read back
uv run sinbosen-la548 eq-bypass <ip> 0 on               # exits locally as unsupported; sends no packet
uv run sinbosen-la548 eq-reset <ip> 0                   # exits locally as unsupported; reset bands individually
uv run sinbosen-la548 set-dynamic-eq-band <ip> 0 0 --freq 100 --gain 6 --q 2 --type 0 --slope 0 --startup-mode 0 --start-time 1 --release-time 100 --ratio 4 --threshold -20 --enable  # input-only
uv run sinbosen-la548 set-compressor <ip> 0 --start-time 1 --release-time 100 --ratio 4 --threshold -20 --enable  # input or --output
uv run sinbosen-la548 set-limiter <ip> 0 --start-time 1 --release-time 50 --threshold -6 --detection-type 0 --enable   # output-only
uv run sinbosen-la548 set-expander <ip> 0 --start-time 1 --release-time 100 --ratio 2 --threshold -50 --enable   # input-only
uv run sinbosen-la548 set-mix-route <ip> 0 2 on        # connect input 0 to output 2 ('on' = connected, confirmed live)
uv run sinbosen-la548 set-input-source <ip> 0 on --source 1 --freq 100 --gain -6.0  # input-only
uv run sinbosen-la548 set-fir <ip> 0 on --type 1 --win-type 0 --freq-low 80 --freq-high 8000 --atten 30 --order 1
uv run sinbosen-la548 set-freq-shifter <ip> 0 3 0 on   # level 3, direction 0 (input-only, see docs/PROTOCOL.md)
uv run sinbosen-la548 save-program <ip> 0              # save current live state to program slot 0
uv run sinbosen-la548 load-program <ip> 0              # OVERWRITES all live settings from program slot 0
uv run sinbosen-la548 read-fir-coefs <ip> 0 fir0.bin    # exact 2048-byte DA0408X channel backup
uv run sinbosen-la548 write-fir-coefs <ip> 0 fir0.bin   # restore and verify that backup; rolls back on mismatch
uv run sinbosen-la548 channel-copy <ip> 0 1 4           # copy input channel 0's settings onto input channel 1 (this unit: 4 inputs/8 outputs, confirmed - use 8 for --output)
uv run sinbosen-la548 diff-group before.bin after.bin  # locate a control's byte offset in a read_group dump
```

### LA448 over USB

Give `usb` wherever a command takes `<ip>`, or `usb:<serial>` when more than
one unit is plugged in (`search` lists the serials). The same address works in
`Client("usb")` and in the UI's address box.

```sh
uv run --extra usb sinbosen-la548 search               # lists usb:<serial> for each attached unit
uv run --extra usb sinbosen-la548 status usb
uv run --extra usb sinbosen-la548 dump-group usb backup.bin
uv run --extra usb sinbosen-la548 set-volume usb 0 -6.0 --verify
```

Confirmed on a real LA448: the session, keepalive, lock state, program names
and settings snapshot (it uses the same DA0408X layout), and acknowledged,
readback-verified writes of mute and gain to their existing values. Not yet
confirmed over USB: a write that changes a value, and each of the other
controls; use `--verify`. `write-fir-coefs` (and the UI's FIR restore) is
refused over USB without sending anything, because its request is longer than
one 64-byte report and how to split it is unknown. Everything measured with the hardware-in-the-loop
suite was measured on the LA548 over UDP. See docs/PROTOCOL.md, "USB HID
transport (LA448)".

What the CLI guarantees:

- **`set-*` commands** print `acknowledged` when the device acks the frame.
  An ack only means the frame arrived (docs/PROTOCOL.md, "Transport safety
  model"). Add `--verify` to read settings back and compare the stored
  control with the exact bytes sent; a mismatch exits 1.
- **Values** outside the ranges the manufacturer's app allows are refused
  before anything is sent, e.g. volume -80..+12 dB or compressor ratio
  1..128 (docs/PROTOCOL.md, "Value bounds").
- **Backups:** `dump-group` and `read-fir-coefs` write atomically and never
  replace a good backup with an empty or invalid reply. `dump-group` saves
  only a reply that decodes as a DA0408X snapshot (`--no-validate` to
  override).
- **`write-fir-coefs`** refuses a locked device and backs up the current
  block first. If the readback doesn't match, it restores the backed-up
  block.
- **`import-rew`** reads REW's "Export filter settings as text" file for the
  Generic equaliser. PK (and Modal) filters become peaking bands and AP
  becomes the second-order allpass; it replaces the channel's EQ, resetting
  bands the file does not use. It sets EQ bands only: shelves, low/high-pass
  (set those with `set-xover`), notch and anything out of the device's range
  are refused with a reason before any packet is sent (`--skip-unsupported`
  imports the rest). The Studio's Tune screen has an **Import from REW...**
  button that also loads HP and LP into the crossover. See docs/PROTOCOL.md,
  "Importing REW filters"; the PK Q conversion follows the documented device
  model and has not been checked by measurement on a unit.
- **`diff-group`** exits like diff(1): 0 identical, 1 different (including a
  length mismatch), 2 when a file can't be read.

Or use it as a library:

```python
from sinbosen_la548.client import Client

with Client("192.168.1.100") as client:
    print(client.read_lock_state())
```

Typed settings readback is available for the fixture-matched **DA0408X**
snapshot layout (4 inputs, 8 outputs, 7 input EQ bands, 15 output EQ bands):

```python
from sinbosen_la548.client import Client

with Client("192.168.1.100") as client:
    settings = client.read_settings()
    print(settings.outputs[0].hp)
    print(settings.inputs[0].eq_bands[0])
    print(settings.mix_matrix[0][0])  # input A -> output 1
```

This API performs one read and no writes, rejects incompatible snapshots, and
preserves original bytes. The profile's 96 kHz sample rate is confirmed on
hardware but not read from the device. FIR parameters decode through
`fir_adapter.FIRParameters`. Each channel's 512-value FIR coefficient block
is what FIR type 5 applies, and backups keep it byte-exact. See [settings readback](docs/SETTINGS_READBACK.md).

## Local DSP Studio

```sh
uv sync --extra ui
uv run --extra ui sinbosen-la548-ui
```

Open the printed URL, <http://127.0.0.1:7860> by default. `--port` changes the
port. The app starts in offline demo with the bundled fixture; use the
connection chip in the top bar to connect to a unit (an IP address, `usb` or
`usb:<serial>`). `--demo` is still accepted and changes nothing. Stop it with
Ctrl-C, which closes the device connection.

The server binds to `127.0.0.1` and only accepts requests from its own page
(other Host and Origin headers are refused). `--host` binds elsewhere, but the
UI has no authentication and can change the device's output levels, so the
launcher prints a warning when you do.

The Tune screen is one dense page: a top bar (connection, Live, count of
unapplied changes, Revert, Apply, Save), a strip per channel (all 4 inputs and 8
outputs) with nickname, gain, delay, polarity, mute and link, a full-width
response plot, and filter cards for the selected channel, which is Out 1 when
the page opens. Gain, polarity and mute are written at once (a typed gain when
you press Enter); everything else is a draft until **Apply**, unless **Live** is
on. Channels can be linked so edits to crossover, EQ, delay or limiter settings
apply to every member; links never tie gain, mute or polarity. On the plot, EQ
bands and crossovers can be dragged (up and down is gain for a band and slope
for a crossover) and the wheel over a handle changes Q. The top bar shows an
estimate of the selected channel's latency, computed from the draft and the
measured figures in [docs/PROTOCOL.md](docs/PROTOCOL.md). A strip's
nickname is edited by double-clicking it. Nicknames and links are kept in
`~/.config/sinbosen-la548/studio.json` on this computer, not on the device.

The **Mixer** tab, next to Tune, shows the routing matrix: a row per input and a
column per output, each crossing a toggle (the unit routes on or off, with no
level). The strips stay above it. A routing change is a draft like a filter
edit, shown unapplied until **Apply** (or sent by **Live**), so a stray click
cannot send a full-range signal to a tweeter at once.

**Import from REW...** on the selected-channel bar loads a Room EQ Wizard
"Export filter settings as text" file (Generic equaliser) into the channel as
drafts. It overrides the channel's EQ: every band in use is overwritten or
cleared. It replaces the high-pass or low-pass only when the file has one, and
leaves the other alone. A dialog first states what will be overwritten, what
will be loaded and what cannot be imported (shelves, notches, other HP/LP
kinds). The Q conversion for peaking filters follows the documented device
model and has not been checked by measurement on a unit. See
[docs/UI_GUIDE.md](docs/UI_GUIDE.md).

Not yet in the new UI, and available only with `--classic`: loading programs, presets, measured traces and alignment, FIR coefficient
backup/restore, and editing the limiter, compressor, FIR and the other
advanced blocks.

```sh
uv run --extra ui sinbosen-la548-ui --classic
```

`--classic` starts the older Gradio UI as it was, with its own address box.
The two UIs cannot be open on the same device at once, and switching drops
unapplied drafts.

See the [UI guide](docs/UI_GUIDE.md) for both. The new screen has only been
exercised in offline demo and with fakes in tests, not on a real unit. For the
classic UI, automated offline checks pass; its hardware acceptance and OSM
display comparison remain opt-in verification work.

### Saving

Everything you send to the unit, including Apply, changes only its live
settings, and the unit forgets them when it is switched off: it starts again
from its current program slot. To keep a tuning, click **Save** in the top bar,
choose one of the unit's 30 program slots (the current one is preselected),
press the button that names it and confirm; the unit stores its live settings there and that slot becomes
the one it starts in. With unapplied edits, Save applies them first and saves
only if Apply succeeds. Saving switches Live off.

## Development

```sh
uv run --extra ui pytest
uv run --extra ui ruff check .
uv run --extra ui mypy
```

Unit tests cover the pure protocol/struct-packing logic and run without any
hardware (`testpaths = ["tests"]` in `pyproject.toml`, so `uv run pytest`
never touches real hardware). `hardware_test/` is a separate
hardware-in-the-loop suite that validates the write API against a real unit
through an audio loopback: an audio interface plays test signals into the
DSP's inputs and records its outputs. See
[hardware_test/README.md](hardware_test/README.md) for wiring, configuration
(`LA548_HIL_*` environment variables), safety rules, and how to run it
(opt-in only, via `--run-hardware` plus a real `LA548_HOST`).

## Project layout

```
src/sinbosen_la548/
  protocol.py     # frame envelope pack/unpack, checksum, Opcode enum
  discovery.py    # CH9120 UDP broadcast search (best-effort, see docs/PROTOCOL.md)
  transport.py    # UDP socket wrapper, HandShake keepalive, request/reply matching
  client.py       # high-level API: connect, read_lock_state, get_user_name, read_group,
                  #   set_mute/volume/phase/delay/xover/eq_band (EQ-all methods fail locally),
                  #   set_dynamic_eq_band, set_compressor/limiter/expander, set_mix_route,
                  #   read_mix_matrix/write_mix_matrix, set_input_source, set_fir,
                  #   set_freq_shifter, save_program, load_program, read_fir_coefs, channel_copy
  registry.py     # ParamSpec table - the only source of truth for a control's opcode/header fields
  codecs.py       # payload encode/decode for each control's wire format
  blobdiff.py     # diff two read_group() dumps to locate a control's byte offset
  cli.py          # `sinbosen-la548` command-line entry point
docs/
  PROTOCOL.md           # protocol reference
  SETTINGS_READBACK.md  # typed settings snapshot layout
  FILTER_MODELS.md      # measured filter models behind the response previews
  UI_GUIDE.md           # DSP Studio web UI guide (Tune screen and classic UI)
hardware_test/
  Hardware-in-the-loop test suite (audio loopback through a real unit) - see its own README
```

## License

MIT; see [LICENSE](LICENSE).
