#pragma once
#include <lvgl.h>
#include "LVGL_Driver.h"
#include "BAT_Driver.h"
#include "Gyro_QMI8658.h"

void UI_Init(void);
void FallDetection_Loop(void);
