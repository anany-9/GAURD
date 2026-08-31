/*
 * UI_Main.cpp  — GUARD Device UI  (v13 — Haptics, Physical Charging LED, Screen-Off)
 */

#include "UI_Main.h"
#include "BAT_Driver.h"
#include "Gyro_QMI8658.h"
#include "Display_ST7789.h"
#include "RTC_PCF85063.h"
#include "Audio_PCM5101.h"
#include "PWR_Key.h"          // ← Vibro_Haptic_Touch / Vibro_Haptic_Alert / PWR_Is_Charging
#include <WiFi.h>
#include <WebServer.h>
#include <BLEDevice.h>
#include <BLEAdvertising.h>
#include <BLEUtils.h>
#include <ArduinoJson.h>
#include <esp_timer.h>
#include <FFat.h>
#include <FS.h>

// ── Driver native resolution (landscape, MADCTL=0x60) ────────────────────
#define UI_W  320
#define UI_H  240

// ── WiFi ──────────────────────────────────────────────────────────────────
#define WIFI_SSID  "Galaxy M33 5G 4B28"
#define WIFI_PASS  "sssv0259"

// ── iBeacon ───────────────────────────────────────────────────────────────
#define IBEACON_MAJOR  1217
#define IBEACON_MINOR  23
static const uint8_t IBEACON_UUID[16] = {
  0xE2,0xC5,0x6D,0xB5,0xDF,0xFB,0x48,0xD2,
  0xB0,0x60,0xD0,0xF5,0xA7,0x10,0x96,0xE0
};

#define MAX_TASKS   10
#define MAX_NOTIFS  10

enum TaskPriority : uint8_t {
  PRIORITY_LOW    = 0,
  PRIORITY_NORMAL = 1,
  PRIORITY_HIGH   = 2
};

static const char *PRIORITY_NAMES[] = { "LOW", "NORMAL", "HIGH" };

struct Task {
  char title[64];
  char desc[128];
  TaskPriority priority;
  bool completed;
  bool pendingApproval;
  bool pendingSkip;
  bool approved;
  bool skipped;
};

struct Notification {
  char title[64];
  char body[128];
  bool isAlert;
  bool dismissed;
};

static Task         tasks[MAX_TASKS];
static int          taskCount    = 0;
static Notification notifs[MAX_NOTIFS];
static int          notifCount   = 0;
static int          unreadNotifs = 0;

static char worker_name[64] = "Worker";
static uint8_t ui_volume     = 50;
static uint8_t ui_brightness = 80;

// ── LVGL handles ──────────────────────────────────────────────────────────
static lv_obj_t *lbl_worker_name;
static lv_obj_t *lbl_wifi_status;
static lv_obj_t *lbl_ip;
static lv_obj_t *lbl_time_large;
static lv_obj_t *lbl_date_large;
static lv_obj_t *lbl_battery;
static lv_obj_t *lbl_bat_charging;
static lv_obj_t *bar_battery;
static lv_obj_t *lbl_imu_accel;
static lv_obj_t *lbl_imu_gyro;
static lv_obj_t *lbl_uptime;
static lv_obj_t *lbl_step_count;
static lv_obj_t *task_list;
static lv_obj_t *lbl_task_count;
static lv_obj_t *notif_list;
static lv_obj_t *lbl_notif_badge;
static lv_obj_t *popup_box  = NULL;
static lv_obj_t *lbl_fall_status = nullptr;
static lv_obj_t *sos_modal  = NULL;

static lv_obj_t *audio_modal    = NULL;
static lv_obj_t *audio_bar      = NULL;
static lv_obj_t *lbl_audio_time = NULL;
static lv_timer_t *audio_timer  = NULL;

static lv_obj_t *slider_brightness  = NULL;
static lv_obj_t *slider_volume      = NULL;
static lv_obj_t *lbl_brightness_val = NULL;
static lv_obj_t *lbl_volume_val     = NULL;

static WebServer          server(80);
static BLEAdvertising    *pAdvertising = nullptr;
static SemaphoreHandle_t  ui_mutex;

// ── FFat File Tracking ────────────────────────────────────────────────────
static File               uploadFile;
static uint32_t           total_bytes_written = 0;
static const char* AUDIO_FILE_PATH = "/msg.mp3";

// ── Forward Declarations ──────────────────────────────────────────────────
static void refresh_task_list();
static void refresh_notif_list();
static void show_request_sent_popup(const char *title, bool isSkip);
static void brightness_changed_cb(lv_event_t *e);
static void volume_changed_cb(lv_event_t *e);
static void show_audio_player_modal();
void NetworkTask(void *param);

// ── Fall / Accident & Step Detection ─────────────────────────────────────
#define FREEFALL_THRESHOLD   0.4f
#define IMPACT_THRESHOLD     2.8f
#define FREEFALL_MIN_MS      80
#define IMPACT_WINDOW_MS     600
#define FALL_COOLDOWN_MS     15000

#define STEP_THRESHOLD_HIGH  1.15f
#define STEP_THRESHOLD_LOW   0.95f
#define STEP_COOLDOWN_MS     250

static bool     fall_in_freefall       = false;
static uint32_t fall_freefall_start_ms = 0;
static bool     fall_awaiting_impact   = false;
static uint32_t fall_impact_window_ms  = 0;
static uint32_t fall_last_alert_ms     = 0;
static bool     fall_detected          = false;
static uint32_t fall_detected_time_ms  = 0;

static uint32_t step_count             = 0;
static uint32_t last_step_time_ms      = 0;
static float    mag_avg                = 1.0f;
static bool     step_is_high           = false;

static bool     help_alert_pending     = false;
static uint32_t help_alert_time_ms     = 0;
#define HELP_COOLDOWN_MS  30000

// ─────────────────────────────────────────────────────────────────────────
// ── Haptic touch event – attached to every interactive widget ─────────────
// ─────────────────────────────────────────────────────────────────────────
static void haptic_touch_cb(lv_event_t *e) {
  (void)e;
  Vibro_Haptic_Touch();
}

// Helper: add haptic feedback to any clickable object
static void add_haptic(lv_obj_t *obj) {
  lv_obj_add_event_cb(obj, haptic_touch_cb, LV_EVENT_PRESSED, NULL);
}

// ── Alert haptic + speaker helper ────────────────────────────────────────
// Call this whenever a real alert (fall, SOS, incoming alert notification)
// needs the long vibration AND a short audio tone.
static void trigger_alert_haptic() {
  Vibro_Haptic_Alert();
  // Play a short built-in beep via the PCM5101 DAC.
  // Volume_adjustment and audio are already initialised in UI_Init.
  // We use a 1-second pre-loaded tone file if it exists, otherwise skip audio.
  if (FFat.exists("/alert.mp3")) {
    uint8_t vol = (uint8_t)(ui_volume * Volume_MAX / 100);
    Volume_adjustment(vol);
    audio.connecttoFS(FFat, "/alert.mp3");
  }
}

// ─────────────────────────────────────────────────────────────────────────
// Battery helpers
// ─────────────────────────────────────────────────────────────────────────
static int bat_percent() {
  float v = BAT_analogVolts;
  if (v >= 4.20f) return 100;
  if (v >= 4.00f) return (int)(87  + (v - 4.00f) / 0.20f * 13.0f);
  if (v >= 3.80f) return (int)(70  + (v - 3.80f) / 0.20f * 17.0f);
  if (v >= 3.60f) return (int)(45  + (v - 3.60f) / 0.20f * 25.0f);
  if (v >= 3.40f) return (int)(20  + (v - 3.40f) / 0.20f * 25.0f);
  if (v >= 3.20f) return (int)(8   + (v - 3.20f) / 0.20f * 12.0f);
  if (v >= 3.00f) return (int)(0   + (v - 3.00f) / 0.20f * 8.0f);
  return 0;
}

// bat_is_charging is now driven by the physical LED pin via PWR_Is_Charging()
// rather than voltage delta inference.
static bool bat_is_charging = false;

static void card_style(lv_obj_t *obj, lv_color_t bg) {
  lv_obj_set_style_bg_color(obj, bg, 0);
  lv_obj_set_style_bg_opa(obj, LV_OPA_COVER, 0);
  lv_obj_set_style_border_width(obj, 0, 0);
  lv_obj_set_style_radius(obj, 10, 0);
  lv_obj_set_style_pad_all(obj, 8, 0);
  lv_obj_clear_flag(obj, LV_OBJ_FLAG_SCROLLABLE);
}

static void card_accent_line(lv_obj_t *card, uint32_t hex_color = 0x0D9488) {
  lv_obj_set_style_border_color(card, lv_color_hex(hex_color), 0);
  lv_obj_set_style_border_width(card, 3, 0);
  lv_obj_set_style_border_side(card, LV_BORDER_SIDE_LEFT, 0);
}

static void start_ibeacon() {
  BLEDevice::init("ESP32-S3-Device");
  pAdvertising = BLEDevice::getAdvertising();
  uint8_t d[25];
  d[0] = 0x4C; d[1] = 0x00; d[2] = 0x02; d[3] = 0x15;
  memcpy(&d[4], IBEACON_UUID, 16);
  d[20] = (IBEACON_MAJOR >> 8) & 0xFF;
  d[21] =  IBEACON_MAJOR       & 0xFF;
  d[22] = (IBEACON_MINOR >> 8) & 0xFF;
  d[23] =  IBEACON_MINOR       & 0xFF;
  d[24] = 0xC5;
  BLEAdvertisementData adv;
  adv.setFlags(0x04);
  adv.setManufacturerData(String((char*)d, 25));
  pAdvertising->setAdvertisementData(adv);
  pAdvertising->setScanResponse(false);
  pAdvertising->start();
}

static void close_popup_cb(lv_event_t *) {
  if (popup_box) { lv_obj_del(popup_box); popup_box = NULL; }
}

