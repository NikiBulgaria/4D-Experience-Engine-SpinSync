#include "Custom_Esp32_S3.h"

Uduino_Esp32 * Uduino_Esp32::_instance = NULL;

Uduino_Esp32::Uduino_Esp32(const char* identity): Uduino(identity) {
  _instance = this;
}

bool Uduino_Esp32::connectWifi(const char* ssid, const char* password) {
  return true; 
}

void Uduino_Esp32::startUDP(unsigned int p) {
  port = p;
  UDP_Receiver.begin(port);
  // THIS WAS THE BUG: without this line, identity replies went to Serial only.
  // Now Uduino knows to route the handshake reply back to Unity over UDP.
  addPrintFunction(printIdentity);
  Serial.printf("Custom Uduino UDP listening on port %d\n", port);
}

// Static function called by Uduino base class to send the identity reply.
// Calls println() which goes through our overridden write() → UDP send.
void Uduino_Esp32::printIdentity(char identity[]) {
  Uduino_Esp32::_instance->println(identity);
}

bool Uduino_Esp32::isWifiConnected() {
  return WiFi.status() == WL_CONNECTED;
}

void Uduino_Esp32::update() {
  Uduino::update(); 

  int packetSize = UDP_Receiver.parsePacket();
  if (packetSize) {
    // Save Unity's IP so we can reply to it
    if (remote == IPAddress(0, 0, 0, 0)) {
        remote = UDP_Receiver.remoteIP();
    }
    while (UDP_Receiver.available()) {
        char c = (char)UDP_Receiver.read();
        Uduino::update(c); 
    }
  }
}

// --- OUTGOING: ESP32-S3 → Unity ---
size_t Uduino_Esp32::addToBuffer(uint8_t c) {
  sendBuffer[sendBufferPosition] = c;
  sendBufferPosition++;
  sendWifiBuffer();
  return 1;
}

size_t Uduino_Esp32::sendWifiBuffer() {
  if(sendBufferPosition >= SEND_MAX_BUFFER - 1 || 
    (sendBufferPosition > 1 && 
     sendBuffer[sendBufferPosition - 1] == '\n' && 
     sendBuffer[sendBufferPosition - 2] == '\r')) {
    
    size_t written = 0;
    if(remote != IPAddress(0,0,0,0)) {
      UDP_Receiver.beginPacket(remote, port);
      written = UDP_Receiver.write((uint8_t*)sendBuffer, strlen(sendBuffer));
      UDP_Receiver.endPacket();
    }

    sendBufferPosition = 0;
    memset(sendBuffer, '\0', SEND_MAX_BUFFER);
    return written;
  }
  return 0;
}

size_t Uduino_Esp32::write(uint8_t c) {
  return addToBuffer(c);
}

size_t Uduino_Esp32::write(const uint8_t *buffer, size_t size) {
  for(size_t i = 0; i < size; i++) {
    addToBuffer(buffer[i]);
  }
  return size;
}
