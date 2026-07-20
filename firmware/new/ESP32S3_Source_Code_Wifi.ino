#include <WiFi.h>
#include "Custom_Esp32_S3.h"
#include <Wire.h>
#include <stdlib.h>

const char* ssid     = "IPhoneNiki135";
const char* password = "zdrkp123";
const int   udpPort  = 4222;

#define HAS_OLED           true
#define OLED_SDA           8
#define OLED_SCL           9
#define OLED_FAST_I2C      true

#define RELAY_ACTIVE_LOW   true
#define SAFETY_TIMEOUT     1500
#define MAX_TRACKED_PINS   16
#define DISPLAY_INTERVAL   250

#define LOW_LATENCY_WIFI   false

#if HAS_OLED
  #include <Adafruit_GFX.h>
  #include <Adafruit_SSD1306.h>
  #define SCREEN_WIDTH  128
  #define SCREEN_HEIGHT 64
  Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);
  bool oledOk = false;
#endif

Uduino_Esp32 uduino("ESP32S3_Absolute");

unsigned long lastHeartbeat = 0;
unsigned long lastDraw = 0;
bool unityConnected = false;

int  trackedPins[MAX_TRACKED_PINS];
bool trackedState[MAX_TRACKED_PINS];
int  trackedCount = 0;

int rejectedPin = -1;
unsigned long rejectedAt = 0;

char lastFrame[192] = "";

bool pinAllowed(int pin) {
  if (pin < 0 || pin > 48) return false;
  if (pin >= 22 && pin <= 25) return false;
  if (pin >= 26 && pin <= 37) return false;
  if (pin == 19 || pin == 20) return false;
  if (pin == 43 || pin == 44) return false;
#if HAS_OLED
  if (pin == OLED_SDA || pin == OLED_SCL) return false;
#endif
  return true;
}

int relayLevel(int state) {
#if RELAY_ACTIVE_LOW
  return (state == 1) ? LOW : HIGH;
#else
  return (state == 1) ? HIGH : LOW;
#endif
}

int trackIndex(int pin) {
  for (int i = 0; i < trackedCount; i++) {
    if (trackedPins[i] == pin) return i;
  }
  if (trackedCount >= MAX_TRACKED_PINS) return -1;
  trackedPins[trackedCount] = pin;
  trackedState[trackedCount] = false;
  trackedCount++;
  return trackedCount - 1;
}

void applyRelay(int pin, int state) {
  int idx = trackIndex(pin);
  if (idx < 0) return;
  pinMode(pin, OUTPUT);
  digitalWrite(pin, relayLevel(state));
  trackedState[idx] = (state == 1);
}

void shutdownAllRelays() {
  for (int i = 0; i < trackedCount; i++) {
    pinMode(trackedPins[i], OUTPUT);
    digitalWrite(trackedPins[i], relayLevel(0));
    trackedState[i] = false;
  }
}

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

void OnHeartbeat() {
  lastHeartbeat = millis();
  unityConnected = true;
}

void OnPing() {
  lastHeartbeat = millis();
  unityConnected = true;
  uduino.println("uduinoPing");
}

void OnSetRelay() {
  lastHeartbeat = millis();
  unityConnected = true;

  char* pinArg = uduino.next();
  if (pinArg == NULL) return;

  char* stateArg = uduino.next();
  if (stateArg == NULL) return;

  int pin   = atoi(pinArg);
  int state = atoi(stateArg);

  if (!pinAllowed(pin)) {
    rejectedPin = pin;
    rejectedAt = millis();
    Serial.printf("REJECTED GPIO %d reserved on ESP32-S3\n", pin);
    return;
  }

  applyRelay(pin, state);
}

void OnAllOff() {
  lastHeartbeat = millis();
  unityConnected = true;
  shutdownAllRelays();
}

void CheckConnectionHealth() {
  if (millis() - lastHeartbeat > SAFETY_TIMEOUT) {
    if (unityConnected) {
      Serial.println("SAFETY: link lost, relays off");
      shutdownAllRelays();
      unityConnected = false;
    }
  }
}

void BuildFrame(char* out, size_t size) {
  bool wifiOk = (WiFi.status() == WL_CONNECTED);

  if (rejectedPin >= 0 && millis() - rejectedAt < 8000) {
    snprintf(out, size, "BAD PIN %d\nreserved on S3\ncheck relay wiring\nuse 4-7 10-18 38-42",
             rejectedPin);
    return;
  }

  char relays[64];
  relays[0] = '\0';
  for (int i = 0; i < trackedCount && i < 4; i++) {
    char one[16];
    snprintf(one, sizeof(one), "%d:%s ", trackedPins[i],
             trackedState[i] ? "ON" : "off");
    strncat(relays, one, sizeof(relays) - strlen(relays) - 1);
  }
  if (trackedCount == 0) strncpy(relays, "none", sizeof(relays));

  snprintf(out, size, "STATUS: %s\n--------------------\nIP:  %s\nPC:  %s\n%s\n%s",
           wifiOk ? "ONLINE" : "WiFi LOST",
           WiFi.localIP().toString().c_str(),
           uduino.hasRemote() ? uduino.getRemote().toString().c_str() : "-",
           unityConnected ? "Link: ACTIVE" : "Link: WAITING",
           relays);
}

void UpdateDisplay() {
#if HAS_OLED
  if (!oledOk) return;

  char frame[192];
  BuildFrame(frame, sizeof(frame));
  if (strcmp(frame, lastFrame) == 0) return;
  strncpy(lastFrame, frame, sizeof(lastFrame) - 1);
  lastFrame[sizeof(lastFrame) - 1] = '\0';

  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(WHITE);
  display.setCursor(0, 0);
  display.println(frame);
  display.display();
#else
  static unsigned long lastPrint = 0;
  if (millis() - lastPrint > 5000) {
    lastPrint = millis();
    Serial.printf("[LOOP] WiFi:%s | Link:%s | IP:%s\n",
      WiFi.status() == WL_CONNECTED ? "OK" : "LOST",
      unityConnected ? "ACTIVE" : "WAITING",
      WiFi.localIP().toString().c_str());
  }
#endif
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n\n=== ESP32-S3 Uduino Booting ===");

#if HAS_OLED
  Wire.begin(OLED_SDA, OLED_SCL);
  #if OLED_FAST_I2C
    Wire.setClock(400000);
  #endif
  oledOk = display.begin(SSD1306_SWITCHCAPVCC, 0x3C);
  if (!oledOk) {
    Serial.println("OLED not found, continuing without display");
  }
#endif

  showStatus(">> BOOTING <<", "Connecting WiFi...", ssid);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
#if LOW_LATENCY_WIFI
  WiFi.setSleep(false);
#endif
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

  uduino.startUDP(udpPort);

  uduino.addCommand("SetRelay",  OnSetRelay);
  uduino.addCommand("Heartbeat", OnHeartbeat);
  uduino.addCommand("Ping",      OnPing);
  uduino.addCommand("AllOff",    OnAllOff);

  lastHeartbeat = millis();
  showStatus("READY", ipStr.c_str(), "Waiting for app");
  Serial.printf("UDP listening on port %d\n", udpPort);
  lastFrame[0] = '\0';
}

void loop() {
  uduino.update();
  CheckConnectionHealth();

  if (millis() - lastDraw > DISPLAY_INTERVAL) {
    lastDraw = millis();
    UpdateDisplay();
  }
}
