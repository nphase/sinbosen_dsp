# Guided tuning with REW: design

Items marked **(call)** were decided by the author of this document, not by
the user, and are open to correction.

## Intent

Tuning a system today means running a sweep in Room EQ Wizard, reading the
result, working out a correction, typing it into the DSP, and sweeping again.
The user wants the studio to run that loop: pick the speaker or speakers to
measure, have REW sweep, see a proposed correction, apply it, and sweep again
to see how it went, for as many rounds as it takes to be happy.

The correction is not EQ only. The system may also move level, delay,
polarity and crossover settings, and must ask before doing anything that
could endanger a driver.

## The user's rig (the first system this must work on)

- LA448. Out 1+2 subwoofer, Out 3+4 mains, Out 5+6 DJ monitors.
- Laptop running REW and the studio. UMIK-1 USB microphone (no loopback).
- Laptop stereo output split into In 3 (left) and In 4 (right).
- Music normally arrives on In 1 and In 2. In 3 and In 4 are measurement
  inputs only.
- The LA448 is connected over USB. Network may be tried later.
- The user has REW's Pro upgrade ("pretty sure").

## How a session goes

1. **Setup, once per unit.** The user describes the system: named speaker
   groups (Subs = Out 1+2, Mains = Out 3+4, Monitors = Out 5+6), each
   group's kind (sub, full-range, monitor) and a safe frequency range per
   output. They name the measurement inputs (sweep = In 3, timing reference
   = In 4) and the reference speaker (one main). Stored app-side with the
   nicknames and links.
2. **Pick a target.** One group, one output of a group, or several groups
   combined (Subs + Mains).
3. **Measure.** The studio routes the sweep input to the picked outputs
   only, routes the timing input to the reference speaker, tells REW to
   sweep, waits, and pulls the result. The measured curve appears on the
   Tune plot.
4. **Propose.** The studio shows a correction as ordinary pending edits:
   draft curve on the plot, changed fields outlined, a short list of what
   changed and why, and a predicted result curve. Changes that need
   confirmation are listed separately and are off until ticked.
5. **Apply and verify.** The user applies. The studio sweeps again and lays
   the new measurement over the prediction.
6. **Repeat or stop.** The user can run another round, undo the last round,
   or finish. Finishing restores the routing that was in place before the
   session.

## Decided by the user

- The loop above: pick, sweep, evaluate, correct, re-sweep, repeat at will.
- Targets can be a single speaker or several combined.
- The system may change EQ, level, delay, polarity and crossover.
- It asks for confirmation when a change is dangerous.
- **Split of work (approach C):** REW's "match target" computes EQ; the
  studio works out delay, polarity and crossover itself.
- **Verify the LA448 first.** A phase 0 on the user's unit comes before
  anything else (see Phases).
- **Timing by reference cable:** the laptop's second channel into In 4 gives
  REW an acoustic timing reference, so separate sweeps share a clock.

## Decided by the author (call)

- **The studio drives.** A guided panel on the Tune screen talks to REW's
  local API (`http://127.0.0.1:4735`) and to the unit. REW is the measuring
  instrument and EQ fitter; the user does not need to operate it during a
  session beyond having it open with the mic and output configured.
- **REW Pro is the main path.** Starting sweeps through the API needs
  REW's Pro upgrade, which the user has. The studio-started sweep is built
  and tested first, and is the only path on which the sweep span and level
  gate is enforced. The studio checks at connect whether REW accepts a
  measure command. If it does not, the studio still sets the routing, asks
  the user to press Measure in REW and picks up the new measurement; it
  then says plainly that it cannot limit the sweep, and checks the span
  only after the fact.
- **Measurement inputs are kept out of the result.** Music arrives on
  In 1 and In 2, so corrections must describe the outputs, not In 3. At
  session start the studio checks that In 3 and In 4 have no EQ, gain,
  delay or dynamics in effect. If they do, it removes their modelled
  response and delay from each measurement before working out a
  correction, and says so. It does not change them. Processing on In 1 and
  In 2 is outside this feature.
- **Soloing is done with the mixer, not mutes.** During a session the
  studio routes the sweep input only to the outputs under test and the
  timing input only to the reference speaker. The user's routing and mutes
  are recorded at session start and restored at finish, on error, and
  offered for restore at next launch if the app died mid-session.
- **Nothing is written without Apply.** Proposals are pending edits. Live
  mode is ignored for proposals. The exception is session routing, which is
  written when a measurement starts (it has to be) and is not counted as an
  unapplied change.
