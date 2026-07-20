"""
game_engine.py — the show's brain.

Direct port of VideoHardwareController.cs:

  Idle -> (START) -> question wheel spins -> answer effects
       (SetBreakTime / SetLoopCount / PlayVideoFromPlaylist / sub-questions)
  -> GET READY countdown -> wheel fades out -> video plays, firing
     HardwareTriggers at their timestamps -> trim OUT (or clip end)
  -> relays forced OFF -> loops / break cooldown -> Idle.

Unity's coroutines become epoch-guarded QTimer.singleShot chains: StopAll()
bumps the epoch, which silently cancels every pending step — the same effect
as StopAllCoroutines().

The engine owns its QMediaPlayer; the show window only supplies the video
output item (QGraphicsVideoItem).
"""

from __future__ import annotations

import os
import time
from typing import List, Optional, Tuple

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer

from rng import entropy
from config import (AppConfig, GameAnswer, GameQuestion, VideoScenario,
                    format_time)

# PlayerState port
S_IDLE = "Idle"
S_QUESTION_WAIT = "Game_QuestionWait"
S_SPINNING = "Game_Spinning"
S_PROCESSING = "Game_Processing"
S_COUNTDOWN = "Countdown"
S_TRANSITION = "Transitioning"
S_PLAYING = "Playing"
S_BREAK = "BreakCooldown"

_LOADED = (QMediaPlayer.MediaStatus.LoadedMedia,
           QMediaPlayer.MediaStatus.BufferedMedia)


