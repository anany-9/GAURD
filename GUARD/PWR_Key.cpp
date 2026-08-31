#include "PWR_Key.h"
#include "BAT_Driver.h"          // BAT_analogVolts
#include "hal/usb_serial_jtag_ll.h"  // ESP32-S3 VBUS sense (no hardware pin needed)

// ── State ─────────────────────────────────────────────────────────────────
static uint8_t  BAT_State     = 0;
static uint8_t  Device_State  = 0;
static uint16_t Long_Press    = 0;
static bool     screen_is_off = false;

// ── Software charging detection state ────────────────────────────────────
static float    chg_v_prev         = 0.0f;
static uint8_t  chg_rise_count     = 0;    // consecutive rising samples
static bool     chg_active         = false; // latched charging state
static uint8_t  chg_sample_count   = 0;    // ignore first few noisy samples at boot

// ── VBUS / USB presence ───────────────────────────────────────────────────
// The ESP32-S3 USB Serial/JTAG peripheral keeps a status bit that reflects
// whether VBUS is present on the USB connector — no GPIO or resistor needed.
// usb_serial_jtag_ll_txfifo_writable() returns 1 only when a USB host/charger
// is connected and VBUS is above the detection threshold (~4 V).
bool PWR_USB_Connected(void) {
  return (usb_serial_jtag_ll_txfifo_writable() == 1);
}

// ── Combined charging detection ───────────────────────────────────────────
// Called once per second from UI_Main::ui_update_cb().
// Returns true  → show charging indicator in UI
// Returns false → battery discharging or USB not connected
bool PWR_Is_Charging(void) {
  bool usb_present = PWR_USB_Connected();

  if (!usb_present) {
    // USB unplugged — reset tracking so we get a clean reading when re-plugged
    chg_rise_count   = 0;
    chg_active       = false;
    chg_v_prev       = BAT_analogVolts;
    chg_sample_count = 0;
    return false;
  }

  // USB is present. Now determine if the ETA6098 is actively charging.
  float v_now = BAT_analogVolts;

  // Skip the first few samples after boot / plug-in to let ADC settle
  if (chg_sample_count < BAT_CHARGE_CONFIRM_SAMPLES) {
    chg_v_prev = v_now;
    chg_sample_count++;
    // While we're still settling, at least report USB is connected
    return true;
  }

  // Battery already full — charger goes to trickle / standby.
  // Voltage will be flat at the top. Still show "plugged in" indicator.
  if (v_now >= BAT_CHARGE_FULL_V) {
    chg_active     = true;   // plugged + full = show charge icon (done)
    chg_rise_count = 0;
    chg_v_prev     = v_now;
    return true;
  }

  // Count consecutive samples where voltage is measurably rising
  if (v_now > chg_v_prev + BAT_CHARGE_V_RISE_THRESHOLD) {
    if (chg_rise_count < 255) chg_rise_count++;
  } else {
    // Voltage flat or falling — decay the counter slowly so brief dips
    // (ADC noise, load spikes) don't immediately clear the charging flag
    if (chg_rise_count > 0) chg_rise_count--;
  }

  chg_v_prev = v_now;

  // Latch ON once we have enough rising samples; latch OFF only when
  // counter fully drains (hysteresis avoids rapid toggling in the UI)
  if (chg_rise_count >= BAT_CHARGE_CONFIRM_SAMPLES) {
    chg_active = true;
  } else if (chg_rise_count == 0) {
    chg_active = false;
  }

  return chg_active;
}

// ── Screen sleep / wake ───────────────────────────────────────────────────
bool PWR_Screen_Is_Off(void) {
  return screen_is_off;
}

void Fall_Asleep(void) {
  screen_is_off = true;
  LCD_Backlight  = 0;
  Set_Backlight(0);
}

static void Wake_Up(void) {
  screen_is_off = false;
  Set_Backlight(80);
}

// ── Power off ─────────────────────────────────────────────────────────────
void Shutdown(void) {
  digitalWrite(PWR_Control_PIN, LOW);
  LCD_Backlight = 0;
  Set_Backlight(0);
}

// ── Restart ───────────────────────────────────────────────────────────────
void Restart(void) {
  esp_restart();
}

