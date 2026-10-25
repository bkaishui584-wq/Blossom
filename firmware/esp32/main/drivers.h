// 外设驱动接口。
//
// 每个外设都从这里进，命令层不直接碰引脚。驱动分两种实现：
//   KIND 0：模拟驱动，只记日志、不动引脚。型号没确认之前用这个。
//   KIND 1：真实驱动，按 device_config.h 的引脚和范围动作。
// 两种实现返回同样的结果，所以电脑端和测试不用分情况。
#pragma once

#include <stdbool.h>
#include <stddef.h>

void drivers_init(void);

// ---------- LED ----------
// 返回 0 成功，非 0 失败并把原因写进 why。
int led_apply(int channel, bool on, char *why, size_t why_size);
int led_state(int channel);

// ---------- 舵机 ----------
// speed_deg_s 和 duration_ms 允许为 0，表示「用固件自己的保守默认」。
int servo_apply(int channel, int angle_deg, int speed_deg_s, long duration_ms, char *why, size_t why_size);
// 最近一次转到的角度；-1 表示这一路还没动过。
int servo_last_angle(int channel);
// 把请求的速度收进安全范围，返回真正用的值。
int servo_clamp_speed(int speed_deg_s);

// ---------- 距离传感器 ----------
// 返回 1 表示这次测到了有效值，0 表示没测到（为什么写在 why 里，不拿 0 冒充读数）。
int distance_read(int channel, int *value_mm, char *why, size_t why_size);

// ---------- 音频 ----------
int audio_supported(void);
int audio_play(const char *track_id, int volume, char *why, size_t why_size);
int audio_playing(void);
// 已登记的曲目名，拼成 JSON 数组的样子。没有就返回 "[]"。
const char *audio_tracks_json(void);

// ---------- 安全 ----------
// 停掉所有持续动作（灯灭、运动窗口清掉）。和 device.stop 是一个意思。
void drivers_stop_all(void);
// 太久没有有效指令时调用：回到安全状态。
void drivers_safety_idle(void);
