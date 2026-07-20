"""
wheel_widget.py — physics prize wheel.

Port of WheelController.cs:
  * weighted slices (chance -> slice angle), auto-colored with the
    no-adjacent-duplicates palette walk (GetUniqueColorIndex)
  * SpinPhysics(): motor phase toward a randomized target speed, then a coast
    phase with per-spin randomized friction (0.4x–2.0x), a mid-coast random
    nudge, and the |omega| < 0.5 stop condition
  * IdentifyWinner(): picker fixed at 12 o'clock

Unity applied torque through a Rigidbody2D; here torque-ish numbers keep their
inspector names and are divided by UNITY_TORQUE_DIVISOR when integrated, so the
familiar values (200000 acceleration, 3000–18000 kick) still feel right.
"""

from __future__ import annotations

import math
from rng import entropy
import time
from typing import List, Optional, Tuple

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QPainter,
                         QPainterPath, QPen, QPolygonF, QRadialGradient)
from PyQt6.QtWidgets import QSizePolicy, QWidget

from config import WheelSettings

UNITY_TORQUE_DIVISOR = 60.0     # AddTorque numbers -> deg/s^2 (see README)
STOP_EPSILON = 0.5              # WheelController Update() stop window
COAST_SAFETY_LIMIT = 45.0       # never coast longer than this (failsafe)