// ── Vibration motor ───────────────────────────────────────────────────────
// Each helper spawns a minimal one-shot FreeRTOS task so the caller
// is never blocked (safe to call from LVGL touch callbacks or DriverTask).
void Vibro_Haptic_Touch(void) {
  struct Params { uint32_t ms; };
  static Params p_touch = { VIBRO_HAPTIC_MS };
  xTaskCreate([](void *arg) {
    uint32_t dur = ((Params *)arg)->ms;
    digitalWrite(VIBRO_PIN, HIGH);
    vTaskDelay(pdMS_TO_TICKS(dur));
    digitalWrite(VIBRO_PIN, LOW);
    vTaskDelete(NULL);
  }, "vib_t", 1024, &p_touch, 1, NULL);
}

void Vibro_Haptic_Alert(void) {
  struct Params { uint32_t ms; };
  static Params p_alert = { VIBRO_ALERT_MS };
  xTaskCreate([](void *arg) {
    uint32_t dur = ((Params *)arg)->ms;
    digitalWrite(VIBRO_PIN, HIGH);
    vTaskDelay(pdMS_TO_TICKS(dur));
    digitalWrite(VIBRO_PIN, LOW);
    vTaskDelete(NULL);
  }, "vib_a", 1024, &p_alert, 1, NULL);
}

// ── Main power-key loop (called every 50 ms from DriverTask) ─────────────
void PWR_Loop(void) {
  if (!BAT_State) return;

  bool key_pressed = !digitalRead(PWR_KEY_Input_PIN);  // active-LOW

  if (key_pressed) {
    if (BAT_State == 2) {
      Long_Press++;

      if (Long_Press >= Device_Shutdown_Time) {
        // ── ≥ 5 s held: hard power off — fires immediately while holding
        if (Device_State < 3) {
          Device_State = 3;
          Shutdown();
        }

      } else if (Long_Press >= Device_Restart_Time) {
        // ── ≥ 3 s held: mark for restart — only executes on release,
        //    so the user can keep holding to reach shutdown instead.
        if (Device_State < 2)
          Device_State = 2;

      } else if (Long_Press >= Device_Sleep_Time) {
        // ── ≥ 500 ms held: mark for screen toggle on release
        if (Device_State < 1)
          Device_State = 1;
      }
    }

  } else {
    // ── Key released ─────────────────────────────────────────────────────
    if (BAT_State == 1) {
      // First release after boot → fully arm the key handler
      BAT_State = 2;

    } else if (BAT_State == 2 && Long_Press > 0) {

      if (Device_State == 3) {
        // Shutdown already fired while holding — reset state so next press works
        Device_State = 0;

      } else if (Device_State == 2) {
        // ── Released inside restart window (3 s – 5 s): do restart ───────
        Device_State = 0;
        Restart();

      } else if (Long_Press < Device_Sleep_Time) {
        // ── Short press (< 500 ms): toggle screen on / off ───────────────
        // Device_State is 0 here — short taps never reach Device_Sleep_Time
        if (screen_is_off) Wake_Up();
        else               Fall_Asleep();
        Device_State = 0;

      } else if (Device_State == 1) {
        // ── Released inside sleep window (500 ms – 3 s): toggle screen ───
        if (screen_is_off) Wake_Up();
        else               Fall_Asleep();
        Device_State = 0;

      } else {
        Device_State = 0;
      }
    }

    Long_Press = 0;
  }
}

// ── Initialisation ────────────────────────────────────────────────────────
void PWR_Init(void) {
  // Power key & latch
  pinMode(PWR_KEY_Input_PIN, INPUT);
  pinMode(PWR_Control_PIN, OUTPUT);

  // Vibration motor — start LOW (off)
  pinMode(VIBRO_PIN, OUTPUT);
  digitalWrite(VIBRO_PIN, LOW);

  // No extra pinMode needed for charging detection —
  // VBUS sensing is handled entirely by the USB Serial/JTAG peripheral.

  // Latch power rail
  digitalWrite(PWR_Control_PIN, LOW);
  vTaskDelay(100);

  if (!digitalRead(PWR_KEY_Input_PIN)) {
    BAT_State = 1;
    digitalWrite(PWR_Control_PIN, HIGH);
  }
}