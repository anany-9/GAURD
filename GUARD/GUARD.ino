/*
 * LVGL_Arduino.ino
 * Main sketch for Waveshare ESP32-S3 Touch 2.8
 * Replace Lvgl_Example1() with UI_Init()
 */

#include "Display_ST7789.h"
#include "Audio_PCM5101.h"
#include "RTC_PCF85063.h"
#include "Gyro_QMI8658.h"
#include "LVGL_Driver.h"
#include "PWR_Key.h"
#include "SD_Card.h"
#include "BAT_Driver.h"
#include "UI_Main.h"

void DriverTask(void *parameter) {
  uint32_t fall_tick = 0;
  while (1) {
    PWR_Loop();
    BAT_Get_Volts();
    PCF85063_Loop();
    QMI8658_Loop();

    // Run fall detection at ~50 ms cadence (every 5th 10ms tick or adapt as needed)
    // DriverTask runs every 100ms; call FallDetection_Loop each iteration
    // for ~100ms granularity (sufficient for the 80ms freefall window).
    FallDetection_Loop();

    vTaskDelay(pdMS_TO_TICKS(50));   // tightened from 100ms → 50ms for better detection
  }
}

void setup() {
  Serial.begin(115200);
  PWR_Init();
  BAT_Init();
  I2C_Init();
  PCF85063_Init();
  QMI8658_Init();
  Backlight_Init();
  SD_Init();
  Audio_Init();
  LCD_Init();
  Lvgl_Init();

  // Start UI
  UI_Init();

  // Driver loop on core 0
  xTaskCreatePinnedToCore(DriverTask, "DriverTask", 4096, NULL, 3, NULL, 0);
}

void loop() {
  Lvgl_Loop();
  vTaskDelay(pdMS_TO_TICKS(5));
}