class GameEngine(QObject):
    state_changed = pyqtSignal(str)
    question_text = pyqtSignal(str)
    countdown_text = pyqtSignal(str)
    status_updated = pyqtSignal(dict)
    timecode_updated = pyqtSignal(float, float)   # elapsed, total (trim-relative)
    volume_changed = pyqtSignal(float, bool)      # volume 0-1, muted
    playing_changed = pyqtSignal(bool)            # True while actually playing
    wheel_rebuild = pyqtSignal(list)          # [(label, weight), ...]
    spin_requested = pyqtSignal()
    wheel_reset = pyqtSignal()
    fade_wheel = pyqtSignal(bool)             # FadeCanvas(show)
    start_button_text = pyqtSignal(str)
    relays_changed = pyqtSignal(dict)
    log = pyqtSignal(str)

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg

        # --- media -----------------------------------------------------------
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(max(0.0, min(1.0, cfg.ui.volume)))
        self.audio.setMuted(bool(cfg.ui.muted))
        self.player.setAudioOutput(self.audio)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(self._on_media_error)

        # --- state (VideoHardwareController private fields) -------------------
        self.state = S_IDLE
        self.current_video: Optional[VideoScenario] = None
        self.session_break_time = 0.0
        self.loops_remaining = 1
        self.total_loops = 1
        self.saved_loop_question: Optional[GameQuestion] = None
        self.current_question: Optional[GameQuestion] = None
        self.active_root_index = 0
        self.break_label = "..."
        self.relays: dict = {r.id: False for r in cfg.esp.relays}

        self._epoch = 0                       # bump == StopAllCoroutines()
        self._current_pieces: List[Tuple[str, float]] = []
        self._current_answers: List[GameAnswer] = []
        self._preparing = False
        self._spin_rescued = False
        self._prepare_attempt = 0
        self._tried_paths: set = set()
        self._virtual = False            # running the cue without a decoder
        self._virtual_started = 0.0
        self._virtual_length = 0.0
        self._virtual_paused_at = 0.0
        self._last_pos = -1.0
        self._stall_since = 0.0
        self._stall_fixes = 0
        self._break_left = 0.0

        self._play_timer = QTimer(self)       # Update() while Playing
        self._play_timer.setInterval(33)
        self._play_timer.timeout.connect(self._playing_tick)

        self._break_timer = QTimer(self)
        self._break_timer.setInterval(100)
        self._break_timer.timeout.connect(self._break_tick)

    # ---------------------------------------------------------------- helpers
    def attach_video_output(self, video_item):
        self.player.setVideoOutput(video_item)

    def sync_relay_list(self):
        """Keep the state map in step when relays are added or removed."""
        ids = [r.id for r in self.cfg.esp.relays]
        for rid in ids:
            self.relays.setdefault(rid, False)
        for stale in [k for k in self.relays if k not in ids]:
            self.relays.pop(stale, None)

    def relay_states(self) -> dict:
        """RelayBank.game_source — the 'True State' the hardware must match."""
        return dict(self.relays)

    def _later(self, delay_s: float, fn):
        ep = self._epoch
        QTimer.singleShot(max(0, int(delay_s * 1000)),
                          lambda: fn() if ep == self._epoch else None)

    def _set_state(self, new_state: str):
        self.state = new_state
        self.state_changed.emit(new_state)
        self._update_status()
        # UpdateCenterText port
        if new_state == S_IDLE:
            self.countdown_text.emit("READY")
            self.question_text.emit("PRESS START")
        elif new_state in (S_SPINNING, S_PROCESSING):
            self.countdown_text.emit("SPINNING...")

    # ---------------------------------------------------------------- status bar
    def _update_status(self):
        self.status_updated.emit({
            "video": self.current_video.wheel_label if self.current_video else "...",
            "relays": dict(self.relays),
            "break_label": self.break_label,
            "loops_done": self.total_loops - self.loops_remaining,
            "loops_total": self.total_loops,
            "state": self.state,
        })

    # ================================================================= START / STOP
    def handle_button_press(self):
        if self.state == S_IDLE:
            self.start_sequence()
        else:
            self.stop_all()

    def start_sequence(self):
        if self.state != S_IDLE:
            return
        if not self.cfg.root_questions:
            self.log.emit("No Root Questions defined!")
            self.question_text.emit("CONFIG ERROR")
            return

        self.start_button_text.emit(self.cfg.ui.stop_button_text)

        self.session_break_time = 0.0
        self.loops_remaining = 1
        self.total_loops = 1
        self.saved_loop_question = None
        self.break_label = "..."
        self.current_video = None

        self._reset_answer_counts(self.cfg.root_questions)
        self.active_root_index = 0
        self.load_question(self.cfg.root_questions[0])

    def _reset_answer_counts(self, questions: List[GameQuestion]):
        for q in questions:
            if q is None:
                continue
            for a in q.answers:
                a.times_picked = 0
                if a.sub_question is not None:
                    self._reset_answer_counts([a.sub_question])

    def stop_all(self):
        """StopAll port — cancels every pending sequence step."""
        self._epoch += 1
        self._preparing = False
        self._virtual = False
        self._virtual_paused_at = 0.0
        self._prepare_attempt = 0
        self._tried_paths = set()
        self._last_pos = -1.0
        self._stall_since = 0.0
        self._stall_fixes = 0
        self._play_timer.stop()
        self._break_timer.stop()
        self.player.stop()
        self.timecode_updated.emit(0.0, 0.0)
        self.playing_changed.emit(False)
        self.set_speed(1.0)                       # StopSystem also reset speed
        self.wheel_reset.emit()                   # WheelController.ResetSpinState
        self.current_video = None
        self._force_relays_off()
        self.fade_wheel.emit(True)
        self.start_button_text.emit(self.cfg.ui.start_button_text)
        self._set_state(S_IDLE)

    # ================================================================= QUESTIONS
    def load_question(self, q: Optional[GameQuestion]):
        if q is None:
            self.stop_all()
            return
        self.current_question = q
        self._set_state(S_QUESTION_WAIT)
        self.question_text.emit(q.text)

        available: List[GameAnswer] = []
        for ans in q.answers:
            ok = True
            if q.mode == "RemoveOptionAfterUse":
                if q.max_picks > 0 and ans.times_picked >= q.max_picks:
                    ok = False
            if ok:
                available.append(ans)
        if not available:                         # all exhausted -> reset
            for ans in q.answers:
                ans.times_picked = 0
                available.append(ans)

        # Slice text follows the playlist entry's CURRENT name, so renaming a
        # video updates the wheel instead of leaving a stale "Demo Video A".
        self._current_answers = list(available)
        self._current_pieces = [(a.resolve_label(self.cfg.playlist),
                                 a.chance_weight) for a in available]
        self.wheel_rebuild.emit(list(self._current_pieces))

        def _spin():                              # SpinRoutine port
            self._set_state(S_SPINNING)
            self._spin_rescued = False
            self.spin_requested.emit()
            self._arm_spin_watchdog()
        self._later(1.0, _spin)

    def on_wheel_result(self, winner_index: int):
        if self.state == S_SPINNING:
            self._process_game_answer(winner_index)

    def _process_game_answer(self, index: int):
        self._set_state(S_PROCESSING)
        q = self.current_question
        if q is None or not q.answers:
            self.stop_all()
            return

        if index < 0:
            # the watchdog fired: pick using the real weights so a rescued
            # spin is still a fair one
            weights = [max(0.0, w) for _lbl, w in self._current_pieces]
            index = entropy.weighted_index(weights) if any(weights) else 0
            label = (self._current_pieces[index][0]
                     if 0 <= index < len(self._current_pieces) else "?")
            self.log.emit(f"Rescued spin resolved to '{label}'.")

        # index straight into the answers this wheel was built from — labels
        # may repeat or be playlist-derived, so never match on text
        if 0 <= index < len(self._current_answers):
            choice = self._current_answers[index]
        else:
            choice = q.answers[0]
        choice.times_picked += 1
        self.log.emit(f"[{q.log_name}] -> {choice.label}")

        if choice.effect == "SetBreakTime":
            self.session_break_time = choice.float_value
            self.break_label = choice.label
        elif choice.effect == "SetLoopCount":
            self.loops_remaining = int(round(choice.float_value))
            self.total_loops = self.loops_remaining
        elif choice.effect == "PlayVideoFromPlaylist":
            self.saved_loop_question = q
            if 0 <= choice.playlist_index < len(self.cfg.playlist):
                self.current_video = self.cfg.playlist[choice.playlist_index]
        else:
            # quirk preserved: a literal "no" on root question #2 labels the break
            if (choice.label.strip().lower() == "no"
                    and self.active_root_index == 1):
                self.break_label = "No"

        self._update_status()
        self._later(1.5, lambda: self._after_answer(choice))

    def _after_answer(self, choice: GameAnswer):
        if choice.sub_question is not None:
            self.load_question(choice.sub_question)
            return

        self.active_root_index += 1
        if choice.skip_next_root:
            self.log.emit(f"[GameFlow] Skipping root question "
                          f"#{self.active_root_index} as requested by "
                          f"'{choice.label}'")
            self.active_root_index += 1

        if self.current_video is not None and self.current_video.path:
            self._countdown_sequence()
        elif self.current_video is not None:
            # a video was chosen but no file is attached to that entry
            self.question_text.emit("NO FILE ON THIS VIDEO")
            self.log.emit(
                f"'{self.current_video.wheel_label}' has no clip assigned — "
                f"set its file in the Editor page.")
            self._later(2.5, self.stop_all)
        elif self.active_root_index < len(self.cfg.root_questions):
            self.load_question(self.cfg.root_questions[self.active_root_index])
        else:
            # Dead end: never leave the machine parked in Processing, or the
            # START button needs two presses and the screen keeps stale text.
            self.question_text.emit("END OF FLOW \u2014 NO VIDEO CHOSEN")
            self.log.emit(
                "The flow ended without picking a video. Give one question an "
                "answer whose effect is 'Play Video From Playlist'.")
            self._later(2.5, self.stop_all)

    # ================================================================= COUNTDOWN + PLAYBACK
    def _countdown_sequence(self):
        """Begin the clip. Nothing in here is allowed to end the show: every
        failure escalates through the recovery ladder in `_prepare_failed`."""
        self._set_state(S_COUNTDOWN)
        self._prepare_attempt = 0
        self._tried_paths = set()
        v = self.current_video

        if v is None or not v.path or not os.path.isfile(v.path):
            missing = (v.path if v is not None else "")
            self.log.emit(f"'{v.wheel_label if v else '?'}' has no playable file"
                          + (f" ({missing})" if missing else "")
                          + " — looking for another clip.")
            replacement = self._fallback_video()
            if replacement is not None:
                self.current_video = replacement
                self.log.emit(f"Falling back to '{replacement.wheel_label}'.")
                v = replacement
            else:
                self.log.emit("No playable file anywhere in the playlist — "
                              "running the cue without picture so the hardware "
                              "still fires.")
                self._begin_virtual_playback()
                return

        self._begin_prepare(v)

    # ------------------------------------------------------------ preparation
    def _begin_prepare(self, v):
        self.question_text.emit("GET READY")
        self._preparing = True
        self._tried_paths.add(os.path.abspath(v.path))
        self.player.stop()
        if self._prepare_attempt > 0:
            # a re-set of the source shakes loose a backend that stalled
            self.player.setSource(QUrl())
        self.player.setSource(QUrl.fromLocalFile(os.path.abspath(v.path)))
        if self._prepare_attempt >= 2:
            # some backends only decode headers once playback is requested
            self.player.play()
            self.player.pause()

        ep = self._epoch
        patience = 8.0 + 4.0 * self._prepare_attempt

        def _timeout():
            if ep == self._epoch and self._preparing:
                self._prepare_failed(f"no response after {patience:.0f}s")
        self._later(patience, _timeout)

    def _prepare_failed(self, reason: str):
        """Escalate rather than stop. The show always goes on."""
        if not self._preparing:
            return
        self._preparing = False
        v = self.current_video
        name = v.wheel_label if v is not None else "?"
        self._prepare_attempt += 1

        if self._prepare_attempt <= 3 and v is not None and v.path:
            self.log.emit(f"'{name}' did not open ({reason}) — retry "
                          f"{self._prepare_attempt} of 3.")
            self.question_text.emit("LOADING\u2026")
            self._later(0.4, lambda: self._begin_prepare(v))
            return

        replacement = self._fallback_video()
        if replacement is not None:
            self.log.emit(f"Giving up on '{name}' — switching to "
                          f"'{replacement.wheel_label}' so the show continues.")
            self.current_video = replacement
            self._prepare_attempt = 0
            self._later(0.2, lambda: self._begin_prepare(replacement))
            return

        self.log.emit(f"No clip would open. Running '{name}' as a timed cue so "
                      f"the relays still fire on schedule.")
        self._begin_virtual_playback()

    def _fallback_video(self):
        """The next playlist entry with a real file that we have not tried."""
        for candidate in self.cfg.playlist:
            if not candidate.path:
                continue
            full = os.path.abspath(candidate.path)
            if full in self._tried_paths or not os.path.isfile(full):
                continue
            return candidate
        return None

    # ------------------------------------------------- picture-less fallback
    def _begin_virtual_playback(self):
        """Run the cue on a clock instead of a decoder.

        The screen shows a notice, but triggers fire at their timestamps, the
        countdown, loops and breaks all behave normally, and the show never
        halts because of a file or codec problem.
        """
        v = self.current_video
        if v is None:
            self._later(0.5, self.stop_all)
            return
        length = v.end_time if v.end_time > 0 else 0.0
        if length <= 0:
            length = max((t.timestamp for t in v.triggers), default=10.0) + 3.0
        self._virtual_length = max(1.0, length - v.start_time)
        self._virtual = True
        self._preparing = False
        self.question_text.emit("NO PICTURE \u2014 CUE RUNNING")
        trim_start = v.start_time
        trim_end = v.end_time if v.end_time > 0 else float("inf")
        for t in v.triggers:
            t.has_fired = not (trim_start <= t.timestamp < trim_end)
        self._run_countdown_then(self._start_virtual_playback)

    def _start_virtual_playback(self):
        self._virtual_started = time.monotonic()
        self._virtual_paused_at = 0.0
        self.playing_changed.emit(True)
        self._set_state(S_PLAYING)
        self._play_timer.start()
        self._update_status()

    def _on_media_status(self, status):
        if self._preparing and status in _LOADED:
            self._preparing = False
            self._on_prepared()
        elif (status == QMediaPlayer.MediaStatus.EndOfMedia
                and self.state == S_PLAYING):
            self.finish_video()
        elif (status == QMediaPlayer.MediaStatus.InvalidMedia
                and self._preparing):
            self._prepare_failed("the decoder rejected the file")

    def _check_stall(self, pos: float) -> bool:
        """Playback claims to be running but the clock is frozen.

        Nudge it back to life, and if it stays stuck hand the rest of the cue
        to the wall clock so the triggers still fire on time.
        """
        now = time.monotonic()
        moved = abs(pos - self._last_pos) > 0.02
        self._last_pos = pos
        if moved:
            self._stall_since = 0.0
            self._stall_fixes = 0
            return True

        if self._stall_since <= 0.0:
            self._stall_since = now
            return True

        stalled = now - self._stall_since
        if stalled < 2.5:
            return True

        self._stall_since = now
        self._stall_fixes += 1
        v = self.current_video
        if self._stall_fixes == 1:
            self.log.emit("Playback stalled — nudging the player.")
            self.player.pause()
            self.player.play()
            return False
        if self._stall_fixes == 2:
            self.log.emit("Still stalled — seeking forward slightly.")
            self.player.setPosition(int((pos + 0.25) * 1000))
            self.player.play()
            return False

        self.log.emit("Player will not recover — finishing this cue on the "
                      "clock so the show keeps running.")
        remaining = 0.0
        if v is not None:
            end = v.end_time if v.end_time > 0 else pos + 5.0
            remaining = max(1.0, end - pos)
        self._virtual = True
        self._virtual_length = (pos - (v.start_time if v else 0.0)) + remaining
        self._virtual_started = now - (pos - (v.start_time if v else 0.0))
        self.player.stop()
        return False

    def _on_media_error(self, _error, message: str):
        self.log.emit(f"Media error: {message}")
        if self._preparing:
            self._prepare_failed(message or "player error")

    def _on_prepared(self):
        """videoPlayer.isPrepared reached — seek trim IN, arm triggers, count."""
        v = self.current_video
        if v is None or self.state != S_COUNTDOWN:
            return
        self._virtual = False
        self._prepare_attempt = 0
        trim_start = v.start_time
        trim_end = v.end_time if v.end_time > 0 else float("inf")
        if trim_start > 0:
            self.player.setPosition(int(trim_start * 1000))
        for t in v.triggers:
            t.has_fired = not (trim_start <= t.timestamp < trim_end)
        self._run_countdown_then(self._start_video_playback)

    def _run_countdown_then(self, go_callback):
        seconds = max(0, int(self.cfg.ui.countdown_seconds))

        def step(i: int):
            if i > 0:
                self.countdown_text.emit(str(i))
                self._later(1.0, lambda: step(i - 1))
            else:
                self.countdown_text.emit("GO!")
                self._later(0.5, _go)

        def _go():
            self.countdown_text.emit("")
            self._set_state(S_TRANSITION)
            self.fade_wheel.emit(False)
            self._later(self.cfg.ui.fade_duration, go_callback)

        if seconds > 0:
            step(seconds)
        else:
            _go()

    def _start_video_playback(self):
        self.player.play()
        self.playing_changed.emit(True)
        self._set_state(S_PLAYING)
        self._play_timer.start()
        self._update_status()

    def _playing_tick(self):
        """Update() port — trigger firing + trim enforcement."""
        if self.state != S_PLAYING:
            self._play_timer.stop()
            return
        v = self.current_video
        if v is None:
            return

        if self._virtual:
            if self._virtual_paused_at > 0.0:
                return                             # paused -> clock frozen
            # no decoder involved: the cue runs on a wall clock
            elapsed = time.monotonic() - self._virtual_started
            pos = v.start_time + elapsed
            dur = v.start_time + self._virtual_length
        else:
            if self.player.playbackState() != \
                    QMediaPlayer.PlaybackState.PlayingState:
                return                             # paused -> no triggers
            pos = self.player.position() / 1000.0
            dur = self.player.duration() / 1000.0
            if not self._check_stall(pos):
                return

        for trig in v.triggers:                    # CheckTriggers
            if not trig.has_fired and pos >= trig.timestamp:
                trig.has_fired = True
                self.execute_trigger(trig)

        trim_start = v.start_time
        trim_end = v.end_time if v.end_time > 0 else (dur if dur > 0 else 0.0)
        self.timecode_updated.emit(max(0.0, pos - trim_start),
                                   max(0.0, trim_end - trim_start))

        reached_trim = v.end_time > 0 and pos >= v.end_time
        reached_end = dur > 0 and pos >= dur - 0.12
        if reached_trim or reached_end:
            self.finish_video()

    def execute_trigger(self, trigger):
        """ExecuteTrigger port — updates the 'True State' the RelayBank mirrors."""
        if trigger.relay_id not in self.relays:
            self.log.emit(f"[Trigger] unknown relay '{trigger.relay_id}' — "
                          f"it may have been deleted.")
            return
        self.relays[trigger.relay_id] = bool(trigger.state)
        self.log.emit(f"[Trigger] {trigger.label(self.cfg.esp.relays)}")
        self.relays_changed.emit(dict(self.relays))
        self._update_status()

    def finish_video(self):
        if self.state != S_PLAYING:                # double-decrement guard
            return
        self.timecode_updated.emit(0.0, 0.0)
        self.playing_changed.emit(False)
        self._play_timer.stop()
        self.player.stop()
        self._force_relays_off()

        self.loops_remaining -= 1
        if self.loops_remaining > 0:
            # A break belongs BETWEEN videos. Running one after the last
            # repeat left the audience staring at a countdown for a round
            # that never comes.
            self._break_sequence()
        else:
            self.log.emit("Last repeat finished — no closing break.")
            self.stop_all()

    # ================================================================= BREAK / LOOP
    def _break_sequence(self):
        self._set_state(S_BREAK)
        self.fade_wheel.emit(True)
        if self.session_break_time > 0:
            self.question_text.emit("BREAK TIME")
            self._break_left = self.session_break_time
            self.countdown_text.emit(f"{self._break_left:.0f}")
            self._break_timer.start()
        else:
            self._after_break()

    def _break_tick(self):
        self._break_left -= self._break_timer.interval() / 1000.0
        if self._break_left > 0:
            self.countdown_text.emit(f"{self._break_left:.0f}")
        else:
            self._break_timer.stop()
            self._after_break()

    def _after_break(self):
        if self.loops_remaining > 0:
            self.active_root_index = 0             # loop restarts the flow
            if self.saved_loop_question is not None:
                self.load_question(self.saved_loop_question)
            else:
                self.load_question(self.cfg.root_questions[0])
        else:
            self.stop_all()

    # ================================================================= misc controls
    def _arm_spin_watchdog(self):
        """The show must never sit forever on SPINNING.

        If the wheel has not reported a winner well after the longest possible
        spin, pick one here and carry on.
        """
        ws = self.cfg.wheel
        longest = max(ws.max_spin_time, getattr(ws, "draw_max_time", 9.0))
        ep = self._epoch

        def _rescue():
            if ep != self._epoch or self.state != S_SPINNING:
                return
            self.log.emit("The wheel never reported a result — choosing one "
                          "so the show continues.")
            self._spin_rescued = True
            self.on_wheel_result(-1)

        self._later(longest + 8.0, _rescue)

    def _force_relays_off(self):
        for rid in self.relays:
            self.relays[rid] = False
        self.relays_changed.emit(dict(self.relays))
        self._update_status()

    # ------------------------------------------------- audio
    def set_volume(self, volume: float):
        volume = max(0.0, min(1.0, float(volume)))
        self.cfg.ui.volume = volume
        self.audio.setVolume(volume)
        self.volume_changed.emit(volume, self.audio.isMuted())

    def set_muted(self, muted: bool):
        self.cfg.ui.muted = bool(muted)
        self.audio.setMuted(bool(muted))
        self.volume_changed.emit(self.audio.volume(), bool(muted))
        self.log.emit("Audio muted." if muted else "Audio unmuted.")

    def toggle_mute(self):
        self.set_muted(not self.audio.isMuted())

    @property
    def is_muted(self) -> bool:
        return self.audio.isMuted()

    @property
    def volume(self) -> float:
        return self.audio.volume()

    # ------------------------------------------------- transport
    def is_playing(self) -> bool:
        return (self.state == S_PLAYING and self.player.playbackState()
                == QMediaPlayer.PlaybackState.PlayingState)

    def restart_video(self):
        """Jump back to the trim IN point and re-arm every trigger — a clean
        restart of the current clip without touching loops or the game flow."""
        if self.state != S_PLAYING or self.current_video is None:
            self.log.emit("Restart ignored: no video is playing.")
            return
        v = self.current_video
        trim_start = v.start_time
        trim_end = v.end_time if v.end_time > 0 else float("inf")
        for t in v.triggers:
            t.has_fired = not (trim_start <= t.timestamp < trim_end)
        self._force_relays_off()
        self.player.setPosition(int(trim_start * 1000))
        self.player.play()
        self.playing_changed.emit(True)
        self.log.emit(f"Restarted '{v.wheel_label}' from {trim_start:.2f}s.")

    def seek_to(self, seconds: float):
        """Scrub during the show; relay state is recomputed for that moment."""
        if self.state != S_PLAYING or self.current_video is None:
            return
        v = self.current_video
        trim_start = v.start_time
        trim_end = v.end_time if v.end_time > 0 else (
            self.player.duration() / 1000.0 or float("inf"))
        target = max(trim_start, min(float(seconds), max(trim_start, trim_end - 0.05)))
        self.player.setPosition(int(target * 1000))

        rebuilt = {rid: False for rid in self.relays}
        for t in v.triggers:
            fired = t.timestamp <= target
            t.has_fired = fired or not (trim_start <= t.timestamp < trim_end)
            if fired and t.relay_id in rebuilt:
                rebuilt[t.relay_id] = bool(t.state)
        if rebuilt != self.relays:
            self.relays = rebuilt
            self.relays_changed.emit(dict(self.relays))
            self._update_status()

    def seek_by(self, delta_seconds: float):
        if self.state == S_PLAYING:
            self.seek_to(self.player.position() / 1000.0 + delta_seconds)

    def toggle_pause(self):
        """AdvancedVideoControls.TogglePlayPause."""
        if self.state != S_PLAYING:
            return
        if self._virtual:
            # picture-less cue: freeze or resume the wall clock
            if self._virtual_paused_at > 0.0:
                self._virtual_started += time.monotonic() - self._virtual_paused_at
                self._virtual_paused_at = 0.0
                self.countdown_text.emit("")
                self.playing_changed.emit(True)
            else:
                self._virtual_paused_at = time.monotonic()
                self.countdown_text.emit("PAUSED")
                self.playing_changed.emit(False)
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.countdown_text.emit("PAUSED")
            self.playing_changed.emit(False)
        else:
            self.player.play()
            self.countdown_text.emit("")
            self.playing_changed.emit(True)

    def skip_to_end(self):
        """AdvancedVideoControls.SkipToEnd — respects loops/breaks."""
        if self.state != S_PLAYING:
            self.log.emit("Cannot skip: not in Playing state.")
            return
        self.finish_video()

    def set_speed(self, speed: float):
        self.player.setPlaybackRate(max(0.1, min(8.0, float(speed))))