- **What needs confirmation.** A proposed change is held back, unticked,
  when it could send a driver more than it gets now:
  - lowering or removing a high-pass, or raising or removing a low-pass;
  - any crossover frequency outside the output's stated safe range;
  - raising an output's gain;
  - EQ boost of more than 3 dB in any band, or more than 6 dB net at any
    frequency.
  Cuts, delay moves and polarity flips are ordinary pending edits.
- **Never touched:** limiters, compressors, dynamic EQ, FIR, input-side
  processing, and any output outside the picked target.
- **Sweeps are gated.** No output is swept until it has a safe range. The
  sweep's frequency span is limited to the union of the picked outputs'
  safe ranges, and the first sweep of each output in a session starts at a
  low level the user confirms. The sweep level is set in REW and is never
  raised by the studio on its own.
- **No blind discovery.** The system does not sweep every output unprompted
  to find out what is connected. The "current state" check is a normal
  measurement of each configured group, run when the user asks.
- **What each kind of target gets.**
  - A single group or output: level trim and EQ toward the target curve.
    Left and right of a group get the same EQ by default (averaged
    measurement), with per-side EQ as an option.
  - Combined groups (Subs + Mains): each group is measured alone, then the
    studio searches delay, polarity and crossover frequency and slope for
    the best summed response through the crossover region, using the
    measured responses and the unit's own filter models. A combined sweep
    verifies it. EQ of the sum comes after alignment.
- **Order within a full tune:** per-group level and EQ, then alignment
  between groups, then overall EQ of the sum. The studio suggests the next
  step; the user can do any step alone.
- **Target curve.** Flat by default with REW's own target settings (level,
  low and high roll-off matched to the group's safe range); the user can
  pick a house curve already loaded in REW. The studio reads the target
  from REW rather than keeping its own.
- **EQ limits passed to REW:** cuts up to 12 dB, boosts up to 3 dB, Q up to
  8, correction only inside the output's safe range and not above 10 kHz
  unless asked, at most the number of free bands on the channel. Bands the
  user already set are kept; the studio only uses free bands unless the
  user allows replacing "bands added by tuning" from earlier rounds.
- **REW filters are refitted to the unit.** REW's filters are textbook
  shapes; the unit's are not always (see docs/FILTER_MODELS.md). Each REW
  filter is converted to the unit band whose modelled response matches it
  best, and the prediction shown is computed from the unit's models, not
  taken from REW. If the mismatch is more than 0.5 dB the proposal says so.
- **Honest verification.** After each round the studio reports how far the
  new measurement is from the prediction. If a round made things worse by
  its own measure, it says so and offers undo.
- **Undo is per round:** each round's edits are remembered so the last
  round can be reversed as a normal pending edit plus Apply.
- **One mic position per session** is assumed. The panel reminds the user
  to say when the mic has moved; measurements from before the move are not
  mixed with later ones. Multi-position averaging is out of scope.
- **Reference path.** The timing reference runs through the unit, so the
  studio subtracts the reference output's known delay and filter delay when
  comparing timings, and warns if a proposal would change the reference
  speaker's own crossover (timings before and after are then not
  comparable without a re-measure).
- **Session routing is an immediate write.** `DSPService.write_now` accepts
  only gain, polarity and mute today. It is extended to mix routes for
  session routing, the same way it was added for the immediate controls.
  This is the one engine change this design needs.
- **Muted channels.** If a picked output or a measurement input is muted
  when a measurement starts, the studio does not unmute it on its own. It
  says which channel is muted and offers to unmute it for the session; the
  mute is restored with the routing at finish.
- **Left and right are swept separately.** "Averaged" means one sweep per
  output, averaged in REW, never both sides playing into one sweep (that
  combs at an off-centre mic). For alignment, a group's response is the
  sum of its outputs' separate sweeps, which the shared timing reference
  makes valid; the combined sweep checks it.
- **The reference chirp is an allowed exception** to "never touch outputs
  outside the target": it plays through the reference main even when only
  Subs are picked. The reference speaker goes through the same first-sweep
  level check, and the chirp band must sit inside its safe range.
- **REW settings the studio may change:** per-measurement equaliser and
  target settings, and the match-target settings. It does not change REW's
  defaults, house curve or audio device setup. If acoustic timing reference
  cannot be switched on through the API, the user sets it once in REW and
  the studio checks for it.
