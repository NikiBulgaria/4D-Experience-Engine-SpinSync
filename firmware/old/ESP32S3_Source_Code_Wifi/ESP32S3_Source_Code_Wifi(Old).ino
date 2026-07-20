// =====================================================
// BROWNOUT DISABLE — MUST be the very first includes.
// On ESP32-S3, a crash+restart causes a BOD spike.
// Disabling it here prevents the false-positive loop.
// =====================================================
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"

#include <WiFi.h>
#include "Custom_Esp32_S3.h"   // NOTE: Use the renamed file (no hyphen)
#include <Wire.h>
#include <vector>

// ==================== CONFIGURATION ====================
const char* ssid     = "IPhoneNiki135";
const char* password = "zdrkp123";
const int   udpPort  = 4222;

// ==================== OLED (OPTIONAL) ====================
// The ESP32-S3 N16R8 default I2C pins are SDA=8, SCL=9.
// If you have no OLED connected, set HAS_OLED to false.
#define HAS_OLED true
#define OLED_SDA  8
#define OLED_SCL  9

#if HAS_OLED
  #include <Adafruit_GFX.h>
  #include <Adafruit_SSD1306.h>
  #define SCREEN_WIDTH 128
  #define SCREEN_HEIGHT 64
  Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);
  bool oledOk = false;
#endif

// ==================== STATE ====================
Uduino_Esp32 uduino("ESP32S3_Absolute");

unsigned long lastHeartbeat = 0;
const long safetyTimeout = 1500;
bool unityConnected = false;
std::vector<int> activePins;

// ==================== HELPERS ====================
void showStatus(const char* line1, const char* line2 = "", const char* line3 = "") {
  Serial.printf("[STATUS] %s | %s | %s\n", line1, line2, line3);
  #if HAS_OLED
    if (!oledOk) return;
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(WHITE);
    display.setCursor(0, 0);  display.println(line1);
    display.setCursor(0, 16); display.println(line2);
    display.setCursor(0, 32); display.println(line3);
    display.display();
  #endif
}

// ==================== SETUP ====================
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n\n=== ESP32-S3 Uduino Booting ===");

  // 2. Optional OLED init — NEVER halts on failure
  #if HAS_OLED
    Wire.begin(OLED_SDA, OLED_SCL);
    // begin() returns false if OLED not found — we just skip it, no for(;;)
    oledOk = display.begin(SSD1306_SWITCHCAPVCC, 0x3C);
    if (!oledOk) {
      Serial.println("OLED not found — continuing without display");
    }
  #endif

  showStatus(">> BOOTING <<", "Connecting WiFi...", ssid);

  // 3. WiFi — simple and direct (like your working IR remote code)
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(ssid, password);

  Serial.print("Connecting to WiFi");
  unsigned long startAttempt = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");

    unsigned long elapsed = (millis() - startAttempt) / 1000;
    if (elapsed < 5)        showStatus("Searching...", ssid);
    else if (elapsed < 15)  showStatus("Waiting...", "Is hotspot on?", ssid);
    else                    showStatus("Still searching...", "Check password", ssid);
  }

  String ipStr = WiFi.localIP().toString();
  Serial.println("\nConnected! IP: " + ipStr);
  showStatus("CONNECTED!", ipStr.c_str(), "Starting UDP...");
  delay(1000);

  // 4. Start Uduino UDP listener
  uduino.startUDP(udpPort);

  // 5. Register commands from Unity
  uduino.addCommand("SetRelay",  OnSetRelay);
  uduino.addCommand("Heartbeat", OnHeartbeat);

  showStatus("READY", ipStr.c_str(), "Waiting for Unity");
  Serial.printf("UDP listening on port %d — send commands from Unity!\n", udpPort);
}

// ==================== LOOP ====================
void loop() {
  uduino.update();
  CheckConnectionHealth();

  // Throttled display update at ~4 FPS
  static unsigned long lastDraw = 0;
  if (millis() - lastDraw > 250) {
    UpdateDisplay();
    lastDraw = millis();
  }
}

// ==================== COMMANDS ====================
void OnHeartbeat() {
  lastHeartbeat = millis();
  unityConnected = true;
}

void OnSetRelay() {
  lastHeartbeat = millis();
  unityConnected = true;

  int pin   = uduino.charToInt(uduino.next());
  int state = uduino.charToInt(uduino.next());

  // Track pin for safety shutdown
  bool known = false;
  for (int p : activePins) { if (p == pin) known = true; }
  if (!known) activePins.push_back(pin);

  pinMode(pin, OUTPUT);
  digitalWrite(pin, state == 1 ? LOW : HIGH); // Relay: LOW = ON
}

void CheckConnectionHealth() {
  if (millis() - lastHeartbeat > safetyTimeout) {
    if (unityConnected) {
      Serial.println("SAFETY: Unity connection lost — shutting down relays");
      for (int pin : activePins) {
        pinMode(pin, OUTPUT);
        digitalWrite(pin, HIGH); // Relay OFF
      }
      unityConnected = false;
    }
  }
}

// ==================== DISPLAY ====================
void UpdateDisplay() {
  #if HAS_OLED
    if (!oledOk) return;
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(WHITE);
    display.setCursor(0, 0);

    if (WiFi.status() == WL_CONNECTED) {
      display.println("STATUS: ONLINE");
      display.println("--------------------");
      display.print("Net: "); display.println(ssid);
      display.print("IP:  "); display.println(WiFi.localIP());
    } else {
      display.println("STATUS: WiFi LOST!");
      display.println("--------------------");
      display.println("Reconnecting...");
    }

    display.println("");
    if (unityConnected) {
      if ((millis() / 500) % 2 == 0) display.println("Unity: LINKED <3");
      else                           display.println("Unity: LINKED   ");
      display.print("Port: "); display.println(udpPort);
    } else {
      display.setTextColor(BLACK, WHITE);
      display.println(" Unity: WAITING...  ");
      display.setTextColor(WHITE);
      display.println("Open Unity Scene");
    }
    display.display();
  #else
    // Serial-only status every 5 seconds
    static unsigned long lastPrint = 0;
    if (millis() - lastPrint > 5000) {
      Serial.printf("[LOOP] WiFi:%s | Unity:%s | IP:%s\n",
        WiFi.status() == WL_CONNECTED ? "OK" : "LOST",
        unityConnected ? "LINKED" : "WAITING",
        WiFi.localIP().toString().c_str());
      lastPrint = millis();
    }
  #endif
}