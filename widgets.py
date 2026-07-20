"""
widgets.py — shared custom widgets for the settings window.

  LedBlock        the relay LED indicator blocks from the Unity editor panel
  TriggerTimeline scrubbable full-clip timeline with the trim window shaded
                  and every HardwareTrigger drawn as a colored marker
                  (FIRED / NEXT / pending states included)
  PaletteEditor   wheel color list editor
  header()/hline() tiny styling helpers
"""

from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (QColorDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QSizePolicy, QVBoxLayout, QDialog, QWidget)

from config import HardwareTrigger, format_time

from config import relay_color as _relay_color


def trigger_color(trig, relays) -> QColor:
    """Green-ish family per relay; ON is solid, OFF is dimmed."""
    idx = next((i for i, r in enumerate(relays) if r.id == trig.relay_id), 0)
    c = QColor(_relay_color(idx))
    if not trig.state:
        c = c.darker(175)
    return c


def header(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet("color:#35c5d0; font-weight:800; letter-spacing:1px;"
                      "font-size:12px; padding:2px 0;")
    return lbl


def hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet("color:#2a2e36;")
    return f


# ==========================================================================
class LedBlock(QWidget):
    """Port of the editor's relay LED block: glowing dot + 'Motor: ON'."""

    def __init__(self, tag: str, color: QColor, parent=None):
        super().__init__(parent)
        self.tag = tag
        self.color = color
        self.name = tag
        self.on_label = "ON"
        self.off_label = "OFF"
        self.is_on = False
        self.setMinimumSize(150, 44)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

    def configure(self, name: str, on_label: str, off_label: str):
        self.name, self.on_label, self.off_label = name, on_label, off_label
        self.update()

    def set_state(self, on: bool):
        if on != self.is_on:
            self.is_on = on
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        p.setPen(QPen(QColor("#2a2e36"), 1))
        p.setBrush(QColor(22, 25, 30))
        p.drawRoundedRect(r, 7, 7)

        dot = QRectF(12, r.height() / 2 - 7, 14, 14)
        if self.is_on:
            glow = QColor(self.color)
            glow.setAlpha(70)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawEllipse(dot.adjusted(-5, -5, 5, 5))
            p.setBrush(self.color)
        else:
            p.setBrush(QColor(52, 56, 64))
        p.setPen(QPen(QColor(12, 13, 16), 1))
        p.drawEllipse(dot)

        f = QFont(self.font())
        f.setPointSize(8)
        p.setFont(f)
        p.setPen(QColor("#7f8694"))
        p.drawText(QRectF(36, 4, r.width() - 40, 14),
                   Qt.AlignmentFlag.AlignLeft, self.tag)
        f.setPointSize(10)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#e8ecf2") if self.is_on else QColor("#9aa1ad"))
        state = self.on_label if self.is_on else self.off_label
        p.drawText(QRectF(36, 18, r.width() - 40, 20),
                   Qt.AlignmentFlag.AlignLeft, f"{self.name}: {state}")


# ==========================================================================
class TriggerTimeline(QWidget):
    """Full-clip scrub track: shaded trim window, trigger markers, playhead.
    Dragging emits scrub_requested(seconds) — scrubbing outside the trim
    window is allowed for inspection, exactly like the Unity test panel."""

    scrub_requested = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.duration = 0.0
        self.playhead = 0.0
        self.trim_start = 0.0
        self.trim_end = 0.0            # 0 = clip end
        self.triggers: List[HardwareTrigger] = []
        self.relays: List = []          # RelayConfig list, for colour + labels
        self.next_index: Optional[int] = None
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # ---- state ----------------------------------------------------------
    def set_relays(self, relays):
        self.relays = list(relays)
        self.update()

    def set_duration(self, seconds: float):
        self.duration = max(0.0, seconds)
        self.update()

    def set_playhead(self, seconds: float):
        self.playhead = max(0.0, seconds)
        self.update()

    def set_trim(self, start: float, end: float):
        self.trim_start, self.trim_end = start, end
        self.update()

    def set_triggers(self, triggers: List[HardwareTrigger],
                     next_index: Optional[int] = None):
        self.triggers = triggers
        self.next_index = next_index
        self.update()

    # ---- interaction ------------------------------------------------------
    def _time_at(self, x: float) -> float:
        track = self._track_rect()
        if self.duration <= 0 or track.width() <= 0:
            return 0.0
        frac = (x - track.left()) / track.width()
        return max(0.0, min(1.0, frac)) * self.duration

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.duration > 0:
            self.scrub_requested.emit(self._time_at(event.position().x()))

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and self.duration > 0:
            self.scrub_requested.emit(self._time_at(event.position().x()))

    # ---- painting ---------------------------------------------------------
    def _track_rect(self) -> QRectF:
        return QRectF(10, 20, max(10, self.width() - 20), 16)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = self._track_rect()

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(18, 20, 24))
        p.drawRoundedRect(track.adjusted(-4, -4, 4, 4), 8, 8)
        p.setBrush(QColor(38, 42, 50))
        p.drawRoundedRect(track, 6, 6)

        if self.duration > 0:
            # trim window band
            t0 = self.trim_start
            t1 = self.trim_end if self.trim_end > 0 else self.duration
            x0 = track.left() + track.width() * min(1.0, t0 / self.duration)
            x1 = track.left() + track.width() * min(1.0, max(t0, t1) / self.duration)
            p.setBrush(QColor(53, 197, 208, 60))
            p.drawRect(QRectF(x0, track.top(), max(2.0, x1 - x0), track.height()))
            p.setPen(QPen(QColor("#35c5d0"), 2))
            p.drawLine(int(x0), int(track.top() - 3), int(x0), int(track.bottom() + 3))
            p.drawLine(int(x1), int(track.top() - 3), int(x1), int(track.bottom() + 3))

            # trigger markers
            f = QFont(self.font())
            f.setPointSize(7)
            f.setBold(True)
            p.setFont(f)
            for i, trig in enumerate(self.triggers):
                frac = min(1.0, max(0.0, trig.timestamp / self.duration))
                x = track.left() + track.width() * frac
                color = trigger_color(trig, self.relays)
                col = QColor(color)
                if trig.has_fired:
                    col.setAlpha(120)
                pen_w = 4 if (self.next_index is not None
                              and i == self.next_index) else 2
                p.setPen(QPen(col, pen_w))
                p.drawLine(int(x), int(track.top() - 6),
                           int(x), int(track.bottom() + 6))
                p.setPen(col)
                p.drawText(QRectF(x - 12, track.bottom() + 7, 24, 12),
                           Qt.AlignmentFlag.AlignCenter,
                           trig.short(self.relays))

            # playhead
            frac = min(1.0, max(0.0, self.playhead / self.duration))
            x = track.left() + track.width() * frac
            p.setPen(QPen(QColor("#f2f5f9"), 2))
            p.drawLine(int(x), int(track.top() - 8),
                       int(x), int(track.bottom() + 8))
            p.setBrush(QColor("#f2f5f9"))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QRectF(x - 5, track.top() - 13, 10, 10))

        # time labels
        f = QFont(self.font())
        f.setPointSize(8)
        p.setFont(f)
        p.setPen(QColor("#7f8694"))
        p.drawText(QRectF(10, self.height() - 15, 80, 12),
                   Qt.AlignmentFlag.AlignLeft, "0:00")
        p.drawText(QRectF(self.width() - 90, self.height() - 15, 80, 12),
                   Qt.AlignmentFlag.AlignRight, format_time(self.duration))


