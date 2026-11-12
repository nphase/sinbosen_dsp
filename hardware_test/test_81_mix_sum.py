"""Two inputs routed to one output: summed, averaged, or scaled?

test_80 proved a single crosspoint connects. This test checks what happens
when input A and input B are both routed to output 1, as for a mono
subwoofer downmix of a stereo source:

1. A alone routed, tone on A        -> level_a
2. B alone routed, tone on B        -> level_b
3. both routed, tone on A only      -> level_a_both: equal to level_a if each
   crosspoint is unity gain; ~6 dB lower if the mixer scales by route count
4. both routed, same tone on A and B -> level_sum: the coherent sum of the
   two per-input levels if the mixer adds
5. both routed, B polarity-inverted  -> level_cancel: a deep null proves a
   true sample-wise sum rather than e.g. a max or switch

The tone is kept at 0.1 so a +6 dB sum stays well clear of clipping.
"""

from __future__ import annotations

import time

import numpy as np

from . import analysis, signal_gen
from .rig import Rig

TONE_DURATION_S = 2.0
TONE_AMPLITUDE = 0.1
FREQ_HZ = 100  # a subwoofer-band tone, the use case for a mono downmix
TARGET_OUTPUT = 0  # output 1
TOLERANCE_DB = 0.5
MIN_CANCEL_DB = 20.0


def _route(rig: Rig, *, a: bool, b: bool) -> None:
    rig.client.set_mix_route(0, TARGET_OUTPUT, a)
    rig.client.set_mix_route(1, TARGET_OUTPUT, b)
    time.sleep(rig.settle_s)


def _level(rig: Rig, gain_a: float, gain_b: float) -> float:
    mono = signal_gen.sine(FREQ_HZ, TONE_DURATION_S, amplitude=TONE_AMPLITUDE, sample_rate=rig.sample_rate)[:, 0]
    signal = np.stack([gain_a * mono, gain_b * mono], axis=1).astype(np.float32)
    captured = rig.measure(signal)
    return analysis.onset_aligned_dbfs(captured[:, TARGET_OUTPUT], rig.sample_rate, TONE_DURATION_S)


def test_81_two_inputs_on_one_output_sum(rig: Rig) -> None:
    rig.reset_both_channels_safe()

    _route(rig, a=True, b=False)
    level_a = _level(rig, 1.0, 0.0)
    _route(rig, a=False, b=True)
    level_b = _level(rig, 0.0, 1.0)
    _route(rig, a=True, b=True)
    level_a_both = _level(rig, 1.0, 0.0)
    level_b_both = _level(rig, 0.0, 1.0)
    level_sum = _level(rig, 1.0, 1.0)
    level_cancel = _level(rig, 1.0, -1.0)

    predicted_sum = 20 * np.log10(10 ** (level_a_both / 20) + 10 ** (level_b_both / 20))
    print(f"A alone routed:           {level_a:7.2f} dBFS")
    print(f"B alone routed:           {level_b:7.2f} dBFS")
    print(f"both routed, tone on A:   {level_a_both:7.2f} dBFS ({level_a_both - level_a:+.2f} dB vs A alone)")
    print(f"both routed, tone on B:   {level_b_both:7.2f} dBFS ({level_b_both - level_b:+.2f} dB vs B alone)")
    print(f"both routed, A + B:       {level_sum:7.2f} dBFS ({level_sum - level_a_both:+.2f} dB vs A; "
          f"coherent sum predicts {predicted_sum:.2f})")
    print(f"both routed, A - B:       {level_cancel:7.2f} dBFS ({level_cancel - level_a_both:+.2f} dB vs A)")

    rig.reset_both_channels_safe()

    assert abs(level_a_both - level_a) < TOLERANCE_DB, "routing a second input changed A's own gain"
    assert abs(level_b_both - level_b) < TOLERANCE_DB, "routing a second input changed B's own gain"
    assert abs(level_sum - predicted_sum) < TOLERANCE_DB, "A + B is not the coherent sum of the two paths"
    assert level_a_both - level_cancel > MIN_CANCEL_DB, "A - B did not cancel, so the mixer is not a plain sum"
