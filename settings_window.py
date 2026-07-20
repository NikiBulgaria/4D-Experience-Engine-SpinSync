"""
settings_window.py — the control room, reachable while the show keeps running.

Left-hand buttons switch pages:

  ▶ EDITOR     playlist, trims, triggers, resizable preview + Trigger Tester
  ◈ GAME FLOW  questions, answers, effects, sub-questions
  ◎ WHEEL      physics, fairness model, palette, live spinnable preview
  ⚡ ESP        connection, protocol, relay tester, console, traffic, diagnostics
  🎛 SHOW       live transport, audio, on-screen timecode and overlays
  ⚙ GENERAL    timings, labels, kiosk options, config files

Nothing here pauses the show: open it mid-video, change something, and the
running playback picks it up immediately.
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Tuple

import json

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QGuiApplication, QKeySequence, QShortcut
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox,
                             QDoubleSpinBox, QFileDialog, QFrame, QGridLayout,
                             QGroupBox, QHeaderView, QHBoxLayout, QLabel, QLineEdit,
                             QListWidget, QMainWindow, QMessageBox,
                             QProgressBar, QPushButton, QScrollArea, QSlider,
                             QMenu, QSpinBox, QStyle, QSplitter, QStackedWidget,
                             QTableWidget, QVBoxLayout, QWidget)

import esp_link
from config import (AppConfig, ESP32_S3_SAFE_PINS, HardwareTrigger,
                    RelayConfig, VideoScenario, next_relay_id, pin_warning,
                    relay_color)
from esp_link import EspLink, RelayBank
from game_engine import GameEngine
from question_editor import GameFlowEditor
from rng import entropy
from test_panel import TestModePanel
from wheel_widget import WheelWidget
from widgets import (LedBlock, PaletteEditor, TimecodeEdit, TriggerTimeline,
                     header, hline)


def _scroll(widget: QWidget) -> QScrollArea:
    """Wrap a panel so it stays reachable however small the window gets."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(widget)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setMinimumWidth(120)
    return area


def _form(spacing: int = 10, label_width: int = 190) -> QGridLayout:
    g = QGridLayout()
    g.setHorizontalSpacing(14)
    g.setVerticalSpacing(spacing)
    g.setColumnMinimumWidth(0, label_width)
    g.setColumnStretch(1, 1)
    return g


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet("color:#7f8694; font-size:11px;")
    return lbl


# ==========================================================================
class PlaylistTab(QWidget):
    changed = pyqtSignal()
    playlist_mutated = pyqtSignal()      # count/labels changed

    def __init__(self, cfg: AppConfig, test_panel: TestModePanel, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.test_panel = test_panel
        self._loading = False

        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.setHandleWidth(8)
        split.setChildrenCollapsible(False)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.addWidget(split)

        # ---------------- left: playlist + editor --------------------------
        left = QWidget()
        left.setMinimumWidth(240)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(10, 8, 8, 8)
        lv.setSpacing(8)
        lv.addWidget(header("\u25b6  VIDEO PLAYLIST"))

        self.vlist = QListWidget()
        self.vlist.currentRowChanged.connect(self._select)
        test_panel.video_selected.connect(self._follow_preview)
        lv.addWidget(self.vlist, 1)

        btns = QHBoxLayout()
        for text, slot, w in (("+ Video", self._add, None),
                              ("Duplicate", self._dup, None),
                              ("\u25b2", lambda: self._move(-1), 30),
                              ("\u25bc", lambda: self._move(+1), 30),
                              ("\u2715", self._remove, 30)):
            b = QPushButton(text)
            if w:
                b.setFixedWidth(w)
            if text == "\u2715":
                b.setStyleSheet("QPushButton{color:#ff5c5c;}")
            b.clicked.connect(slot)
            btns.addWidget(b)
        lv.addLayout(btns)
        lv.addWidget(hline())

        form = QWidget()
        fv = QVBoxLayout(form)
        fv.setContentsMargins(0, 0, 8, 0)
        fv.setSpacing(10)

        g = QGridLayout()
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(9)
        g.setColumnMinimumWidth(0, 150)
        g.setColumnStretch(1, 1)
        g.addWidget(QLabel("Display Name"), 0, 0)
        self.label_edit = QLineEdit()
        self.label_edit.textChanged.connect(self._label_changed)
        g.addWidget(self.label_edit, 0, 1, 1, 2)

        g.addWidget(QLabel("Clip (file)"), 1, 0)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("path to video file…")
        self.path_edit.textChanged.connect(
            lambda t: self._write(lambda s: setattr(s, "path", t)))
        g.addWidget(self.path_edit, 1, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        g.addWidget(browse, 1, 2)

        self.skip_check = QCheckBox("Skip (exclude from auto wheel)")
        self.skip_check.toggled.connect(
            lambda v: self._write(lambda s: setattr(s, "skip", v)))
        g.addWidget(self.skip_check, 2, 0, 1, 2)
        self.force_check = QCheckBox("Force (only forced videos on auto wheel)")
        self.force_check.setToolTip("If any video is forced, auto-built "
                                    "wheels contain only forced videos.")
        self.force_check.toggled.connect(
            lambda v: self._write(lambda s: setattr(s, "force_choice", v)))
        g.addWidget(self.force_check, 3, 0, 1, 3)

        self.custom_check = QCheckBox("Custom %")
        self.custom_check.toggled.connect(self._custom_toggled)
        g.addWidget(self.custom_check, 4, 0)
        self.chance_spin = QDoubleSpinBox()
        self.chance_spin.setRange(0, 100)
        self.chance_spin.valueChanged.connect(
            lambda v: self._write(lambda s: setattr(s, "chance_weight", v)))
        g.addWidget(self.chance_spin, 4, 1)

        g.addWidget(QLabel("Trim IN (s)"), 5, 0)
        self.trim_in = QDoubleSpinBox()
        self.trim_in.setRange(0, 359999)
        self.trim_in.setDecimals(2)
        self.trim_in.valueChanged.connect(
            lambda v: self._write(lambda s: setattr(s, "start_time", v)))
        g.addWidget(self.trim_in, 5, 1)
        g.addWidget(QLabel("Trim OUT (s)"), 6, 0)
        self.trim_out = QDoubleSpinBox()
        self.trim_out.setRange(0, 359999)
        self.trim_out.setDecimals(2)
        self.trim_out.valueChanged.connect(
            lambda v: self._write(lambda s: setattr(s, "end_time", v)))
        g.addWidget(self.trim_out, 6, 1)
        fv.addLayout(g)

        self.trig_header = header("\u26a1 TRIGGERS (0)")
        fv.addWidget(self.trig_header)
        self.trig_table = QTableWidget(0, 4)
        self.trig_table.setHorizontalHeaderLabels(["Time", "Relay", "State", ""])
        self.trig_table.verticalHeader().setVisible(False)
        # A leftover from the three-column layout set column 2 to 40 px right
        # after setting it to 96, which is why the State column only ever
        # showed "O". Resize modes now do the job properly: the relay name
        # takes the slack, everything else asks for exactly what it needs.
        head = self.trig_table.horizontalHeader()
        head.setStretchLastSection(False)
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.trig_table.setColumnWidth(0, 104)
        self.trig_table.setColumnWidth(2, 88)
        self.trig_table.setMinimumHeight(170)
        self.trig_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        fv.addWidget(self.trig_table)

        trow = QHBoxLayout()
        addt = QPushButton("+ ADD TRIGGER")
        addt.clicked.connect(self._add_trigger)
        sortt = QPushButton("Sort by time")
        sortt.clicked.connect(self._sort_triggers)
        trow.addWidget(addt)
        trow.addWidget(sortt)
        trow.addStretch(1)
        fv.addLayout(trow)
        fv.addStretch(1)

        form.setMinimumWidth(0)
        lv.addWidget(_scroll(form), 2)
        split.addWidget(left)

        # ---------------- right: test mode ----------------------------------
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(_scroll(test_panel))
        split.addWidget(right)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 6)

        test_panel.trim_sync_cb = self._pull_trims
        self.refresh_list(select=0)

    # ---------------- selection / form load --------------------------------
    def scenario(self) -> Optional[VideoScenario]:
        row = self.vlist.currentRow()
        if 0 <= row < len(self.cfg.playlist):
            return self.cfg.playlist[row]
        return None

    def refresh_list(self, select: Optional[int] = None):
        cur = self.vlist.currentRow() if select is None else select
        self.vlist.blockSignals(True)
        self.vlist.clear()
        for i, s in enumerate(self.cfg.playlist):
            flags = "".join(f for f, on in (("S", s.skip), ("F", s.force_choice))
                            if on)
            suffix = f"  [{flags}]" if flags else ""
            self.vlist.addItem(f"Video {i} \u00b7 {s.wheel_label}{suffix}")
        self.vlist.blockSignals(False)
        if self.cfg.playlist:
            cur = max(0, min(cur if cur is not None else 0,
                             len(self.cfg.playlist) - 1))
            self.vlist.setCurrentRow(cur)
        self._load_form()

    def _select(self, row: int):
        self._load_form()
        # keep the preview on the same clip the list is showing
        if row >= 0:
            self.test_panel.select_video(row)

    def _follow_preview(self, index: int):
        """The preview dropdown changed — move the list selection with it."""
        if 0 <= index < self.vlist.count() and self.vlist.currentRow() != index:
            self.vlist.blockSignals(True)
            self.vlist.setCurrentRow(index)
            self.vlist.blockSignals(False)
            self._load_form()

    def _load_form(self):
        s = self.scenario()
        self._loading = True
        enabled = s is not None
        for w in (self.label_edit, self.path_edit, self.skip_check,
                  self.force_check, self.custom_check, self.chance_spin,
                  self.trim_in, self.trim_out, self.trig_table):
            w.setEnabled(enabled)
        if s is not None:
            self.label_edit.setText(s.wheel_label)
            self.path_edit.setText(s.path)
            self.skip_check.setChecked(s.skip)
            self.force_check.setChecked(s.force_choice)
            self.custom_check.setChecked(s.use_custom_chance)
            self.chance_spin.setValue(s.chance_weight)
            self.chance_spin.setEnabled(s.use_custom_chance)
            self.trim_in.setValue(s.start_time)
            self.trim_out.setValue(s.end_time)
        else:
            self.label_edit.clear()
            self.path_edit.clear()
        self._loading = False
        self._rebuild_triggers()

    def _pull_trims(self):
        """Test panel edited the trims from the playhead — mirror them here."""
        s = self.scenario()
        if s is None:
            return
        self._loading = True
        self.trim_in.setValue(s.start_time)
        self.trim_out.setValue(s.end_time)
        self._loading = False
        self.changed.emit()

    # ---------------- write-through ----------------------------------------
    def _write(self, setter):
        if self._loading:
            return
        s = self.scenario()
        if s is None:
            return
        setter(s)
        self.test_panel.refresh_from_scenario()
        self.changed.emit()

    def _label_changed(self, text: str):
        if self._loading:
            return
        s = self.scenario()
        if s is None:
            return
        s.wheel_label = text
        row = self.vlist.currentRow()
        if row >= 0:
            self.vlist.item(row).setText(f"Video {row} \u00b7 {text}")
        self.test_panel.reload_playlist()
        self.playlist_mutated.emit()
        self.changed.emit()

    def _custom_toggled(self, on: bool):
        self.chance_spin.setEnabled(on)
        self._write(lambda s: setattr(s, "use_custom_chance", on))

    def _browse(self):
        s = self.scenario()
        if s is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose video clip", s.path or "",
            "Videos (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv);;All files (*)")
        if path:
            self.path_edit.setText(path)

    # ---------------- list mutations -----------------------------------------
    def _add(self):
        self.cfg.playlist.append(VideoScenario(
            wheel_label=f"Video {len(self.cfg.playlist)}"))
        self._after_mutation(len(self.cfg.playlist) - 1)

    def _dup(self):
        s = self.scenario()
        if s is None:
            return
        clone = VideoScenario.from_dict(s.to_dict())
        clone.wheel_label += " (copy)"
        self.cfg.playlist.insert(self.vlist.currentRow() + 1, clone)
        self._after_mutation(self.vlist.currentRow() + 1)

    def _remove(self):
        row = self.vlist.currentRow()
        if 0 <= row < len(self.cfg.playlist):
            del self.cfg.playlist[row]
            self._after_mutation(max(0, row - 1))

    def _move(self, delta: int):
        row = self.vlist.currentRow()
        j = row + delta
        pl = self.cfg.playlist
        if 0 <= row < len(pl) and 0 <= j < len(pl):
            pl[row], pl[j] = pl[j], pl[row]
            self._after_mutation(j)

    def _after_mutation(self, select: int):
        self.refresh_list(select=select)
        self.test_panel.reload_playlist()
        self.playlist_mutated.emit()
        self.changed.emit()

    # ---------------- triggers -------------------------------------------------
    def _rebuild_triggers(self):
        s = self.scenario()
        triggers = s.triggers if s else []
        self.trig_header.setText(f"\u26a1 TRIGGERS ({len(triggers)})")
        self.trig_table.setRowCount(len(triggers))
        for i, trig in enumerate(triggers):
            spin = TimecodeEdit(trig.timestamp, decimals=2)
            spin.value_changed.connect(
                lambda v, t=trig: self._trigger_edit(t, "timestamp", v))
            self.trig_table.setCellWidget(i, 0, spin)

            # Relay and state are separate controls: pick the output in one
            # column, what it should do in the next.
            relay_combo = QComboBox()
            for relay in self.cfg.esp.relays:
                relay_combo.addItem(relay.name, relay.id)
            current = relay_combo.findData(trig.relay_id)
            if current < 0:                     # the relay was deleted
                relay_combo.addItem(f"(missing {trig.relay_id})", trig.relay_id)
                current = relay_combo.count() - 1
            relay_combo.setCurrentIndex(current)
            relay_combo.currentIndexChanged.connect(
                lambda idx, t=trig, c=relay_combo:
                self._trigger_edit(t, "relay_id", c.itemData(idx)))
            self.trig_table.setCellWidget(i, 1, relay_combo)

            state_combo = QComboBox()
            state_combo.addItem("ON", "1")
            state_combo.addItem("OFF", "0")
            state_combo.setCurrentIndex(0 if trig.state else 1)
            state_combo.currentIndexChanged.connect(
                lambda idx, t=trig, c=state_combo:
                self._trigger_edit(t, "state", c.itemData(idx) == "1"))
            self.trig_table.setCellWidget(i, 2, state_combo)

            rm = QPushButton(" Delete")
            rm.setIcon(self.style().standardIcon(
                QStyle.StandardPixmap.SP_DialogDiscardButton))
            rm.setToolTip("Remove this trigger")
            rm.setStyleSheet("QPushButton{color:#ff8f8f;}")
            rm.clicked.connect(lambda _=False, t=trig: self._del_trigger(t))
            self.trig_table.setCellWidget(i, 3, rm)

    def _trigger_edit(self, trig: HardwareTrigger, attr: str, value):
        setattr(trig, attr, value)
        self.test_panel.refresh_from_scenario()
        self.changed.emit()

    def _trigger_target(self, trig: HardwareTrigger, data):
        if not data or ":" not in str(data):
            return
        relay_id, _, flag = str(data).rpartition(":")
        trig.relay_id, trig.state = relay_id, flag == "1"
        self.test_panel.refresh_from_scenario()
        self.changed.emit()

    def _add_trigger(self):
        s = self.scenario()
        if s is None:
            return
        t = self.test_panel.test_scrub_time if self.test_panel.test_mode_active else 0.0
        first = self.cfg.esp.relays[0].id if self.cfg.esp.relays else "r1"
        s.triggers.append(HardwareTrigger(timestamp=round(t, 2),
                                          relay_id=first, state=True))
        self._rebuild_triggers()
        self.test_panel.refresh_from_scenario()
        self.changed.emit()

    def _del_trigger(self, trig: HardwareTrigger):
        s = self.scenario()
        if s and trig in s.triggers:
            s.triggers.remove(trig)
            self._rebuild_triggers()
            self.test_panel.refresh_from_scenario()
            self.changed.emit()

    def _sort_triggers(self):
        s = self.scenario()
        if s:
            s.triggers.sort(key=lambda t: t.timestamp)
            self._rebuild_triggers()
            self.test_panel.refresh_from_scenario()
            self.changed.emit()



