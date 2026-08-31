#pragma once
#include "Arduino.h"
#include "Display_ST7789.h"

#define PWR_KEY_Input_PIN    6
#define PWR_Control_PIN      7

// ── Vibration Motor ───────────────────────────────────────────────────────
#define VIBRO_PIN            15
#define VIBRO_HAPTIC_MS      40    // short touch feedback duration (ms)
#define VIBRO_ALERT_MS       600   // long alert feedback duration (ms)

// ── Software Charging Detection (no extra GPIO needed) ───────────────────
// Method 1 — ESP32-S3 USB Serial/JTAG peripheral can sense VBUS presence
//             without any external wiring. Gives "USB physically plugged in".
// Method 2 — Battery voltage rising trend confirms the ETA6098 charger IC
//             is actively pushing current into the cell.
// Both are combined: USB present + voltage rising = charging.
// USB present + voltage flat near top = charge complete / trickle.
#define BAT_CHARGE_V_RISE_THRESHOLD  0.005f   // V rise per poll to count as charging
#define BAT_CHARGE_CONFIRM_SAMPLES   3        // consecutive rising samples needed
#define BAT_CHARGE_FULL_V            4.18f    // above this → charge complete, not rising

#define Measurement_offset           0.990476
#define EXAMPLE_BAT_TICK_PERIOD_MS   50

// Long-press timing (counted in PWR_Loop ticks, each tick = 50 ms)
// Keep tiers well separated so the user can clearly feel each stage.
#define Device_Sleep_Time    10    // 10 × 50 ms =  500 ms → screen off / on
#define Device_Restart_Time  60    // 60 × 50 ms = 3000 ms → restart  (hold 3 s)
#define Device_Shutdown_Time 100   // 100× 50 ms = 5000 ms → power off (hold 5 s)

// ── Public API ────────────────────────────────────────────────────────────
void PWR_Init(void);
void PWR_Loop(void);

void Fall_Asleep(void);
void Shutdown(void);
void Restart(void);

// Haptic helpers called from UI / alert code
void Vibro_Haptic_Touch(void);   // short buzz on every touch
void Vibro_Haptic_Alert(void);   // long buzz for alerts

// True when USB VBUS is physically present (plugged in), regardless of charge state.
// Detected via ESP32-S3 USB Serial/JTAG peripheral — no GPIO needed.
bool PWR_USB_Connected(void);

// True when USB is connected AND the charger is actively charging the battery,
// OR the battery is full and USB is still connected (shows charge symbol in UI).
// Drop-in replacement for the old hardware-LED version — same call site in UI_Main.
bool PWR_Is_Charging(void);

// Screen-sleep state
bool PWR_Screen_Is_Off(void);