- **Limits REW cannot take.** Whatever of the EQ limits REW's match-target
  settings do not accept (the builder confirms the fields in the phase 2
  plan) is enforced afterwards: filters outside the limits are dropped or
  clamped during the refit, and the prediction shows the result of what is
  kept.
- **Supersedes the planned file import.** The "Import from REW" filter-file
  action in the studio spec stays as planned; the filter-to-band conversion
  here is the same code (`rew.py`) and should be built once.
- **Dependencies.** REW access uses the standard library HTTP client. No
  new dependency.

## Phases

Each is its own plan and leaves a working app.

0. **LA448 write check.** Over USB on the LA448 only mute and gain writes
   have been tried, each with its existing value (docs/PROTOCOL.md, "USB
   HID transport"). Before the loop is built, each kind of setting it uses
   is written with a changed value, read back and restored on the user's
   unit: mix route, output gain, delay, polarity, crossover (frequency,
   type, slope, enable) and an EQ band. It follows the existing
   `hardware_test` restore discipline, with outputs muted or amplifiers
   off. Results go into docs/PROTOCOL.md. A control that fails stops the
   phases that depend on it until it is understood. Whether the LA448's
   filter shapes match the LA548 models is not measured here (there is no
   loopback on this rig); it shows up in phase 2 as the gap between
   prediction and the next sweep, which is why that gap is always reported.
1. **REW link and measuring.** Setup description, safe ranges, REW
   connection and status, session routing with restore, triggered or
   hand-started sweeps, measured overlay on the Tune plot.
2. **EQ rounds.** Level and EQ proposals for a single group, refit to unit
   bands, prediction, confirmation rules, verify, per-round undo.
3. **Alignment.** Delay, polarity and crossover search for combined groups,
   then EQ of the sum.

## Goals

- A sub-plus-mains tune from untuned to verified takes no typing of filter
  values and no manual muting.
- Every proposal is seen on the plot before it reaches the unit.
- The measurement after a round matches the prediction closely enough to be
  trusted, and the app shows the difference either way.

## Non-goals

- Replacing REW's measurement or its EQ fitter.
- Multi-position or spatial averaging, room-mode analysis, FIR generation.
- Unattended tuning with no user present.
- Tuning limiters or dynamics.

## Anti-goals

- A sweep or setting that damages a driver because the system assumed what
  was connected.
- Leaving the unit in measurement routing after a session, crash or error.
- Corrections applied without the user seeing them first.
- Chasing a flat line with large boosts or narrow filters into room nulls.

## Constraints

- First target is the LA448 over USB. Nothing here may depend on the
  network transport or on FIR writes, which USB cannot do.
- Same engine guarantees as every other write: bounds, lock check, conflict
  check against a fresh readback, readback verification.
- The engine stays as it is apart from the `write_now` extension above;
  this is another caller of `DSPService`. Route writes exist in the engine
  (`client.set_mix_route`, `ControlKey(False, in, "route", out)`) but have
  no studio API endpoint yet. The Mixer tab (studio phase 2) is not
  required.
- The Measured overlay was deferred to studio phase 3; phase 1 here brings
  forward the part it needs.
- REW must be running with its API enabled; the studio detects and explains
  when it is not. REW's API is local HTTP; sweeps are asynchronous unless
  blocking mode is set.
- Works in offline demo with a simulated REW so the flow can be tested
  without hardware.

## Approaches

1. **REW decides everything.** REW's match-target for EQ and its alignment
   tool for delay and polarity. Least to build, but REW cannot choose
   crossovers and knows nothing of the unit's filter shapes or safe ranges.
2. **Studio decides everything.** Own EQ fitter and own alignment search on
   the unit's real filter models. Most consistent, most work, and re-invents
   a mature EQ fitter.
3. **Split (chosen by the user).** REW fits EQ; the studio does alignment
   and crossover, refits REW's filters to the unit, and predicts results
   from its own models. Cost: REW's filters have to be reconciled with the
   unit's shapes.

Timing: inferring sub-to-main delay from a third, combined sweep was
considered and needs no rewiring; the user chose the reference cable, which
measures the delay directly. The combined sweep remains as the check.

An AI agent driving REW and the unit through commands was dropped: it has
no place to show a draft on the plot before it is written.

## Left to the builder

The REW client's module layout and polling or subscription choice, the
panel's exact layout within the Tune screen's style, the alignment search
method and its scoring, the storage schema for setup and round history, the
simulated REW for tests, and how the first-sweep level check is worded.