# ==========================================================================
class PaletteEditor(QWidget):
    """Wheel slice color list. Click a swatch to edit, +/- to grow/shrink."""

    changed = pyqtSignal(list)          # list[str] hex colors

    def __init__(self, colors: List[str], parent=None):
        super().__init__(parent)
        self.colors = list(colors)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._row = QHBoxLayout()
        self._row.setSpacing(6)
        root.addLayout(self._row)

        controls = QHBoxLayout()
        add = QPushButton("+ Add Color")
        rem = QPushButton("\u2212 Remove Last")
        for b in (add, rem):
            b.setMaximumWidth(130)
        add.clicked.connect(self._add)
        rem.clicked.connect(self._remove)
        controls.addWidget(add)
        controls.addWidget(rem)
        controls.addStretch(1)
        root.addLayout(controls)
        self._rebuild()

    def set_colors(self, colors: List[str]):
        self.colors = list(colors)
        self._rebuild()

    def _rebuild(self):
        while self._row.count():
            item = self._row.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for i, c in enumerate(self.colors):
            btn = QPushButton()
            btn.setFixedSize(34, 26)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(f"background:{c}; border:1px solid #0e0f12;"
                              "border-radius:5px;")
            btn.setToolTip(c)
            btn.clicked.connect(lambda _=False, idx=i: self._edit(idx))
            self._row.addWidget(btn)
        self._row.addStretch(1)

    def _edit(self, idx: int):
        col = QColorDialog.getColor(QColor(self.colors[idx]), self,
                                    "Slice Color")
        if col.isValid():
            self.colors[idx] = col.name()
            self._rebuild()
            self.changed.emit(list(self.colors))

    def _add(self):
        col = QColorDialog.getColor(QColor("#3498db"), self, "New Slice Color")
        if col.isValid():
            self.colors.append(col.name())
            self._rebuild()
            self.changed.emit(list(self.colors))

    def _remove(self):
        if len(self.colors) > 1:
            self.colors.pop()
            self._rebuild()
            self.changed.emit(list(self.colors))