static void show_popup(const char *title, const char *body, bool isAlert) {
  if (popup_box) { lv_obj_del(popup_box); popup_box = NULL; }
  popup_box = lv_obj_create(lv_layer_top());
  lv_obj_set_size(popup_box, 300, 80);
  lv_obj_align(popup_box, LV_ALIGN_TOP_MID, 0, 4);
  lv_obj_set_style_radius(popup_box, 12, 0);
  lv_obj_set_style_border_width(popup_box, 2, 0);
  lv_obj_set_style_shadow_width(popup_box, 12, 0);
  lv_obj_set_style_shadow_opa(popup_box, LV_OPA_30, 0);
  lv_obj_set_style_pad_all(popup_box, 8, 0);
  lv_obj_set_style_pad_row(popup_box, 4, 0);
  lv_obj_set_flex_flow(popup_box, LV_FLEX_FLOW_COLUMN);
  lv_obj_clear_flag(popup_box, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_set_style_bg_color(popup_box, isAlert ? lv_color_hex(0xFFF3CD) : lv_color_hex(0xE8F8F5), 0);
  lv_obj_set_style_border_color(popup_box, isAlert ? lv_color_hex(0xF59E0B) : lv_color_hex(0x0D9488), 0);

  lv_obj_t *row = lv_obj_create(popup_box);
  lv_obj_set_size(row, lv_pct(100), LV_SIZE_CONTENT);
  lv_obj_set_style_bg_opa(row, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(row, 0, 0);
  lv_obj_set_style_pad_all(row, 0, 0);
  lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
  lv_obj_set_flex_align(row, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);

  lv_obj_t *lt = lv_label_create(row);
  lv_label_set_text_fmt(lt, "%s %s", isAlert ? LV_SYMBOL_WARNING : LV_SYMBOL_BELL, title);
  lv_obj_set_style_text_font(lt, &lv_font_montserrat_12, 0);
  lv_obj_set_style_text_color(lt, lv_color_hex(0x1A1A2E), 0);

  lv_obj_t *btn = lv_btn_create(row);
  lv_obj_set_size(btn, 22, 18);
  lv_obj_set_style_bg_color(btn, lv_color_hex(0xCCCCCC), 0);
  lv_obj_set_style_radius(btn, 4, 0);
  lv_obj_t *xlbl = lv_label_create(btn);
  lv_label_set_text(xlbl, LV_SYMBOL_CLOSE);
  lv_obj_set_style_text_font(xlbl, &lv_font_montserrat_10, 0);
  lv_obj_center(xlbl);
  lv_obj_add_event_cb(btn, close_popup_cb, LV_EVENT_CLICKED, NULL);
  add_haptic(btn);    // haptic on close button

  lv_obj_t *lb = lv_label_create(popup_box);
  lv_label_set_text(lb, body);
  lv_label_set_long_mode(lb, LV_LABEL_LONG_WRAP);
  lv_obj_set_width(lb, lv_pct(100));
  lv_obj_set_style_text_font(lb, &lv_font_montserrat_10, 0);
  lv_obj_set_style_text_color(lb, lv_color_hex(0x444444), 0);

  // ── If this is an alert, fire haptic + audio ──────────────────────────
  if (isAlert) {
    trigger_alert_haptic();
  }

  lv_timer_create([](lv_timer_t *tmr) {
    if (popup_box) { lv_obj_del(popup_box); popup_box = NULL; }
    lv_timer_del(tmr);
  }, 5000, NULL);
}

void FallDetection_Loop() {
  uint32_t now = millis();
  float ax = Accel.x, ay = Accel.y, az = Accel.z;
  float mag = sqrtf(ax * ax + ay * ay + az * az);

  mag_avg = (mag_avg * 0.8f) + (mag * 0.2f);

  if (mag_avg > STEP_THRESHOLD_HIGH && !step_is_high) {
    step_is_high = true;
  } else if (mag_avg < STEP_THRESHOLD_LOW && step_is_high) {
    step_is_high = false;
    if (now - last_step_time_ms > STEP_COOLDOWN_MS) {
      step_count++;
      last_step_time_ms = now;
    }
  }

  if (now - fall_last_alert_ms < FALL_COOLDOWN_MS && fall_last_alert_ms != 0) {
    fall_in_freefall = false;
    fall_awaiting_impact = false;
    return;
  }

  if (!fall_awaiting_impact) {
    if (mag < FREEFALL_THRESHOLD) {
      if (!fall_in_freefall) {
        fall_in_freefall = true;
        fall_freefall_start_ms = now;
      } else if ((now - fall_freefall_start_ms) >= FREEFALL_MIN_MS) {
        fall_in_freefall     = false;
        fall_awaiting_impact  = true;
        fall_impact_window_ms = now;
      }
    } else {
      fall_in_freefall = false;
    }
  }

  if (fall_awaiting_impact) {
    if ((now - fall_impact_window_ms) > IMPACT_WINDOW_MS) {
      fall_awaiting_impact = false;
    } else if (mag > IMPACT_THRESHOLD) {
      fall_awaiting_impact  = false;
      fall_last_alert_ms    = now;
      fall_detected         = true;
      fall_detected_time_ms = now;
      lv_async_call([](void *) {
        xSemaphoreTake(ui_mutex, portMAX_DELAY);
        if (notifCount >= MAX_NOTIFS) {
          memmove(&notifs[0], &notifs[1], sizeof(Notification) * (MAX_NOTIFS - 1));
          notifCount = MAX_NOTIFS - 1;
        }
        int ni = notifCount++;
        strlcpy(notifs[ni].title, "ACCIDENT DETECTED", sizeof(notifs[ni].title));
        strlcpy(notifs[ni].body,  "Possible fall or collision! Check on the user.", sizeof(notifs[ni].body));
        notifs[ni].isAlert   = true;
        notifs[ni].dismissed = false;
        unreadNotifs++;
        xSemaphoreGive(ui_mutex);
        if (lbl_fall_status) {
          lv_label_set_text(lbl_fall_status, LV_SYMBOL_WARNING " FALL!");
          lv_obj_set_style_text_color(lbl_fall_status, lv_color_hex(0xDC2626), 0);
        }
        refresh_notif_list();
        show_popup("ACCIDENT DETECTED", "Possible fall or collision!", true);
        // show_popup already calls trigger_alert_haptic() for isAlert=true
      }, NULL);
    }
  }
}


// ─────────────────────────────────────────────────────────────────────────
// AUDIO PLAYER OVERLAY LOGIC (Strict FFat Version)
// ─────────────────────────────────────────────────────────────────────────

static void play_voice_message() {
    Serial.println("\n[DEBUG] Audio Player: Play button pressed.");

    if(audio.isRunning()) {
        Serial.println("[DEBUG] Audio is currently running. Stopping...");
        audio.stopSong();
    }

    File f = FFat.open(AUDIO_FILE_PATH, FILE_READ);
    if(!f || f.size() == 0) {
        Serial.println("[DEBUG] Audio Player Error: FFat File missing or 0 bytes!");
        if(lbl_audio_time) lv_label_set_text(lbl_audio_time, "Error: No File");
        if(f) f.close();
        return;
    }
    Serial.printf("[DEBUG] UI Audio Replay: Valid File found -> %s (%u bytes)\n", AUDIO_FILE_PATH, f.size());
    f.close();

    uint8_t mapped_vol = (uint8_t)(ui_volume * Volume_MAX / 100);
    Volume_adjustment(mapped_vol);

    bool ret = audio.connecttoFS(FFat, AUDIO_FILE_PATH);

    if(ret) {
        Serial.println("[DEBUG] Audio Player: Decoding started Successfully.");
        if(lbl_audio_time) lv_label_set_text(lbl_audio_time, "Playing...");
    } else {
        Serial.println("[DEBUG] Audio Player: Failed to Decode! (Unsupported format?)");
        if(lbl_audio_time) lv_label_set_text(lbl_audio_time, "Error: Decode Fail");
    }
}

static void audio_timer_cb(lv_timer_t * t) {
    if(audio.isRunning()) {
        uint32_t current = audio.getAudioCurrentTime();
        uint32_t total = audio.getAudioFileDuration();

        if(total > 0 && audio_bar && lbl_audio_time) {
            lv_bar_set_value(audio_bar, (current * 100) / total, LV_ANIM_ON);
            char buf[32];
            snprintf(buf, sizeof(buf), "%02d:%02d / %02d:%02d", current/60, current%60, total/60, total%60);
            lv_label_set_text(lbl_audio_time, buf);
        }
    } else {
        if(audio_bar) lv_bar_set_value(audio_bar, 100, LV_ANIM_OFF);
        if(lbl_audio_time && strncmp(lv_label_get_text(lbl_audio_time), "Error", 5) != 0) {
            lv_label_set_text(lbl_audio_time, "Finished");
        }
    }
}

static void btn_audio_play_cb(lv_event_t * e) {
    play_voice_message();
}

static void btn_audio_close_cb(lv_event_t * e) {
    if(audio.isRunning()) audio.stopSong();
    if(audio_timer) { lv_timer_del(audio_timer); audio_timer = NULL; }
    if(audio_modal) { lv_obj_del(audio_modal); audio_modal = NULL; }
    FFat.remove(AUDIO_FILE_PATH);
    Serial.println("[DEBUG] Audio Modal closed and file deleted.");
}

static void show_audio_player_modal() {
    if(audio_modal) return;

    audio_modal = lv_obj_create(lv_layer_top());
    lv_obj_set_size(audio_modal, lv_pct(100), lv_pct(100));
    lv_obj_set_style_bg_color(audio_modal, lv_color_hex(0x000000), 0);
    lv_obj_set_style_bg_opa(audio_modal, LV_OPA_70, 0);
    lv_obj_set_style_border_width(audio_modal, 0, 0);
    lv_obj_set_style_radius(audio_modal, 0, 0);
    lv_obj_clear_flag(audio_modal, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t * card = lv_obj_create(audio_modal);
    lv_obj_set_size(card, 260, 160);
    lv_obj_center(card);
    lv_obj_set_style_radius(card, 12, 0);
    lv_obj_set_style_border_color(card, lv_color_hex(0x0D9488), 0);
    lv_obj_set_style_border_width(card, 3, 0);
    lv_obj_set_flex_flow(card, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(card, LV_FLEX_ALIGN_SPACE_EVENLY, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t * title = lv_label_create(card);
    lv_label_set_text(title, LV_SYMBOL_AUDIO " VOICE MESSAGE");
    lv_obj_set_style_text_font(title, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_color(title, lv_color_hex(0x0D9488), 0);

    audio_bar = lv_bar_create(card);
    lv_obj_set_size(audio_bar, 220, 12);
    lv_bar_set_range(audio_bar, 0, 100);
    lv_bar_set_value(audio_bar, 0, LV_ANIM_OFF);
    lv_obj_set_style_bg_color(audio_bar, lv_color_hex(0xCCFBF1), LV_PART_MAIN);
    lv_obj_set_style_bg_color(audio_bar, lv_color_hex(0x0D9488), LV_PART_INDICATOR);

    lbl_audio_time = lv_label_create(card);
    lv_label_set_text(lbl_audio_time, "Loading...");
    lv_obj_set_style_text_font(lbl_audio_time, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_color(lbl_audio_time, lv_color_hex(0x475569), 0);

    lv_obj_t * btn_row = lv_obj_create(card);
    lv_obj_set_size(btn_row, lv_pct(100), LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(btn_row, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(btn_row, 0, 0);
    lv_obj_set_flex_flow(btn_row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(btn_row, LV_FLEX_ALIGN_SPACE_EVENLY, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_all(btn_row, 0, 0);

    lv_obj_t * btn_play = lv_btn_create(btn_row);
    lv_obj_set_size(btn_play, 100, 36);
    lv_obj_set_style_bg_color(btn_play, lv_color_hex(0x0D9488), 0);
    lv_obj_t * lbl_play = lv_label_create(btn_play);
    lv_label_set_text(lbl_play, LV_SYMBOL_PLAY " REPLAY");
    lv_obj_center(lbl_play);
    lv_obj_add_event_cb(btn_play, btn_audio_play_cb, LV_EVENT_CLICKED, NULL);
    add_haptic(btn_play);

    lv_obj_t * btn_close = lv_btn_create(btn_row);
    lv_obj_set_size(btn_close, 100, 36);
    lv_obj_set_style_bg_color(btn_close, lv_color_hex(0xEF4444), 0);
    lv_obj_t * lbl_close = lv_label_create(btn_close);
    lv_label_set_text(lbl_close, LV_SYMBOL_CLOSE " CLOSE");
    lv_obj_center(lbl_close);
    lv_obj_add_event_cb(btn_close, btn_audio_close_cb, LV_EVENT_CLICKED, NULL);
    add_haptic(btn_close);

    audio_timer = lv_timer_create(audio_timer_cb, 500, NULL);
    play_voice_message();
}

// ─────────────────────────────────────────────────────────────────────────
// SOS Confirmation Logic
// ─────────────────────────────────────────────────────────────────────────
static void close_sos_modal_cb(lv_event_t *) {
    if(sos_modal) {
        lv_obj_del(sos_modal);
        sos_modal = NULL;
    }
}

static void execute_sos_cb(lv_event_t *) {
    if(sos_modal) {
        lv_obj_del(sos_modal);
        sos_modal = NULL;
    }

    uint32_t now = millis();
    if (now - help_alert_time_ms < HELP_COOLDOWN_MS && help_alert_time_ms != 0) {
        show_popup("Help", "Alert already sent. Please wait.", false);
        return;
    }

    xSemaphoreTake(ui_mutex, portMAX_DELAY);
    help_alert_pending  = true;
    help_alert_time_ms  = now;
    if (notifCount < MAX_NOTIFS) {
        int ni = notifCount++;
        strlcpy(notifs[ni].title, "HELP REQUESTED", sizeof(notifs[ni].title));
        strlcpy(notifs[ni].body,  "Worker has requested emergency assistance!", sizeof(notifs[ni].body));
        notifs[ni].isAlert   = true;
        notifs[ni].dismissed = false;
        unreadNotifs++;
    }
    xSemaphoreGive(ui_mutex);

    lv_async_call([](void *) {
        refresh_notif_list();
        show_popup("HELP SENT", "Emergency alert sent to manager!", true);
        // show_popup already fires trigger_alert_haptic() for isAlert=true
    }, NULL);
}

static void help_btn_cb(lv_event_t *) {
    if(sos_modal) return;

    sos_modal = lv_obj_create(lv_layer_top());
    lv_obj_set_size(sos_modal, lv_pct(100), lv_pct(100));
    lv_obj_set_style_bg_color(sos_modal, lv_color_hex(0x000000), 0);
    lv_obj_set_style_bg_opa(sos_modal, LV_OPA_60, 0);
    lv_obj_set_style_border_width(sos_modal, 0, 0);
    lv_obj_set_style_radius(sos_modal, 0, 0);
    lv_obj_clear_flag(sos_modal, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t * card = lv_obj_create(sos_modal);
    lv_obj_set_size(card, 250, 130);
    lv_obj_center(card);
    lv_obj_set_style_radius(card, 12, 0);
    lv_obj_set_style_border_color(card, lv_color_hex(0xDC2626), 0);
    lv_obj_set_style_border_width(card, 3, 0);
    lv_obj_set_flex_flow(card, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(card, LV_FLEX_ALIGN_SPACE_EVENLY, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t * lbl_title = lv_label_create(card);
    lv_label_set_text(lbl_title, LV_SYMBOL_WARNING " CONFIRM EMERGENCY");
    lv_obj_set_style_text_font(lbl_title, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_color(lbl_title, lv_color_hex(0xDC2626), 0);

    lv_obj_t * lbl_desc = lv_label_create(card);
    lv_label_set_text(lbl_desc, "Send SOS alert to Manager?");
    lv_obj_set_style_text_font(lbl_desc, &lv_font_montserrat_12, 0);

    lv_obj_t * btn_row = lv_obj_create(card);
    lv_obj_set_size(btn_row, lv_pct(100), LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(btn_row, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(btn_row, 0, 0);
    lv_obj_set_flex_flow(btn_row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(btn_row, LV_FLEX_ALIGN_SPACE_EVENLY, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_all(btn_row, 0, 0);

    lv_obj_t * btn_cancel = lv_btn_create(btn_row);
    lv_obj_set_size(btn_cancel, 90, 36);
    lv_obj_set_style_bg_color(btn_cancel, lv_color_hex(0x94A3B8), 0);
    lv_obj_t * lbl_cancel = lv_label_create(btn_cancel);
    lv_label_set_text(lbl_cancel, "Cancel");
    lv_obj_center(lbl_cancel);
    lv_obj_add_event_cb(btn_cancel, close_sos_modal_cb, LV_EVENT_CLICKED, NULL);
    add_haptic(btn_cancel);

    lv_obj_t * btn_send = lv_btn_create(btn_row);
    lv_obj_set_size(btn_send, 95, 36);
    lv_obj_set_style_bg_color(btn_send, lv_color_hex(0xDC2626), 0);
    lv_obj_t * lbl_send = lv_label_create(btn_send);
    lv_label_set_text(lbl_send, "SEND SOS");
    lv_obj_center(lbl_send);
    lv_obj_add_event_cb(btn_send, execute_sos_cb, LV_EVENT_CLICKED, NULL);
    add_haptic(btn_send);
}

// ─────────────────────────────────────────────────────────────────────────
// 1-second timer UI Updates
// ─────────────────────────────────────────────────────────────────────────
static void ui_update_cb(lv_timer_t *) {
  char buf[48];

  // ── Use physical charging LED pin instead of voltage inference ──────────
  bat_is_charging = PWR_Is_Charging();
  int pct = bat_percent();

  if (bat_is_charging) {
    snprintf(buf, sizeof(buf), "%d%% " LV_SYMBOL_CHARGE, pct);
    lv_label_set_text(lbl_bat_charging, "Charging");
    lv_obj_set_style_text_color(lbl_bat_charging, lv_color_hex(0x0D9488), 0);
  } else {
    snprintf(buf, sizeof(buf), "%d%%", pct);
    lv_label_set_text(lbl_bat_charging, "");
  }
  lv_label_set_text(lbl_battery, buf);
  lv_bar_set_value(bar_battery, pct, LV_ANIM_ON);

  lv_color_t bat_col = (pct < 20) ? lv_color_hex(0xDC2626) :
                       (pct < 50) ? lv_color_hex(0xF59E0B) :
                                    lv_color_hex(0x059669);
  lv_obj_set_style_bg_color(bar_battery, bat_col, LV_PART_INDICATOR);
  lv_obj_set_style_text_color(lbl_battery, bat_col, 0);

  snprintf(buf, sizeof(buf), "%lu", step_count);
  lv_label_set_text(lbl_step_count, buf);

  snprintf(buf, sizeof(buf), "A %.1f %.1f %.1f", Accel.x, Accel.y, Accel.z);
  lv_label_set_text(lbl_imu_accel, buf);
  snprintf(buf, sizeof(buf), "G %.0f %.0f %.0f", Gyro.x, Gyro.y, Gyro.z);
  lv_label_set_text(lbl_imu_gyro, buf);

  unsigned long s = millis() / 1000;
  unsigned long h = s / 3600; s %= 3600;
  unsigned long m = s / 60;   s %= 60;
  snprintf(buf, sizeof(buf), "UPTIME: %02lu:%02lu:%02lu", h, m, s);
  lv_label_set_text(lbl_uptime, buf);

  static const char *DOW[] = {"Sun","Mon","Tue","Wed","Thu","Fri","Sat"};
  uint8_t dow = datetime.dotw < 7 ? datetime.dotw : 0;

  snprintf(buf, sizeof(buf), "%02d:%02d:%02d", datetime.hour, datetime.minute, datetime.second);
  lv_label_set_text(lbl_time_large, buf);

  snprintf(buf, sizeof(buf), "%s %02d/%02d/%04d", DOW[dow], datetime.day, datetime.month, datetime.year);
  lv_label_set_text(lbl_date_large, buf);
}

// ─────────────────────────────────────────────────────────────────────────
// Dashboard tab
// ─────────────────────────────────────────────────────────────────────────
static void build_dashboard(lv_obj_t *parent) {
  lv_obj_set_style_pad_all(parent, 6, 0);
  lv_obj_set_style_pad_row(parent, 5, 0);
  lv_obj_set_flex_flow(parent, LV_FLEX_FLOW_COLUMN);
  lv_obj_set_style_bg_color(parent, lv_color_hex(0xF8FAFC), 0);
  lv_obj_clear_flag(parent, LV_OBJ_FLAG_SCROLLABLE);

  lv_obj_t *topbar = lv_obj_create(parent);
  lv_obj_set_size(topbar, lv_pct(100), 24);
  lv_obj_set_style_bg_color(topbar, lv_color_hex(0x0F172A), 0);
  lv_obj_set_style_bg_opa(topbar, LV_OPA_COVER, 0);
  lv_obj_set_style_border_width(topbar, 0, 0);
  lv_obj_set_style_radius(topbar, 6, 0);
  lv_obj_set_style_pad_hor(topbar, 8, 0);
  lv_obj_set_style_pad_ver(topbar, 0, 0);
  lv_obj_set_flex_flow(topbar, LV_FLEX_FLOW_ROW);
  lv_obj_set_flex_align(topbar, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_clear_flag(topbar, LV_OBJ_FLAG_SCROLLABLE);

  lbl_worker_name = lv_label_create(topbar);
  lv_label_set_text(lbl_worker_name, worker_name);
  lv_obj_set_style_text_font(lbl_worker_name, &lv_font_montserrat_12, 0);
  lv_obj_set_style_text_color(lbl_worker_name, lv_color_hex(0x5EEAD4), 0);

  lv_obj_t *wrow = lv_obj_create(topbar);
  lv_obj_set_size(wrow, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
  lv_obj_set_style_bg_opa(wrow, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(wrow, 0, 0);
  lv_obj_set_style_pad_all(wrow, 0, 0);
  lv_obj_set_flex_flow(wrow, LV_FLEX_FLOW_ROW);
  lv_obj_set_style_pad_column(wrow, 6, 0);
  lv_obj_clear_flag(wrow, LV_OBJ_FLAG_SCROLLABLE);

  lbl_wifi_status = lv_label_create(wrow);
  lv_label_set_text(lbl_wifi_status, LV_SYMBOL_WIFI " ...");
  lv_obj_set_style_text_font(lbl_wifi_status, &lv_font_montserrat_10, 0);
  lv_obj_set_style_text_color(lbl_wifi_status, lv_color_hex(0x94A3B8), 0);

  lbl_ip = lv_label_create(wrow);
  lv_label_set_text(lbl_ip, "---");
  lv_obj_set_style_text_font(lbl_ip, &lv_font_montserrat_10, 0);
  lv_obj_set_style_text_color(lbl_ip, lv_color_hex(0x64748B), 0);

  lv_obj_t *cols = lv_obj_create(parent);
  lv_obj_set_size(cols, lv_pct(100), LV_SIZE_CONTENT);
  lv_obj_set_flex_grow(cols, 1);
  lv_obj_set_style_bg_opa(cols, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(cols, 0, 0);
  lv_obj_set_style_pad_all(cols, 0, 0);
  lv_obj_set_flex_flow(cols, LV_FLEX_FLOW_ROW);
  lv_obj_set_style_pad_column(cols, 6, 0);
  lv_obj_clear_flag(cols, LV_OBJ_FLAG_SCROLLABLE);

  lv_obj_t *left = lv_obj_create(cols);
  lv_obj_set_flex_grow(left, 1);
  lv_obj_set_height(left, LV_SIZE_CONTENT);
  lv_obj_set_style_bg_opa(left, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(left, 0, 0);
  lv_obj_set_style_pad_all(left, 0, 0);
  lv_obj_set_flex_flow(left, LV_FLEX_FLOW_COLUMN);
  lv_obj_set_style_pad_row(left, 6, 0);
  lv_obj_clear_flag(left, LV_OBJ_FLAG_SCROLLABLE);

  lv_obj_t *time_card = lv_obj_create(left);
  lv_obj_set_size(time_card, lv_pct(100), LV_SIZE_CONTENT);
  card_style(time_card, lv_color_hex(0x0D9488));
  lv_obj_set_style_pad_all(time_card, 10, 0);
  lv_obj_set_flex_flow(time_card, LV_FLEX_FLOW_COLUMN);

  lbl_time_large = lv_label_create(time_card);
  lv_label_set_text(lbl_time_large, "--:--:--");
  lv_obj_set_style_text_font(lbl_time_large, &lv_font_montserrat_20, 0);
  lv_obj_set_style_text_color(lbl_time_large, lv_color_hex(0xFFFFFF), 0);

  lbl_date_large = lv_label_create(time_card);
  lv_label_set_text(lbl_date_large, "--/--/----");
  lv_obj_set_style_text_font(lbl_date_large, &lv_font_montserrat_12, 0);
  lv_obj_set_style_text_color(lbl_date_large, lv_color_hex(0xCCFBF1), 0);

  lv_obj_t *step_card = lv_obj_create(left);
  lv_obj_set_size(step_card, lv_pct(100), LV_SIZE_CONTENT);
  card_style(step_card, lv_color_hex(0xFFFBEB));
  card_accent_line(step_card, 0xF59E0B);
  lv_obj_set_flex_flow(step_card, LV_FLEX_FLOW_COLUMN);
  lv_obj_set_style_pad_row(step_card, 2, 0);

  lv_obj_t *step_hdr = lv_label_create(step_card);
  lv_label_set_text(step_hdr, "STEPS");
  lv_obj_set_style_text_font(step_hdr, &lv_font_montserrat_10, 0);
  lv_obj_set_style_text_color(step_hdr, lv_color_hex(0xD97706), 0);

  lbl_step_count = lv_label_create(step_card);
  lv_label_set_text(lbl_step_count, "0");
  lv_obj_set_style_text_font(lbl_step_count, &lv_font_montserrat_16, 0);
  lv_obj_set_style_text_color(lbl_step_count, lv_color_hex(0xB45309), 0);

  lv_obj_t *right = lv_obj_create(cols);
  lv_obj_set_flex_grow(right, 1);
  lv_obj_set_height(right, LV_SIZE_CONTENT);
  lv_obj_set_style_bg_opa(right, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(right, 0, 0);
  lv_obj_set_style_pad_all(right, 0, 0);
  lv_obj_set_flex_flow(right, LV_FLEX_FLOW_COLUMN);
  lv_obj_set_style_pad_row(right, 6, 0);
  lv_obj_clear_flag(right, LV_OBJ_FLAG_SCROLLABLE);

  lv_obj_t *bat = lv_obj_create(right);
  lv_obj_set_size(bat, lv_pct(100), LV_SIZE_CONTENT);
  card_style(bat, lv_color_hex(0xF0FDF9));
  card_accent_line(bat, 0x059669);
  lv_obj_set_style_pad_row(bat, 3, 0);
  lv_obj_set_flex_flow(bat, LV_FLEX_FLOW_COLUMN);

  lv_obj_t *bat_hdr = lv_obj_create(bat);
  lv_obj_set_size(bat_hdr, lv_pct(100), LV_SIZE_CONTENT);
  lv_obj_set_style_bg_opa(bat_hdr, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(bat_hdr, 0, 0);
  lv_obj_set_style_pad_all(bat_hdr, 0, 0);
  lv_obj_set_flex_flow(bat_hdr, LV_FLEX_FLOW_ROW);
  lv_obj_set_flex_align(bat_hdr, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_clear_flag(bat_hdr, LV_OBJ_FLAG_SCROLLABLE);

  lv_obj_t *bat_icon = lv_label_create(bat_hdr);
  lv_label_set_text(bat_icon, LV_SYMBOL_BATTERY_FULL " BAT");
  lv_obj_set_style_text_font(bat_icon, &lv_font_montserrat_10, 0);
  lv_obj_set_style_text_color(bat_icon, lv_color_hex(0x059669), 0);

  lbl_battery = lv_label_create(bat_hdr);
  lv_label_set_text(lbl_battery, "---%");
  lv_obj_set_style_text_font(lbl_battery, &lv_font_montserrat_12, 0);
  lv_obj_set_style_text_color(lbl_battery, lv_color_hex(0x059669), 0);

  bar_battery = lv_bar_create(bat);
  lv_obj_set_size(bar_battery, lv_pct(100), 6);
  lv_bar_set_range(bar_battery, 0, 100);
  lv_bar_set_value(bar_battery, 0, LV_ANIM_OFF);
  lv_obj_set_style_bg_color(bar_battery, lv_color_hex(0xD1FAE5), LV_PART_MAIN);
  lv_obj_set_style_bg_color(bar_battery, lv_color_hex(0x059669), LV_PART_INDICATOR);
  lv_obj_set_style_radius(bar_battery, 4, 0);
  lv_obj_set_style_radius(bar_battery, 4, LV_PART_INDICATOR);

  lv_obj_t *bat_info_row = lv_obj_create(bat);
  lv_obj_set_size(bat_info_row, lv_pct(100), LV_SIZE_CONTENT);
  lv_obj_set_style_bg_opa(bat_info_row, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(bat_info_row, 0, 0);
  lv_obj_set_style_pad_all(bat_info_row, 0, 0);
  lv_obj_set_flex_flow(bat_info_row, LV_FLEX_FLOW_ROW);
  lv_obj_set_flex_align(bat_info_row, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_clear_flag(bat_info_row, LV_OBJ_FLAG_SCROLLABLE);

  lbl_uptime = lv_label_create(bat_info_row);
  lv_label_set_text(lbl_uptime, "UPTIME:");
  lv_obj_set_style_text_font(lbl_uptime, &lv_font_montserrat_10, 0);
  lv_obj_set_style_text_color(lbl_uptime, lv_color_hex(0x94A3B8), 0);

  lbl_bat_charging = lv_label_create(bat_info_row);
  lv_label_set_text(lbl_bat_charging, "");
  lv_obj_set_style_text_font(lbl_bat_charging, &lv_font_montserrat_10, 0);

  lv_obj_t *imu = lv_obj_create(right);
  lv_obj_set_size(imu, lv_pct(100), LV_SIZE_CONTENT);
  card_style(imu, lv_color_hex(0xF5F3FF));
  card_accent_line(imu, 0x6366F1);
  lv_obj_set_style_pad_row(imu, 2, 0);
  lv_obj_set_flex_flow(imu, LV_FLEX_FLOW_COLUMN);

  lv_obj_t *imu_hdr = lv_label_create(imu);
  lv_label_set_text(imu_hdr, LV_SYMBOL_SHUFFLE " IMU");
  lv_obj_set_style_text_font(imu_hdr, &lv_font_montserrat_10, 0);
  lv_obj_set_style_text_color(imu_hdr, lv_color_hex(0x6366F1), 0);

  lbl_imu_accel = lv_label_create(imu);
  lv_label_set_text(lbl_imu_accel, "A 0.0 0.0 0.0");
  lv_obj_set_style_text_font(lbl_imu_accel, &lv_font_montserrat_10, 0);
  lv_obj_set_style_text_color(lbl_imu_accel, lv_color_hex(0x4F46E5), 0);

  lbl_imu_gyro = lv_label_create(imu);
  lv_label_set_text(lbl_imu_gyro, "G 0 0 0");
  lv_obj_set_style_text_font(lbl_imu_gyro, &lv_font_montserrat_10, 0);
  lv_obj_set_style_text_color(lbl_imu_gyro, lv_color_hex(0x7C3AED), 0);

  lbl_fall_status = lv_label_create(imu);
  lv_label_set_text(lbl_fall_status, LV_SYMBOL_OK " Normal");
  lv_obj_set_style_text_font(lbl_fall_status, &lv_font_montserrat_10, 0);
  lv_obj_set_style_text_color(lbl_fall_status, lv_color_hex(0x059669), 0);

  lv_obj_t *sos = lv_btn_create(parent);
  lv_obj_set_size(sos, lv_pct(100), 34);
  lv_obj_set_style_bg_color(sos, lv_color_hex(0xEF4444), 0);
  lv_obj_set_style_bg_color(sos, lv_color_hex(0xB91C1C), LV_STATE_PRESSED);
  lv_obj_set_style_radius(sos, 8, 0);
  lv_obj_set_style_shadow_width(sos, 6, 0);
  lv_obj_set_style_shadow_color(sos, lv_color_hex(0xFCA5A5), 0);
  lv_obj_set_style_shadow_opa(sos, LV_OPA_50, 0);
  lv_obj_t *sos_lbl = lv_label_create(sos);
  lv_label_set_text(sos_lbl, LV_SYMBOL_WARNING "  EMERGENCY SOS  " LV_SYMBOL_WARNING);
  lv_obj_set_style_text_font(sos_lbl, &lv_font_montserrat_14, 0);
  lv_obj_set_style_text_color(sos_lbl, lv_color_hex(0xFFFFFF), 0);
  lv_obj_center(sos_lbl);
  lv_obj_add_event_cb(sos, help_btn_cb, LV_EVENT_CLICKED, NULL);
  add_haptic(sos);    // haptic on SOS button press
}

// ─────────────────────────────────────────────────────────────────────────
// Tasks tab
// ─────────────────────────────────────────────────────────────────────────
static void build_tasks(lv_obj_t *parent) {
  lv_obj_set_style_pad_all(parent, 6, 0);
  lv_obj_set_style_pad_row(parent, 5, 0);
  lv_obj_set_flex_flow(parent, LV_FLEX_FLOW_COLUMN);
  lv_obj_set_style_bg_color(parent, lv_color_hex(0xF8FAFC), 0);

  lv_obj_t *hdr = lv_obj_create(parent);
  lv_obj_set_size(hdr, lv_pct(100), LV_SIZE_CONTENT);
  lv_obj_set_style_bg_opa(hdr, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(hdr, 0, 0);
  lv_obj_set_style_pad_all(hdr, 0, 0);
  lv_obj_set_flex_flow(hdr, LV_FLEX_FLOW_ROW);
  lv_obj_set_flex_align(hdr, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_clear_flag(hdr, LV_OBJ_FLAG_SCROLLABLE);

  lbl_task_count = lv_label_create(hdr);
  lv_label_set_text(lbl_task_count, "Tasks (0)");
  lv_obj_set_style_text_font(lbl_task_count, &lv_font_montserrat_10, 0);
  lv_obj_set_style_text_color(lbl_task_count, lv_color_hex(0x64748B), 0);

  task_list = lv_obj_create(parent);
  lv_obj_set_width(task_list, lv_pct(100));
  lv_obj_set_flex_grow(task_list, 1);
  lv_obj_set_style_bg_opa(task_list, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(task_list, 0, 0);
  lv_obj_set_style_pad_all(task_list, 0, 0);
  lv_obj_set_flex_flow(task_list, LV_FLEX_FLOW_COLUMN);
  lv_obj_set_style_pad_row(task_list, 4, 0);
}

// ─────────────────────────────────────────────────────────────────────────
// Alerts tab
// ─────────────────────────────────────────────────────────────────────────
static void build_notifs(lv_obj_t *parent) {
  lv_obj_set_style_pad_all(parent, 6, 0);
  lv_obj_set_style_pad_row(parent, 5, 0);
  lv_obj_set_flex_flow(parent, LV_FLEX_FLOW_COLUMN);
  lv_obj_set_style_bg_color(parent, lv_color_hex(0xF8FAFC), 0);

  lv_obj_t *hdr = lv_obj_create(parent);
  lv_obj_set_size(hdr, lv_pct(100), LV_SIZE_CONTENT);
  lv_obj_set_style_bg_opa(hdr, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(hdr, 0, 0);
  lv_obj_set_style_pad_all(hdr, 0, 0);
  lv_obj_set_flex_flow(hdr, LV_FLEX_FLOW_ROW);
  lv_obj_set_flex_align(hdr, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_clear_flag(hdr, LV_OBJ_FLAG_SCROLLABLE);

  lv_obj_t *h_lbl = lv_label_create(hdr);
  lv_label_set_text(h_lbl, "Notifications");
  lv_obj_set_style_text_font(h_lbl, &lv_font_montserrat_10, 0);
  lv_obj_set_style_text_color(h_lbl, lv_color_hex(0x64748B), 0);

  lbl_notif_badge = lv_label_create(hdr);
  lv_label_set_text(lbl_notif_badge, "");
  lv_obj_set_style_text_font(lbl_notif_badge, &lv_font_montserrat_10, 0);
  lv_obj_set_style_text_color(lbl_notif_badge, lv_color_hex(0xDC2626), 0);

  notif_list = lv_obj_create(parent);
  lv_obj_set_width(notif_list, lv_pct(100));
  lv_obj_set_flex_grow(notif_list, 1);
  lv_obj_set_style_bg_opa(notif_list, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(notif_list, 0, 0);
  lv_obj_set_style_pad_all(notif_list, 0, 0);
  lv_obj_set_flex_flow(notif_list, LV_FLEX_FLOW_COLUMN);
  lv_obj_set_style_pad_row(notif_list, 4, 0);
}

// ─────────────────────────────────────────────────────────────────────────
// Settings tab
// ─────────────────────────────────────────────────────────────────────────
static void build_settings(lv_obj_t *parent) {
  lv_obj_set_style_pad_all(parent, 10, 0);
  lv_obj_set_style_pad_row(parent, 12, 0);
  lv_obj_set_flex_flow(parent, LV_FLEX_FLOW_COLUMN);
  lv_obj_set_style_bg_color(parent, lv_color_hex(0xF8FAFC), 0);

  auto make_setting_row = [&](lv_obj_t *par, const char *icon, const char *label,
                               lv_obj_t **out_slider, lv_obj_t **out_val_lbl, int init_val, lv_event_cb_t cb) {
    lv_obj_t *card = lv_obj_create(par);
    lv_obj_set_size(card, lv_pct(100), LV_SIZE_CONTENT);
    card_style(card, lv_color_hex(0xFFFFFF));
    lv_obj_set_style_border_color(card, lv_color_hex(0x0D9488), 0);
    lv_obj_set_style_border_width(card, 1, 0);
    lv_obj_set_style_pad_row(card, 6, 0);
    lv_obj_set_flex_flow(card, LV_FLEX_FLOW_COLUMN);

    lv_obj_t *row = lv_obj_create(card);
    lv_obj_set_size(row, lv_pct(100), LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(row, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(row, 0, 0);
    lv_obj_set_style_pad_all(row, 0, 0);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *lbl_icon = lv_label_create(row);
    lv_label_set_text_fmt(lbl_icon, "%s  %s", icon, label);
    lv_obj_set_style_text_font(lbl_icon, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_color(lbl_icon, lv_color_hex(0x0F172A), 0);

    *out_val_lbl = lv_label_create(row);
    char buf[16]; snprintf(buf, sizeof(buf), "%d%%", init_val);
    lv_label_set_text(*out_val_lbl, buf);
    lv_obj_set_style_text_font(*out_val_lbl, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_color(*out_val_lbl, lv_color_hex(0x0D9488), 0);

    *out_slider = lv_slider_create(card);
    lv_obj_set_width(*out_slider, lv_pct(100));
    lv_slider_set_range(*out_slider, 0, 100);
    lv_slider_set_value(*out_slider, init_val, LV_ANIM_OFF);
    lv_obj_set_style_bg_color(*out_slider, lv_color_hex(0xCCFBF1), LV_PART_MAIN);
    lv_obj_set_style_bg_color(*out_slider, lv_color_hex(0x0D9488), LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(*out_slider, lv_color_hex(0x0F766E), LV_PART_KNOB);
    lv_obj_set_style_radius(*out_slider, 4, LV_PART_KNOB);
    lv_obj_add_event_cb(*out_slider, cb, LV_EVENT_VALUE_CHANGED, NULL);
    add_haptic(*out_slider);
  };

  make_setting_row(parent, LV_SYMBOL_IMAGE, "BRIGHTNESS",
                   &slider_brightness, &lbl_brightness_val, ui_brightness, brightness_changed_cb);

  make_setting_row(parent, LV_SYMBOL_AUDIO, "VOLUME",
                   &slider_volume, &lbl_volume_val, ui_volume, volume_changed_cb);
}

// ─────────────────────────────────────────────────────────────────────────
// UI_Init
// ─────────────────────────────────────────────────────────────────────────
void UI_Init(void) {
  Serial.println("[DEBUG] Mounting FFat Partition...");
  if (!FFat.begin(true)) {
      Serial.println("[ERROR] Failed to mount FFat! Please check Tools > Partition Scheme.");
  } else {
      Serial.printf("[INFO] FFat Mounted. Free Space: %u / %u bytes\n", FFat.freeBytes(), FFat.totalBytes());
  }

  ui_mutex = xSemaphoreCreateMutex();

  uint8_t mapped_vol = (uint8_t)(ui_volume * Volume_MAX / 100);
  Volume_adjustment(mapped_vol);

  lv_obj_set_style_bg_color(lv_scr_act(), lv_color_hex(0xF8FAFC), 0);

  lv_obj_t *tabview = lv_tabview_create(lv_scr_act(), LV_DIR_TOP, 28);
  lv_obj_set_size(tabview, UI_W, UI_H);
  lv_obj_set_pos(tabview, 0, 0);
  lv_obj_set_style_bg_color(tabview, lv_color_hex(0xFFFFFF), 0);

  lv_obj_t *tab_btns = lv_tabview_get_tab_btns(tabview);
  lv_obj_set_style_bg_color(tab_btns, lv_color_hex(0xFFFFFF), 0);
  lv_obj_set_style_border_color(tab_btns, lv_color_hex(0xE2E8F0), 0);
  lv_obj_set_style_border_width(tab_btns, 1, 0);
  lv_obj_set_style_border_side(tab_btns, LV_BORDER_SIDE_BOTTOM, 0);
  lv_obj_set_style_text_font(tab_btns, &lv_font_montserrat_10, LV_PART_ITEMS);
  lv_obj_set_style_text_color(tab_btns, lv_color_hex(0x94A3B8), LV_PART_ITEMS);
  lv_obj_set_style_text_color(tab_btns, lv_color_hex(0x0D9488), LV_PART_ITEMS | LV_STATE_CHECKED);
  lv_obj_set_style_border_side(tab_btns, LV_BORDER_SIDE_BOTTOM, LV_PART_ITEMS | LV_STATE_CHECKED);
  lv_obj_set_style_border_color(tab_btns, lv_color_hex(0x0D9488), LV_PART_ITEMS | LV_STATE_CHECKED);
  lv_obj_set_style_border_width(tab_btns, 2, LV_PART_ITEMS | LV_STATE_CHECKED);
  lv_obj_set_style_bg_color(tab_btns, lv_color_hex(0xF0FDF9), LV_PART_ITEMS | LV_STATE_CHECKED);

  // ── Add haptic to tab buttons ─────────────────────────────────────────
  add_haptic(tab_btns);

  lv_obj_t *tab_dash     = lv_tabview_add_tab(tabview, LV_SYMBOL_HOME    " Home");
  lv_obj_t *tab_tasks    = lv_tabview_add_tab(tabview, LV_SYMBOL_LIST    " Tasks");
  lv_obj_t *tab_notifs   = lv_tabview_add_tab(tabview, LV_SYMBOL_BELL    " Alerts");
  lv_obj_t *tab_settings = lv_tabview_add_tab(tabview, LV_SYMBOL_SETTINGS " Settings");

  build_dashboard(tab_dash);
  build_tasks(tab_tasks);
  build_notifs(tab_notifs);
  build_settings(tab_settings);

  refresh_task_list();
  refresh_notif_list();

  lv_timer_create(ui_update_cb, 1000, NULL);

  xTaskCreatePinnedToCore(NetworkTask, "NetTask", 8192, NULL, 2, NULL, 0);
}


// ─────────────────────────────────────────────────────────────────────────
// AUDIO MESSAGE STREAM UPLOAD (Using FFat)
// ─────────────────────────────────────────────────────────────────────────

static void handle_audio_upload() {
  HTTPUpload& upload = server.upload();

  if (upload.status == UPLOAD_FILE_START) {
    Serial.printf("\n[DEBUG] Audio Upload Started: %s\n", upload.filename.c_str());
    if(audio.isRunning()) audio.stopSong();
    if (FFat.exists(AUDIO_FILE_PATH)) FFat.remove(AUDIO_FILE_PATH);
    uploadFile = FFat.open(AUDIO_FILE_PATH, FILE_WRITE);
    if(!uploadFile) Serial.println("[ERROR] Failed to open FFat file for writing!");
    else            Serial.println("[INFO] FFat file opened successfully.");
    total_bytes_written = 0;

  } else if (upload.status == UPLOAD_FILE_WRITE) {
    if(uploadFile) {
        size_t written = uploadFile.write(upload.buf, upload.currentSize);
        total_bytes_written += written;
        if(written != upload.currentSize)
             Serial.printf("[ERROR] FFat write failed! Tried: %u, Wrote: %u\n", upload.currentSize, written);
    }

  } else if (upload.status == UPLOAD_FILE_END) {
    if(uploadFile) { uploadFile.flush(); uploadFile.close(); }
    Serial.printf("[DEBUG] Audio Upload Completed! Total Bytes Written to FFat: %u\n\n", total_bytes_written);
  }
}

static void handle_audio_message() {
  File verify = FFat.open(AUDIO_FILE_PATH, FILE_READ);
  if(!verify || verify.size() == 0) {
      Serial.println("[ERROR] Upload finished but file is empty or missing in FFat.");
      if(verify) verify.close();
      server.send(500, "application/json", "{\"status\":\"error\", \"message\":\"Failed to save audio file to ESP32 Flash (FFat)\"}");
      return;
  }
  verify.close();

  server.send(200, "application/json", "{\"status\":\"ok\", \"message\":\"Audio successfully saved to FFat\"}");

  lv_async_call([](void *) {
    show_audio_player_modal();
  }, NULL);
}

// ─────────────────────────────────────────────────────────────────────────
// Remaining HTTP Handlers & Callbacks
// ─────────────────────────────────────────────────────────────────────────
static void handle_status() {
  StaticJsonDocument<512> doc;
  doc["uptime_ms"]      = millis();
  doc["battery_pct"]    = bat_percent();
  doc["battery_v"]      = BAT_analogVolts;
  doc["charging"]       = bat_is_charging;
  doc["imu_ax"] = Accel.x; doc["imu_ay"] = Accel.y; doc["imu_az"] = Accel.z;
  doc["imu_gx"] = Gyro.x;  doc["imu_gy"] = Gyro.y;  doc["imu_gz"] = Gyro.z;
  doc["step_count"]     = step_count;
  doc["task_count"]     = taskCount;
  doc["notif_count"]    = notifCount;
  doc["fall_detected"]  = fall_detected;
  doc["fall_detected_time"] = fall_detected_time_ms;
  doc["help_alert"]     = help_alert_pending;
  doc["help_alert_time"]= help_alert_time_ms;
  doc["worker_name"]    = worker_name;
  char dt[32];
  snprintf(dt, sizeof(dt), "%04d-%02d-%02d %02d:%02d:%02d", datetime.year, datetime.month, datetime.day, datetime.hour, datetime.minute, datetime.second);
  doc["datetime"] = dt;
  char buf[512];
  serializeJson(doc, buf, sizeof(buf));
  server.send(200, "application/json", buf);
}

static void handle_get_tasks() {
  StaticJsonDocument<1024> doc;
  JsonArray arr = doc.createNestedArray("tasks");
  for (int i = 0; i < taskCount; i++) {
    JsonObject o = arr.createNestedObject();
    o["index"]           = i;
    o["title"]           = tasks[i].title;
    o["desc"]            = tasks[i].desc;
    o["priority"]        = (int)tasks[i].priority;
    o["priority_name"]   = PRIORITY_NAMES[tasks[i].priority];
    o["completed"]       = tasks[i].completed;
    o["pending_approval"]= tasks[i].pendingApproval;
    o["pending_skip"]    = tasks[i].pendingSkip;
    o["approved"]        = tasks[i].approved;
    o["skipped"]         = tasks[i].skipped;
  }
  char buf[1024];
  serializeJson(doc, buf, sizeof(buf));
  server.send(200, "application/json", buf);
}

static void handle_add_task() {
  if (!server.hasArg("plain")) { server.send(400,"text/plain","No body"); return; }
  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, server.arg("plain"))) { server.send(400,"text/plain","Bad JSON"); return; }
  if (taskCount >= MAX_TASKS) { server.send(503,"text/plain","Full"); return; }
  xSemaphoreTake(ui_mutex, portMAX_DELAY);
  int i = taskCount++;
  strlcpy(tasks[i].title, doc["title"] | "Task", sizeof(tasks[i].title));
  strlcpy(tasks[i].desc,  doc["desc"]  | "",     sizeof(tasks[i].desc));
  int pri = doc["priority"] | 1;
  if (pri < 0) pri = 0; if (pri > 2) pri = 2;
  tasks[i].priority        = (TaskPriority)pri;
  tasks[i].completed       = false;
  tasks[i].pendingApproval = false;
  tasks[i].pendingSkip     = false;
  tasks[i].approved        = false;
  tasks[i].skipped         = false;
  xSemaphoreGive(ui_mutex);
  lv_async_call([](void *){ refresh_task_list(); }, NULL);
  server.send(200, "application/json", "{\"status\":\"ok\"}");
}

static void handle_complete_task() {
  if (!server.hasArg("plain")) { server.send(400,"text/plain","No body"); return; }
  StaticJsonDocument<128> doc;
  if (deserializeJson(doc, server.arg("plain"))) { server.send(400,"text/plain","Bad JSON"); return; }
  int idx = doc["index"] | -1;
  if (idx < 0 || idx >= taskCount) { server.send(404,"text/plain","Not found"); return; }
  xSemaphoreTake(ui_mutex, portMAX_DELAY);
  tasks[idx].pendingApproval = true;
  xSemaphoreGive(ui_mutex);
  lv_async_call([](void *){ refresh_task_list(); }, NULL);
  server.send(200, "application/json", "{\"status\":\"pending_approval\"}");
}

static void handle_approve_task() {
  if (!server.hasArg("plain")) { server.send(400,"text/plain","No body"); return; }
  StaticJsonDocument<128> doc;
  if (deserializeJson(doc, server.arg("plain"))) { server.send(400,"text/plain","Bad JSON"); return; }
  int idx = doc["index"] | -1;
  if (idx < 0 || idx >= taskCount) { server.send(404,"text/plain","Not found"); return; }
  xSemaphoreTake(ui_mutex, portMAX_DELAY);
  tasks[idx].pendingApproval = false;
  tasks[idx].approved = tasks[idx].completed = true;
  xSemaphoreGive(ui_mutex);
  lv_async_call([](void *){ refresh_task_list(); }, NULL);
  server.send(200, "application/json", "{\"status\":\"approved\"}");
}

static void handle_approve_skip() {
  if (!server.hasArg("plain")) { server.send(400,"text/plain","No body"); return; }
  StaticJsonDocument<128> doc;
  if (deserializeJson(doc, server.arg("plain"))) { server.send(400,"text/plain","Bad JSON"); return; }
  int idx = doc["index"] | -1;
  if (idx < 0 || idx >= taskCount) { server.send(404,"text/plain","Not found"); return; }
  xSemaphoreTake(ui_mutex, portMAX_DELAY);
  tasks[idx].pendingSkip = false;
  tasks[idx].skipped     = true;
  xSemaphoreGive(ui_mutex);
  lv_async_call([](void *){ refresh_task_list(); }, NULL);
  server.send(200, "application/json", "{\"status\":\"skipped\"}");
}

static void handle_deny_skip() {
  if (!server.hasArg("plain")) { server.send(400,"text/plain","No body"); return; }
  StaticJsonDocument<128> doc;
  if (deserializeJson(doc, server.arg("plain"))) { server.send(400,"text/plain","Bad JSON"); return; }
  int idx = doc["index"] | -1;
  if (idx < 0 || idx >= taskCount) { server.send(404,"text/plain","Not found"); return; }
  xSemaphoreTake(ui_mutex, portMAX_DELAY);
  tasks[idx].pendingSkip = false;
  xSemaphoreGive(ui_mutex);
  lv_async_call([](void *arg) {
    int idx2 = (int)(intptr_t)arg;
    refresh_task_list();
    char body[64];
    snprintf(body, sizeof(body), "Skip denied for: %s", tasks[idx2].title);
    show_popup("Skip Denied", body, false);
  }, (void*)(intptr_t)idx);
  server.send(200, "application/json", "{\"status\":\"skip_denied\"}");
}

static void handle_notify() {
  if (!server.hasArg("plain")) { server.send(400,"text/plain","No body"); return; }
  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, server.arg("plain"))) { server.send(400,"text/plain","Bad JSON"); return; }
  xSemaphoreTake(ui_mutex, portMAX_DELAY);
  if (notifCount >= MAX_NOTIFS) {
    memmove(&notifs[0], &notifs[1], sizeof(Notification) * (MAX_NOTIFS - 1));
    notifCount = MAX_NOTIFS - 1;
  }
  int i = notifCount++;
  strlcpy(notifs[i].title, doc["title"] | "Notification", sizeof(notifs[i].title));
  strlcpy(notifs[i].body,  doc["body"]  | "",              sizeof(notifs[i].body));
  notifs[i].isAlert   = doc["alert"] | false;
  notifs[i].dismissed = false;
  unreadNotifs++;
  xSemaphoreGive(ui_mutex);
  int ni = i;
  lv_async_call([](void *arg) {
    int idx = (int)(intptr_t)arg;
    refresh_notif_list();
    show_popup(notifs[idx].title, notifs[idx].body, notifs[idx].isAlert);
    // show_popup fires trigger_alert_haptic() automatically for isAlert=true
  }, (void*)(intptr_t)ni);
  server.send(200, "application/json", "{\"status\":\"ok\"}");
}

static void handle_fall_reset() {
  fall_detected         = false;
  fall_detected_time_ms = 0;
  lv_async_call([](void *) {
    if (lbl_fall_status) {
      lv_label_set_text(lbl_fall_status, LV_SYMBOL_OK " Normal");
      lv_obj_set_style_text_color(lbl_fall_status, lv_color_hex(0x059669), 0);
    }
  }, NULL);
  server.send(200, "application/json", "{\"status\":\"ok\"}");
}

static void handle_get_worker() {
  StaticJsonDocument<128> doc;
  doc["worker_name"] = worker_name;
  char buf[128];
  serializeJson(doc, buf, sizeof(buf));
  server.send(200, "application/json", buf);
}

static void handle_set_worker() {
  if (!server.hasArg("plain")) { server.send(400,"text/plain","No body"); return; }
  StaticJsonDocument<128> doc;
  if (deserializeJson(doc, server.arg("plain"))) { server.send(400,"text/plain","Bad JSON"); return; }
  const char *name = doc["name"] | nullptr;
  if (!name || strlen(name) == 0) { server.send(400,"text/plain","Missing name"); return; }
  xSemaphoreTake(ui_mutex, portMAX_DELAY);
  strlcpy(worker_name, name, sizeof(worker_name));
  xSemaphoreGive(ui_mutex);
  char *heap_name = strdup(worker_name);
  lv_async_call([](void *arg) {
    if (lbl_worker_name) lv_label_set_text(lbl_worker_name, (const char *)arg);
    free(arg);
  }, heap_name);
  server.send(200, "application/json", "{\"status\":\"ok\"}");
}

static void handle_help_reset() {
  help_alert_pending  = false;
  help_alert_time_ms  = 0;
  lv_async_call([](void *) {
    show_popup("Help Alert", "Manager acknowledged your help request.", false);
  }, NULL);
  server.send(200, "application/json", "{\"status\":\"ok\"}");
}

static void handle_set_settings() {
  if (!server.hasArg("plain")) { server.send(400,"text/plain","No body"); return; }
  StaticJsonDocument<128> doc;
  if (deserializeJson(doc, server.arg("plain"))) { server.send(400,"text/plain","Bad JSON"); return; }

  if (doc.containsKey("brightness")) {
    int b = doc["brightness"];
    if (b < 0) b = 0; if (b > 100) b = 100;
    ui_brightness = (uint8_t)b;
    Set_Backlight(ui_brightness);
    lv_async_call([](void *arg) {
      if (slider_brightness) lv_slider_set_value(slider_brightness, (int)(intptr_t)arg, LV_ANIM_ON);
      if (lbl_brightness_val) {
        char buf[16]; snprintf(buf, sizeof(buf), "%d%%", (int)(intptr_t)arg);
        lv_label_set_text(lbl_brightness_val, buf);
      }
    }, (void*)(intptr_t)ui_brightness);
  }

  if (doc.containsKey("volume")) {
    int v = doc["volume"];
    if (v < 0) v = 0; if (v > 100) v = 100;
    ui_volume = (uint8_t)v;
    uint8_t mapped_vol = (uint8_t)(ui_volume * Volume_MAX / 100);
    Volume_adjustment(mapped_vol);
    lv_async_call([](void *arg) {
      if (slider_volume) lv_slider_set_value(slider_volume, (int)(intptr_t)arg, LV_ANIM_ON);
      if (lbl_volume_val) {
        char buf[16]; snprintf(buf, sizeof(buf), "%d%%", (int)(intptr_t)arg);
        lv_label_set_text(lbl_volume_val, buf);
      }
    }, (void*)(intptr_t)ui_volume);
  }
  server.send(200, "application/json", "{\"status\":\"ok\"}");
}

static void handle_set_rtc() {
  if (!server.hasArg("plain")) { server.send(400,"text/plain","No body"); return; }
  StaticJsonDocument<192> doc;
  if (deserializeJson(doc, server.arg("plain"))) { server.send(400,"text/plain","Bad JSON"); return; }
  datetime_t t;
  t.year   = doc["year"]   | 2024;
  t.month  = doc["month"]  | 1;
  t.day    = doc["day"]    | 1;
  t.dotw   = doc["dotw"]   | 0;
  t.hour   = doc["hour"]   | 0;
  t.minute = doc["minute"] | 0;
  t.second = doc["second"] | 0;
  PCF85063_Set_All(t);
  server.send(200, "application/json", "{\"status\":\"ok\"}");
}

static void setup_wifi_server() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) delay(200);

  if (WiFi.status() == WL_CONNECTED) {
    server.on("/status",        HTTP_GET,  handle_status);
    server.on("/tasks",         HTTP_GET,  handle_get_tasks);
    server.on("/task/add",      HTTP_POST, handle_add_task);
    server.on("/task/complete", HTTP_POST, handle_complete_task);
    server.on("/task/approve",  HTTP_POST, handle_approve_task);
    server.on("/notify",        HTTP_POST, handle_notify);
    server.on("/fall/reset",    HTTP_POST, handle_fall_reset);
    server.on("/worker",        HTTP_GET,  handle_get_worker);
    server.on("/worker",        HTTP_POST, handle_set_worker);
    server.on("/task/skip/approve", HTTP_POST, handle_approve_skip);
    server.on("/task/skip/deny",    HTTP_POST, handle_deny_skip);
    server.on("/help/reset",    HTTP_POST, handle_help_reset);
    server.on("/settings",      HTTP_POST, handle_set_settings);
    server.on("/rtc/set",       HTTP_POST, handle_set_rtc);
    server.on("/audio/message", HTTP_POST, handle_audio_message, handle_audio_upload);
    server.begin();

    char *heap_ip = strdup(WiFi.localIP().toString().c_str());
    lv_async_call([](void *arg) {
      char buf[40]; snprintf(buf, sizeof(buf), "%s", (char*)arg);
      lv_label_set_text(lbl_ip, buf);
      lv_label_set_text(lbl_wifi_status, LV_SYMBOL_WIFI " Connected");
      lv_obj_set_style_text_color(lbl_wifi_status, lv_color_hex(0x059669), 0);
      free(arg);
    }, heap_ip);
  } else {
    lv_async_call([](void *) {
      lv_label_set_text(lbl_wifi_status, LV_SYMBOL_WIFI " No WiFi");
      lv_obj_set_style_text_color(lbl_wifi_status, lv_color_hex(0xDC2626), 0);
      lv_label_set_text(lbl_ip, "---");
    }, NULL);
  }
}

static void request_complete_cb(lv_event_t *e) {
  int idx = (int)(intptr_t)lv_event_get_user_data(e);
  if (idx < 0 || idx >= taskCount) return;
  xSemaphoreTake(ui_mutex, portMAX_DELAY);
  if (!tasks[idx].pendingApproval && !tasks[idx].approved && !tasks[idx].pendingSkip) tasks[idx].pendingApproval = true;
  xSemaphoreGive(ui_mutex);
  char *heap_title = strdup(tasks[idx].title);
  lv_async_call([](void *arg) {
    refresh_task_list();
    show_request_sent_popup((const char *)arg, false);
    free(arg);
  }, heap_title);
}

static void request_skip_cb(lv_event_t *e) {
  int idx = (int)(intptr_t)lv_event_get_user_data(e);
  if (idx < 0 || idx >= taskCount) return;
  xSemaphoreTake(ui_mutex, portMAX_DELAY);
  if (!tasks[idx].pendingApproval && !tasks[idx].approved && !tasks[idx].pendingSkip && !tasks[idx].skipped) tasks[idx].pendingSkip = true;
  xSemaphoreGive(ui_mutex);
  char *heap_title = strdup(tasks[idx].title);
  lv_async_call([](void *arg) {
    refresh_task_list();
    show_request_sent_popup((const char *)arg, true);
    free(arg);
  }, heap_title);
}

static void show_request_sent_popup(const char *title, bool isSkip) {
  if (popup_box) { lv_obj_del(popup_box); popup_box = NULL; }
  popup_box = lv_obj_create(lv_layer_top());
  lv_obj_set_size(popup_box, 286, 60);
  lv_obj_align(popup_box, LV_ALIGN_TOP_MID, 0, 4);
  lv_obj_set_style_radius(popup_box, 12, 0);
  lv_obj_set_style_border_width(popup_box, 2, 0);
  lv_color_t bc = isSkip ? lv_color_hex(0x6366F1) : lv_color_hex(0xF59E0B);
  lv_obj_set_style_border_color(popup_box, bc, 0);
  lv_obj_set_style_bg_color(popup_box, isSkip ? lv_color_hex(0xEEF2FF) : lv_color_hex(0xFFF3CD), 0);
  lv_obj_set_style_pad_all(popup_box, 8, 0);
  lv_obj_set_style_pad_row(popup_box, 4, 0);
  lv_obj_set_flex_flow(popup_box, LV_FLEX_FLOW_COLUMN);
  lv_obj_clear_flag(popup_box, LV_OBJ_FLAG_SCROLLABLE);

  lv_obj_t *row = lv_obj_create(popup_box);
  lv_obj_set_size(row, lv_pct(100), LV_SIZE_CONTENT);
  lv_obj_set_style_bg_opa(row, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(row, 0, 0);
  lv_obj_set_style_pad_all(row, 0, 0);
  lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
  lv_obj_set_flex_align(row, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);

  lv_obj_t *icon = lv_label_create(row);
  lv_label_set_text(icon, isSkip ? LV_SYMBOL_NEXT " Skip requested" : LV_SYMBOL_UPLOAD " Completion requested");
  lv_obj_set_style_text_font(icon, &lv_font_montserrat_12, 0);
  lv_obj_set_style_text_color(icon, isSkip ? lv_color_hex(0x4338CA) : lv_color_hex(0x7D4E00), 0);

  lv_obj_t *btn = lv_btn_create(row);
  lv_obj_set_size(btn, 22, 18);
  lv_obj_set_style_bg_color(btn, lv_color_hex(0xCCCCCC), 0);
  lv_obj_set_style_radius(btn, 4, 0);
  lv_obj_t *xlbl = lv_label_create(btn);
  lv_label_set_text(xlbl, LV_SYMBOL_CLOSE);
  lv_obj_set_style_text_font(xlbl, &lv_font_montserrat_10, 0);
  lv_obj_center(xlbl);
  lv_obj_add_event_cb(btn, close_popup_cb, LV_EVENT_CLICKED, NULL);
  add_haptic(btn);

  lv_obj_t *sub = lv_label_create(popup_box);
  char msg[96]; snprintf(msg, sizeof(msg), "Awaiting manager: %s", title);
  lv_label_set_text(sub, msg);
  lv_label_set_long_mode(sub, LV_LABEL_LONG_DOT);
  lv_obj_set_width(sub, lv_pct(100));
  lv_obj_set_style_text_font(sub, &lv_font_montserrat_10, 0);
  lv_obj_set_style_text_color(sub, isSkip ? lv_color_hex(0x4338CA) : lv_color_hex(0x7D4E00), 0);

  lv_timer_create([](lv_timer_t *tmr) {
    if (popup_box) { lv_obj_del(popup_box); popup_box = NULL; }
    lv_timer_del(tmr);
  }, 4000, NULL);
}

static void refresh_task_list() {
  lv_obj_clean(task_list);

  if (taskCount == 0) {
    lv_obj_t *l = lv_label_create(task_list);
    lv_label_set_text(l, "No tasks yet.");
    lv_obj_set_style_text_font(l, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(0x9CA3AF), 0);
    return;
  }

  for (int i = 0; i < taskCount; i++) {
    lv_color_t card_bg;
    if (tasks[i].approved)        card_bg = lv_color_hex(0xECFDF5);
    else if (tasks[i].skipped)    card_bg = lv_color_hex(0xF5F3FF);
    else if (tasks[i].pendingApproval || tasks[i].pendingSkip) card_bg = lv_color_hex(0xFFFBEB);
    else                           card_bg = lv_color_hex(0xFFFFFF);

    lv_obj_t *card = lv_obj_create(task_list);
    lv_obj_set_width(card, lv_pct(100));
    lv_obj_set_height(card, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_color(card, card_bg, 0);
    lv_obj_set_style_radius(card, 8, 0);
    lv_obj_set_style_pad_hor(card, 8, 0);
    lv_obj_set_style_pad_ver(card, 6, 0);
    lv_obj_set_flex_flow(card, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(card, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_set_style_border_width(card, 3, 0);
    lv_obj_set_style_border_side(card, LV_BORDER_SIDE_LEFT, 0);
    lv_color_t pri_col;
    switch (tasks[i].priority) {
      case PRIORITY_HIGH:   pri_col = lv_color_hex(0xEF4444); break;
      case PRIORITY_NORMAL: pri_col = lv_color_hex(0x0D9488); break;
      default:              pri_col = lv_color_hex(0x94A3B8); break;
    }
    lv_obj_set_style_border_color(card, pri_col, 0);

    lv_obj_t *col = lv_obj_create(card);
    lv_obj_set_flex_grow(col, 1);
    lv_obj_set_height(col, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(col, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(col, 0, 0);
    lv_obj_set_style_pad_all(col, 0, 0);
    lv_obj_set_flex_flow(col, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(col, 2, 0);
    lv_obj_clear_flag(col, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *title_row = lv_obj_create(col);
    lv_obj_set_size(title_row, lv_pct(100), LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(title_row, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(title_row, 0, 0);
    lv_obj_set_style_pad_all(title_row, 0, 0);
    lv_obj_set_flex_flow(title_row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(title_row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(title_row, 4, 0);
    lv_obj_clear_flag(title_row, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *pri_badge = lv_label_create(title_row);
    lv_label_set_text(pri_badge, PRIORITY_NAMES[tasks[i].priority]);
    lv_obj_set_style_text_font(pri_badge, &lv_font_montserrat_10, 0);
    lv_obj_set_style_text_color(pri_badge, pri_col, 0);

    lv_obj_t *t = lv_label_create(title_row);
    lv_label_set_text(t, tasks[i].title);
    lv_label_set_long_mode(t, LV_LABEL_LONG_DOT);
    lv_obj_set_width(t, 140);
    lv_obj_set_style_text_font(t, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_color(t, lv_color_hex(0x0F172A), 0);

    if (tasks[i].desc[0] != '\0') {
      lv_obj_t *d = lv_label_create(col);
      lv_label_set_text(d, tasks[i].desc);
      lv_label_set_long_mode(d, LV_LABEL_LONG_DOT);
      lv_obj_set_width(d, 170);
      lv_obj_set_style_text_font(d, &lv_font_montserrat_10, 0);
      lv_obj_set_style_text_color(d, lv_color_hex(0x64748B), 0);
    }

    lv_obj_t *btn_col = lv_obj_create(card);
    lv_obj_set_width(btn_col, LV_SIZE_CONTENT);
    lv_obj_set_height(btn_col, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(btn_col, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(btn_col, 0, 0);
    lv_obj_set_style_pad_all(btn_col, 0, 0);
    lv_obj_set_flex_flow(btn_col, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(btn_col, 3, 0);
    lv_obj_clear_flag(btn_col, LV_OBJ_FLAG_SCROLLABLE);

    if (tasks[i].approved) {
      lv_obj_t *b = lv_label_create(btn_col);
      lv_label_set_text(b, LV_SYMBOL_OK " Done");
      lv_obj_set_style_text_font(b, &lv_font_montserrat_10, 0);
      lv_obj_set_style_text_color(b, lv_color_hex(0x059669), 0);
    } else if (tasks[i].skipped) {
      lv_obj_t *b = lv_label_create(btn_col);
      lv_label_set_text(b, LV_SYMBOL_NEXT " Skipped");
      lv_obj_set_style_text_font(b, &lv_font_montserrat_10, 0);
      lv_obj_set_style_text_color(b, lv_color_hex(0x6366F1), 0);
    } else if (tasks[i].pendingApproval) {
      lv_obj_t *b = lv_label_create(btn_col);
      lv_label_set_text(b, LV_SYMBOL_REFRESH " Wait...");
      lv_obj_set_style_text_font(b, &lv_font_montserrat_10, 0);
      lv_obj_set_style_text_color(b, lv_color_hex(0xF59E0B), 0);
    } else if (tasks[i].pendingSkip) {
      lv_obj_t *b = lv_label_create(btn_col);
      lv_label_set_text(b, LV_SYMBOL_REFRESH " Skip?");
      lv_obj_set_style_text_font(b, &lv_font_montserrat_10, 0);
      lv_obj_set_style_text_color(b, lv_color_hex(0x6366F1), 0);
    } else {
      lv_obj_t *done_btn = lv_btn_create(btn_col);
      lv_obj_set_size(done_btn, 72, 24);
      lv_obj_set_style_bg_color(done_btn, lv_color_hex(0x0D9488), 0);
      lv_obj_set_style_radius(done_btn, 6, 0);
      lv_obj_set_style_shadow_width(done_btn, 0, 0);
      lv_obj_t *dl = lv_label_create(done_btn);
      lv_label_set_text(dl, LV_SYMBOL_OK " Done?");
      lv_obj_set_style_text_font(dl, &lv_font_montserrat_10, 0);
      lv_obj_set_style_text_color(dl, lv_color_hex(0xFFFFFF), 0);
      lv_obj_center(dl);
      lv_obj_add_event_cb(done_btn, request_complete_cb, LV_EVENT_CLICKED, (void*)(intptr_t)i);
      add_haptic(done_btn);

      lv_obj_t *skip_btn = lv_btn_create(btn_col);
      lv_obj_set_size(skip_btn, 72, 24);
      lv_obj_set_style_bg_color(skip_btn, lv_color_hex(0x6366F1), 0);
      lv_obj_set_style_radius(skip_btn, 6, 0);
      lv_obj_set_style_shadow_width(skip_btn, 0, 0);
      lv_obj_t *sl = lv_label_create(skip_btn);
      lv_label_set_text(sl, LV_SYMBOL_NEXT " Skip?");
      lv_obj_set_style_text_font(sl, &lv_font_montserrat_10, 0);
      lv_obj_set_style_text_color(sl, lv_color_hex(0xFFFFFF), 0);
      lv_obj_center(sl);
      lv_obj_add_event_cb(skip_btn, request_skip_cb, LV_EVENT_CLICKED, (void*)(intptr_t)i);
      add_haptic(skip_btn);
    }
  }

  char buf[24]; snprintf(buf, sizeof(buf), "Tasks (%d)", taskCount);
  lv_label_set_text(lbl_task_count, buf);
}

static void refresh_notif_list() {
  lv_obj_clean(notif_list);
  int visible = 0;
  for (int i = 0; i < notifCount; i++) if (!notifs[i].dismissed) visible++;
  if (visible == 0) {
    lv_obj_t *l = lv_label_create(notif_list);
    lv_label_set_text(l, "No notifications.");
    lv_obj_set_style_text_font(l, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(0x9CA3AF), 0);
    lv_label_set_text(lbl_notif_badge, "");
    return;
  }
  char bb[8]; snprintf(bb, sizeof(bb), "%d", unreadNotifs);
  lv_label_set_text(lbl_notif_badge, unreadNotifs > 0 ? bb : "");

  for (int i = notifCount - 1; i >= 0; i--) {
    if (notifs[i].dismissed) continue;
    lv_obj_t *card = lv_obj_create(notif_list);
    lv_obj_set_width(card, lv_pct(100));
    lv_obj_set_height(card, LV_SIZE_CONTENT);
    lv_obj_set_style_radius(card, 8, 0);
    lv_obj_set_style_border_width(card, 2, 0);
    lv_obj_set_style_border_side(card, LV_BORDER_SIDE_LEFT, 0);
    lv_obj_set_style_pad_hor(card, 10, 0);
    lv_obj_set_style_pad_ver(card, 6, 0);
    lv_obj_set_style_pad_row(card, 3, 0);
    lv_obj_set_flex_flow(card, LV_FLEX_FLOW_COLUMN);
    lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(card, notifs[i].isAlert ? lv_color_hex(0xFFF1F2) : lv_color_hex(0xF0FDF9), 0);
    lv_obj_set_style_border_color(card, notifs[i].isAlert ? lv_color_hex(0xEF4444) : lv_color_hex(0x0D9488), 0);

    lv_obj_t *t = lv_label_create(card);
    lv_label_set_text_fmt(t, "%s  %s", notifs[i].isAlert ? LV_SYMBOL_WARNING : LV_SYMBOL_BELL, notifs[i].title);
    lv_obj_set_style_text_font(t, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_color(t, notifs[i].isAlert ? lv_color_hex(0xDC2626) : lv_color_hex(0x0D9488), 0);

    lv_obj_t *b = lv_label_create(card);
    lv_label_set_text(b, notifs[i].body);
    lv_label_set_long_mode(b, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(b, lv_pct(100));
    lv_obj_set_style_text_font(b, &lv_font_montserrat_10, 0);
    lv_obj_set_style_text_color(b, lv_color_hex(0x475569), 0);
  }
}

static void brightness_changed_cb(lv_event_t *e) {
  lv_obj_t *slider = (lv_obj_t *)lv_event_get_target(e);
  int val = lv_slider_get_value(slider);
  ui_brightness = (uint8_t)val;
  Set_Backlight(ui_brightness);
  char buf[16]; snprintf(buf, sizeof(buf), "%d%%", val);
  lv_label_set_text(lbl_brightness_val, buf);
}

static void volume_changed_cb(lv_event_t *e) {
  lv_obj_t *slider = (lv_obj_t *)lv_event_get_target(e);
  int val = lv_slider_get_value(slider);
  ui_volume = (uint8_t)val;
  char buf[16]; snprintf(buf, sizeof(buf), "%d%%", val);
  lv_label_set_text(lbl_volume_val, buf);
  uint8_t mapped_vol = (uint8_t)(ui_volume * Volume_MAX / 100);
  Volume_adjustment(mapped_vol);
}

void NetworkTask(void *param) {
  start_ibeacon();
  setup_wifi_server();
  while (1) {
    if (WiFi.status() == WL_CONNECTED) server.handleClient();
    vTaskDelay(pdMS_TO_TICKS(5));
  }
}