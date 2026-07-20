#include "Custom_Esp32_S3.h"

Uduino_Esp32 * Uduino_Esp32::_instance = NULL;

Uduino_Esp32::Uduino_Esp32(const char* identity): Uduino(identity) {
  _instance = this;
  memset(sendBuffer, '\0', SEND_MAX_BUFFER);
}

bool Uduino_Esp32::connectWifi(const char* ssid, const char* password) {
  return WiFi.status() == WL_CONNECTED;
}

void Uduino_Esp32::startUDP(unsigned int p) {
  port = p;
  UDP_Receiver.begin(port);
  addPrintFunction(printIdentity);
  Serial.printf("Custom Uduino UDP listening on port %d\n", port);
}

void Uduino_Esp32::printIdentity(char identity[]) {
  if (Uduino_Esp32::_instance != NULL) {
    Uduino_Esp32::_instance->println(identity);
  }
}

bool Uduino_Esp32::isWifiConnected() {
  return WiFi.status() == WL_CONNECTED;
}

bool Uduino_Esp32::hasRemote() {
  return remote != IPAddress(0, 0, 0, 0);
}

IPAddress Uduino_Esp32::getRemote() {
  return remote;
}

void Uduino_Esp32::update() {
  Uduino::update();

  int packetSize = 0;
  int guard = 0;

  while ((packetSize = UDP_Receiver.parsePacket()) > 0 && guard < 12) {
    guard++;
    remote = UDP_Receiver.remoteIP();

    while (UDP_Receiver.available()) {
      char c = (char)UDP_Receiver.read();
      Uduino::update(c);
    }
  }
}

size_t Uduino_Esp32::addToBuffer(uint8_t c) {
  if (sendBufferPosition >= SEND_MAX_BUFFER - 2) {
    sendBufferPosition = 0;
    sendBuffer[0] = '\0';
  }

  sendBuffer[sendBufferPosition] = c;
  sendBufferPosition++;
  sendBuffer[sendBufferPosition] = '\0';

  sendWifiBuffer();
  return 1;
}

size_t Uduino_Esp32::sendWifiBuffer() {
  bool complete = (sendBufferPosition > 1 &&
                   sendBuffer[sendBufferPosition - 1] == '\n' &&
                   sendBuffer[sendBufferPosition - 2] == '\r');

  if (sendBufferPosition >= SEND_MAX_BUFFER - 2 || complete) {
    size_t written = 0;

    if (remote != IPAddress(0, 0, 0, 0) && WiFi.status() == WL_CONNECTED) {
      UDP_Receiver.beginPacket(remote, port);
      written = UDP_Receiver.write((const uint8_t*)sendBuffer, sendBufferPosition);
      UDP_Receiver.endPacket();
    }

    sendBufferPosition = 0;
    sendBuffer[0] = '\0';
    return written;
  }
  return 0;
}

size_t Uduino_Esp32::write(uint8_t c) {
  return addToBuffer(c);
}

size_t Uduino_Esp32::write(const uint8_t *buffer, size_t size) {
  for (size_t i = 0; i < size; i++) {
    addToBuffer(buffer[i]);
  }
  return size;
}
