"""
rng.py — where the unpredictability comes from.

You asked whether there is a library that makes the system *truly* random
rather than "random-looking". There is, and it is already in Python's standard
library:

  random.Random        (default)  Mersenne Twister. Fast, statistically fine,
                                  but fully deterministic: anyone who learns
                                  the internal state can predict every future
                                  value. Not unpredictable, just shuffled.

  random.SystemRandom  ("system") Reads os.urandom(), i.e. the operating
                                  system's cryptographically secure pool
                                  (BCryptGenRandom on Windows, getrandom() on
                                  Linux). It is continuously re-seeded from
                                  hardware entropy — interrupt timings, ring
                                  oscillator jitter, RDSEED on modern CPUs.
                                  There is no seed to recover and no sequence
                                  to extrapolate: the next spin cannot be
                                  predicted from any number of past spins.
                                  This is the same generator `secrets` uses
                                  for passwords and session tokens.

"system" is the default here. Every spin draws its target speed, motor time,
coast friction, disturbance and (in draw mode) the winner itself from that
pool, so no two spins are alike and none of them are foreseeable.

"fast" is kept only so a test rig can be made repeatable on purpose.
"""

from __future__ import annotations

import random
from typing import List, Sequence


class Entropy:
    """Thin façade so the whole app pulls randomness from one place."""

    def __init__(self, mode: str = "system"):
        self._mode = ""
        self._rng: random.Random = random.SystemRandom()
        self.set_mode(mode)

    # ---------------------------------------------------------------- mode
    def set_mode(self, mode: str):
        mode = (mode or "system").lower()
        if mode == self._mode:
            return
        self._mode = mode
        self._rng = random.Random() if mode == "fast" else random.SystemRandom()

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def is_cryptographic(self) -> bool:
        return isinstance(self._rng, random.SystemRandom)

    def describe(self) -> str:
        if self.is_cryptographic:
            return ("OS cryptographic pool (os.urandom) — unpredictable, "
                    "re-seeded from hardware entropy")
        return "Mersenne Twister — fast and repeatable, NOT unpredictable"

    # ---------------------------------------------------------------- draws
    def uniform(self, a: float, b: float) -> float:
        if b < a:
            a, b = b, a
        return self._rng.uniform(a, b)

    def randint(self, a: int, b: int) -> int:
        if b < a:
            a, b = b, a
        return self._rng.randint(a, b)

    def random(self) -> float:
        return self._rng.random()

    def choice(self, seq: Sequence):
        return self._rng.choice(seq)

    def chance(self, percent: float) -> bool:
        return self._rng.random() * 100.0 < percent

    def weighted_index(self, weights: List[float]) -> int:
        """Pick an index with probability proportional to its weight.

        Used by the wheel's "draw" outcome mode, where the odds must be exact
        rather than whatever the geometry happens to produce.
        """
        clean = [max(0.0, float(w)) for w in weights]
        total = sum(clean)
        if total <= 0:
            return self._rng.randrange(len(clean)) if clean else 0
        # random() * total is drawn from the CSPRNG when mode == "system"
        target = self._rng.random() * total
        cumulative = 0.0
        for i, w in enumerate(clean):
            cumulative += w
            if target < cumulative:
                return i
        return len(clean) - 1


# One shared instance; the Wheel tab re-points it when the mode changes.
entropy = Entropy("system")