# ==========================================================================
#  WHEEL
# ==========================================================================
class WheelTab(QWidget):
    changed = pyqtSignal()

    def __init__(self, cfg: AppConfig,
                 sample_provider: Callable[[], List[Tuple[str, float]]],
                 parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.sample_provider = sample_provider

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(14)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 8, 0)
        lv.setSpacing(10)

        ws = cfg.wheel

        # ---------------- fairness / randomness ----------------
        lv.addWidget(header("\U0001F3B2  RANDOMNESS"))
        rg = _form()
        rg.addWidget(QLabel("Entropy source"), 0, 0)
        self.entropy_combo = QComboBox()
        self.entropy_combo.addItem("OS cryptographic (unpredictable)", "system")
        self.entropy_combo.addItem("Fast pseudo-random (repeatable)", "fast")
        self.entropy_combo.setCurrentIndex(0 if ws.entropy != "fast" else 1)
        self.entropy_combo.currentIndexChanged.connect(self._entropy_changed)
        rg.addWidget(self.entropy_combo, 0, 1)

        rg.addWidget(QLabel("Outcome model"), 1, 0)
        self.outcome_combo = QComboBox()
        self.outcome_combo.addItem("Physics — whatever it lands on", "physics")
        self.outcome_combo.addItem("Draw — exact odds, then animate", "draw")
        self.outcome_combo.setCurrentIndex(1 if ws.outcome_mode == "draw" else 0)
        self.outcome_combo.currentIndexChanged.connect(self._outcome_changed)
        rg.addWidget(self.outcome_combo, 1, 1)
        lv.addLayout(rg)

        self.entropy_note = _hint("")
        lv.addWidget(self.entropy_note)

        self.draw_box = QGroupBox("DRAW-MODE SPIN LENGTH")
        dg = _form(8, 150)
        self.draw_min = QDoubleSpinBox(); self.draw_min.setRange(0.5, 60)
        self.draw_min.setValue(ws.draw_min_time); self.draw_min.setSuffix(" s")
        self.draw_min.valueChanged.connect(lambda v: self._set("draw_min_time", v))
        self.draw_max = QDoubleSpinBox(); self.draw_max.setRange(0.5, 60)
        self.draw_max.setValue(ws.draw_max_time); self.draw_max.setSuffix(" s")
        self.draw_max.valueChanged.connect(lambda v: self._set("draw_max_time", v))
        self.turn_min = QSpinBox(); self.turn_min.setRange(1, 40)
        self.turn_min.setValue(ws.draw_min_turns)
        self.turn_min.valueChanged.connect(lambda v: self._set("draw_min_turns", v))
        self.turn_max = QSpinBox(); self.turn_max.setRange(1, 60)
        self.turn_max.setValue(ws.draw_max_turns)
        self.turn_max.valueChanged.connect(lambda v: self._set("draw_max_turns", v))
        dg.addWidget(QLabel("Shortest spin"), 0, 0); dg.addWidget(self.draw_min, 0, 1)
        dg.addWidget(QLabel("Longest spin"), 1, 0); dg.addWidget(self.draw_max, 1, 1)
        dg.addWidget(QLabel("Fewest turns"), 2, 0); dg.addWidget(self.turn_min, 2, 1)
        dg.addWidget(QLabel("Most turns"), 3, 0); dg.addWidget(self.turn_max, 3, 1)
        self.draw_box.setLayout(dg)
        lv.addWidget(self.draw_box)

        lv.addWidget(hline())
        lv.addWidget(header("\u26a1  PHYSICS"))
        g = _form(9, 150)
        self._spins = {}

        def add_row(row, label, attr, lo, hi, step, hint):
            g.addWidget(QLabel(label), row, 0)
            sp = QDoubleSpinBox()
            sp.setRange(lo, hi)
            sp.setSingleStep(step)
            sp.setDecimals(2 if hi <= 100 else 0)
            sp.setValue(getattr(ws, attr))
            sp.valueChanged.connect(lambda v, a=attr: self._set(a, v))
            g.addWidget(sp, row, 1)
            g.addWidget(_hint(hint), row, 2)
            self._spins[attr] = sp

        add_row(0, "Max Speed", "target_max_speed", 100, 20000, 50, "deg/s")
        add_row(1, "Speed Variance", "speed_variance", 0, 10000, 25, "+/- deg/s")
        add_row(2, "Acceleration", "acceleration_power", 1000, 2000000, 5000,
                "torque (Unity scale)")
        add_row(3, "Bearing Drag", "bearing_friction", 0.05, 10, 0.05,
                "higher = stops sooner")
        add_row(4, "Min Spin Time", "min_spin_time", 0.1, 30, 0.1, "motor seconds")
        add_row(5, "Max Spin Time", "max_spin_time", 0.1, 30, 0.1, "")
        add_row(6, "Label Padding", "text_padding", 0.20, 0.95, 0.02,
                "label radius")
        lv.addLayout(g)

        self.range_label = QLabel()
        self.range_label.setStyleSheet("color:#35c5d0; font-size:11px;")
        lv.addWidget(self.range_label)
        lv.addWidget(hline())
        lv.addWidget(header("\u25c9  COLOR PALETTE"))
        self.palette = PaletteEditor(ws.colors)
        self.palette.changed.connect(self._colors_changed)
        lv.addWidget(self.palette)
        lv.addStretch(1)
        lay.addWidget(_scroll(left), 1)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setSpacing(10)
        self.preview = WheelWidget(ws)
        self.preview.setMinimumSize(340, 340)
        rv.addWidget(self.preview, 1)
        row = QHBoxLayout()
        row.setSpacing(10)
        spin = QPushButton("\u25b6  SPIN!")
        spin.setMinimumHeight(40)
        spin.clicked.connect(self.preview.spin)
        regen = QPushButton("\u21ba  REBUILD FROM GAME FLOW")
        regen.clicked.connect(self.regenerate)
        row.addWidget(spin)
        row.addWidget(regen)
        rv.addLayout(row)
        self.result = QLabel("\u2014")
        self.result.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.result.setStyleSheet("color:#43d17c; font-weight:800; font-size:15px;")
        rv.addWidget(self.result)
        rv.addWidget(_hint("The preview uses the real wheel, the real weights "
                           "and the real randomness — spin it until the feel "
                           "matches your machine."))
        self.preview.spin_ended.connect(self._winner)
        lay.addWidget(right, 1)

        self._refresh_range()
        self._refresh_mode_ui()
        self.regenerate()

    # ---------------------------------------------------------------- edits
    def _set(self, attr: str, value):
        setattr(self.cfg.wheel, attr, value)
        self.preview.set_settings(self.cfg.wheel)
        self._refresh_range()
        self.changed.emit()

    def _entropy_changed(self, idx: int):
        mode = self.entropy_combo.itemData(idx)
        self.cfg.wheel.entropy = mode
        entropy.set_mode(mode)
        self._refresh_mode_ui()
        self.changed.emit()

    def _outcome_changed(self, idx: int):
        self.cfg.wheel.outcome_mode = self.outcome_combo.itemData(idx)
        self._refresh_mode_ui()
        self.changed.emit()

    def _refresh_mode_ui(self):
        entropy.set_mode(self.cfg.wheel.entropy)
        draw = self.cfg.wheel.outcome_mode == "draw"
        self.draw_box.setVisible(draw)
        self.entropy_note.setText(
            f"Source: {entropy.describe()}.<br>"
            + ("<b>Draw</b>: the winner is picked from that pool using the exact "
               "chance weights, then the wheel is animated so it lands there. "
               "Odds are mathematically exact and no sequence of past spins "
               "can predict the next one."
               if draw else
               "<b>Physics</b>: the slice under the pointer when the wheel stops "
               "wins, exactly like the Unity build. Organic, with the tiny "
               "geometric bias any real wheel has."))

    def _colors_changed(self, colors: list):
        self.cfg.wheel.colors = colors
        self.preview.set_settings(self.cfg.wheel)
        self.changed.emit()

    def _refresh_range(self):
        ws = self.cfg.wheel
        self.range_label.setText(
            f"Effective speed range:  "
            f"{ws.target_max_speed - ws.speed_variance:.0f} \u2013 "
            f"{ws.target_max_speed + ws.speed_variance:.0f} deg/s")

    def regenerate(self):
        self.preview.set_pieces(self.sample_provider())
        self.result.setText("\u2014")

    def _winner(self, idx: int):
        if 0 <= idx < len(self.preview.pieces):
            self.result.setText(f"WINNER: {self.preview.pieces[idx][0]}")


