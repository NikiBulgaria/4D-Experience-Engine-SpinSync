#ifndef _Custom_Esp32_S3_H_
#define _Custom_Esp32_S3_H_

#include <Uduino.h>
#include <string.h>
#include <WiFi.h>
#include <WiFiUdp.h>

class Uduino_Esp32 : public Uduino
{
  public:
    Uduino_Esp32(const char* identity);
    static Uduino_Esp32 * _instance;

    bool connectWifi(const char* ssid, const char* password);
    void startUDP(unsigned int p); 
    void update();
    
    // Helpers
    size_t write(uint8_t);
    size_t write(const uint8_t *buffer, size_t size);
    bool isWifiConnected();

    // Required: routes identity reply back to Unity over UDP (not just Serial)
    static void printIdentity(char identity[]);

  private:
    WiFiUDP UDP_Receiver;
    unsigned int port = 4222;  
    IPAddress remote = IPAddress(0,0,0,0);
    
    char sendBuffer[SEND_MAX_BUFFER];       
    int sendBufferPosition = 0;  
    
    size_t sendWifiBuffer();
    size_t addToBuffer(uint8_t c);
};
#endif
