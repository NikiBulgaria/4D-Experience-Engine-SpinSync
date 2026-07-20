"""
test_panel.py — TRIGGER TESTER (Test Mode).

Port of AdvancedVideoControls.cs + AdvancedVideoControlsEditor.cs:

  * Enter Test Mode on any playlist entry: the clip loads, seeks to the trim
    IN point and arms only the triggers inside the trim window.
  * Play/Pause honors the trim window; reaching the OUT point auto-pauses,
    rewinds to IN and forces both relays OFF **unconditionally** (the Unity
    FIX: never EvaluateRelaysAt(trimStart) here, or a MotorOn trigger sitting
    at t=IN would flip the motor back on).
  * Scrubbing anywhere in the full clip re-computes the relay state and the
    fired flags for that exact moment (EvaluateRelaysAt port).
  * Trigger table with FIRED / NEXT / pending status, jump-to-trigger chips,
    playback-speed presets, relay LEDs, and trim IN/OUT editing from the
    playhead.
  * 'Live hardware' really drives the ESP32 relays while testing.
"""

from __future__ import annotations

import os
from typing import Callable, Optional, Tuple

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFrame,
                             QGridLayout, QHeaderView, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QSizePolicy, QSplitter, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from config import (AppConfig, VideoScenario, relay_color,
                    format_time)
from esp_link import RelayBank
from widgets import (DetachablePanel, LedBlock, TriggerTimeline, header,
                     hline)

_LOADED = (QMediaPlayer.MediaStatus.LoadedMedia,
           QMediaPlayer.MediaStatus.BufferedMedia)

_SMALL_BTN = ("QPushButton{padding:5px 10px;}"
              "QPushButton:checked{background:#1d939e;color:#fff;}")