# ==========================================================================
class DetachedWindow(QDialog):
    """A real top-level window, so Windows Snap (drag to an edge, or
    Win + arrow keys) works on it exactly like any other application."""

    closed = pyqtSignal()

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.WindowType.Window
                            | Qt.WindowType.WindowMinMaxButtonsHint
                            | Qt.WindowType.WindowCloseButtonHint)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(320, 220)
        self._box = QVBoxLayout(self)
        self._box.setContentsMargins(0, 0, 0, 0)

    def hold(self, widget: QWidget):
        self._box.addWidget(widget)

    def closeEvent(self, event):
        self.closed.emit()
        event.accept()


class DetachablePanel(QWidget):
    """Wraps any widget with a header that can pop it into its own window.

    Detached, it is an ordinary resizable window: drag it to a screen edge to
    snap it, put it on a second monitor, or size it freely. Re-attaching drops
    it back exactly where it was.
    """

    detached = pyqtSignal(bool)

    def __init__(self, title: str, content: QWidget, parent=None):
        super().__init__(parent)
        self.title = title
        self.content = content
        self.window_ref = None
        self._geometry = None

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(6)
        self.caption = QLabel(title)
        self.caption.setStyleSheet(
            "color:#7f8694; font-size:10px; font-weight:700; letter-spacing:1px;")
        bar.addWidget(self.caption)
        bar.addStretch(1)

        self.detach_btn = QPushButton("\u2197  DETACH")
        self.detach_btn.setToolTip(
            "Pop the preview into its own window. Drag it to a screen edge to "
            "snap it, or move it to a second monitor.")
        self.detach_btn.setMaximumWidth(104)
        self.detach_btn.clicked.connect(self.toggle)
        bar.addWidget(self.detach_btn)
        box.addLayout(bar)

        self.holder = QVBoxLayout()
        self.holder.setContentsMargins(0, 0, 0, 0)
        self.holder.addWidget(content)
        box.addLayout(self.holder, 1)

        self.placeholder = QLabel("Preview is in its own window.\n"
                                  "Close it, or press Re-attach, to bring it back.")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet(
            "color:#7f8694; border:1px dashed #3a4049; border-radius:8px;"
            "padding:26px;")
        self.placeholder.hide()
        box.addWidget(self.placeholder, 1)

    # ------------------------------------------------------------------
    def is_detached(self) -> bool:
        return self.window_ref is not None

    def toggle(self):
        self.detach() if not self.is_detached() else self.attach()

    def detach(self):
        if self.is_detached():
            return
        self.holder.removeWidget(self.content)
        win = DetachedWindow(self.title, self)
        win.hold(self.content)
        self.content.show()
        win.closed.connect(self.attach)
        if self._geometry is not None:
            win.restoreGeometry(self._geometry)
        else:
            win.resize(max(640, self.content.width()),
                       max(420, self.content.height()))
        win.show()
        self.window_ref = win
        self.placeholder.show()
        self.detach_btn.setText("\u2199  RE-ATTACH")
        self.detached.emit(True)

    def attach(self):
        if not self.is_detached():
            return
        win, self.window_ref = self.window_ref, None
        self._geometry = win.saveGeometry()
        try:
            win.closed.disconnect(self.attach)
        except TypeError:
            pass
        self.content.setParent(None)
        self.holder.addWidget(self.content)
        self.content.show()
        self.placeholder.hide()
        self.detach_btn.setText("\u2197  DETACH")
        win.deleteLater()
        self.detached.emit(False)


