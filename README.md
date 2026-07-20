# 🎡 SpinSync: Hardware-Synchronized Prize Wheel & Video Trigger System

**SpinSync** is a custom PyQt6 desktop application designed to bridge the gap between interactive digital interfaces and real-world hardware. Originally conceptualized in Unity, this Python port features a fully physics-driven prize wheel and an advanced video synchronization panel that triggers live ESP32-controlled relays.

Whether you are building custom arcade cabinets, interactive restaurant displays, or hardware-linked video experiences, SpinSync guarantees precise hardware responses (motors and voltage switches) synced perfectly to specific video timestamps.

![UI Showcase](https://img.shields.io/badge/UI-PyQt6-blue?style=flat-square)
![Hardware](https://img.shields.io/badge/Hardware-ESP32--S3-success?style=flat-square)
![Physics](https://img.shields.io/badge/Physics-Unity_Port-blueviolet?style=flat-square)
![Randomness](https://img.shields.io/badge/RNG-CSPRNG-orange?style=flat-square)

---

## ✨ Key Features

### 🎡 Physics-Driven Prize Wheel (`wheel_widget.py`)
A direct port of my unity `WheelController.cs` physics engine, featuring:
* **Realistic Spin Physics:** Torque, motor acceleration phases, and coasting with per-spin randomized friction (0.4x–2.0x).
* **Dynamic Visuals:** Auto-colored slices using a non-adjacent duplicate palette walk, a tangential text renderer, and pulse-flashing winner highlights.
* **Two Outcome Models:** *Physics* (whatever the pointer lands on wins, exactly like Unity) or *Draw* (winner drawn first with exact odds, then the wheel animates to it).
* **Cryptographic Randomness:** Every spin pulls from the OS entropy pool, so no sequence of past spins predicts the next one.

### 🎬 Advanced Trigger Tester (`test_panel.py`)
A robust video playback and hardware testing environment (ported from `AdvancedVideoControls.cs`):
* **Scrubbable Timeline:** Visually shaded trim windows with active playback playheads.
* **Live Hardware Execution:** "Live Mode" routes triggers directly to physical ESP32 relay banks in real-time.
* **State-Aware Testing:** Scrubbing backwards or forwards instantly re-evaluates the relay states (Motor/Voltage On/Off) to match the exact video timestamp.
* **Resizable Preview:** Drag the divider to grow the picture, with Fit / Fill / Stretch scaling.
* **Variable Playback:** Quick-select playback speeds (0.25x to 4.0x) for precise timing adjustments.

### 🎛 Full Live Control (`show_window.py`)
* **Never interrupts the show:** open settings mid-video, change anything, and playback picks it up immediately.
* **Transport bar** that appears only while a clip is running — restart, seek, skip, stop, mute, volume, scrub.
* **Corner timecode** on a gradient "bookmark" strip that fades toward the middle of the screen so it never boxes off the picture.

### 🧩 Custom PyQt6 Widgets (`widgets.py`)
* **LedBlock:** Glowing UI indicators for real-time relay state feedback (e.g., `Motor: ON`).
* **TriggerTimeline:** A custom clip timeline visualizing `FIRED`, `NEXT`, and `PENDING` states for every hardware trigger.
* **PaletteEditor:** An intuitive UI for adding and managing wheel slice colors.

---

## 🏗️ Architecture & Core Components

```text
├── firmware/
│   ├── old/           # Source code and libraries used to make it work for the Unity application
│   └── new/           # Optimized files for the current Python application
├── main.py            # Entry point: theme, wiring, clean shutdown.
├── show_window.py     # Fullscreen kiosk screen: video, wheel overlay, timecode, transport.
├── settings_window.py # Six-page control room (Editor / Game Flow / Wheel / ESP / Show / General).
├── game_engine.py     # Show state machine: questions, countdown, triggers, loops, breaks.
├── wheel_widget.py    # The physics engine and rendering for the prize wheel.
├── test_panel.py      # Video playback, trim editing, and live hardware test mode.
├── question_editor.py # Game flow: questions, answers, effects, sub-questions.
├── widgets.py         # Custom UI elements (LEDs, Timelines, Palette Editors).
├── config.py          # Data models: WheelSettings, HardwareTrigger, VideoScenario.
├── esp_link.py        # Hardware communication bridge for the ESP32 RelayBank.
├── rng.py             # Entropy service (OS CSPRNG).
└── start.bat          # Windows launcher.
```

### Hardware Integration
The system expects an active connection to an ESP32 managing a `RelayBank`. Triggers defined in the video scenario (e.g., `MotorOn`, `VoltageRelayOff`) are dispatched to the hardware, allowing instantaneous physical feedback alongside the on-screen video.

---

## 🚀 Setup & Installation

1. **Prerequisite:** Install **Python 3.10+**.
2. **Run:** Double-click `start.bat`. It checks Python, upgrades `pip`, installs everything in `requirements.txt`, and launches the app.
3. **Connect the board:** press `Esc` → **⚡ ESP** → type the IP shown on the OLED (or press **SCAN NETWORK**) → **CONNECT**.
4. **Add your videos:** **▶ EDITOR** → point each entry at a file, set trims and triggers.

For the microcontroller side see **[ARDUINO_SETUP.md](ARDUINO_SETUP.md)**.

---

## 🎚 Relays — as many as you need

The two hardcoded outputs are gone. The **⚡ ESP** page now has a full relay
manager: add, rename, re-pin, reorder and remove any number of relays. Defaults
are **Relay 1**, **Relay 2** and **Voltage** on safe GPIOs 4, 5 and 6.

* **Every relay owns its test timer.** Testing one no longer cancels another —
  each row has its own hold duration, its own ON / OFF / HOLD buttons, its own
  indicator and its own countdown bar.
* **Duplicate or reserved pins are flagged** live, with the safe GPIO list.
* **Deleting a relay warns you** how many video triggers point at it, and
  prunes it from the test sequence.
* Video triggers are chosen from a dropdown of *relay × ON/OFF*, so they follow
  renames automatically.

### Programmable test sequence
Underneath the relay list is a scripted bench run: set a length (up to three
minutes, with 30 s / 1 min / 3 min presets), then add steps — each step
switches one relay at one moment. Press **RUN SEQUENCE** and the timeline plays
back with a live playhead and progress bar, so you can rehearse a whole cue
without a video. Steps can be re-timed, re-targeted, sorted or cleared.

## 🪟 Layout

* **Detach the preview.** The editor's preview has a **DETACH** button that pops
  it into a real top-level window — drag it to a screen edge for Windows Snap,
  size it freely, or move it to a second monitor. Closing it or pressing
  **RE-ATTACH** puts it back.
* **Controls adapt.** Everything under the preview lives in its own scroll area,
  so however large you make the picture the panel below stays usable — a
  scrollbar appears and the trigger table and timeline compact themselves.
* **Everything is a divider.** The preview/controls split, and the ESP page's
  console split, are draggable. The console now opens at a sensible height
  instead of covering the page.

## ↩️ Undo, redo and per-page reset

Next to **Save Settings** are **RESET PAGE**, **UNDO** and **REDO**. The undo
buttons name what they will roll back (`UNDO playlist`, `REDO wheel`), and a
burst of typing collapses into one step rather than fifty. **RESET PAGE**
restores just the page you are on — resetting the wheel never touches your
playlist — and is itself undoable. The ESP page keeps your board IP and port
when reset.

## 🎮 Controls

| Key | Action |
|---|---|
| `Space` / `Enter` | Start · Reset |
| `P` | Pause / resume |
| `R` | Restart the clip from its IN point (re-arms every trigger) |
| `N` | Skip video |
| `S` | Stop everything and force relays off |
| `M` | Mute · `↑` `↓` volume |
| `←` `→` | Seek 5 s (relay state recomputed for that moment) |
| `F` | Video fit: fit / fill / stretch |
| `T` | Toggle transport bar · `H` help |
| `Esc` | Settings · `F11` fullscreen |

Kiosk touch: five taps in the top-left corner also opens settings.

---

## 📡 The ESP32 Link

### Wire protocol
Plain-text UDP on port **4222**, identical to Uduino so the Unity project keeps working:

```
SetRelay 26 1<CR><LF>     relay pin + state (1 = ON, firmware drives the pin LOW)
Heartbeat<CR><LF>         keep-alive; firmware kills all relays after 1.5 s of silence
Ping<CR><LF>              zero-allocation liveness check (new firmware)
AllOff<CR><LF>            emergency stop (new firmware)
identity<CR><LF>          handshake; board answers "uduinoIdentity <name>"
connected<CR><LF>         sets Uduino::init = true, as Unity does on discovery
```

### Connection states
The link is not just "connected or not". Five states, each with its own colour and button:

| State | Meaning | Button |
|---|---|---|
| **OFFLINE** (grey) | Not connected and not trying. | CONNECT |
| **CONNECTING** (amber) | Sending, waiting for the first reply. | CANCEL |
| **RECONNECTING** (orange) | Was connected, replies stopped, still sending and retrying. The socket is rebuilt every 20 s. | DISCONNECT |
| **CONNECTED** (green) | The board is answering. | DISCONNECT |
| **SENDING** (teal) | Verification switched off, commands go out unchecked. | DISCONNECT |

A **RETRY** button rebuilds the socket without touching settings. The same wording appears on the show screen's status bar.

---

## 🐛 Bugs Found & Fixed

Everything below came from reading the actual Uduino sources (`Uduino.cpp`, `Uduino_Wifi.cpp`) and the ESP32-S3 datasheet.

### The board replies to a fixed port
`Custom_Esp32_S3.cpp` sends every reply with `beginPacket(remote, port)` — always to `<PC ip>:4222`, never to the source port of the packet that asked. A client bound to an ephemeral port therefore never hears a reply, no matter how healthy the link is. **SpinSync binds to 4222.**

### Commands need a carriage return
`Uduino.cpp` sets `term='\r'` and discards non-printable characters, so a command is executed **only** when a carriage return arrives — `\n` alone or no terminator means the text is buffered forever and **the relays never move**. Default is now CRLF, and the app warns if you pick a line ending without `\r`.

### GPIO 26 / 27 reboot an ESP32-S3
`DualRelayController.cs` ships with `PinRelay1 = 26`, `PinRelay2 = 27`. On a classic ESP32 those are ordinary pins. On the **S3** they are bonded to the in-package SPI flash (26 = CS1, 27 = HOLD), and on an **N16R8** GPIO 33–37 are octal PSRAM as well. Driving them stops the CPU fetching code and the chip panics.

**Safe GPIOs:** 1–7, 10–18, 21, 38–42, 47, 48 (avoid 8/9 if the OLED uses them).
The app shows a red warning if a reserved pin is selected, and the new firmware **refuses** the pin and prints `BAD PIN 27` on the OLED instead of crashing.

### A 32-byte leak on every identity probe
`Uduino::getPrintedIdentity()` `malloc`s the reply string and never frees it. Probing every 2.5 s leaks ~56 KB/hour — free heap on an S3 is ~200–250 KB, so a long show dies of heap exhaustion. The new firmware adds a **`Ping`** command that answers from a static string, and the app uses `identity` only once to learn the board name.

### A short command could panic the parser
`OnSetRelay` called `charToInt(uduino.next())` twice with no null check. Any truncated datagram makes `next()` return `NULL` and the dereference is an instant `LoadProhibited` panic. Both arguments are validated now.

### One packet per loop
`parsePacket()` was called once per `update()`, so the board consumed at most one datagram per `loop()` — while the OLED refresh blocked that loop ~90 ms in every 250 ms. Roughly a third of the time the board was deaf, and the small lwIP queue discarded the overflow. Simulated over ten minutes, ~4 % of probes are lost on a *healthy* link; a four-second watchdog turned that into eight false disconnects per ten minutes. The new firmware drains every queued packet, only redraws the OLED when the text changes, and the app tolerates 15 s of silence before reporting a problem.

### ⚠️ The brownout trick does not work on the S3
An earlier revision of this firmware called `WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0)` to disable the brownout detector. That is a classic **ESP32** trick; on the **ESP32-S3** it misconfigures the detector instead, which fires immediately and produces an endless

```
rst:0x3 (RTC_SW_SYS_RST) ... E BOD: Brownout detector was triggered
```

boot loop. **It has been removed.** If you see genuine brownouts when relays switch, fix the power rather than the detector: give the relay board its own supply, share grounds, and add a bulk capacitor (470 µF+) across the relay module's supply. `LOW_LATENCY_WIFI` is left **off** by default for the same reason — it lowers UDP latency but raises average current.

---

## 🎲 Randomness

`rng.py` uses `random.SystemRandom`, which reads `os.urandom()` — the OS cryptographic pool (BCryptGenRandom on Windows), continuously re-seeded from hardware entropy. Unlike the default Mersenne Twister there is no seed to recover and no sequence to extrapolate. It is the same generator `secrets` uses for passwords.

Two outcome models on the **◎ WHEEL** page:
* **Physics** — organic, with the tiny geometric bias any real wheel has.
* **Draw** — winner drawn with exact weights, then animated to. Verified over 20,000 spins: the landing always matched the draw, and a 3:1:1:1 wheel produced 50.1 / 16.6 / 16.5 / 16.8 %.

---

## 🛠️ Development & Extending

Since the core logic is ported from C#/Unity, Unity developers will find familiar paradigms here, especially regarding the `Rigidbody2D` torque approximations (`UNITY_TORQUE_DIVISOR = 60.0`). Extend the `ACTION_COLORS` dictionary in `widgets.py` to wire up additional relays (pneumatics, LEDs, secondary motors).

Configuration lives in a single readable `settings.json`. Old config files are migrated automatically on load.

---

## 🎯 Chances behave like the Unity inspector

Type a chance and the others rescale so the set adds up to 100 — enter 2 % on
the first of two options and the second becomes 98 %. With more than two the
remainder is shared in proportion, so their relative odds survive: 20 / 30 / 60
with the first set to 20 % becomes 20 / 26.67 / 53.33.

Beside every number is the **effective odds** the wheel will really use
(`→ 26.7%`), because raw weights of 3 : 1 : 1 : 1 mean 50 / 16.7 / 16.7 / 16.7
whether or not they add to 100. Untick **Auto-balance to 100%** to enter raw
weights instead; the arrow keeps telling the truth. **Normalise now** rewrites
every chance as its real percentage.

## ⏱ Times are timecodes

Trigger and sequence times are entered as **mm:ss** or **hh:mm:ss**, not raw
seconds. `1:03`, `01:03`, `1:02:03`, `1:02:03.5` and plain `63` all work, and
the field reformats itself when you leave it. Up and Down nudge by a second,
Shift for a tenth. Hours only appear once a time passes an hour.

## 🔁 Relay commands are self-healing

UDP guarantees nothing, and this board drops packets while its OLED blocks the
loop — measured at roughly 4 % on a healthy link. Sending a state change once
and assuming it landed is how a relay ends up **ON in the app and OFF on the
bench, with the board never even trying**.

Every change is now sent, then **repeated three times** at 200 ms spacing, and
the complete relay state is **re-asserted on a slow rotation** (one relay every
1.5 s, at the lowest priority so it never delays a real change or the
heartbeat). `SetRelay` is idempotent, so repeating it costs nothing. If the
link drops, the record of what the board knows is cleared, so reconnecting
re-states everything.

Simulated over 3000 runs of "press ON, leave it for two minutes" at 4 % loss:

| | relay stuck wrong | average time wrong |
|---|---|---|
| Send once | **3.7 % of the time, permanently** | 4.36 s |
| Confirm + refresh | 0 % over 5 s | 0.008 s |

Cost: about 1.6 extra packets per second, against a heartbeat that already
sends 2 per second.

## 🎡 The wheel keeps its own time

Windows throttles timers for windows that are not in front. The wheel added a
clamped delta each tick, so a six-second spin stretched to thirty or sixty
seconds of wall clock when the show window lost focus — it looked frozen on
SPINNING and then "caught up". It now advances by **real elapsed time in fixed
sub-steps**, so a spin takes the same time whether or not the window has focus.

There is also a **spin watchdog**: if the wheel has not reported a winner well
after the longest possible spin, the engine draws one itself using the real
weights and carries on. A stuck wheel can no longer hang the show, and the
rescued pick is still fair — 40,000 rescued draws on a 3:1:1:1 wheel gave
49.9 / 16.5 / 16.9 / 16.7 %.

## 🛡 The show does not stop

A video failing to open used to end the run. It no longer can. Every failure
walks a recovery ladder instead:

1. **Retry the clip up to three times**, with growing patience (8 s, 12 s, 16 s).
   Each retry clears and re-sets the source, and the third also pokes the
   decoder with a play/pause, which shakes loose a backend that stalled on the
   file it had just finished playing.
2. **Fall back to another playlist entry** that has a real file, and say so in
   the log.
3. **Run the cue without a picture.** The screen shows `NO PICTURE — CUE
   RUNNING`, but the triggers still fire at their timestamps on a wall clock,
   and the countdown, loops and breaks all behave normally. Pause still works.

A missing file no longer aborts either — it looks for the next playable entry
first. There is also a **stall watchdog**: if playback claims to be running but
the position stops advancing for 2.5 s, it nudges the player, then seeks, and
if it still will not move it hands the remainder of the cue to the wall clock.

The hardware is the point of the show, so it keeps firing even when the picture
cannot.

## 🐞 Fixed: the trigger dropdown lied

Every trigger row displayed *Relay 1 — ON* regardless of what was stored, while
the Trigger Tester and the hardware used the correct relay. Nothing was hidden
and nothing was silently changed: the rows were built with a Python tuple as the
combo box's item data, and `QComboBox.findData()` cannot match a tuple through
QVariant. It returned -1 every time, `setCurrentIndex(max(0, -1))` fell back to
row 0, and the first entry was shown. The keys are plain strings now
(`r3:1`), so the dropdown shows what is actually stored. A trigger pointing at a
deleted relay shows `(missing relay r4)` instead of silently reading as
something else.

## 📐 Adaptive layout

Every panel now shrinks instead of running off the edge:

* **Tables use resize modes.** Trigger and sequence tables stretch the relay
  column and hold the rest to what they need. A stale line was setting the
  State column to 40 px right after setting it to 96, which is why it only ever
  showed `O` instead of `ON` / `OFF`.
* **Text fields yield first.** Answer labels and effect dropdowns have an
  *Ignored* width policy, so the controls to their right stay on screen rather
  than being pushed past the window edge.
* **Every page scrolls.** Editor, Game Flow, Wheel, ESP, Show and General are
  all inside scroll areas with scrollbars on demand, so nothing is unreachable
  at any window size.
* **Nothing sits flush against a frame.** The flow editor had zero margins,
  which is why *ADD ANSWER* and the answers header touched the panel border.

Minimum window width is now roughly **546 px**, down from the 2081 px that
Windows complained about on a 1786 px screen.

`python3 ui_audit.py` re-runs this audit: it lists widgets that still cannot
shrink, tables without resize policies, and which modules have scroll areas.

## 🧪 verify.py

`python3 verify.py` statically checks the whole codebase without needing a
display or PyQt installed. It catches the failures that otherwise only appear
on launch:

* `self._something` referenced but never defined
* `pyqtSignal(...)` emitted with the wrong number of arguments
* attribute access on a project class that has no such member

It exists because an edit once removed six `EspTab` methods at once and the app
died on startup with `AttributeError: '_ip_edited'`. Deleting that method again
makes `verify.py` report it immediately.

## 🔧 Troubleshooting

| Symptom | Cause |
|---|---|
| Status stuck on CONNECTING | Different networks, Windows Firewall blocking inbound UDP, or something else holding port 4222. |
| Board reacts but app says disconnected | Normal probe loss — raise *Call it lost after* on the ESP page. |
| Board resets when a command arrives | Relay pin is a reserved GPIO (26/27 on an S3). Check the red pin warning. |
| Board resets after hours of running | Old firmware's identity leak — flash `firmware/new/`. |
| Relays never move | Line ending without a carriage return, or wrong pins. |
| Boot loop with `E BOD` | A brownout register write (removed), or genuinely insufficient power. |
| Black video | Prefer H.264 MP4 and run `pip install -U PyQt6`; the FFmpeg backend arrived in Qt 6.5. |