# ==========================================================================
#  RELAY MANAGER  —  add / rename / re-pin / remove, per-relay test timers,
#                    and a replayable switching sequence
# ==========================================================================
class RelayManager(QWidget):
    changed = pyqtSignal()

    def __init__(self, cfg: AppConfig, bank: RelayBank, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.bank = bank
        self.rows = {}                 # relay_id -> dict of widgets

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        # ---------------- relay list ----------------
        box = QGroupBox("RELAYS")
        bv = QVBoxLayout(box)
        bv.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.bind_check = QCheckBox("Bind to show (relays follow video "
                                    "triggers and Test Mode)")
        self.bind_check.setChecked(bank.bind_to_game)
        self.bind_check.toggled.connect(
            lambda v: setattr(self.bank, "bind_to_game", v))
        top.addWidget(self.bind_check)
        top.addStretch(1)
        add = QPushButton("\uff0b  ADD RELAY")
        add.setToolTip("Add another switchable output.")
        add.clicked.connect(self.add_relay)
        top.addWidget(add)
        off_all = QPushButton("\u23f9  ALL OFF")
        off_all.setStyleSheet("QPushButton{color:#ff5c5c; font-weight:700;}")
        off_all.clicked.connect(self.bank.all_off)
        top.addWidget(off_all)
        bv.addLayout(top)

        head = QHBoxLayout()
        head.setSpacing(8)
        for text, width in (("", 34), ("NAME", 150), ("GPIO", 62),
                            ("ON TEXT", 84), ("OFF TEXT", 84), ("HOLD", 82),
                            ("TEST", 190), ("", 96)):
            lbl = QLabel(text)
            lbl.setStyleSheet("color:#7f8694; font-size:10px; font-weight:700;")
            if width:
                lbl.setMinimumWidth(width)
            head.addWidget(lbl)
        head.addStretch(1)
        bv.addLayout(head)

        # A row is naturally ~1000 px wide. Without its own scroll area that
        # width becomes the window's minimum, which does not fit on a TV.
        rows_holder = QWidget()
        self.rows_box = QVBoxLayout(rows_holder)
        self.rows_box.setContentsMargins(0, 0, 0, 0)
        self.rows_box.setSpacing(6)
        self.rows_scroll = QScrollArea()
        self.rows_scroll.setWidgetResizable(True)
        self.rows_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.rows_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.rows_scroll.setWidget(rows_holder)
        self.rows_scroll.setMinimumHeight(150)
        bv.addWidget(self.rows_scroll)

        self.pin_warn = QLabel("")
        self.pin_warn.setWordWrap(True)
        self.pin_warn.setStyleSheet(
            "color:#ff5c5c; font-weight:700; padding:6px;"
            "border:1px solid #6a2020; border-radius:6px;")
        bv.addWidget(self.pin_warn)
        root.addWidget(box)

        # ---------------- programmable sequence ----------------
        seq = QGroupBox("TEST SEQUENCE  \u00b7  scripted bench run")
        sv = QVBoxLayout(seq)
        sv.setSpacing(9)
        sv.addWidget(_hint(
            "Build a timeline, then replay it. Each step switches one relay at "
            "one moment, so you can rehearse a whole cue \u2014 for example "
            "Relay 1 on at 4 s, off at 9 s, Voltage on at 10 s \u2014 without "
            "touching a video."))

        srow = QHBoxLayout()
        srow.setSpacing(10)
        srow.addWidget(QLabel("Length"))
        self.length_spin = QDoubleSpinBox()
        self.length_spin.setRange(1.0, 180.0)
        self.length_spin.setSingleStep(5.0)
        self.length_spin.setSuffix(" s")
        self.length_spin.setValue(cfg.esp.test_sequence_length)
        self.length_spin.setToolTip("Total run time, up to three minutes.")
        self.length_spin.valueChanged.connect(self._length_changed)
        srow.addWidget(self.length_spin)
        for label, secs in (("30s", 30.0), ("1 min", 60.0), ("3 min", 180.0)):
            b = QPushButton(label)
            b.setMaximumWidth(58)
            b.clicked.connect(lambda _=False, s=secs: self.length_spin.setValue(s))
            srow.addWidget(b)
        srow.addStretch(1)
        self.play_btn = QPushButton("\u25b6  RUN SEQUENCE")
        self.play_btn.setMinimumHeight(34)
        self.play_btn.setStyleSheet(
            "QPushButton{background:#1d939e; color:#fff; font-weight:800;}")
        self.play_btn.clicked.connect(self._toggle_sequence)
        srow.addWidget(self.play_btn)
        sv.addLayout(srow)

        self.seq_timeline = TriggerTimeline()
        self.seq_timeline.setMinimumHeight(70)
        self.seq_timeline.set_relays(cfg.esp.relays)
        self.seq_timeline.scrub_requested.connect(self._timeline_clicked)
        sv.addWidget(self.seq_timeline)

        self.seq_bar = QProgressBar()
        self.seq_bar.setRange(0, 1000)
        self.seq_bar.setFormat("idle")
        sv.addWidget(self.seq_bar)

        self.seq_table = QTableWidget(0, 3)
        self.seq_table.setHorizontalHeaderLabels(["Time", "Relay", "State"])
        seq_head = self.seq_table.horizontalHeader()
        seq_head.setStretchLastSection(False)
        seq_head.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        seq_head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        seq_head.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.seq_table.setColumnWidth(0, 104)
        self.seq_table.setColumnWidth(2, 88)
        self.seq_table.setMinimumHeight(150)
        self.seq_table.verticalHeader().setVisible(False)
        sv.addWidget(self.seq_table)

        brow = QHBoxLayout()
        brow.setSpacing(8)
        for text, slot, tip in (
                ("\uff0b  ADD STEP", self.add_step, "Insert a switch"),
                ("\u2715  REMOVE", self.remove_step, "Delete the selected step"),
                ("\u21c5  SORT BY TIME", self.sort_steps, "Order chronologically"),
                ("\u2298  CLEAR", self.clear_steps, "Empty the sequence")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            brow.addWidget(b)
        brow.addStretch(1)
        sv.addLayout(brow)
        root.addWidget(seq)

        bank.states_changed.connect(self.on_states)
        bank.override_progress.connect(self.on_override)
        bank.sequence_progress.connect(self.on_sequence_progress)
        bank.sequence_finished.connect(self._sequence_done)

        self.rebuild()

    # ------------------------------------------------------------ relay rows
    def rebuild(self):
        while self.rows_box.count():
            item = self.rows_box.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.rows = {}

        for index, relay in enumerate(self.cfg.esp.relays):
            self.rows_box.addWidget(self._make_row(index, relay))

        self.bank.sync_relay_list()
        self.seq_timeline.set_relays(self.cfg.esp.relays)
        self._check_pins()
        self.refresh_sequence()

    def _make_row(self, index: int, relay) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        dot = QLabel("\u25cf")
        dot.setFixedWidth(34)
        dot.setStyleSheet(f"color:{relay_color(index)}; font-size:19px;")
        h.addWidget(dot)

        name = QLineEdit(relay.name)
        name.setMinimumWidth(120)
        name.setPlaceholderText("name")
        name.textChanged.connect(lambda t, r=relay: self._set(r, "name", t))
        h.addWidget(name)

        pin = QSpinBox()
        pin.setRange(0, 48)
        pin.setMinimumWidth(58)
        pin.setValue(int(relay.pin))
        pin.valueChanged.connect(lambda v, r=relay: self._set(r, "pin", v))
        h.addWidget(pin)

        on_lbl = QLineEdit(relay.on_label)
        on_lbl.setMinimumWidth(70)
        on_lbl.textChanged.connect(lambda t, r=relay: self._set(r, "on_label", t))
        h.addWidget(on_lbl)

        off_lbl = QLineEdit(relay.off_label)
        off_lbl.setMinimumWidth(70)
        off_lbl.textChanged.connect(lambda t, r=relay: self._set(r, "off_label", t))
        h.addWidget(off_lbl)

        hold = QDoubleSpinBox()
        hold.setRange(0.1, 3600.0)
        hold.setMinimumWidth(76)
        hold.setSuffix(" s")
        hold.setValue(float(relay.test_duration))
        hold.setToolTip("This relay's own test timer — changing it does not "
                        "affect the others.")
        hold.valueChanged.connect(
            lambda v, r=relay: self._set(r, "test_duration", v))
        h.addWidget(hold)

        test = QWidget()
        tl = QHBoxLayout(test)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(4)
        b_on = QPushButton("ON")
        b_on.setToolTip("Switch on and hold for this relay's own duration.")
        b_on.clicked.connect(lambda _=False, r=relay: self.bank.set_relay(r.id, True))
        b_off = QPushButton("OFF")
        b_off.clicked.connect(lambda _=False, r=relay: self.bank.set_relay(r.id, False))
        b_pulse = QPushButton("HOLD")
        b_pulse.setToolTip("Pulse on for the hold time, then release.")
        b_pulse.clicked.connect(lambda _=False, r=relay: self.bank.pulse(r.id))
        for b in (b_on, b_off, b_pulse):
            b.setMaximumWidth(60)
            tl.addWidget(b)
        test.setMinimumWidth(170)
        h.addWidget(test)

        led = LedBlock(relay.name, QColor(relay_color(index)))
        led.configure(relay.name, relay.on_label, relay.off_label)
        led.setMinimumWidth(140)
        h.addWidget(led)

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setMaximumWidth(120)
        bar.setFormat("\u2014")
        bar.setValue(0)
        h.addWidget(bar)

        up = QPushButton("\u25b2")
        up.setMaximumWidth(30)
        up.setToolTip("Move up")
        up.clicked.connect(lambda _=False, i=index: self.move_relay(i, -1))
        down = QPushButton("\u25bc")
        down.setMaximumWidth(30)
        down.setToolTip("Move down")
        down.clicked.connect(lambda _=False, i=index: self.move_relay(i, 1))
        rm = QPushButton("\u2715")
        rm.setMaximumWidth(30)
        rm.setToolTip("Remove this relay")
        rm.setStyleSheet("QPushButton{color:#ff5c5c;}")
        rm.clicked.connect(lambda _=False, i=index: self.remove_relay(i))
        for b in (up, down, rm):
            h.addWidget(b)
        h.addStretch(1)

        self.rows[relay.id] = {"led": led, "bar": bar, "dot": dot, "name": name}
        return row

    # ------------------------------------------------------------ edits
    def _set(self, relay, attr: str, value):
        setattr(relay, attr, value)
        if attr in ("name", "on_label", "off_label"):
            widgets = self.rows.get(relay.id)
            if widgets:
                widgets["led"].configure(relay.name, relay.on_label,
                                         relay.off_label)
        if attr == "pin":
            self._check_pins()
        self.seq_timeline.set_relays(self.cfg.esp.relays)
        self.refresh_sequence()
        self.changed.emit()

    def add_relay(self):
        rid = next_relay_id(self.cfg.esp.relays)
        n = len(self.cfg.esp.relays) + 1
        used = {r.pin for r in self.cfg.esp.relays}
        pin = next((p for p in ESP32_S3_SAFE_PINS if p not in used), 4)
        self.cfg.esp.relays.append(
            RelayConfig(rid, f"Relay {n}", pin, "ON", "OFF", 3.0))
        self.rebuild()
        self.changed.emit()

    def remove_relay(self, index: int):
        if not (0 <= index < len(self.cfg.esp.relays)):
            return
        if len(self.cfg.esp.relays) <= 1:
            QMessageBox.information(self, "Keep at least one",
                                    "There has to be at least one relay.")
            return
        relay = self.cfg.esp.relays[index]
        used = sum(1 for v in self.cfg.playlist
                   for t in v.triggers if t.relay_id == relay.id)
        message = f"Remove '{relay.name}'?"
        if used:
            message += (f"\n\n{used} video trigger(s) point at it and will stop "
                        f"doing anything.")
        if QMessageBox.question(self, "Remove relay", message) != \
                QMessageBox.StandardButton.Yes:
            return
        self.cfg.esp.relays.pop(index)
        self.cfg.esp.test_sequence = [t for t in self.cfg.esp.test_sequence
                                      if t.relay_id != relay.id]
        self.rebuild()
        self.changed.emit()

    def move_relay(self, index: int, delta: int):
        target = index + delta
        relays = self.cfg.esp.relays
        if 0 <= target < len(relays):
            relays[index], relays[target] = relays[target], relays[index]
            self.rebuild()
            self.changed.emit()

    def _check_pins(self):
        problems = []
        seen = {}
        for relay in self.cfg.esp.relays:
            why = pin_warning(int(relay.pin))
            if why:
                problems.append(f"{relay.name}: {why}")
            if relay.pin in seen:
                problems.append(f"{relay.name} and {seen[relay.pin]} are both "
                                f"on GPIO {relay.pin}.")
            seen[relay.pin] = relay.name
        if problems:
            safe = ", ".join(str(p) for p in ESP32_S3_SAFE_PINS[:14])
            self.pin_warn.setText("\u26a0  " + "  ".join(problems)
                                  + f"<br>Safe GPIOs: {safe} \u2026")
            self.pin_warn.show()
        else:
            self.pin_warn.hide()

    # ------------------------------------------------------------ live state
    def on_states(self, states: dict):
        for rid, widgets in self.rows.items():
            widgets["led"].set_state(bool(states.get(rid, False)))

    def on_override(self, relay_id: str, remaining: float, total: float):
        widgets = self.rows.get(relay_id)
        if not widgets:
            return
        total = max(0.001, total)
        widgets["bar"].setValue(max(0, min(100, int(100 * remaining / total))))
        widgets["bar"].setFormat(f"{remaining:.1f}s" if remaining > 0 else "\u2014")

    # ------------------------------------------------------------ sequence
    def _length_changed(self, value: float):
        self.cfg.esp.test_sequence_length = value
        self.refresh_sequence()
        self.changed.emit()

    def refresh_sequence(self):
        seq = self.cfg.esp.test_sequence
        relays = self.cfg.esp.relays
        self.seq_timeline.set_duration(self.cfg.esp.test_sequence_length)
        self.seq_timeline.set_triggers(seq)

        self.seq_table.blockSignals(True)
        self.seq_table.setRowCount(len(seq))
        for i, step in enumerate(seq):
            time_spin = TimecodeEdit(
                step.timestamp,
                maximum=max(1.0, self.cfg.esp.test_sequence_length),
                decimals=2)
            time_spin.value_changed.connect(
                lambda v, s=step: self._step_set(s, "timestamp", v))
            self.seq_table.setCellWidget(i, 0, time_spin)

            combo = QComboBox()
            for relay in relays:
                combo.addItem(relay.name, relay.id)
            idx = combo.findData(step.relay_id)
            combo.setCurrentIndex(max(0, idx))
            combo.currentIndexChanged.connect(
                lambda j, s=step, c=combo: self._step_set(s, "relay_id",
                                                          c.itemData(j)))
            self.seq_table.setCellWidget(i, 1, combo)

            state = QComboBox()
            state.addItem("ON", True)
            state.addItem("OFF", False)
            state.setCurrentIndex(0 if step.state else 1)
            state.currentIndexChanged.connect(
                lambda j, s=step, c=state: self._step_set(s, "state",
                                                          c.itemData(j)))
            self.seq_table.setCellWidget(i, 2, state)
        self.seq_table.blockSignals(False)

    def _step_set(self, step, attr: str, value):
        setattr(step, attr, value)
        self.seq_timeline.set_triggers(self.cfg.esp.test_sequence)
        self.changed.emit()

    def add_step(self):
        if not self.cfg.esp.relays:
            return
        seq = self.cfg.esp.test_sequence
        when = min(self.cfg.esp.test_sequence_length,
                   (max((s.timestamp for s in seq), default=0.0) + 2.0))
        seq.append(HardwareTrigger(when, self.cfg.esp.relays[0].id, True))
        self.refresh_sequence()
        self.changed.emit()

    def remove_step(self):
        row = self.seq_table.currentRow()
        if 0 <= row < len(self.cfg.esp.test_sequence):
            self.cfg.esp.test_sequence.pop(row)
            self.refresh_sequence()
            self.changed.emit()

    def sort_steps(self):
        self.cfg.esp.test_sequence.sort(key=lambda s: s.timestamp)
        self.refresh_sequence()
        self.changed.emit()

    def clear_steps(self):
        self.cfg.esp.test_sequence.clear()
        self.refresh_sequence()
        self.changed.emit()

    def _timeline_clicked(self, seconds: float):
        self.seq_timeline.set_playhead(seconds)

    def _toggle_sequence(self):
        if self.bank.sequence_running:
            self.bank.stop_sequence()
        else:
            self.sort_steps()
            self.bank.start_sequence()
            self.play_btn.setText("\u23f9  STOP")

    def on_sequence_progress(self, position: float, length: float):
        length = max(0.001, length)
        self.seq_bar.setValue(int(1000 * min(1.0, position / length)))
        self.seq_bar.setFormat(f"{position:0.1f}s / {length:0.0f}s")
        self.seq_timeline.set_playhead(position)

    def _sequence_done(self):
        self.play_btn.setText("\u25b6  RUN SEQUENCE")
        self.seq_bar.setFormat("idle")
        self.seq_bar.setValue(0)
        self.seq_timeline.set_playhead(0.0)


# ==========================================================================
#  ESP  —  connection, protocol, relays, console, diagnostics
# ==========================================================================
class EspTab(QWidget):
    changed = pyqtSignal()

    def __init__(self, cfg: AppConfig, link: EspLink, bank: RelayBank,
                 parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.link = link
        self.bank = bank
        self._scan_until = 0.0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        # Everything above the divider scrolls; the console lives below it and
        # can be dragged to any height you like.
        self.page_split = QSplitter(Qt.Orientation.Vertical)
        self.page_split.setChildrenCollapsible(False)
        self.page_split.setHandleWidth(8)
        outer.addWidget(self.page_split)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setSpacing(12)
        self.page_split.addWidget(_scroll(content))
        e = cfg.esp

        # ---------------- connection ----------------
        conn = QGroupBox("CONNECTION")
        cg = _form(9, 150)
        cg.addWidget(QLabel("Board IP"), 0, 0)
        self.ip_edit = QLineEdit(e.ip)
        self.ip_edit.setPlaceholderText("the address on the ESP's OLED, e.g. 172.20.10.5")
        self.ip_edit.editingFinished.connect(self._ip_edited)
        cg.addWidget(self.ip_edit, 0, 1)

        port_row = QHBoxLayout()
        port_row.setSpacing(10)
        self.port_spin = QSpinBox(); self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(e.port)
        self.port_spin.valueChanged.connect(self._port_edited)
        port_row.addWidget(QLabel("Board port"))
        port_row.addWidget(self.port_spin)
        port_row.addSpacing(16)
        self.local_spin = QSpinBox(); self.local_spin.setRange(0, 65535)
        self.local_spin.setValue(e.local_port)
        self.local_spin.setToolTip(
            "The firmware replies to <PC ip>:<this port>, so it must match the "
            "board port. Leave it at 4222.")
        self.local_spin.valueChanged.connect(
            lambda v: self._esp_set("local_port", v))
        port_row.addWidget(QLabel("Listen on"))
        port_row.addWidget(self.local_spin)
        port_row.addStretch(1)
        cg.addLayout(port_row, 1, 0, 1, 2)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self.connect_btn = QPushButton()
        self.connect_btn.setMinimumHeight(38)
        self.connect_btn.clicked.connect(self._connect_clicked)
        self.retry_btn = QPushButton("\u21ba  RETRY")
        self.retry_btn.setMinimumHeight(38)
        self.retry_btn.setToolTip("Rebuild the socket and start looking again, "
                                  "without changing any settings.")
        self.retry_btn.clicked.connect(link.reconnect)
        scan = QPushButton("\U0001F50D  SCAN NETWORK")
        scan.setMinimumHeight(38)
        scan.setToolTip("Broadcast an identity probe and fill in whichever "
                        "board answers.")
        scan.clicked.connect(self._scan)
        detect = QPushButton("\u2699  TEST PROTOCOL")
        detect.setMinimumHeight(38)
        detect.setToolTip("Try CRLF, then LF, then no line ending, and keep "
                          "whichever the board answers.")
        detect.clicked.connect(lambda: link.begin_auto_detect())
        btn_row.addWidget(self.connect_btn, 1)
        btn_row.addWidget(self.retry_btn)
        btn_row.addWidget(scan)
        btn_row.addWidget(detect)
        cg.addLayout(btn_row, 2, 0, 1, 2)

        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.RichText)
        cg.addWidget(self.status_label, 3, 0, 1, 2)
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color:#7f8694; font-family:Consolas;")
        cg.addWidget(self.stats_label, 4, 0, 1, 2)

        opts = QHBoxLayout()
        opts.setSpacing(16)
        self.autoconn_check = QCheckBox("Connect automatically on launch")
        self.autoconn_check.setChecked(e.auto_connect)
        self.autoconn_check.toggled.connect(lambda v: self._esp_set("auto_connect", v))
        self.probe_check = QCheckBox("Verify with identity probe")
        self.probe_check.setChecked(e.identity_probe)
        self.probe_check.setToolTip("Untick to run in trust mode: keep sending "
                                    "without waiting for replies.")
        self.probe_check.toggled.connect(lambda v: self._esp_set("identity_probe", v))
        opts.addWidget(self.autoconn_check)
        opts.addWidget(self.probe_check)
        opts.addStretch(1)
        cg.addLayout(opts, 5, 0, 1, 2)

        tol = QHBoxLayout()
        tol.setSpacing(10)
        tol.addWidget(QLabel("Probe every"))
        pi = QDoubleSpinBox(); pi.setRange(0.5, 30.0); pi.setSingleStep(0.5)
        pi.setSuffix(" s"); pi.setValue(e.probe_interval)
        pi.setToolTip("How often to ask the board to identify itself.")
        pi.valueChanged.connect(lambda v: self._esp_set("probe_interval", v))
        tol.addWidget(pi)
        tol.addSpacing(16)
        tol.addWidget(QLabel("Call it lost after"))
        lt = QDoubleSpinBox(); lt.setRange(2.0, 120.0); lt.setSingleStep(1.0)
        lt.setSuffix(" s"); lt.setValue(e.link_tolerance)
        lt.setToolTip("The board reads one packet per loop and its screen "
                      "refresh blocks that loop, so a few replies always go "
                      "missing. Keep this generous.")
        lt.valueChanged.connect(lambda v: self._esp_set("link_tolerance", v))
        tol.addWidget(lt)
        tol.addSpacing(16)
        self.sticky_check = QCheckBox("Ride through brief gaps")
        self.sticky_check.setChecked(e.sticky_link)
        self.sticky_check.toggled.connect(lambda v: self._esp_set("sticky_link", v))
        tol.addWidget(self.sticky_check)
        tol.addStretch(1)
        cg.addLayout(tol, 7, 0, 1, 2)

        rates = QHBoxLayout()
        rates.setSpacing(10)
        rates.addWidget(QLabel("Heartbeat"))
        hb = QDoubleSpinBox(); hb.setRange(0.1, 5.0); hb.setSingleStep(0.05)
        hb.setSuffix(" s"); hb.setValue(e.heartbeat_rate)
        hb.valueChanged.connect(self._hb_edited)
        rates.addWidget(hb)
        rates.addSpacing(16)
        rates.addWidget(QLabel("Send delay"))
        sd = QDoubleSpinBox(); sd.setRange(0.005, 0.5); sd.setDecimals(3)
        sd.setSingleStep(0.005); sd.setSuffix(" s"); sd.setValue(e.send_delay)
        sd.valueChanged.connect(lambda v: self._esp_set("send_delay", v))
        rates.addWidget(sd)
        rates.addStretch(1)
        cg.addLayout(rates, 8, 0, 1, 2)
        conn.setLayout(cg)
        root.addWidget(conn)

        root.addWidget(_hint(
            "The firmware kills every relay after <b>1.5 s</b> without a packet, "
            "so the heartbeat must stay well under that. If the app dies, the "
            "board shuts the relays off on its own."))

        # ---------------- relays ----------------
        self.relay_manager = RelayManager(cfg, bank)
        self.relay_manager.changed.connect(self.changed.emit)
        root.addWidget(self.relay_manager)

        # ---------------- console (built after the scroll area) ----------
        console = QGroupBox("CONSOLE  \u00b7  send raw Uduino commands")
        cvg = QVBoxLayout(console)
        cvg.setSpacing(9)
        send_row = QHBoxLayout()
        send_row.setSpacing(10)
        self.cmd_edit = QLineEdit()
        self.cmd_edit.setPlaceholderText("SetRelay 26 1")
        self.cmd_edit.returnPressed.connect(self._send_cmd)
        send_btn = QPushButton("SEND")
        send_btn.clicked.connect(self._send_cmd)
        send_row.addWidget(self.cmd_edit, 1)
        send_row.addWidget(send_btn)
        cvg.addLayout(send_row)

        quick = QHBoxLayout()
        quick.setSpacing(8)
        for text, cmd in (("identity", "identity"), ("Ping", "Ping"),
                          ("Heartbeat", "Heartbeat"), ("AllOff", "AllOff")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, c=cmd: self.link.send_now(c))
            quick.addWidget(b)
        quick.addWidget(_hint("Per-relay switching lives in the RELAYS panel "
                              "above."))
        quick.addStretch(1)
        cvg.addLayout(quick)

        self.traffic = QListWidget()
        self.traffic.setMinimumHeight(120)
        self.traffic.setStyleSheet("font-family:Consolas; font-size:11px;")
        # select any range of lines and copy them
        self.traffic.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection)
        self.traffic.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.traffic.customContextMenuRequested.connect(self._console_menu)
        cvg.addWidget(self.traffic, 1)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        copy_sel = QPushButton("Copy selected")
        copy_sel.setToolTip("Copy the highlighted lines  (Ctrl+C)")
        copy_sel.clicked.connect(self.copy_selected)
        copy_all = QPushButton("Copy all")
        copy_all.clicked.connect(self.copy_all)
        save_btn = QPushButton("Save to file\u2026")
        save_btn.clicked.connect(self.save_log)
        clr = QPushButton("Clear")
        clr.clicked.connect(self.traffic.clear)
        self.autoscroll = QCheckBox("Follow new lines")
        self.autoscroll.setChecked(True)
        self.pause_check = QCheckBox("Pause log")
        self.pause_check.setToolTip("Stop adding lines so you can read and "
                                    "copy without them moving.")
        for wdg in (copy_sel, copy_all, save_btn, clr):
            tools.addWidget(wdg)
        tools.addWidget(self.autoscroll)
        tools.addWidget(self.pause_check)
        tools.addStretch(1)
        self.line_count = QLabel("0 lines")
        self.line_count.setStyleSheet("color:#7f8694;")
        tools.addWidget(self.line_count)
        cvg.addLayout(tools)

        QShortcut(QKeySequence.StandardKey.Copy, self.traffic,
                  activated=self.copy_selected)
        self.page_split.addWidget(console)
        self.page_split.setStretchFactor(0, 3)
        self.page_split.setStretchFactor(1, 2)
        # Explicit sizes: without them the console opened flush against the top
        # and buried the rest of the page behind it.
        self.page_split.setSizes([620, 280])
        console.setMinimumHeight(190)

        # ---------------- protocol ----------------
        adv = QGroupBox("PROTOCOL")
        ag = _form(9, 150)
        ag.addWidget(QLabel("Argument separator"), 0, 0)
        self.delim_edit = QLineEdit(e.arg_delimiter)
        self.delim_edit.setMaximumWidth(70)
        self.delim_edit.textChanged.connect(
            lambda t: self._esp_set("arg_delimiter", t or " "))
        ag.addWidget(self.delim_edit, 0, 1)
        ag.addWidget(QLabel("Line ending"), 1, 0)
        self.term_combo = QComboBox()
        self.term_combo.addItem("CRLF  \\r\\n   (recommended)", "\r\n")
        self.term_combo.addItem("CR  \\r", "\r")
        self.term_combo.addItem("LF  \\n   (never executes)", "\n")
        self.term_combo.addItem("none  (never executes)", "")
        idx = {"\r\n": 0, "\r": 1, "\n": 2, "": 3}.get(e.terminator, 0)
        self.term_combo.setCurrentIndex(idx)
        self.term_combo.currentIndexChanged.connect(self._term_changed)
        ag.addWidget(self.term_combo, 1, 1)
        self.term_warn = QLabel("")
        self.term_warn.setWordWrap(True)
        self.term_warn.setStyleSheet("color:#ff5c5c; font-weight:700;")
        ag.addWidget(self.term_warn, 3, 0, 1, 2)
        self.autodetect_check = QCheckBox("Auto-detect the line ending when connecting")
        self.autodetect_check.setChecked(e.auto_detect_protocol)
        self.autodetect_check.toggled.connect(
            lambda v: self._esp_set("auto_detect_protocol", v))
        ag.addWidget(self.autodetect_check, 2, 0, 1, 2)
        adv.setLayout(ag)
        root.addWidget(adv)

        # ---------------- diagnostics ----------------
        diag = QGroupBox("IF IT WILL NOT CONNECT")
        dvg = QVBoxLayout(diag)
        dvg.addWidget(_hint(
            "Read straight from your <code>Custom_Esp32_S3.cpp</code>:<br><br>"
            "<b>1 &nbsp;It answers on a fixed port.</b> "
            "<code>beginPacket(remote, port)</code> sends every reply to "
            "&lt;PC&nbsp;ip&gt;:4222, never to the port our packet came from. "
            "This app now listens on 4222 for exactly that reason — if another "
            "program already holds the port, the log says so and the status "
            "stays SEARCHING even though commands still go out.<br><br>"
            "<b>2 &nbsp;It latches the first address it hears.</b> "
            "<code>if (remote == 0.0.0.0) remote = remoteIP();</code> runs once "
            "per boot. If the board was talking to a different machine, or your "
            "PC's address changed, power-cycle the ESP.<br><br>"
            "<b>3 &nbsp;Commands need a line ending.</b> Uduino only acts on a "
            "command once the line terminates, and the board's own transmit "
            "buffer flushes on <code>\\r\\n</code>. CRLF is therefore the right "
            "setting; TEST PROTOCOL confirms it.<br><br>"
            "<b>4 &nbsp;Same network, and let it through the firewall.</b> "
            "Windows asks once on first launch — say yes, or the reply is "
            "dropped before it reaches us."))
        root.addWidget(diag)
        root.addStretch(1)

        # ---------------- wiring ----------------
        link.state_changed.connect(lambda _s: self._refresh_status())
        link.board_identity.connect(lambda _n: self._refresh_status())
        link.board_found.connect(self._board_found)
        link.packet_sent.connect(self._tx)
        link.packet_received.connect(self._rx)

        self._poll = QTimer(self)
        self._poll.setInterval(400)
        self._poll.timeout.connect(self._refresh_stats)
        self._poll.start()

        self._refresh_status()
        self._check_terminator()

    # ---------------------------------------------------------------- edits
    def _esp_set(self, attr: str, value):
        setattr(self.cfg.esp, attr, value)
        self.changed.emit()

    def _ip_edited(self):
        self._esp_set("ip", self.ip_edit.text().strip())

    def _port_edited(self, value: int):
        self._esp_set("port", value)
        if self.local_spin.value() != value:
            self.local_spin.setValue(value)      # they must match for replies

    def _hb_edited(self, value: float):
        self._esp_set("heartbeat_rate", value)
        self.link.apply_settings(self.cfg.esp)

    def _term_changed(self, index: int):
        self._esp_set("terminator", self.term_combo.itemData(index))
        self._check_terminator()

    def _check_terminator(self):
        """Uduino.cpp sets term='\r' and discards non-printable characters, so a
        command is only ever executed by a carriage return."""
        if "\r" not in self.cfg.esp.terminator:
            self.term_warn.setText(
                "\u26a0  Uduino only executes a command when it receives a "
                "carriage return. With this setting the board will hear the "
                "text but never act on it \u2014 relays will not move.")
            self.term_warn.show()
        else:
            self.term_warn.hide()

    def _send_cmd(self):
        text = self.cmd_edit.text().strip()
        if text:
            self.link.send_now(text)
            self.cmd_edit.clear()

    # ---------------------------------------------------------------- link
    def _connect_clicked(self):
        if self.link.is_enabled:
            self.link.stop()
        else:
            self.cfg.esp.ip = self.ip_edit.text().strip()
            self.cfg.esp.port = self.port_spin.value()
            self.link.apply_settings(self.cfg.esp)
            self.link.start()
        self._refresh_status()

    def _scan(self):
        if not self.link.is_enabled:
            self._connect_clicked()
        self._scan_until = time.monotonic() + 6.0
        self.link.scan_for_board()

    def _board_found(self, ip: str, name: str):
        if time.monotonic() > self._scan_until or ip == self.cfg.esp.ip:
            return
        self._scan_until = 0.0
        who = f"'{name}' " if name else ""
        ans = QMessageBox.question(
            self, "ESP32 found",
            f"Board {who}answered from {ip}.\nUse this address?")
        if ans == QMessageBox.StandardButton.Yes:
            self.ip_edit.setText(ip)
            self.cfg.esp.ip = ip
            self.link.stop()
            self.link.apply_settings(self.cfg.esp)
            self.link.start()
            self.changed.emit()

    def _refresh_status(self):
        st = self.link.state
        color = esp_link.state_color(st)
        ident = f"  \u00b7  {self.link.identity}" if self.link.identity else ""
        hint = esp_link.state_hint(st)
        if st == esp_link.STATE_OFFLINE and not self.cfg.esp.auto_connect:
            hint = ("Not connected. Auto-connect on launch is switched off, so "
                    "nothing is being tried until you press CONNECT.")
        self.status_label.setText(
            f"<span style='color:{color}; font-weight:700; font-size:15px'>"
            f"\u25cf {esp_link.state_label(st)}</span>{ident}"
            f"<br><span style='color:#7f8694'>{hint}</span>")

        self.connect_btn.setText(esp_link.button_label(st))
        busy = st in (esp_link.STATE_CONNECTING, esp_link.STATE_RECONNECTING)
        self.connect_btn.setStyleSheet(
            "QPushButton{background:#1d939e; color:#fff; font-weight:800;}"
            if st == esp_link.STATE_OFFLINE else "")
        self.retry_btn.setEnabled(self.link.is_enabled)
        self.retry_btn.setText("\u21ba  RETRY NOW" if busy else "\u21ba  RETRY")

    def _refresh_stats(self):
        s = self.link.snapshot()
        if not self.link.is_enabled:
            self.stats_label.setText("")
            return
        age = s["reply_age"]
        age_txt = "no reply yet" if age < 0 else f"last reply {age:.1f}s ago"
        rtt = f"{s['rtt_ms']:.0f} ms" if s["rtt_ms"] >= 0 else "\u2014"
        q = s["quality"]
        qual = f"{q:.0f}%" if q >= 0 else "\u2014"
        self.stats_label.setText(
            f"listening :{s['local_port']}   sent {s['tx']}   received {s['rx']}"
            f"   ping {rtt}   {s['probe_cmd']} {s['probes_answered']}/{s['probes_sent']}"
            f" ({qual})   queue {s['queue']}   {age_txt}")

    # ---------------------------------------------------------------- relays
    def _bind_toggled(self, on: bool):
        self.bank.bind_to_game = on

    # ---------------------------------------------------------------- console
    def _console_menu(self, point):
        menu = QMenu(self)
        act_sel = QAction("Copy selected", self)
        act_sel.triggered.connect(self.copy_selected)
        act_all = QAction("Copy all", self)
        act_all.triggered.connect(self.copy_all)
        act_save = QAction("Save to file\u2026", self)
        act_save.triggered.connect(self.save_log)
        act_clear = QAction("Clear", self)
        act_clear.triggered.connect(self.traffic.clear)
        for a in (act_sel, act_all, None, act_save, act_clear):
            menu.addSeparator() if a is None else menu.addAction(a)
        menu.exec(self.traffic.mapToGlobal(point))

    def _lines(self, selected_only: bool) -> str:
        items = (self.traffic.selectedItems() if selected_only
                 else [self.traffic.item(i) for i in range(self.traffic.count())])
        if selected_only:
            rows = sorted(self.traffic.row(i) for i in items)
            items = [self.traffic.item(r) for r in rows]
        return "\n".join(i.text() for i in items if i is not None)

    def copy_selected(self):
        text = self._lines(True)
        if not text:
            self.link.log.emit("Nothing selected — click a line, or use Copy all.")
            return
        QGuiApplication.clipboard().setText(text)
        self.link.log.emit(f"Copied {len(text.splitlines())} line(s).")

    def copy_all(self):
        text = self._lines(False)
        QGuiApplication.clipboard().setText(text)
        self.link.log.emit(f"Copied the whole log ({len(text.splitlines())} lines).")

    def save_log(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save console log", "esp_log.txt", "Text (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._lines(False))
            self.link.log.emit(f"Log written to {path}")
        except OSError as ex:
            QMessageBox.warning(self, "Could not save", str(ex))

    # ---------------------------------------------------------------- traffic
    def _tx(self, text: str, priority: int):
        tag = {100: "CRIT", 50: "HIGH"}.get(priority, "LOW ")
        self._push(f"\u2191 [{tag}] {text}")

    def _rx(self, text: str, ip: str):
        self._push(f"\u2193 {ip}  {text}")

    def _push(self, line: str):
        if self.pause_check.isChecked():
            return
        self.traffic.addItem(f"{time.strftime('%H:%M:%S')}  {line}")
        while self.traffic.count() > 2000:
            self.traffic.takeItem(0)
        if self.autoscroll.isChecked():
            self.traffic.scrollToBottom()
        self.line_count.setText(f"{self.traffic.count()} lines")


# ==========================================================================
#  SHOW  —  live transport + on-screen overlays
# ==========================================================================
class ShowTab(QWidget):
    changed = pyqtSignal()
    ui_changed = pyqtSignal()

    def __init__(self, cfg: AppConfig, engine: GameEngine, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.engine = engine

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setSpacing(12)
        outer.addWidget(_scroll(content))
        u = cfg.ui

        # ---------------- live transport ----------------
        live = QGroupBox("LIVE CONTROL  \u00b7  works while the show is running")
        lg = QVBoxLayout(live)
        lg.setSpacing(10)

        self.state_label = QLabel("IDLE")
        self.state_label.setStyleSheet("font-size:15px; font-weight:800; color:#9adfe6;")
        lg.addWidget(self.state_label)

        row1 = QHBoxLayout()
        row1.setSpacing(10)
        for text, slot, style in (
                ("\u25b6  START", engine.start_sequence, "#1d939e"),
                ("\u23f8  PAUSE / RESUME", engine.toggle_pause, None),
                ("\u21ba  RESTART CLIP", engine.restart_video, None),
                ("\u23ed  SKIP VIDEO", engine.skip_to_end, None),
                ("\u23f9  STOP + RESET", engine.stop_all, "#a33")):
            b = QPushButton(text)
            b.setMinimumHeight(42)
            if style:
                b.setStyleSheet(f"QPushButton{{background:{style}; color:#fff;"
                                "font-weight:800;}")
            b.clicked.connect(slot)
            row1.addWidget(b)
        lg.addLayout(row1)

        seek = QHBoxLayout()
        seek.setSpacing(10)
        for text, secs in (("\u23ea  -30s", -30.0), ("-10s", -10.0), ("-5s", -5.0),
                           ("+5s", 5.0), ("+10s", 10.0), ("+30s  \u23e9", 30.0)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, s=secs: engine.seek_by(s))
            seek.addWidget(b)
        seek.addStretch(1)
        lg.addLayout(seek)

        audio = QHBoxLayout()
        audio.setSpacing(10)
        self.mute_btn = QPushButton()
        self.mute_btn.setMinimumHeight(34)
        self.mute_btn.setMinimumWidth(130)
        self.mute_btn.clicked.connect(engine.toggle_mute)
        audio.addWidget(self.mute_btn)
        audio.addWidget(QLabel("Volume"))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(int(u.volume * 100))
        self.volume_slider.valueChanged.connect(
            lambda v: engine.set_volume(v / 100.0))
        audio.addWidget(self.volume_slider, 1)
        self.volume_label = QLabel(f"{int(u.volume * 100)}%")
        self.volume_label.setMinimumWidth(46)
        audio.addWidget(self.volume_label)
        audio.addSpacing(16)
        audio.addWidget(QLabel("Speed"))
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.1, 8.0)
        self.speed_spin.setSingleStep(0.05)
        self.speed_spin.setValue(1.0)
        self.speed_spin.setSuffix(" \u00d7")
        self.speed_spin.valueChanged.connect(engine.set_speed)
        audio.addWidget(self.speed_spin)
        lg.addLayout(audio)
        root.addWidget(live)

        # ---------------- timecode ----------------
        tc = QGroupBox("ON-SCREEN TIMECODE")
        tg = _form(9, 190)
        self.tc_enabled = QCheckBox("Show the running clock over the video")
        self.tc_enabled.setChecked(u.timecode_enabled)
        self.tc_enabled.toggled.connect(lambda v: self._ui_set("timecode_enabled", v))
        tg.addWidget(self.tc_enabled, 0, 0, 1, 2)

        tg.addWidget(QLabel("Corner"), 1, 0)
        self.tc_corner = QComboBox()
        for label, value in (("Top right", "top_right"), ("Top left", "top_left"),
                             ("Bottom right", "bottom_right"),
                             ("Bottom left", "bottom_left")):
            self.tc_corner.addItem(label, value)
        i = self.tc_corner.findData(u.timecode_corner)
        self.tc_corner.setCurrentIndex(max(0, i))
        self.tc_corner.currentIndexChanged.connect(
            lambda idx: self._ui_set("timecode_corner",
                                     self.tc_corner.itemData(idx)))
        tg.addWidget(self.tc_corner, 1, 1)

        tg.addWidget(QLabel("Backing strength"), 2, 0)
        self.tc_opacity = QSlider(Qt.Orientation.Horizontal)
        self.tc_opacity.setRange(0, 100)
        self.tc_opacity.setValue(int(u.timecode_opacity * 100))
        self.tc_opacity.valueChanged.connect(
            lambda v: self._ui_set("timecode_opacity", v / 100.0))
        tg.addWidget(self.tc_opacity, 2, 1)

        tg.addWidget(QLabel("Size"), 3, 0)
        self.tc_scale = QDoubleSpinBox()
        self.tc_scale.setRange(0.5, 3.0)
        self.tc_scale.setSingleStep(0.05)
        self.tc_scale.setValue(u.timecode_scale)
        self.tc_scale.setSuffix(" \u00d7")
        self.tc_scale.valueChanged.connect(lambda v: self._ui_set("timecode_scale", v))
        tg.addWidget(self.tc_scale, 3, 1)

        self.tc_total = QCheckBox("Include the clip length (00:12 / 01:30)")
        self.tc_total.setChecked(u.timecode_show_total)
        self.tc_total.toggled.connect(lambda v: self._ui_set("timecode_show_total", v))
        tg.addWidget(self.tc_total, 4, 0, 1, 2)
        self.tc_remain = QCheckBox("Count down instead of up")
        self.tc_remain.setChecked(u.timecode_show_remaining)
        self.tc_remain.toggled.connect(
            lambda v: self._ui_set("timecode_show_remaining", v))
        tg.addWidget(self.tc_remain, 5, 0, 1, 2)
        tc.setLayout(tg)
        root.addWidget(tc)
        root.addWidget(_hint(
            "The clock sits in its corner on a strip that starts at the chosen "
            "strength beside the text and fades to nothing towards the middle "
            "of the screen, so it never boxes off part of the picture."))

        # ---------------- overlays ----------------
        ov = QGroupBox("OVERLAYS & PICTURE")
        og = _form(9, 190)
        og.addWidget(QLabel("Video fit"), 0, 0)
        self.aspect_combo = QComboBox()
        for label, value in (("Fit \u2014 whole frame, black bars", "fit"),
                             ("Fill \u2014 cover the screen, crop edges", "fill"),
                             ("Stretch \u2014 distort to fill", "stretch")):
            self.aspect_combo.addItem(label, value)
        i = self.aspect_combo.findData(u.show_aspect)
        self.aspect_combo.setCurrentIndex(max(0, i))
        self.aspect_combo.currentIndexChanged.connect(
            lambda idx: self._ui_set("show_aspect", self.aspect_combo.itemData(idx)))
        og.addWidget(self.aspect_combo, 0, 1)

        self.transport_check = QCheckBox("Show the on-screen control bar when "
                                         "the mouse moves")
        self.transport_check.setChecked(u.transport_bar)
        self.transport_check.toggled.connect(lambda v: self._ui_set("transport_bar", v))
        og.addWidget(self.transport_check, 1, 0, 1, 2)

        og.addWidget(QLabel("Hide controls after"), 2, 0)
        self.autohide_spin = QDoubleSpinBox()
        self.autohide_spin.setRange(1.0, 30.0)
        self.autohide_spin.setSuffix(" s")
        self.autohide_spin.setValue(u.transport_autohide)
        self.autohide_spin.valueChanged.connect(
            lambda v: self._ui_set("transport_autohide", v))
        og.addWidget(self.autohide_spin, 2, 1)

        self.status_check = QCheckBox("Show the status bar along the bottom")
        self.status_check.setChecked(u.show_status_bar)
        self.status_check.toggled.connect(lambda v: self._ui_set("show_status_bar", v))
        og.addWidget(self.status_check, 3, 0, 1, 2)

        self.keep_fs = QCheckBox("Keep the show fullscreen when settings open "
                                 "(for a second monitor)")
        self.keep_fs.setChecked(u.settings_keeps_fullscreen)
        self.keep_fs.toggled.connect(
            lambda v: self._ui_set("settings_keeps_fullscreen", v))
        og.addWidget(self.keep_fs, 4, 0, 1, 2)
        ov.setLayout(og)
        root.addWidget(ov)
        root.addStretch(1)

        engine.state_changed.connect(self._on_state)
        engine.volume_changed.connect(self._on_volume)
        self._on_volume(u.volume, u.muted)
        self._on_state(engine.state)

    def _ui_set(self, attr: str, value):
        setattr(self.cfg.ui, attr, value)
        self.changed.emit()
        self.ui_changed.emit()

    def _on_state(self, state: str):
        self.state_label.setText(f"STATE:  {state.replace('_', ' ').upper()}")

    def _on_volume(self, volume: float, muted: bool):
        self.mute_btn.setText("\U0001F507  UNMUTE" if muted else "\U0001F50A  MUTE")
        self.volume_label.setText(f"{int(volume * 100)}%")
        if not self.volume_slider.isSliderDown():
            self.volume_slider.blockSignals(True)
            self.volume_slider.setValue(int(volume * 100))
            self.volume_slider.blockSignals(False)
class GeneralTab(QWidget):
    changed = pyqtSignal()
    ui_changed = pyqtSignal()
    save_as_requested = pyqtSignal()
    load_requested = pyqtSignal()

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        outer = QVBoxLayout(self)
        content = QWidget()
        root = QVBoxLayout(content)
        outer.addWidget(_scroll(content))

        u = cfg.ui
        g = QGridLayout()

        g.addWidget(QLabel("Fade Duration (s)"), 0, 0)
        fd = QDoubleSpinBox()
        fd.setRange(0.0, 5.0)
        fd.setSingleStep(0.1)
        fd.setValue(u.fade_duration)
        fd.valueChanged.connect(lambda v: self._set("fade_duration", v))
        g.addWidget(fd, 0, 1)

        g.addWidget(QLabel("Countdown (seconds)"), 1, 0)
        cd = QSpinBox()
        cd.setRange(0, 10)
        cd.setValue(u.countdown_seconds)
        cd.valueChanged.connect(lambda v: self._set("countdown_seconds", v))
        g.addWidget(cd, 1, 1)

        g.addWidget(QLabel("Break Header"), 2, 0)
        bh = QLineEdit(u.break_header)
        bh.textChanged.connect(lambda t: self._set("break_header", t))
        g.addWidget(bh, 2, 1)

        g.addWidget(QLabel("Loop Header"), 3, 0)
        lh = QLineEdit(u.loop_header)
        lh.textChanged.connect(lambda t: self._set("loop_header", t))
        g.addWidget(lh, 3, 1)

        g.addWidget(QLabel("Start button text"), 4, 0)
        sb = QLineEdit(u.start_button_text)
        sb.textChanged.connect(lambda t: self._set("start_button_text", t))
        g.addWidget(sb, 4, 1)

        g.addWidget(QLabel("Reset button text"), 5, 0)
        rb = QLineEdit(u.stop_button_text)
        rb.textChanged.connect(lambda t: self._set("stop_button_text", t))
        g.addWidget(rb, 5, 1)

        fs = QCheckBox("Start the show in fullscreen (kiosk)")
        fs.setChecked(u.fullscreen_on_start)
        fs.toggled.connect(lambda v: self._set("fullscreen_on_start", v))
        g.addWidget(fs, 6, 0, 1, 2)

        st = QCheckBox("Show the status bar on the show screen")
        st.setChecked(u.show_status_bar)
        st.toggled.connect(lambda v: self._set("show_status_bar", v))
        g.addWidget(st, 7, 0, 1, 2)

        root.addLayout(g)
        root.addWidget(hline())
        root.addWidget(header("CONFIG FILE"))
        row = QHBoxLayout()
        save_as = QPushButton("Save As\u2026")
        save_as.clicked.connect(self.save_as_requested.emit)
        load = QPushButton("Load\u2026")
        load.clicked.connect(self.load_requested.emit)
        row.addWidget(save_as)
        row.addWidget(load)
        row.addStretch(1)
        root.addLayout(row)

        keys = QLabel("Show-screen keys:  Space/Enter = START/RESET   \u00b7   "
                      "P = pause   \u00b7   N = skip video   \u00b7   S = stop"
                      "   \u00b7   Esc = settings   \u00b7   F11 = fullscreen")
        keys.setWordWrap(True)
        keys.setStyleSheet("color:#7f8694;")
        root.addWidget(keys)
        root.addStretch(1)

    def _set(self, attr: str, value):
        setattr(self.cfg.ui, attr, value)
        self.changed.emit()
        self.ui_changed.emit()



# ==========================================================================
#  MAIN WINDOW  —  sidebar navigation
# ==========================================================================
_NAV_STYLE = """
QPushButton {
    text-align: left; padding: 11px 14px; border-radius: 9px;
    background: transparent; border: 1px solid transparent;
    color: #aeb6c2; font-size: 13px; font-weight: 600;
}
QPushButton:hover   { background: #1e222a; color: #e8ecf2; }
QPushButton:checked { background: #1d939e; color: #ffffff;
                      border-color: #35c5d0; font-weight: 800; }
"""


class SettingsWindow(QMainWindow):
    return_to_show = pyqtSignal()
    ui_changed = pyqtSignal()

    def __init__(self, cfg: AppConfig, engine: GameEngine, link: EspLink,
                 bank: RelayBank, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.engine = engine
        self.link = link
        self._dirty = False
        self._config_path = AppConfig.default_path()
        self.bank = bank

        # rollback history: JSON snapshots, grouped so a burst of keystrokes
        # collapses into a single undo step
        self._history = [json.dumps(cfg.to_dict())]
        self._hist_index = 0
        self._hist_labels = ["opened"]
        self._pending_label = ""
        self._suspend_history = False
        self._hist_timer = QTimer(self)
        self._hist_timer.setSingleShot(True)
        self._hist_timer.setInterval(700)
        self._hist_timer.timeout.connect(self._capture)

        self.setWindowTitle("Settings \u2014 ESP32-S3 Show Controller")
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.resize(min(1380, max(900, area.width() - 80)),
                        min(880, max(560, area.height() - 80)))
        else:
            self.resize(1380, 880)

        # ---- pages ----
        self.stack = QStackedWidget()
        self._pages = []
        self._build_pages()
        for widget in self._pages:
            self.stack.addWidget(widget)

        # ---- sidebar ----
        side = QWidget()
        side.setMinimumWidth(150)
        side.setMaximumWidth(212)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(10, 12, 10, 12)
        sv.setSpacing(6)
        title = QLabel("SHOW CONTROLLER")
        title.setStyleSheet("color:#35c5d0; font-weight:800; letter-spacing:1px;"
                            "font-size:12px; padding:2px 4px 10px 4px;")
        sv.addWidget(title)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        for index, label in enumerate((
                "\u25b6   EDITOR", "\u25c8   GAME FLOW", "\u25ce   WHEEL",
                "\u26a1   ESP", "\U0001F39B   SHOW", "\u2699   GENERAL")):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(_NAV_STYLE)
            btn.clicked.connect(lambda _=False, i=index: self._go(i))
            self._nav_group.addButton(btn, index)
            sv.addWidget(btn)
        sv.addStretch(1)

        self.link_chip = QLabel()
        self.link_chip.setTextFormat(Qt.TextFormat.RichText)
        self.link_chip.setStyleSheet("font-size:11px; padding:4px;")
        sv.addWidget(self.link_chip)
        self.live_chip = QLabel()
        self.live_chip.setStyleSheet("font-size:11px; padding:4px; color:#7f8694;")
        self.live_chip.setWordWrap(True)
        sv.addWidget(self.live_chip)

        # ---- shell ----
        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(10, 10, 10, 10)
        cv.setSpacing(10)

        body = QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(side)
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(2, 2, 2, 2)
        fl.addWidget(self.stack)
        body.addWidget(frame, 1)
        cv.addLayout(body, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        self.status_line = QLabel("Ready.")
        self.status_line.setStyleSheet("color:#7f8694;")
        bottom.addWidget(self.status_line, 1)

        self.reset_btn = QPushButton("\u21bb  RESET PAGE")
        self.reset_btn.setMinimumHeight(38)
        self.reset_btn.clicked.connect(self.reset_page)
        bottom.addWidget(self.reset_btn)

        self.undo_btn = QPushButton("\u21b6  UNDO")
        self.undo_btn.setMinimumHeight(38)
        self.undo_btn.clicked.connect(self.undo)
        bottom.addWidget(self.undo_btn)

        self.redo_btn = QPushButton("\u21b7  REDO")
        self.redo_btn.setMinimumHeight(38)
        self.redo_btn.clicked.connect(self.redo)
        bottom.addWidget(self.redo_btn)
        save_btn = QPushButton("\U0001F4BE  SAVE SETTINGS")
        save_btn.setMinimumHeight(38)
        save_btn.clicked.connect(self.save)
        bottom.addWidget(save_btn)
        show_btn = QPushButton("\u25b6  RETURN TO SHOW")
        show_btn.setMinimumHeight(38)
        show_btn.setStyleSheet("QPushButton{background:#1d939e; color:#fff;"
                               "font-weight:800;}")
        show_btn.clicked.connect(self._return)
        bottom.addWidget(show_btn)
        cv.addLayout(bottom)
        self.setCentralWidget(central)

        # ---- wiring ----
        link.state_changed.connect(lambda _s: self._refresh_chips())
        engine.state_changed.connect(lambda _s: self._refresh_chips())

        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save)
        QShortcut(QKeySequence("F11"), self, activated=self._return)
        for i in range(6):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self,
                      activated=lambda idx=i: self._go(idx))

        self._go(0)
        self._refresh_chips()
        self._refresh_history_buttons()

    # ------------------------------------------------------------ pages
    def _build_pages(self):
        cfg, link, bank, engine = self.cfg, self.link, self.bank, self.engine
        self.test_panel = TestModePanel(cfg, bank, engine.relay_states)
        self.editor_page = PlaylistTab(cfg, self.test_panel)
        self.flow_page = GameFlowEditor(cfg.root_questions, lambda: cfg.playlist)
        self.wheel_page = WheelTab(cfg, self._wheel_sample)
        self.esp_page = EspTab(cfg, link, bank)
        self.show_page = ShowTab(cfg, engine)
        self.general_page = GeneralTab(cfg)
        self._pages = [self.editor_page, _scroll(self.flow_page),
                       self.wheel_page, self.esp_page, self.show_page,
                       self.general_page]

        for page, name in ((self.editor_page, "playlist"),
                           (self.flow_page, "game flow"),
                           (self.wheel_page, "wheel"),
                           (self.esp_page, "ESP"),
                           (self.show_page, "show"),
                           (self.general_page, "general")):
            page.changed.connect(lambda n=name: self.mark_dirty(n))
        self.show_page.ui_changed.connect(self.ui_changed.emit)
        self.general_page.ui_changed.connect(self.ui_changed.emit)
        self.editor_page.playlist_mutated.connect(self._playlist_changed)
        self.test_panel.log.connect(self.show_log)
        self.general_page.save_as_requested.connect(self._save_as)
        self.general_page.load_requested.connect(self._load)
        # relays added or removed must reach the engine and the test panel
        self.esp_page.relay_manager.changed.connect(self._relays_changed)

    def _relays_changed(self):
        self.engine.sync_relay_list()
        self.bank.sync_relay_list()
        self.test_panel.rebuild_relay_leds()
        self.test_panel.refresh_from_scenario()

    # ------------------------------------------------------------ navigation
    def _go(self, index: int):
        # a live hardware test never survives leaving the editor page
        if self.stack.currentWidget() is self.editor_page and index != 0:
            self.test_panel.shutdown()
        self.stack.setCurrentIndex(index)
        btn = self._nav_group.button(index)
        if btn is not None:
            btn.setChecked(True)
        if self._pages[index] is self.wheel_page:
            self.wheel_page.regenerate()

    def open_page(self, name: str):
        order = {"editor": 0, "flow": 1, "wheel": 2, "esp": 3, "show": 4,
                 "general": 5}
        self._go(order.get(name, 0))

    def _playlist_changed(self):
        self.flow_page.refresh_list()
        self.wheel_page.regenerate()

    def _refresh_chips(self):
        st = self.link.state
        self.link_chip.setText(
            f"<span style='color:{esp_link.state_color(st)}'>"
            f"\u25cf ESP {esp_link.state_label(st).lower()}</span>")
        running = self.engine.state != "Idle"
        self.live_chip.setText(
            "\u25b6 Show is running \u2014 changes apply live."
            if running else "Show idle.")

    # ------------------------------------------------------------ helpers
    def _wheel_sample(self) -> List[Tuple[str, float]]:
        from config import wheel_pieces
        for q in self.cfg.root_questions:
            if len(q.answers) >= 2:
                return wheel_pieces(q, self.cfg.playlist)
        if self.cfg.playlist:
            return [(s.wheel_label, s.effective_weight())
                    for s in self.cfg.playlist]
        return [("A", 100.0), ("B", 100.0), ("C", 100.0)]

    def mark_dirty(self, label: str = ""):
        if not self._dirty:
            self._dirty = True
            self.setWindowTitle(
                "Settings \u2014 ESP32-S3 Show Controller  \u25cf unsaved")
        if not self._suspend_history:
            self._pending_label = label or self._page_name()
            self._hist_timer.start()

    # ------------------------------------------------------------ history
    def _page_name(self) -> str:
        names = ["playlist", "game flow", "wheel", "ESP", "show", "general"]
        idx = self.stack.currentIndex()
        return names[idx] if 0 <= idx < len(names) else "settings"

    def _capture(self):
        snapshot = json.dumps(self.cfg.to_dict())
        if snapshot == self._history[self._hist_index]:
            return
        del self._history[self._hist_index + 1:]
        del self._hist_labels[self._hist_index + 1:]
        self._history.append(snapshot)
        self._hist_labels.append(self._pending_label or "edit")
        if len(self._history) > 60:
            self._history.pop(0)
            self._hist_labels.pop(0)
        self._hist_index = len(self._history) - 1
        self._refresh_history_buttons()

    def _refresh_history_buttons(self):
        can_undo = self._hist_index > 0
        can_redo = self._hist_index < len(self._history) - 1
        self.undo_btn.setEnabled(can_undo)
        self.redo_btn.setEnabled(can_redo)
        self.undo_btn.setText(
            f"\u21b6  UNDO {self._hist_labels[self._hist_index]}"
            if can_undo else "\u21b6  UNDO")
        self.redo_btn.setText(
            f"\u21b7  REDO {self._hist_labels[self._hist_index + 1]}"
            if can_redo else "\u21b7  REDO")

    def _restore(self, snapshot: str, what: str):
        self._suspend_history = True
        try:
            loaded = AppConfig.from_dict(json.loads(snapshot))
            self.cfg.playlist[:] = loaded.playlist
            self.cfg.root_questions[:] = loaded.root_questions
            self.cfg.wheel.__dict__.update(loaded.wheel.__dict__)
            self.cfg.esp.__dict__.update(loaded.esp.__dict__)
            self.cfg.ui.__dict__.update(loaded.ui.__dict__)
            self._rebuild_pages()
            self.show_log(what)
        finally:
            self._suspend_history = False
        self._refresh_history_buttons()

    def undo(self):
        if self._hist_index <= 0:
            return
        label = self._hist_labels[self._hist_index]
        self._hist_index -= 1
        self._restore(self._history[self._hist_index], f"Undid: {label}")

    def redo(self):
        if self._hist_index >= len(self._history) - 1:
            return
        self._hist_index += 1
        label = self._hist_labels[self._hist_index]
        self._restore(self._history[self._hist_index], f"Redid: {label}")

    # ------------------------------------------------------------ reset
    def reset_page(self):
        idx = self.stack.currentIndex()
        name = self._page_name()
        if QMessageBox.question(
                self, "Reset page",
                f"Restore the {name} settings to their defaults?\n"
                f"Everything else is left alone, and this can be undone.") != \
                QMessageBox.StandardButton.Yes:
            return
        defaults = AppConfig.make_default()
        if idx == 0:
            self.cfg.playlist[:] = defaults.playlist
        elif idx == 1:
            self.cfg.root_questions[:] = defaults.root_questions
        elif idx == 2:
            self.cfg.wheel.__dict__.update(defaults.wheel.__dict__)
        elif idx == 3:
            keep_ip, keep_port = self.cfg.esp.ip, self.cfg.esp.port
            self.cfg.esp.__dict__.update(defaults.esp.__dict__)
            self.cfg.esp.ip, self.cfg.esp.port = keep_ip, keep_port
        else:
            self.cfg.ui.__dict__.update(defaults.ui.__dict__)
        self._rebuild_pages()
        self.mark_dirty(f"reset {name}")
        self.show_log(f"{name.capitalize()} restored to defaults.")

    # ------------------------------------------------------------ rebuild
    def _rebuild_pages(self):
        """Recreate every page so the widgets show the restored values."""
        index = self.stack.currentIndex()
        self.test_panel.shutdown()
        for i in reversed(range(self.stack.count())):
            w = self.stack.widget(i)
            self.stack.removeWidget(w)
            w.deleteLater()
        self._pages = []
        self._build_pages()
        for i, widget in enumerate(self._pages):
            self.stack.insertWidget(i, widget)
        self.stack.setCurrentIndex(min(index, self.stack.count() - 1))
        self.engine.sync_relay_list()
        self.bank.sync_relay_list()
        self.ui_changed.emit()

    def show_log(self, message: str):
        self.status_line.setText(message)

    # ------------------------------------------------------------ save / load
    def save(self):
        try:
            path = self.cfg.save(self._config_path)
            self._dirty = False
            self.setWindowTitle("Settings \u2014 ESP32-S3 Show Controller")
            self.show_log(f"Saved \u2192 {path}")
        except Exception as ex:
            QMessageBox.warning(self, "Save failed", str(ex))

    def _save_as(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save configuration", self._config_path, "JSON (*.json)")
        if path:
            self._config_path = path
            self.save()

    def _load(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load configuration", self._config_path, "JSON (*.json)")
        if not path:
            return
        loaded = AppConfig.load(path)
        self.cfg.playlist[:] = loaded.playlist
        self.cfg.root_questions[:] = loaded.root_questions
        self.cfg.wheel.__dict__.update(loaded.wheel.__dict__)
        self.cfg.esp.__dict__.update(loaded.esp.__dict__)
        self.cfg.ui.__dict__.update(loaded.ui.__dict__)
        self._config_path = path
        self.editor_page.refresh_list(select=0)
        self.test_panel.reload_playlist()
        self.test_panel.apply_aspect()
        self.flow_page.refresh_list(select=0)
        self.wheel_page.preview.set_settings(self.cfg.wheel)
        self.wheel_page.regenerate()
        entropy.set_mode(self.cfg.wheel.entropy)
        self.ui_changed.emit()
        self.show_log(f"Loaded \u2190 {path}")
        self.mark_dirty()

    # ------------------------------------------------------------ leave / close
    def _return(self):
        self.test_panel.shutdown()
        self.return_to_show.emit()

    def closeEvent(self, event):
        self.test_panel.shutdown()
        if self._dirty:
            ans = QMessageBox.question(
                self, "Unsaved changes", "Save settings before closing?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel)
            if ans == QMessageBox.StandardButton.Save:
                self.save()
            elif ans == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
        event.accept()
        self.return_to_show.emit()