# ==========================================================================
def parse_timecode(text: str, default: float = 0.0) -> float:
    """Accepts 5 / 5.25 / 1:03 / 01:03 / 1:02:03 / 1:02:03.5 -> seconds."""
    text = (text or "").strip().replace(",", ".")
    if not text:
        return default
    parts = text.split(":")
    try:
        if len(parts) == 1:
            return max(0.0, float(parts[0]))
        if len(parts) == 2:
            minutes, seconds = parts
            return max(0.0, int(minutes or 0) * 60 + float(seconds or 0))
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return max(0.0, int(hours or 0) * 3600
                       + int(minutes or 0) * 60 + float(seconds or 0))
    except ValueError:
        return default
    return default


def format_timecode(seconds: float, decimals: int = 2,
                    force_hours: bool = False) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    rest = seconds - hours * 3600 - minutes * 60
    body = f"{rest:0{3 + decimals if decimals else 2}.{decimals}f}" if decimals \
        else f"{int(round(rest)):02d}"
    if hours or force_hours:
        return f"{hours:02d}:{minutes:02d}:{body}"
    return f"{minutes:02d}:{body}"


class TimecodeEdit(QLineEdit):
    """Time field that speaks mm:ss and hh:mm:ss instead of raw seconds.

    Type `1:03`, `01:03`, `1:02:03`, `1:02:03.5` or plain `63` — all mean the
    same thing. Up/Down nudge by a second, with Shift for a tenth.
    """

    value_changed = pyqtSignal(float)

    def __init__(self, seconds: float = 0.0, maximum: float = 359999.0,
                 decimals: int = 2, parent=None):
        super().__init__(parent)
        self._seconds = 0.0
        self._max = maximum
        self._decimals = decimals
        self.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.setPlaceholderText("mm:ss")
        self.setToolTip("Time as mm:ss or hh:mm:ss — 1:03 is one minute three "
                        "seconds. Plain numbers are seconds. Up/Down to nudge.")
        self.set_seconds(seconds)
        self.editingFinished.connect(self._commit)

    def seconds(self) -> float:
        return self._seconds

    def set_maximum(self, maximum: float):
        self._max = max(0.0, float(maximum))

    def set_seconds(self, seconds: float, announce: bool = False):
        value = max(0.0, min(self._max, float(seconds)))
        self._seconds = value
        self.setText(format_timecode(value, self._decimals))
        if announce:
            self.value_changed.emit(value)

    def _commit(self):
        parsed = parse_timecode(self.text(), self._seconds)
        clamped = max(0.0, min(self._max, parsed))
        changed = abs(clamped - self._seconds) > 1e-6
        self._seconds = clamped
        self.setText(format_timecode(clamped, self._decimals))
        if changed:
            self.value_changed.emit(clamped)

    def keyPressEvent(self, event):
        step = 0.1 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0
        if event.key() == Qt.Key.Key_Up:
            self.set_seconds(parse_timecode(self.text(), self._seconds) + step,
                             announce=True)
            return
        if event.key() == Qt.Key.Key_Down:
            self.set_seconds(parse_timecode(self.text(), self._seconds) - step,
                             announce=True)
            return
        super().keyPressEvent(event)