class TestModePanel(QWidget):
    log = pyqtSignal(str)
    video_selected = pyqtSignal(int)

    def __init__(self, cfg: AppConfig, relay_bank: RelayBank,
                 engine_relay_source: Callable[[], Tuple[bool, bool]],
                 parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.bank = relay_bank
        self.engine_source = engine_relay_source

        # --- AdvancedVideoControls test state ---------------------------------
        self.test_mode_active = False
        self.test_video_index = 0
        self.test_scrub_time = 0.0
        self.test_relays = {r.id: False for r in cfg.esp.relays}
        self._preparing = False
        self._duration = 0.0
        self.trim_sync_cb = None      # set by PlaylistTab to mirror trim edits

        # --- media -------------------------------------------------------------
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumHeight(300)
        self.video_widget.setSizePolicy(QSizePolicy.Policy.Expanding,
                                        QSizePolicy.Policy.Expanding)
        self.video_widget.setStyleSheet("background:#000;")
        self.player.setVideoOutput(self.video_widget)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.durationChanged.connect(self._on_duration)
        self.player.errorOccurred.connect(
            lambda _e, msg: self.log.emit(f"[TestMode] media error: {msg}"))

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(33)
        self._tick_timer.timeout.connect(self._update_tick)

        self._build_ui()
        self.apply_aspect()
        self.timeline.set_relays(cfg.esp.relays)
        self.reload_playlist()

    # =====================================================================
    #  UI
    # =====================================================================
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 10)
        root.setSpacing(10)

        top = QHBoxLayout()
        top.addWidget(header("\U0001F3AC  TRIGGER TESTER"))
        top.addStretch(1)
        self.state_chip = QLabel("IDLE")
        self.state_chip.setStyleSheet(
            "color:#9aa1ad; border:1px solid #2a2e36; border-radius:8px;"
            "padding:2px 10px; font-weight:700; font-size:11px;")
        top.addWidget(self.state_chip)
        root.addLayout(top)

        row = QHBoxLayout()
        row.addWidget(QLabel("Video"))
        self.video_combo = QComboBox()
        self.video_combo.setMinimumWidth(150)
        self.video_combo.setSizePolicy(QSizePolicy.Policy.Ignored,
                                       QSizePolicy.Policy.Fixed)
        self.video_combo.setToolTip("Which clip the preview and Test Mode use.")
        self.video_combo.currentIndexChanged.connect(self._combo_changed)
        row.addWidget(self.video_combo, 1)
        self.enter_btn = QPushButton("\u25c9  ENTER TEST MODE")
        self.enter_btn.clicked.connect(self._toggle_test_mode)
        row.addWidget(self.enter_btn)
        root.addLayout(row)

        # Drag this divider to give the picture as much room as you like —
        # the same handle idea as the panel split on the right.
        self.preview_panel = DetachablePanel("PREVIEW", self.video_widget)
        self.preview_panel.detached.connect(self._preview_detached)

        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(8)
        self.split.addWidget(self.preview_panel)

        # Controls live in their own scroll area, so however large you make the
        # preview the panel below stays usable — a scrollbar simply appears.
        self.ctl_scroll = QScrollArea()
        self.ctl_scroll.setWidgetResizable(True)
        self.ctl_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.ctl_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        controls = QWidget()
        ctl = QVBoxLayout(controls)
        ctl.setContentsMargins(0, 0, 6, 0)
        ctl.setSpacing(10)
        self.ctl_scroll.setWidget(controls)
        self.split.addWidget(self.ctl_scroll)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        self.split.setSizes([420, 320])
        self.split.splitterMoved.connect(lambda *_: self._adapt_controls())
        root.addWidget(self.split, 1)
        root = ctl                     # everything below sits under the divider

        info = QHBoxLayout()
        info.setSpacing(10)
        self.name_label = QLabel("—")
        self.name_label.setStyleSheet("font-weight:700; color:#e8ecf2;")
        info.addWidget(self.name_label)
        info.addStretch(1)
        info.addWidget(QLabel("Fit"))
        self.aspect_combo = QComboBox()
        self.aspect_combo.addItem("Fit (letterbox)", "fit")
        self.aspect_combo.addItem("Fill (crop)", "fill")
        self.aspect_combo.addItem("Stretch", "stretch")
        self.aspect_combo.setToolTip(
            "How the clip is scaled inside the preview area.")
        self.aspect_combo.setMinimumWidth(130)
        self.aspect_combo.currentIndexChanged.connect(self._aspect_changed)
        info.addWidget(self.aspect_combo)
        info.addSpacing(12)
        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setStyleSheet("color:#9aa1ad; font-family:Consolas;")
        info.addWidget(self.time_label)
        root.addLayout(info)

        self.timeline = TriggerTimeline()
        self.timeline.scrub_requested.connect(self.scrub_to)
        root.addWidget(self.timeline)

        transport = QHBoxLayout()
        transport.setSpacing(8)
        self.play_btn = QPushButton("\u25b6  PLAY")
        self.play_btn.setMinimumHeight(36)
        self.play_btn.clicked.connect(self.toggle_play_pause)
        self.to_in_btn = QPushButton("\u23ee  IN")
        self.to_in_btn.setMinimumHeight(36)
        self.to_in_btn.setMinimumWidth(78)
        self.to_in_btn.setToolTip("Jump to the trim IN point")
        self.to_in_btn.clicked.connect(self._jump_to_in)
        self.to_out_btn = QPushButton("OUT \u23ed")
        self.to_out_btn.setMinimumHeight(36)
        self.to_out_btn.setMinimumWidth(78)
        self.to_out_btn.setToolTip("Jump to the trim OUT point")
        self.to_out_btn.clicked.connect(self._jump_to_out)
        transport.addWidget(self.to_in_btn)
        transport.addWidget(self.play_btn, 1)
        transport.addWidget(self.to_out_btn)
        root.addLayout(transport)

        # trim editing ---------------------------------------------------------
        trim = QGridLayout()
        trim.setHorizontalSpacing(12)
        trim.setVerticalSpacing(8)
        trim.setColumnMinimumWidth(0, 96)
        trim.setColumnStretch(1, 1)
        trim.addWidget(QLabel("Trim IN (s)"), 0, 0)
        self.in_spin = QDoubleSpinBox()
        self.in_spin.setRange(0, 359999)
        self.in_spin.setDecimals(2)
        self.in_spin.setSingleStep(0.5)
        self.in_spin.valueChanged.connect(self._trim_edited)
        trim.addWidget(self.in_spin, 0, 1)
        b = QPushButton("Set from playhead")
        b.clicked.connect(lambda: self.in_spin.setValue(self.test_scrub_time))
        trim.addWidget(b, 0, 2)

        trim.addWidget(QLabel("Trim OUT (s)"), 1, 0)
        self.out_spin = QDoubleSpinBox()
        self.out_spin.setRange(0, 359999)
        self.out_spin.setDecimals(2)
        self.out_spin.setSingleStep(0.5)
        self.out_spin.setSpecialValueText("clip end")
        self.out_spin.valueChanged.connect(self._trim_edited)
        trim.addWidget(self.out_spin, 1, 1)
        b2 = QPushButton("Set from playhead")
        b2.clicked.connect(lambda: self.out_spin.setValue(self.test_scrub_time))
        trim.addWidget(b2, 1, 2)
        b3 = QPushButton("Clear")
        b3.clicked.connect(lambda: self.out_spin.setValue(0.0))
        trim.addWidget(b3, 1, 3)
        root.addLayout(trim)

        # speed ------------------------------------------------------------------
        sp = QHBoxLayout()
        sp.setSpacing(8)
        sp.addWidget(QLabel("Speed"))
        self._speed_buttons = []
        for s in (0.25, 0.5, 1.0, 2.0, 4.0):
            btn = QPushButton(f"{s:g}\u00d7")
            btn.setCheckable(True)
            btn.setStyleSheet(_SMALL_BTN)
            btn.clicked.connect(lambda _=False, v=s: self.set_speed(v))
            sp.addWidget(btn)
            self._speed_buttons.append((s, btn))
        self.custom_speed = QDoubleSpinBox()
        self.custom_speed.setRange(0.1, 8.0)
        self.custom_speed.setSingleStep(0.05)
        self.custom_speed.setValue(1.0)
        sp.addWidget(self.custom_speed)
        setb = QPushButton("SET")
        setb.setStyleSheet(_SMALL_BTN)
        setb.clicked.connect(lambda: self.set_speed(self.custom_speed.value()))
        sp.addWidget(setb)
        self.active_speed = QLabel("ACTIVE: 1.00\u00d7")
        self.active_speed.setStyleSheet("color:#9aa1ad; font-family:Consolas;")
        sp.addWidget(self.active_speed)
        sp.addStretch(1)
        root.addLayout(sp)

        # relays -------------------------------------------------------------------
        self.led_row = QHBoxLayout()
        self.led_row.setSpacing(10)
        self.leds = {}
        root.addLayout(self.led_row)
        self.rebuild_relay_leds()

        self.live_check = QCheckBox(
            "Live hardware — Test Mode drives the real ESP32 relays")
        self.live_check.setChecked(self.cfg.ui.test_live_hardware)
        self.live_check.toggled.connect(self._live_toggled)
        root.addWidget(self.live_check)

        root.addWidget(hline())
        root.addWidget(header("TRIGGERS ON THIS VIDEO"))

        self.trigger_table = QTableWidget(0, 3)
        self.trigger_table.setHorizontalHeaderLabels(["TIME", "ACTION", "STATUS"])
        self.trigger_table.verticalHeader().setVisible(False)
        self.trigger_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.trigger_table.setSelectionMode(
            QTableWidget.SelectionMode.NoSelection)
        self.trigger_table.setMaximumHeight(150)
        tt_head = self.trigger_table.horizontalHeader()
        tt_head.setStretchLastSection(True)
        tt_head.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        tt_head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        tt_head.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.trigger_table)

        root.addWidget(header("JUMP TO TRIGGER"))
        self.jump_row = QHBoxLayout()
        self.jump_row.setSpacing(4)
        jump_holder = QWidget()
        jump_holder.setLayout(self.jump_row)
        root.addWidget(jump_holder)

    # =====================================================================
    #  helpers
    # =====================================================================
    def _preview_detached(self, detached: bool):
        """When the preview leaves, hand its space to the controls."""
        self.split.setSizes([90, 900] if detached else [420, 320])
        self._adapt_controls()
        self.log.emit("Preview detached — drag it to a screen edge to snap it."
                      if detached else "Preview re-attached.")

    def _adapt_controls(self):
        """Compact the controls when the preview has taken most of the room."""
        available = self.ctl_scroll.height()
        compact = available < 300
        for widget in (self.trigger_table, self.timeline):
            if widget is None:
                continue
        self.trigger_table.setMinimumHeight(90 if compact else 150)
        self.timeline.setMinimumHeight(46 if compact else 64)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._adapt_controls()

    def _aspect_changed(self, index: int):
        mode = self.aspect_combo.itemData(index) or "fit"
        self.cfg.ui.preview_aspect = mode
        self.apply_aspect()

    def apply_aspect(self):
        mode = getattr(self.cfg.ui, "preview_aspect", "fit")
        modes = {"fit": Qt.AspectRatioMode.KeepAspectRatio,
                 "fill": Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                 "stretch": Qt.AspectRatioMode.IgnoreAspectRatio}
        self.video_widget.setAspectRatioMode(modes.get(mode,
                                             Qt.AspectRatioMode.KeepAspectRatio))
        idx = self.aspect_combo.findData(mode)
        if idx >= 0 and idx != self.aspect_combo.currentIndex():
            self.aspect_combo.blockSignals(True)
            self.aspect_combo.setCurrentIndex(idx)
            self.aspect_combo.blockSignals(False)

    def _combo_changed(self, index: int):
        """Switching the clip here reloads the preview and tells the playlist."""
        if index < 0 or index == self.test_video_index and self.test_mode_active:
            return
        self.video_selected.emit(index)
        if self.test_mode_active:
            self.enter_test_mode(index)        # reload the player on the new clip
        else:
            self.test_video_index = index
            self.refresh_from_scenario()

    def select_video(self, index: int):
        """Called by the playlist list so both stay on the same clip."""
        if not (0 <= index < self.video_combo.count()):
            return
        if self.video_combo.currentIndex() != index:
            self.video_combo.setCurrentIndex(index)   # fires _combo_changed
            return
        if self.test_mode_active and self.test_video_index != index:
            self.enter_test_mode(index)
        else:
            self.test_video_index = index
            self.refresh_from_scenario()

    def _scenario(self) -> Optional[VideoScenario]:
        if 0 <= self.test_video_index < len(self.cfg.playlist):
            return self.cfg.playlist[self.test_video_index]
        return None

    def _trim_bounds(self, s: VideoScenario) -> Tuple[float, float]:
        start = s.start_time
        end = s.end_time if s.end_time > 0 else (
            self._duration if self._duration > 0 else float("inf"))
        return start, end

    def reload_playlist(self):
        cur = self.video_combo.currentIndex()
        self.video_combo.blockSignals(True)
        self.video_combo.clear()
        for i, s in enumerate(self.cfg.playlist):
            self.video_combo.addItem(f"[{i}] {s.wheel_label}")
        if 0 <= cur < self.video_combo.count():
            self.video_combo.setCurrentIndex(cur)
        self.video_combo.blockSignals(False)
        self.refresh_from_scenario()

    def refresh_from_scenario(self):
        """Called by the playlist tab whenever trims/triggers were edited."""
        s = self._scenario()
        if s is not None:
            self.in_spin.blockSignals(True)
            self.out_spin.blockSignals(True)
            self.in_spin.setValue(s.start_time)
            self.out_spin.setValue(s.end_time)
            self.in_spin.blockSignals(False)
            self.out_spin.blockSignals(False)
            self.timeline.set_trim(s.start_time, s.end_time)
            self._refresh_trigger_views()

    # =====================================================================
    #  ENTER / EXIT (AdvancedVideoControls.EnterTestMode / ExitTestMode)
    # =====================================================================
    def _toggle_test_mode(self):
        if self.test_mode_active:
            self.exit_test_mode()
        else:
            self.enter_test_mode(self.video_combo.currentIndex())

    def enter_test_mode(self, playlist_index: int):
        if not (0 <= playlist_index < len(self.cfg.playlist)):
            return
        scenario = self.cfg.playlist[playlist_index]
        if not scenario.path or not os.path.isfile(scenario.path):
            self.log.emit(f"[TestMode] '{scenario.wheel_label}' has no "
                          f"playable clip assigned.")
            return

        self.exit_test_mode(silent=True)

        self.test_video_index = playlist_index
        self.test_mode_active = True
        self.test_scrub_time = scenario.start_time
        self.test_relays = {rid: False for rid in self.test_relays}
        self._apply_relays()
        if self.live_check.isChecked():
            self.bank.game_source = self._relay_source

        # arm only triggers inside the trim window
        trim_start = scenario.start_time
        trim_end = scenario.end_time if scenario.end_time > 0 else float("inf")
        for t in scenario.triggers:
            t.has_fired = not (trim_start <= t.timestamp < trim_end)

        self._preparing = True
        self._duration = 0.0
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(os.path.abspath(scenario.path)))

        self.enter_btn.setText("\u25c9  EXIT TEST MODE")
        self.state_chip.setText("TEST")
        self.state_chip.setStyleSheet(
            "color:#06282b; background:#35c5d0; border-radius:8px;"
            "padding:2px 10px; font-weight:800; font-size:11px;")
        self.name_label.setText(f"[TEST]  {scenario.wheel_label}")
        self.in_spin.setValue(scenario.start_time)
        self.out_spin.setValue(scenario.end_time)
        self._tick_timer.start()
        self.log.emit(f"[TestMode] Loaded '{scenario.wheel_label}'  "
                      f"IN:{scenario.start_time:.2f}s  "
                      f"OUT:{scenario.end_time:.2f}s")

    def _on_media_status(self, status):
        if self._preparing and status in _LOADED:
            self._preparing = False
            self._on_test_prepared()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia and self._preparing:
            self._preparing = False
            self.log.emit("[TestMode] Could not open the video file.")
            self.exit_test_mode(silent=True)

    def _on_duration(self, ms: int):
        self._duration = ms / 1000.0
        self.timeline.set_duration(self._duration)

    def _on_test_prepared(self):
        """OnTestPrepared port — seek IN, evaluate relay state at IN."""
        s = self._scenario()
        if s is None or not self.test_mode_active:
            return
        if s.start_time > 0:
            self.player.setPosition(int(s.start_time * 1000))
            self.test_scrub_time = s.start_time
        self._evaluate_relays_at(s.start_time)
        self._refresh_trigger_views()

    def exit_test_mode(self, silent: bool = False):
        self._preparing = False
        self._tick_timer.stop()
        if any(self.test_relays.values()):
            self.test_relays = {rid: False for rid in self.test_relays}
        self._apply_relays()
        self.bank.game_source = self.engine_source
        self.player.stop()
        self.player.setSource(QUrl())
        self.player.setPlaybackRate(1.0)
        self._sync_speed_ui(1.0)
        self.test_mode_active = False
        self.test_scrub_time = 0.0
        self._duration = 0.0
        self.enter_btn.setText("\u25c9  ENTER TEST MODE")
        self.state_chip.setText("IDLE")
        self.state_chip.setStyleSheet(
            "color:#9aa1ad; border:1px solid #2a2e36; border-radius:8px;"
            "padding:2px 10px; font-weight:700; font-size:11px;")
        self.name_label.setText("—")
        self.play_btn.setText("\u25b6  PLAY")
        self.timeline.set_duration(0)
        self.timeline.set_triggers([])
        if not silent:
            self.log.emit("[TestMode] Exited.")

    # =====================================================================
    #  Update() port — live trigger firing + trim enforcement
    # =====================================================================
    def _update_tick(self):
        if not self.test_mode_active:
            self._tick_timer.stop()
            return
        s = self._scenario()
        if s is None:
            return
        pos = self.player.position() / 1000.0
        playing = (self.player.playbackState()
                   == QMediaPlayer.PlaybackState.PlayingState)
        self.test_scrub_time = pos
        self.timeline.set_playhead(pos)

        trim_start, trim_end = self._trim_bounds(s)
        cur = format_time(pos)
        self.time_label.setText(f"{cur} / {format_time(self._duration)}")

        if playing:
            near_clip_end = (self._duration > 0
                             and pos >= self._duration - 0.08)
            if pos >= trim_end or near_clip_end:
                # ---- stop at trim OUT --------------------------------------
                self.player.pause()
                self.player.setPosition(int(trim_start * 1000))
                self.test_scrub_time = trim_start
                self.play_btn.setText("\u25b6  PLAY")
                # FIX preserved: force relays OFF unconditionally — never
                # EvaluateRelaysAt(trimStart) here.
                if any(self.test_relays.values()):
                    self.test_relays = {rid: False
                                        for rid in self.test_relays}
                    self._apply_relays()
                self._refresh_trigger_views()
                return

            for trig in s.triggers:
                if not trig.has_fired and pos >= trig.timestamp:
                    trig.has_fired = True
                    self._apply_trigger(trig)
                    self._refresh_trigger_views()
        else:
            self.play_btn.setText("\u25b6  PLAY")

    # =====================================================================
    #  transport (TestTogglePlayPause / ScrubTo ports)
    # =====================================================================
    def toggle_play_pause(self):
        if not self.test_mode_active:
            return
        s = self._scenario()
        if s is None:
            return
        if (self.player.playbackState()
                == QMediaPlayer.PlaybackState.PlayingState):
            self.player.pause()
            self.play_btn.setText("\u25b6  PLAY")
            return

        trim_start, trim_end = self._trim_bounds(s)
        pos = self.player.position() / 1000.0
        if pos >= trim_end - 0.05 or pos < trim_start:
            self.player.setPosition(int(trim_start * 1000))
            self.test_scrub_time = trim_start
            for t in s.triggers:      # re-arm the trim window only
                t.has_fired = not (trim_start <= t.timestamp < trim_end)
            self._evaluate_relays_at(trim_start)
        self.player.play()
        self.play_btn.setText("\u23f8  PAUSE")
        self._refresh_trigger_views()

    def scrub_to(self, seconds: float):
        """ScrubTo port — full-clip scrubbing with state re-evaluation."""
        if not self.test_mode_active:
            return
        s = self._scenario()
        if s is None:
            return
        seconds = max(0.0, min(seconds,
                               self._duration if self._duration > 0 else seconds))
        self.test_scrub_time = seconds
        if (self.player.playbackState()
                == QMediaPlayer.PlaybackState.PlayingState):
            self.player.pause()
            self.play_btn.setText("\u25b6  PLAY")
        self.player.setPosition(int(seconds * 1000))
        self.timeline.set_playhead(seconds)

        for t in s.triggers:
            t.has_fired = False
        self._evaluate_relays_at(seconds)
        for t in s.triggers:
            if t.timestamp <= seconds:
                t.has_fired = True
        self._refresh_trigger_views()

    def _jump_to_in(self):
        s = self._scenario()
        if s is not None:
            self.scrub_to(s.start_time)

    def _jump_to_out(self):
        s = self._scenario()
        if s is None:
            return
        _, trim_end = self._trim_bounds(s)
        if trim_end != float("inf"):
            self.scrub_to(max(0.0, trim_end - 0.1))

    def set_speed(self, speed: float):
        speed = max(0.1, min(8.0, speed))
        self.player.setPlaybackRate(speed)
        self._sync_speed_ui(speed)

    def _sync_speed_ui(self, speed: float):
        self.active_speed.setText(f"ACTIVE: {speed:.2f}\u00d7")
        for val, btn in self._speed_buttons:
            btn.setChecked(abs(val - speed) < 0.001)

    # =====================================================================
    #  relays (EvaluateRelaysAt / ApplyTrigger ports)
    # =====================================================================
    def _relay_source(self) -> Tuple[bool, bool]:
        return dict(self.test_relays)

    def _live_toggled(self, on: bool):
        self.cfg.ui.test_live_hardware = on
        if self.test_mode_active and on:
            self.bank.game_source = self._relay_source
        else:
            self.bank.game_source = self.engine_source

    def _evaluate_relays_at(self, t: float):
        s = self._scenario()
        if s is None:
            return
        rebuilt = {r.id: False for r in self.cfg.esp.relays}
        for trig in s.triggers:
            if trig.timestamp <= t and trig.relay_id in rebuilt:
                rebuilt[trig.relay_id] = bool(trig.state)
        if rebuilt != self.test_relays:
            self.test_relays = rebuilt
            self._apply_relays()

    def _apply_trigger(self, trigger):
        if trigger.relay_id not in self.test_relays:
            return
        self.test_relays[trigger.relay_id] = bool(trigger.state)
        self._apply_relays()
        self.log.emit(f"[TestMode] \u25ba {trigger.label(self.cfg.esp.relays)}")

    def rebuild_relay_leds(self):
        """Rebuild the indicator row after relays are added or removed."""
        self.timeline.set_relays(self.cfg.esp.relays)
        while self.led_row.count():
            item = self.led_row.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.leds = {}
        for i, relay in enumerate(self.cfg.esp.relays):
            led = LedBlock(relay.name, QColor(relay_color(i)))
            led.configure(relay.name, relay.on_label, relay.off_label)
            self.leds[relay.id] = led
            self.led_row.addWidget(led)
        self.led_row.addStretch(1)
        self.test_relays = {r.id: self.test_relays.get(r.id, False)
                            for r in self.cfg.esp.relays}
        self._apply_relays()

    def _apply_relays(self):
        for rid, led in self.leds.items():
            led.set_state(bool(self.test_relays.get(rid, False)))

    # =====================================================================
    #  trim edits + trigger views
    # =====================================================================
    def _trim_edited(self):
        s = self._scenario()
        if s is None or not self.test_mode_active:
            return
        s.start_time = self.in_spin.value()
        s.end_time = self.out_spin.value()
        self.timeline.set_trim(s.start_time, s.end_time)
        self._refresh_trigger_views()
        if self.trim_sync_cb is not None:
            self.trim_sync_cb()

    def _refresh_trigger_views(self):
        s = self._scenario()
        triggers = s.sorted_triggers() if (s and self.test_mode_active) else []

        next_idx = None
        for i, t in enumerate(triggers):
            if not t.has_fired and t.timestamp > self.test_scrub_time:
                next_idx = i
                break
        self.timeline.set_triggers(triggers, next_idx)
        if s is not None:
            self.timeline.set_trim(s.start_time, s.end_time)

        self.trigger_table.setRowCount(len(triggers))
        for i, t in enumerate(triggers):
            self.trigger_table.setItem(i, 0,
                                       QTableWidgetItem(f"{t.timestamp:7.2f}s"))
            self.trigger_table.setItem(
                i, 1, QTableWidgetItem(t.label(self.cfg.esp.relays)))
            if t.has_fired and t.timestamp <= self.test_scrub_time:
                item = QTableWidgetItem("\u2713  FIRED")
                item.setForeground(QColor("#43d17c"))
            elif i == next_idx:
                item = QTableWidgetItem("\u25b6  NEXT")
                item.setForeground(QColor("#ffb04a"))
            else:
                item = QTableWidgetItem("\u2014 pending")
                item.setForeground(QColor("#7f8694"))
            self.trigger_table.setItem(i, 2, item)

        # jump chips
        while self.jump_row.count():
            it = self.jump_row.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        for t in triggers:
            btn = QPushButton(f"{format_time(t.timestamp)}\n"
                              f"{t.short(self.cfg.esp.relays)}")
            btn.setStyleSheet(_SMALL_BTN)
            btn.setMaximumWidth(70)
            btn.clicked.connect(lambda _=False, ts=t.timestamp:
                                self.scrub_to(ts))
            self.jump_row.addWidget(btn)
        self.jump_row.addStretch(1)

    # =====================================================================
    def shutdown(self):
        """Settings window is closing — never leave test relays live."""
        if self.test_mode_active:
            self.exit_test_mode(silent=True)

    def hideEvent(self, event):
        self.shutdown()
        super().hideEvent(event)