class WheelWidget(QWidget):
    spin_ended = pyqtSignal(int)                 # onSpinEnd(winnerIndex)
    spin_started = pyqtSignal()

    def __init__(self, settings: WheelSettings, parent=None):
        super().__init__(parent)
        self.ws = settings
        self.pieces: List[Tuple[str, float]] = []     # (label, chance)
        self._colors: List[QColor] = []
        self._total_weight = 1.0

        self.rotation = 0.0        # clockwise degrees
        self.omega = 0.0           # clockwise deg/s
        self._phase = "idle"       # idle | motor | coast | draw
        self._draw_from = 0.0
        self._draw_delta = 0.0
        self._draw_winner = 0
        self._draw_elapsed = 0.0
        self._draw_duration = 5.0
        self._drag = 0.0
        self._target_speed = 0.0
        self._motor_left = 0.0
        self._coast_friction = 0.0
        self._coast_time = 0.0
        self._disturb_delay = 0.0
        self._disturb_done = True
        self._winner_flash: Optional[int] = None
        self._flash_t = 0.0
        self._last_t = time.monotonic()

        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setMinimumSize(240, 240)

    # ------------------------------------------------------------ configuration
    def set_settings(self, ws: WheelSettings):
        self.ws = ws
        self._rebuild_colors()
        self.update()

    def set_pieces(self, pieces: List[Tuple[str, float]]):
        """GenerateWheel port — also normalizes an all-zero weight list."""
        clean = [(str(lbl), max(0.0, float(w))) for lbl, w in pieces]
        total = sum(w for _, w in clean)
        if clean and total <= 0.0:
            clean = [(lbl, 1.0) for lbl, _ in clean]
            total = float(len(clean))
        self.pieces = clean
        self._total_weight = total if total > 0 else 1.0
        self._rebuild_colors()
        self.update()

    def _rebuild_colors(self):
        palette = [QColor(c) for c in (self.ws.colors or ["#3498db"])]
        palette = [c if c.isValid() else QColor("#3498db") for c in palette]
        assigned: List[int] = []
        out: List[QColor] = []
        n = len(self.pieces)
        for i in range(n):
            idx = self._unique_color_index(i, n, assigned, len(palette))
            assigned.append(idx)
            out.append(palette[idx])
        self._colors = out

    @staticmethod
    def _unique_color_index(i: int, total: int, prev: List[int], ncolors: int) -> int:
        """GetUniqueColorIndex port: avoid matching the previous slice and,
        for the last slice, the first one (wrap-around)."""
        if ncolors <= 1:
            return 0
        for k in range(ncolors):
            cand = (i + k) % ncolors
            conflict = False
            if i > 0 and cand == prev[i - 1]:
                conflict = True
            if i == total - 1 and prev and cand == prev[0]:
                conflict = True
            if not conflict:
                return cand
        return i % ncolors

    # ------------------------------------------------------------ physics
    @property
    def is_spinning(self) -> bool:
        return self._phase != "idle"

    def spin(self):
        """Entry point used by the show. Honours WheelSettings.outcome_mode."""
        entropy.set_mode(getattr(self.ws, "entropy", "system"))
        if getattr(self.ws, "outcome_mode", "physics") == "draw":
            self.spin_draw()
        else:
            self.spin_physics()

    def spin_draw(self):
        """Exact-odds mode: the winner is drawn from the OS cryptographic RNG
        using the real weights, then the wheel is animated so it lands there.
        Same look, but the probabilities are mathematically exact and the
        result cannot be predicted from previous spins."""
        if self.is_spinning or not self.pieces:
            return
        ws = self.ws
        self._winner_flash = None
        weights = [w for _, w in self.pieces]
        winner = entropy.weighted_index(weights)

        # angle (measured clockwise from the pointer) that lands on `winner`
        cursor = 0.0
        for i, (_, w) in enumerate(self.pieces):
            span = (w / self._total_weight) * 360.0
            if i == winner:
                inside = cursor + span * entropy.uniform(0.18, 0.82)
                break
            cursor += span
        else:
            inside = 0.0
        final_mod = (360.0 - inside) % 360.0

        turns = entropy.randint(int(getattr(ws, "draw_min_turns", 3)),
                                int(getattr(ws, "draw_max_turns", 7)))
        start = self.rotation % 360.0
        delta = (final_mod - start) % 360.0 + 360.0 * max(1, turns)

        self._draw_from = self.rotation
        self._draw_delta = delta
        self._draw_winner = winner
        self._draw_elapsed = 0.0
        self._draw_duration = max(1.0, entropy.uniform(
            getattr(ws, "draw_min_time", 4.0), getattr(ws, "draw_max_time", 9.0)))
        self._phase = "draw"
        self.omega = 0.0
        self._drag = 0.0
        self._last_t = time.monotonic()
        if not self._timer.isActive():
            self._timer.start()
        self.spin_started.emit()

    def spin_physics(self):
        """SpinSequence port."""
        if self.is_spinning or not self.pieces:
            return
        ws = self.ws
        self._winner_flash = None
        self.omega = 0.0
        self._drag = 0.0
        self._target_speed = max(120.0, ws.target_max_speed +
                                 entropy.uniform(-ws.speed_variance, ws.speed_variance))
        lo, hi = sorted((ws.min_spin_time, ws.max_spin_time))
        self._motor_left = entropy.uniform(max(0.05, lo), max(0.06, hi))
        self._coast_friction = max(0.05, ws.bearing_friction *
                                   entropy.uniform(0.4, 2.0))
        # initial kick:  rb.AddTorque(Random.Range(-3000, -18000))
        self.omega += entropy.uniform(3000.0, 18000.0) / UNITY_TORQUE_DIVISOR
        self._phase = "motor"
        self._coast_time = 0.0
        self._disturb_done = True
        self._last_t = time.monotonic()
        if not self._timer.isActive():
            self._timer.start()
        self.spin_started.emit()

    def reset_spin_state(self):
        """ResetSpinState port — external interruption never leaves the wheel
        stuck in 'isSpinning'."""
        self._phase = "idle"
        self.omega = 0.0
        self._drag = self.ws.bearing_friction
        self._winner_flash = None
        if self._timer.isActive():
            self._timer.stop()
        self.update()

    def flash_winner(self, index: int):
        self._winner_flash = index
        self._flash_t = 0.0
        if not self._timer.isActive():
            self._timer.start()

    def _tick(self):
        """Advance the wheel by real elapsed time.

        Windows throttles timers for windows that are not in front. Adding a
        clamped dt each tick therefore stretched a 6-second spin into half a
        minute of wall clock, which looks exactly like a wheel that says
        SPINNING and refuses to move. Catch up in small fixed steps instead, so
        the spin takes the same time whether or not the window has focus.
        """
        now = time.monotonic()
        elapsed = max(0.0, now - self._last_t)
        self._last_t = now
        if elapsed > 1.0:
            elapsed = 1.0                  # returning from a long freeze
        steps = 1
        if elapsed > 0.033:
            steps = min(64, int(elapsed / 0.016) + 1)
        step_dt = elapsed / steps if steps else elapsed
        for _ in range(steps):
            if self._phase == "idle" and self._winner_flash is None:
                break
            self._step(max(0.0001, step_dt))
        self.update()

    def _step(self, dt: float):

        if self._phase == "draw":
            self._draw_elapsed += dt
            t = min(1.0, self._draw_elapsed / self._draw_duration)
            # quintic ease-out: fast launch, long believable settle
            eased = 1.0 - pow(1.0 - t, 5)
            self.rotation = (self._draw_from + self._draw_delta * eased) % 360.0
            if t >= 1.0:
                self._phase = "idle"
                self.omega = 0.0
                winner = self._draw_winner
                self.flash_winner(winner)
                self.spin_ended.emit(winner)
            if self._winner_flash is not None:
                self._flash_t += dt
                if self._flash_t > 2.2:
                    self._winner_flash = None
            if self._phase == "idle" and self._winner_flash is None:
                self._timer.stop()
            self.update()
            return

        if self._phase == "motor":
            if abs(self.omega) < self._target_speed:
                self.omega += (self.ws.acceleration_power /
                               UNITY_TORQUE_DIVISOR) * dt
            self._motor_left -= dt
            if self._motor_left <= 0.0:
                self._phase = "coast"
                self._drag = self._coast_friction
                self._coast_time = 0.0
                self._disturb_delay = entropy.uniform(0.2, 1.8)
                self._disturb_done = False

        elif self._phase == "coast":
            self._coast_time += dt
            if not self._disturb_done:
                if abs(self.omega) <= 5.0:
                    self._disturb_done = True         # too slow — no nudge
                elif self._coast_time >= self._disturb_delay:
                    self._disturb_done = True
                    nudge = abs(self.omega) * entropy.uniform(0.0, 0.20)
                    self.omega += math.copysign(nudge, self.omega)
            if self._coast_time > COAST_SAFETY_LIMIT:
                self.omega = 0.0

        if self._drag > 0.0:
            self.omega *= max(0.0, 1.0 - self._drag * dt)

        self.rotation = (self.rotation + self.omega * dt) % 360.0

        # WheelController.Update() stop condition
        if (self._phase != "idle" and self._drag > 0.0
                and -STOP_EPSILON < self.omega < STOP_EPSILON):
            self.omega = 0.0
            self._phase = "idle"
            winner = self._identify_winner()
            self.flash_winner(winner)
            self.spin_ended.emit(winner)

        if self._winner_flash is not None:
            self._flash_t += dt
            if self._flash_t > 2.2:
                self._winner_flash = None

        if self._phase == "idle" and self._winner_flash is None:
            self._timer.stop()

    def _identify_winner(self) -> int:
        """IdentifyWinner port. Picker sits at 12 o'clock; slices are laid
        clockwise from the top, so the slice under the pointer is the one whose
        span contains (360 - rotation) mod 360."""
        if not self.pieces:
            return -1
        a = (360.0 - (self.rotation % 360.0)) % 360.0
        cursor = 0.0
        for i, (_, w) in enumerate(self.pieces):
            span = (w / self._total_weight) * 360.0
            if cursor <= a < cursor + span:
                return i
            cursor += span
        return len(self.pieces) - 1

    # ------------------------------------------------------------ painting
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        w, h = self.width(), self.height()
        r = min(w, h) * 0.5 - 22
        if r < 40:
            return
        cx, cy = w * 0.5, h * 0.5
        wheel_rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)

        # outer rim ---------------------------------------------------------
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(16, 18, 22))
        p.drawEllipse(wheel_rect.adjusted(-12, -12, 12, 12))
        p.setBrush(QColor(42, 46, 54))
        p.drawEllipse(wheel_rect.adjusted(-7, -7, 7, 7))

        if not self.pieces:
            p.setBrush(QColor(28, 30, 36))
            p.drawEllipse(wheel_rect)
            p.setPen(QColor(120, 126, 138))
            f = QFont(self.font())
            f.setPointSizeF(max(9.0, r * 0.08))
            p.setFont(f)
            p.drawText(wheel_rect, Qt.AlignmentFlag.AlignCenter,
                       "No slices defined")
            self._draw_pointer(p, cx, cy, r)
            return

        # slices (painter.rotate is clockwise-positive in screen coords) -----
        p.save()
        p.translate(cx, cy)
        p.rotate(self.rotation)
        rect0 = QRectF(-r, -r, 2 * r, 2 * r)
        outline = QPen(QColor(14, 15, 18), max(1.0, r * 0.008))

        cursor = 0.0
        for i, (label, wgt) in enumerate(self.pieces):
            span = (wgt / self._total_weight) * 360.0
            color = self._colors[i] if i < len(self._colors) else QColor("#3498db")
            p.setPen(outline)
            p.setBrush(QBrush(color))
            # Qt pie angles: 0 = 3 o'clock, CCW positive. Clockwise-from-top c
            # maps to (90 - c); negative span sweeps clockwise.
            p.drawPie(rect0, round((90.0 - cursor) * 16), round(-span * 16))
            cursor += span

        # winner flash overlay
        if self._winner_flash is not None and 0 <= self._winner_flash < len(self.pieces):
            cursor = 0.0
            pulse = 0.28 + 0.24 * math.sin(self._flash_t * 9.0)
            for i, (_, wgt) in enumerate(self.pieces):
                span = (wgt / self._total_weight) * 360.0
                if i == self._winner_flash:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor(255, 255, 255, int(255 * max(0.0, pulse))))
                    p.drawPie(rect0, round((90.0 - cursor) * 16), round(-span * 16))
                    break
                cursor += span

        # labels (tangential, like the Unity slice prefab text) --------------
        cursor = 0.0
        for i, (label, wgt) in enumerate(self.pieces):
            span = (wgt / self._total_weight) * 360.0
            self._draw_label(p, label, cursor + span * 0.5, span, r)
            cursor += span
        p.restore()

        # hub -----------------------------------------------------------------
        hub_r = r * 0.16
        grad = QRadialGradient(QPointF(cx, cy), hub_r)
        grad.setColorAt(0.0, QColor(64, 70, 82))
        grad.setColorAt(1.0, QColor(22, 24, 28))
        p.setPen(QPen(QColor(10, 11, 13), 2))
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(cx, cy), hub_r, hub_r)

        self._draw_pointer(p, cx, cy, r)

    def _draw_label(self, p: QPainter, text: str, bisector: float,
                    span: float, r: float):
        if not text:
            return
        p.save()
        p.rotate(bisector)
        radius = r * max(0.2, min(0.95, self.ws.text_padding))
        chord = 2.0 * radius * math.sin(math.radians(min(170.0, span) * 0.5)) * 0.9
        chord = max(chord, r * 0.18)

        f = QFont(self.font())
        f.setBold(True)
        size = max(7.0, min(r * 0.085, 26.0))
        f.setPointSizeF(size)
        fm = QFontMetricsF(f)
        shown = text
        while size > 7.0 and fm.horizontalAdvance(shown) > chord:
            size -= 1.0
            f.setPointSizeF(size)
            fm = QFontMetricsF(f)
        if fm.horizontalAdvance(shown) > chord:
            shown = fm.elidedText(shown, Qt.TextElideMode.ElideRight, int(chord))
        p.setFont(f)

        rect = QRectF(-chord / 2, -radius - fm.height() * 0.5,
                      chord, fm.height())
        p.setPen(QColor(0, 0, 0, 150))
        p.drawText(rect.translated(1.2, 1.2), Qt.AlignmentFlag.AlignCenter, shown)
        p.setPen(QColor(255, 255, 255))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, shown)
        p.restore()

    def _draw_pointer(self, p: QPainter, cx: float, cy: float, r: float):
        tip = QPointF(cx, cy - r + max(10.0, r * 0.10))
        base = cy - r - max(8.0, r * 0.06)
        half = max(9.0, r * 0.05)
        tri = QPolygonF([QPointF(cx - half, base), QPointF(cx + half, base), tip])
        path = QPainterPath()
        path.addPolygon(tri)
        path.closeSubpath()
        p.setPen(QPen(QColor(12, 12, 14), 2.5))
        p.setBrush(QColor(235, 238, 244))
        p.drawPath(path)
        p.setBrush(QColor(255, 82, 82))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, base - 1), half * 0.42, half * 0.42)
