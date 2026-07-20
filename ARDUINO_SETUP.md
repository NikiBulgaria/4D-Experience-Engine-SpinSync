# 🔌 Arduino / ESP32-S3 Setup

Everything needed to flash the board from a clean PC. Takes about ten minutes.

---

## 📁 Which firmware do I flash?

```
firmware/
├── old/    # the original build — Unity optimized, known good
└── new/    # optimized for the Python app (SpinSync)
```

| | `old/` | `new/` |
|---|---|---|
| Works with the Unity app | ✅ | ✅ |
| Works with SpinSync | ✅ | ✅ |
| Refuses reserved GPIOs instead of crashing | ❌ | ✅ |
| Survives a malformed packet | ❌ | ✅ |
| `Ping` keep-alive with no memory leak | ❌ | ✅ |
| Reads every queued packet per loop | ❌ | ✅ |
| OLED stops blocking the loop | ❌ | ✅ |
| `AllOff` emergency stop | ❌ | ✅ |

Both speak the same protocol, so **either one works with both applications**.
Use `new/`; keep `old/` as your fallback.

---

## 1️⃣ Arduino IDE

Install **Arduino IDE 2.x** from [arduino.cc](https://www.arduino.cc/en/software).

## 2️⃣ Additional Boards Manager URL

**File → Preferences → Additional boards manager URLs**, paste:

```
https://espressif.github.io/arduino-esp32/package_esp32_index.json
```

That is the current official index. Older tutorials show
`https://dl.espressif.com/dl/package_esp32_index.json` — that is the legacy
address. If the box already has another URL (ESP8266 etc.), separate them with a
comma.

Then **Tools → Board → Boards Manager**, search `esp32`, install
**esp32 by Espressif Systems**. That single package covers the S3.

## 3️⃣ Board settings — the part people get wrong

Your module is 16 MB flash + 8 MB **octal** PSRAM (N16R8):

| Tools → | Value |
|---|---|
| Board | **ESP32S3 Dev Module** |
| Flash Size | **16MB (128Mb)** |
| PSRAM | **OPI PSRAM** |
| Partition Scheme | 16M Flash (3MB APP / 9.9MB FATFS) |
| Upload Speed | 921600 (drop to 115200 if uploads fail) |
| USB CDC On Boot | Enabled, if you want the Serial Monitor over USB-C |

> ⚠️ **PSRAM must be `OPI PSRAM`.** Set it to Disabled or Quad and the board
> boot-loops — which looks exactly like a broken sketch.

## 4️⃣ Libraries

**Sketch → Include Library → Manage Libraries**, install:

* **Adafruit SSD1306**
* **Adafruit GFX Library** (offered as a dependency — accept)
* **Adafruit BusIO** (same)

Only needed for the OLED. No screen? Set `#define HAS_OLED false` at the top of
the sketch and skip all three.

## 5️⃣ Uduino's Arduino library

This one is **not** in the Library Manager, and it is the step everyone forgets
after reinstalling Windows.

In **Unity**, select the **UduinoManager** object. In its inspector find
**Select Arduino libraries Folder**, point it at

```
C:\Users\<you>\Documents\Arduino\libraries
```

and press **Update Uduino's Arduino library**. That writes the `Uduino` folder
which `#include <Uduino.h>` needs.

## 6️⃣ Sketch folder layout

All three files go in one folder named after the `.ino`:

```
ESP32S3_Source_Code_Wifi/
├── ESP32S3_Source_Code_Wifi.ino
├── Custom_Esp32_S3.h
└── Custom_Esp32_S3.cpp
```

Open the `.ino`; the other two appear as tabs automatically.

## 7️⃣ Set your WiFi

Top of the sketch:

```cpp
const char* ssid     = "IPhoneNiki135";
const char* password = "zdrkp123";
const int   udpPort  = 4222;
```

After uploading, the OLED shows the board's IP. That address goes into
SpinSync's **⚡ ESP** page and into Uduino Manager's **WiFi Options** list on the
Unity side.

---

## 🔧 Wiring the relays

> ⚠️ **Do not use GPIO 26 or 27 on an ESP32-S3.** They are the in-package SPI
> flash lines (CS1 and HOLD). Driving them stops the CPU fetching code and the
> board reboots — this is the single most common cause of "it resets when I send
> a command". `DualRelayController.cs` ships with those pins as its defaults
> because they are fine on a *classic* ESP32.

| Range | Usable? |
|---|---|
| 1–7, 10–18, 21, 38–42, 47, 48 | ✅ safe |
| 8, 9 | ⚠️ OLED I2C in this sketch |
| 0, 3, 45, 46 | ⚠️ strapping pins, can upset boot |
| 19, 20 | ❌ native USB |
| 22–25 | ❌ do not exist on the S3 |
| 26–32 | ❌ SPI flash |
| 33–37 | ❌ octal PSRAM (N16R8) |
| 43, 44 | ❌ UART0 console |

The `new/` firmware refuses a reserved pin and shows `BAD PIN 27` on the OLED
rather than crashing, and SpinSync shows a red warning before it can even send
one.

### Power
Relay coils dip the supply when they switch, and that dip can reset the board.
Give the relay module its **own supply**, tie the grounds together, and put a
**470 µF or larger** capacitor across the relay board's supply. Do not try to
disable the brownout detector on an S3 — the classic
`WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0)` trick misconfigures it there and
produces a permanent `E BOD: Brownout detector was triggered` boot loop.

---

## ✅ Verifying it works

1. Open the **Serial Monitor** at **115200**. You should see
   `=== ESP32-S3 Uduino Booting ===`, dots while it joins WiFi, then
   `Connected! IP: …` and `UDP listening on port 4222`.
2. In SpinSync, open **⚡ ESP**, enter that IP, press **CONNECT**.
   The status should reach **CONNECTED** within a couple of seconds.
3. In the same page's **CONSOLE**, press **R1 ON**. The relay should click and
   the OLED line for that pin should read `ON`.
4. If the status reaches only **CONNECTING**, press **TEST PROTOCOL** — it tries
   CRLF then CR and keeps whichever the board answers.

---

## 🆘 Upload problems

| Symptom | Fix |
|---|---|
| No COM port appears | Hold **BOOT**, tap **RESET**, release **BOOT**, then upload. |
| Upload starts then fails | Drop Upload Speed to 115200. |
| Boots but prints garbage | Serial Monitor baud must be **115200**. |
| Boot loop right after flashing | Check **PSRAM = OPI PSRAM** and **Flash Size = 16MB**. |
| `Uduino.h: No such file` | Step 5 — press *Update Uduino's Arduino library* in Unity. |
