"""
config.py — data model + JSON persistence.

Python port of the Unity serialized data structures:
  HardwareAction / HardwareTrigger / VideoScenario   (VideoHardwareController.cs)
  GameQuestion / GameAnswer / AnswerEffect / QuestionMode
  WheelController inspector fields                    (WheelController.cs)
  DualRelayController + EspHeartbeat + TrafficManager settings
Everything is stored in one human-readable settings.json next to the app.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional

# ---------------------------------------------------------------- enums (as strings)

# Legacy trigger names from the Unity build, mapped onto the dynamic model.
LEGACY_ACTIONS = {
    "MotorOn": ("r1", True), "MotorOff": ("r1", False),
    "VoltageRelayOn": ("r2", True), "VoltageRelayOff": ("r2", False),
}

RELAY_COLORS = ["#43d17c", "#35c5d0", "#ffb04a", "#c58cf5", "#ff7a9c",
                "#7ee081", "#6db8ff", "#f5d76e"]


def relay_color(index: int) -> str:
    return RELAY_COLORS[index % len(RELAY_COLORS)]



ANSWER_EFFECTS = ["None", "SetBreakTime", "SetLoopCount", "PlayVideoFromPlaylist"]
QUESTION_MODES = ["InfiniteRandom", "RemoveOptionAfterUse"]

# Priorities — mirror of EspPriority in TrafficManager.cs
PRIORITY_LOW = 0
PRIORITY_HIGH = 50
PRIORITY_CRITICAL = 100


# ---------------------------------------------------------------- data classes

@dataclass
class RelayConfig:
    """One switchable output. Add, rename or delete as many as you like."""
    id: str = "r1"
    name: str = "Relay 1"
    pin: int = 4
    on_label: str = "ON"
    off_label: str = "OFF"
    test_duration: float = 3.0        # every relay owns its own test timer

    def to_dict(self):
        return {"id": self.id, "name": self.name, "pin": int(self.pin),
                "on_label": self.on_label, "off_label": self.off_label,
                "test_duration": float(self.test_duration)}

    @staticmethod
    def from_dict(d) -> "RelayConfig":
        r = RelayConfig()
        r.id = str(d.get("id", "r1"))
        r.name = str(d.get("name", r.id))
        r.pin = int(d.get("pin", 4))
        r.on_label = str(d.get("on_label", "ON"))
        r.off_label = str(d.get("off_label", "OFF"))
        r.test_duration = float(d.get("test_duration", 3.0))
        return r


@dataclass
class HardwareTrigger:
    timestamp: float = 0.0
    relay_id: str = "r1"
    state: bool = True
    # runtime only (Unity: [HideInInspector] hasFired) — never serialized
    has_fired: bool = field(default=False, repr=False, compare=False)

    def to_dict(self):
        return {"timestamp": round(float(self.timestamp), 3),
                "relay": self.relay_id, "state": bool(self.state)}

    def label(self, relays) -> str:
        name = next((r.name for r in relays if r.id == self.relay_id),
                    self.relay_id)
        return f"{name} {'ON' if self.state else 'OFF'}"

    def short(self, relays) -> str:
        idx = next((i for i, r in enumerate(relays) if r.id == self.relay_id), 0)
        return f"{idx + 1}{'+' if self.state else '-'}"

    @staticmethod
    def from_dict(d) -> "HardwareTrigger":
        t = HardwareTrigger()
        t.timestamp = float(d.get("timestamp", 0.0))
        if "relay" in d:
            t.relay_id = str(d.get("relay", "r1"))
            t.state = bool(d.get("state", True))
        else:                                   # Unity-era "action" string
            legacy = str(d.get("action", "MotorOn"))
            t.relay_id, t.state = LEGACY_ACTIONS.get(legacy, ("r1", True))
        return t


@dataclass
class VideoScenario:
    wheel_label: str = "Video Title"
    path: str = ""                      # Unity: VideoClip clip  ->  file path
    skip: bool = False                  # exclude from auto-generated wheels
    force_choice: bool = False          # if set, auto-generated wheels contain only forced entries
    use_custom_chance: bool = False
    chance_weight: float = 50.0
    start_time: float = 0.0             # trim IN  (0 = clip start)
    end_time: float = 0.0               # trim OUT (0 = clip end)
    triggers: List[HardwareTrigger] = field(default_factory=list)

    def sorted_triggers(self) -> List[HardwareTrigger]:
        return sorted(self.triggers, key=lambda t: t.timestamp)

    def effective_weight(self) -> float:
        return self.chance_weight if self.use_custom_chance else 100.0

    def to_dict(self):
        return {
            "wheel_label": self.wheel_label,
            "path": self.path,
            "skip": self.skip,
            "force_choice": self.force_choice,
            "use_custom_chance": self.use_custom_chance,
            "chance_weight": float(self.chance_weight),
            "start_time": round(float(self.start_time), 3),
            "end_time": round(float(self.end_time), 3),
            "triggers": [t.to_dict() for t in self.triggers],
        }

    @staticmethod
    def from_dict(d) -> "VideoScenario":
        s = VideoScenario()
        s.wheel_label = str(d.get("wheel_label", "Video"))
        s.path = str(d.get("path", ""))
        s.skip = bool(d.get("skip", False))
        s.force_choice = bool(d.get("force_choice", False))
        s.use_custom_chance = bool(d.get("use_custom_chance", False))
        s.chance_weight = float(d.get("chance_weight", 50.0))
        s.start_time = float(d.get("start_time", 0.0))
        s.end_time = float(d.get("end_time", 0.0))
        s.triggers = [HardwareTrigger.from_dict(t) for t in d.get("triggers", [])]
        return s


@dataclass
class GameAnswer:
    label: str = "Option"
    chance_weight: float = 50.0
    effect: str = "None"
    float_value: float = 0.0
    playlist_index: int = 0
    skip_next_root: bool = False
    # When the effect plays a playlist entry, show that entry's CURRENT name on
    # the wheel instead of a stale copy typed in months ago.
    sync_label_with_video: bool = True
    sub_question: Optional["GameQuestion"] = None
    # runtime only (Unity: [HideInInspector] timesPicked)
    times_picked: int = field(default=0, repr=False, compare=False)

    def to_dict(self):
        d = {
            "label": self.label,
            "chance_weight": float(self.chance_weight),
            "effect": self.effect,
            "float_value": float(self.float_value),
            "playlist_index": int(self.playlist_index),
            "skip_next_root": self.skip_next_root,
            "sync_label_with_video": self.sync_label_with_video,
        }
        if self.sub_question is not None:
            d["sub_question"] = self.sub_question.to_dict()
        return d

    @staticmethod
    def from_dict(d) -> "GameAnswer":
        a = GameAnswer()
        a.label = str(d.get("label", "Option"))
        a.chance_weight = float(d.get("chance_weight", 50.0))
        eff = d.get("effect", "None")
        a.effect = eff if eff in ANSWER_EFFECTS else "None"
        a.float_value = float(d.get("float_value", 0.0))
        a.playlist_index = int(d.get("playlist_index", 0))
        a.skip_next_root = bool(d.get("skip_next_root", False))
        a.sync_label_with_video = bool(d.get("sync_label_with_video", True))
        sq = d.get("sub_question")
        a.sub_question = GameQuestion.from_dict(sq) if sq else None
        return a

    def resolve_label(self, playlist) -> str:
        """Text actually painted on the wheel slice.

        Fixes the 'wheel still says Demo Video A' problem: an answer that plays
        a playlist entry follows that entry's current name unless the user
        deliberately unticks sync.
        """
        if (self.effect == "PlayVideoFromPlaylist" and self.sync_label_with_video
                and 0 <= self.playlist_index < len(playlist)):
            name = playlist[self.playlist_index].wheel_label.strip()
            if name:
                return name
        return self.label


@dataclass
class GameQuestion:
    text: str = "New Question"
    log_name: str = "Q"
    mode: str = "InfiniteRandom"
    max_picks: int = 1
    answers: List[GameAnswer] = field(default_factory=list)

    def to_dict(self):
        return {
            "text": self.text,
            "log_name": self.log_name,
            "mode": self.mode,
            "max_picks": int(self.max_picks),
            "answers": [a.to_dict() for a in self.answers],
        }

    @staticmethod
    def from_dict(d) -> "GameQuestion":
        q = GameQuestion()
        q.text = str(d.get("text", "New Question"))
        q.log_name = str(d.get("log_name", "Q"))
        m = d.get("mode", "InfiniteRandom")
        q.mode = m if m in QUESTION_MODES else "InfiniteRandom"
        q.max_picks = int(d.get("max_picks", 1))
        q.answers = [GameAnswer.from_dict(a) for a in d.get("answers", [])]
        return q


@dataclass
class WheelSettings:
    # Same names as the Unity inspector (WheelController.cs).
    # Speeds in deg/s. acceleration_power / kick use the Unity numbers and are
    # divided by UNITY_TORQUE_DIVISOR when integrated (see wheel_widget.py).
    target_max_speed: float = 1600.0
    speed_variance: float = 500.0
    acceleration_power: float = 200000.0
    bearing_friction: float = 1.4
    min_spin_time: float = 1.5
    max_spin_time: float = 4.0
    text_padding: float = 0.62          # label radius as a fraction of wheel radius
    # Outcome model:
    #   "physics" -> the slice under the pointer when the wheel stops wins
    #                (organic, tiny geometric bias, exactly like Unity)
    #   "draw"    -> the winner is drawn first from the OS cryptographic RNG
    #                using the exact weights, then the wheel is animated so it
    #                lands on it. Mathematically exact odds, unpredictable.
    outcome_mode: str = "physics"       # physics | draw
    entropy: str = "system"             # system (os.urandom CSPRNG) | fast
    draw_min_time: float = 4.0          # "draw" mode spin duration window
    draw_max_time: float = 9.0
    draw_min_turns: int = 3
    draw_max_turns: int = 7
    colors: List[str] = field(default_factory=lambda: [
        "#e74c3c", "#f39c12", "#f1c40f", "#2ecc71",
        "#1abc9c", "#3498db", "#9b59b6", "#e84393",
    ])

    def to_dict(self):
        return {
            "target_max_speed": self.target_max_speed,
            "speed_variance": self.speed_variance,
            "acceleration_power": self.acceleration_power,
            "bearing_friction": self.bearing_friction,
            "min_spin_time": self.min_spin_time,
            "max_spin_time": self.max_spin_time,
            "text_padding": self.text_padding,
            "outcome_mode": self.outcome_mode, "entropy": self.entropy,
            "draw_min_time": self.draw_min_time, "draw_max_time": self.draw_max_time,
            "draw_min_turns": self.draw_min_turns,
            "draw_max_turns": self.draw_max_turns,
            "colors": list(self.colors),
        }

    @staticmethod
    def from_dict(d) -> "WheelSettings":
        w = WheelSettings()
        for k in ("target_max_speed", "speed_variance", "acceleration_power",
                  "bearing_friction", "min_spin_time", "max_spin_time", "text_padding"):
            if k in d:
                setattr(w, k, float(d[k]))
        w.outcome_mode = str(d.get("outcome_mode", "physics"))
        if w.outcome_mode not in ("physics", "draw"):
            w.outcome_mode = "physics"
        w.entropy = str(d.get("entropy", "system"))
        w.draw_min_time = float(d.get("draw_min_time", 4.0))
        w.draw_max_time = float(d.get("draw_max_time", 9.0))
        w.draw_min_turns = int(d.get("draw_min_turns", 3))
        w.draw_max_turns = int(d.get("draw_max_turns", 7))
        if isinstance(d.get("colors"), list) and d["colors"]:
            w.colors = [str(c) for c in d["colors"]]
        return w


@dataclass
class EspSettings:
    ip: str = "192.168.1.50"
    port: int = 4222                    # ESP32S3_Source_Code_Wifi.ino: udpPort
    heartbeat_rate: float = 0.5         # EspHeartbeat.SendRate (ESP safety timeout = 1.5 s)
    send_delay: float = 0.05            # TrafficManager.SendDelay
    auto_connect: bool = True
    identity_probe: bool = True         # send Uduino "identity" pings, listen for replies
    relays: List["RelayConfig"] = field(default_factory=lambda: [
        RelayConfig("r1", "Relay 1", 4, "ON", "OFF", 3.0),
        RelayConfig("r2", "Relay 2", 5, "ON", "OFF", 3.0),
        RelayConfig("r3", "Voltage", 6, "ON", "OFF", 3.0),
    ])
    # programmable bench test: a timeline of relay switches you can replay
    test_sequence: List["HardwareTrigger"] = field(default_factory=list)
    test_sequence_length: float = 60.0
    arg_delimiter: str = " "            # Uduino token separator ("SetRelay 26 1")
    # Uduino's parser dispatches a command only when the line ends. Arduino's
    # println() emits CRLF and Custom_Esp32_S3.cpp flushes its TX buffer on
    # "\r\n", so CRLF is the native line ending for this firmware.
    terminator: str = "\r\n"            # "" | "\n" | "\r\n"
    # CRITICAL: Custom_Esp32_S3.cpp replies with
    #     UDP_Receiver.beginPacket(remote, port)
    # i.e. to <PC ip>:4222 -- NOT to our source port. We therefore have to bind
    # our own socket to that same port or the identity reply is never heard.
    local_port: int = 4222
    bind_local_port: bool = True
    # The board reads ONE udp packet per loop() and its OLED refresh blocks
    # that loop for ~90 ms every 250 ms, so probe replies are dropped routinely
    # even on a perfectly healthy link. Treat silence as a fault only after a
    # long, deliberate window, and never on a single missed probe.
    reply_timeout: float = 4.0          # legacy key, kept for old config files
    probe_interval: float = 2.5         # seconds between identity probes
    link_tolerance: float = 15.0        # silence before dropping to SEARCHING
    sticky_link: bool = True            # ignore brief gaps once connected
    auto_detect_protocol: bool = True   # probe CRLF/LF/none on connect

    def to_dict(self):
        return {
            "ip": self.ip, "port": self.port,
            "heartbeat_rate": self.heartbeat_rate, "send_delay": self.send_delay,
            "auto_connect": self.auto_connect, "identity_probe": self.identity_probe,
            "relays": [r.to_dict() for r in self.relays],
            "test_sequence": [t.to_dict() for t in self.test_sequence],
            "test_sequence_length": float(self.test_sequence_length),
            "arg_delimiter": self.arg_delimiter, "terminator": self.terminator,
            "local_port": self.local_port, "bind_local_port": self.bind_local_port,
            "reply_timeout": self.reply_timeout,
            "probe_interval": self.probe_interval,
            "link_tolerance": self.link_tolerance,
            "sticky_link": self.sticky_link,
            "auto_detect_protocol": self.auto_detect_protocol,
        }

    @staticmethod
    def from_dict(d) -> "EspSettings":
        e = EspSettings()
        e.ip = str(d.get("ip", e.ip))
        e.port = int(d.get("port", e.port))
        e.heartbeat_rate = float(d.get("heartbeat_rate", e.heartbeat_rate))
        e.send_delay = float(d.get("send_delay", e.send_delay))
        e.auto_connect = bool(d.get("auto_connect", e.auto_connect))
        e.identity_probe = bool(d.get("identity_probe", e.identity_probe))
        if isinstance(d.get("relays"), list) and d["relays"]:
            e.relays = [RelayConfig.from_dict(x) for x in d["relays"]]
        elif "pin_relay1" in d or "relay1_name" in d:
            # upgrade a two-relay config from the Unity era, keeping the pins
            # that were already wired up
            e.relays = [
                RelayConfig("r1", str(d.get("relay1_name", "Relay 1")),
                            int(d.get("pin_relay1", 4)),
                            str(d.get("relay1_on_label", "ON")),
                            str(d.get("relay1_off_label", "OFF")), 3.0),
                RelayConfig("r2", str(d.get("relay2_name", "Relay 2")),
                            int(d.get("pin_relay2", 5)),
                            str(d.get("relay2_on_label", "ON")),
                            str(d.get("relay2_off_label", "OFF")), 3.0),
            ]
        e.test_sequence = [HardwareTrigger.from_dict(x)
                           for x in d.get("test_sequence", [])]
        e.test_sequence_length = float(d.get("test_sequence_length", 60.0))
        e.arg_delimiter = str(d.get("arg_delimiter", e.arg_delimiter)) or " "
        e.terminator = str(d.get("terminator", e.terminator))
        # Files written by the first build carry terminator "" (no line
        # ending), which the firmware's parser never acts on. Those files have
        # no "local_port" key, so that absence identifies them precisely.
        if "local_port" not in d and str(d.get("terminator", "")) == "":
            e.terminator = "\r\n"
        e.local_port = int(d.get("local_port", d.get("port", 4222)))
        e.bind_local_port = bool(d.get("bind_local_port", True))
        e.reply_timeout = float(d.get("reply_timeout", 4.0))
        e.probe_interval = float(d.get("probe_interval", 2.5))
        # old files carry the 4 s window that caused the flapping
        e.link_tolerance = float(d.get("link_tolerance", 15.0))
        e.sticky_link = bool(d.get("sticky_link", True))
        e.auto_detect_protocol = bool(d.get("auto_detect_protocol", True))
        return e


@dataclass
class UiSettings:
    fade_duration: float = 0.8          # VideoHardwareController.fadeDuration
    countdown_seconds: int = 3
    break_header: str = "Break"         # breakStatusHeader
    loop_header: str = "Repeats"        # loopStatusHeader
    fullscreen_on_start: bool = True
    show_status_bar: bool = True
    test_live_hardware: bool = True     # Test Mode drives the real relays (like Unity)
    start_button_text: str = "START SYSTEM"
    stop_button_text: str = "RESET SYSTEM"
    # ---- playback ----
    volume: float = 1.0                 # 0.0 - 1.0
    muted: bool = False
    show_aspect: str = "fit"            # fit | fill | stretch
    preview_aspect: str = "fit"         # editor preview scaling
    # ---- on-screen timecode ("bookmark" strip) ----
    timecode_enabled: bool = True
    timecode_corner: str = "top_right"  # top_right|top_left|bottom_right|bottom_left
    timecode_opacity: float = 0.55      # peak alpha of the gradient
    timecode_scale: float = 1.0
    timecode_show_total: bool = True
    timecode_show_remaining: bool = False
    # ---- transport / kiosk ----
    transport_bar: bool = True          # auto-hiding on-screen control bar
    transport_autohide: float = 3.0
    settings_keeps_fullscreen: bool = False

    def to_dict(self):
        return {
            "fade_duration": self.fade_duration,
            "countdown_seconds": self.countdown_seconds,
            "break_header": self.break_header,
            "loop_header": self.loop_header,
            "fullscreen_on_start": self.fullscreen_on_start,
            "show_status_bar": self.show_status_bar,
            "test_live_hardware": self.test_live_hardware,
            "start_button_text": self.start_button_text,
            "stop_button_text": self.stop_button_text,
            "volume": self.volume, "muted": self.muted,
            "show_aspect": self.show_aspect, "preview_aspect": self.preview_aspect,
            "timecode_enabled": self.timecode_enabled,
            "timecode_corner": self.timecode_corner,
            "timecode_opacity": self.timecode_opacity,
            "timecode_scale": self.timecode_scale,
            "timecode_show_total": self.timecode_show_total,
            "timecode_show_remaining": self.timecode_show_remaining,
            "transport_bar": self.transport_bar,
            "transport_autohide": self.transport_autohide,
            "settings_keeps_fullscreen": self.settings_keeps_fullscreen,
        }

    @staticmethod
    def from_dict(d) -> "UiSettings":
        u = UiSettings()
        u.fade_duration = float(d.get("fade_duration", u.fade_duration))
        u.countdown_seconds = int(d.get("countdown_seconds", u.countdown_seconds))
        u.break_header = str(d.get("break_header", u.break_header))
        u.loop_header = str(d.get("loop_header", u.loop_header))
        u.fullscreen_on_start = bool(d.get("fullscreen_on_start", u.fullscreen_on_start))
        u.show_status_bar = bool(d.get("show_status_bar", u.show_status_bar))
        u.test_live_hardware = bool(d.get("test_live_hardware", u.test_live_hardware))
        u.start_button_text = str(d.get("start_button_text", u.start_button_text))
        u.stop_button_text = str(d.get("stop_button_text", u.stop_button_text))
        u.volume = max(0.0, min(1.0, float(d.get("volume", u.volume))))
        u.muted = bool(d.get("muted", u.muted))
        u.show_aspect = str(d.get("show_aspect", u.show_aspect))
        u.preview_aspect = str(d.get("preview_aspect", u.preview_aspect))
        u.timecode_enabled = bool(d.get("timecode_enabled", u.timecode_enabled))
        u.timecode_corner = str(d.get("timecode_corner", u.timecode_corner))
        u.timecode_opacity = float(d.get("timecode_opacity", u.timecode_opacity))
        u.timecode_scale = float(d.get("timecode_scale", u.timecode_scale))
        u.timecode_show_total = bool(d.get("timecode_show_total", u.timecode_show_total))
        u.timecode_show_remaining = bool(d.get("timecode_show_remaining",
                                               u.timecode_show_remaining))
        u.transport_bar = bool(d.get("transport_bar", u.transport_bar))
        u.transport_autohide = float(d.get("transport_autohide", u.transport_autohide))
        u.settings_keeps_fullscreen = bool(d.get("settings_keeps_fullscreen",
                                                 u.settings_keeps_fullscreen))
        return u


# ---------------------------------------------------------------- root config

class AppConfig:
    VERSION = 1

    def __init__(self):
        self.playlist: List[VideoScenario] = []
        self.root_questions: List[GameQuestion] = []
        self.wheel = WheelSettings()
        self.esp = EspSettings()
        self.ui = UiSettings()

    # ---- (de)serialization ------------------------------------------------
    def to_dict(self):
        return {
            "version": self.VERSION,
            "playlist": [s.to_dict() for s in self.playlist],
            "root_questions": [q.to_dict() for q in self.root_questions],
            "wheel": self.wheel.to_dict(),
            "esp": self.esp.to_dict(),
            "ui": self.ui.to_dict(),
        }

    @staticmethod
    def from_dict(d) -> "AppConfig":
        c = AppConfig()
        c.playlist = [VideoScenario.from_dict(x) for x in d.get("playlist", [])]
        c.root_questions = [GameQuestion.from_dict(x) for x in d.get("root_questions", [])]
        c.wheel = WheelSettings.from_dict(d.get("wheel", {}))
        c.esp = EspSettings.from_dict(d.get("esp", {}))
        c.ui = UiSettings.from_dict(d.get("ui", {}))
        return c

    # ---- disk -------------------------------------------------------------
    @staticmethod
    def default_path() -> str:
        base = os.path.dirname(os.path.abspath(sys.argv[0] or __file__))
        p = os.path.join(base, "settings.json")
        try:
            with open(p, "a", encoding="utf-8"):
                pass
            return p
        except OSError:
            home = os.path.join(os.path.expanduser("~"), ".esp32_show_controller")
            os.makedirs(home, exist_ok=True)
            return os.path.join(home, "settings.json")

    def save(self, path: Optional[str] = None) -> str:
        path = path or AppConfig.default_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        return path

    @staticmethod
    def load(path: Optional[str] = None) -> "AppConfig":
        path = path or AppConfig.default_path()
        try:
            if os.path.exists(path) and os.path.getsize(path) > 2:
                with open(path, "r", encoding="utf-8") as f:
                    return AppConfig.from_dict(json.load(f))
        except Exception as ex:  # corrupted file -> keep it, run defaults
            print(f"[Config] Failed to load '{path}': {ex} — using defaults.")
        cfg = AppConfig.make_default()
        try:
            cfg.save(path)
        except Exception:
            pass
        return cfg

    # ---- factory: a sensible starting configuration -----------------------
    @staticmethod
    def make_default() -> "AppConfig":
        c = AppConfig()

        v1 = VideoScenario(wheel_label="Horror Clip 1", path="")
        v1.triggers = [
            HardwareTrigger(2.0, "r1", True),
            HardwareTrigger(8.0, "r1", False),
            HardwareTrigger(10.0, "r3", True),
            HardwareTrigger(14.0, "r3", False),
        ]
        v2 = VideoScenario(wheel_label="Horror Clip 2", path="")
        v2.triggers = [
            HardwareTrigger(1.0, "r3", True),
            HardwareTrigger(5.0, "r2", True),
            HardwareTrigger(9.0, "r2", False),
            HardwareTrigger(9.5, "r3", False),
        ]
        c.playlist = [v1, v2]

        c.esp.test_sequence = [
            HardwareTrigger(2.0, "r1", True),
            HardwareTrigger(5.0, "r1", False),
            HardwareTrigger(6.0, "r2", True),
            HardwareTrigger(9.0, "r2", False),
            HardwareTrigger(10.0, "r3", True),
            HardwareTrigger(13.0, "r3", False),
        ]
        c.esp.test_sequence_length = 20.0

        q_rounds = GameQuestion(text="HOW MANY ROUNDS?", log_name="Rounds",
                                mode="InfiniteRandom", max_picks=1)
        q_rounds.answers = [
            GameAnswer(label="1 Round", chance_weight=50, effect="SetLoopCount", float_value=1),
            GameAnswer(label="2 Rounds", chance_weight=35, effect="SetLoopCount", float_value=2),
            GameAnswer(label="3 Rounds", chance_weight=15, effect="SetLoopCount", float_value=3),
        ]

        q_break = GameQuestion(text="BREAK BETWEEN ROUNDS?", log_name="Break",
                               mode="InfiniteRandom", max_picks=1)
        q_break.answers = [
            GameAnswer(label="No", chance_weight=40, effect="None"),
            GameAnswer(label="15 sec", chance_weight=35, effect="SetBreakTime", float_value=15),
            GameAnswer(label="30 sec", chance_weight=25, effect="SetBreakTime", float_value=30),
        ]

        c.root_questions = [q_rounds, q_break, build_video_question(c.playlist)]
        return c


def build_video_question(playlist: List[VideoScenario]) -> GameQuestion:
    """
    Auto-generate the 'WHICH VIDEO?' question from the playlist, honoring the
    per-video flags carried over from Unity:
      skip             -> excluded from the wheel
      force_choice     -> if any video is forced, ONLY forced videos appear
      use_custom_chance/chance_weight -> slice weight (default 100)
    """
    q = GameQuestion(text="WHICH VIDEO?", log_name="Video",
                     mode="InfiniteRandom", max_picks=1)
    pool = [(i, s) for i, s in enumerate(playlist) if not s.skip]
    forced = [(i, s) for i, s in pool if s.force_choice]
    if forced:
        pool = forced
    for i, s in pool:
        q.answers.append(GameAnswer(
            label=s.wheel_label,
            chance_weight=s.effective_weight(),
            effect="PlayVideoFromPlaylist",
            playlist_index=i,
        ))
    if not q.answers:
        q.answers.append(GameAnswer(label="No Videos", effect="None"))
    return q


def format_time(seconds: float) -> str:
    """Unity FormatTime port: mm:ss."""
    if seconds < 0 or seconds != seconds:  # negative or NaN
        seconds = 0
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def wheel_pieces(question, playlist):
    """[(label, weight), ...] for a question, with live playlist names."""
    return [(a.resolve_label(playlist), a.chance_weight) for a in question.answers]


# GPIO numbers that must never be driven on an ESP32-S3.
#   22-25  do not exist on this chip
#   26-32  in-package SPI flash (SPICS1/HD/WP/CS0/CLK/Q/D)
#   33-37  octal PSRAM on -R8 modules such as the N16R8
#   19-20  native USB          43-44  UART0 console
# Driving any of them stops the CPU fetching code and the board reboots.
ESP32_S3_RESERVED_PINS = {
    19: "native USB D-", 20: "native USB D+",
    22: "does not exist on ESP32-S3", 23: "does not exist on ESP32-S3",
    24: "does not exist on ESP32-S3", 25: "does not exist on ESP32-S3",
    26: "SPI flash CS1", 27: "SPI flash HOLD", 28: "SPI flash WP",
    29: "SPI flash CS0", 30: "SPI flash CLK", 31: "SPI flash Q",
    32: "SPI flash D",
    33: "octal PSRAM", 34: "octal PSRAM", 35: "octal PSRAM",
    36: "octal PSRAM", 37: "octal PSRAM",
    43: "UART0 TX", 44: "UART0 RX",
}

ESP32_S3_SAFE_PINS = [p for p in range(0, 49)
                      if p not in ESP32_S3_RESERVED_PINS and p not in (8, 9)]


def pin_warning(pin: int) -> str:
    """Empty string when the pin is safe, otherwise why it is not."""
    if pin in ESP32_S3_RESERVED_PINS:
        return (f"GPIO {pin} is {ESP32_S3_RESERVED_PINS[pin]} on the ESP32-S3. "
                f"Driving it reboots the board.")
    if pin in (8, 9):
        return f"GPIO {pin} is the OLED I2C bus in your sketch."
    if pin in (0, 45, 46):
        return f"GPIO {pin} is a strapping pin — it can upset boot."
    return ""


def relay_by_id(relays, relay_id: str):
    for r in relays:
        if r.id == relay_id:
            return r
    return relays[0] if relays else None


def relay_index(relays, relay_id: str) -> int:
    for i, r in enumerate(relays):
        if r.id == relay_id:
            return i
    return 0


def next_relay_id(relays) -> str:
    used = {r.id for r in relays}
    n = 1
    while f"r{n}" in used:
        n += 1
    return f"r{n}"


def balance_weights(items, changed_index: int, total: float = 100.0,
                    attr: str = "chance_weight") -> None:
    """Rescale the other entries so the whole set adds up to `total`.

    Unity's inspector behaves this way: type 2 % into the first of two options
    and the second becomes 98 %. With more than two, the remainder is shared in
    proportion to what they already had, so their relative odds survive.
    Entries are never pushed below zero.
    """
    if not items or not (0 <= changed_index < len(items)):
        return
    fixed = max(0.0, min(total, float(getattr(items[changed_index], attr))))
    setattr(items[changed_index], attr, fixed)

    others = [i for i in range(len(items)) if i != changed_index]
    if not others:
        setattr(items[changed_index], attr, total)
        return

    remaining = max(0.0, total - fixed)
    current = sum(max(0.0, float(getattr(items[i], attr))) for i in others)
    if current <= 0.0:
        share = remaining / len(others)
        for i in others:
            setattr(items[i], attr, round(share, 2))
        return
    for i in others:
        value = max(0.0, float(getattr(items[i], attr)))
        setattr(items[i], attr, round(remaining * value / current, 2))


def effective_shares(weights) -> list:
    """What the wheel will actually do, as percentages that add to 100."""
    positive = [max(0.0, float(w)) for w in weights]
    total = sum(positive)
    if total <= 0.0:
        return [0.0 for _ in positive]
    return [100.0 * w / total for w in positive]
