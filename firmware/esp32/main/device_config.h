// 设备身份、能力开关、通道数量、机械范围和引脚。
//
// 这里每一个数字都要照着你自己的板子和接线改。不认识型号的外设一律先用模拟驱动
// （KIND_SIM），模拟驱动只记日志、不动引脚，免得照着猜出来的接线把硬件弄坏。
#pragma once

// ---------- 设备身份 ----------
// 要和电脑端设备清单里的名字对得上。
#define DEVICE_ID "esp32-main"
#define FIRMWARE_VERSION "2.0.0"
#define PROTOCOL_VERSION 1

// ---------- 能力开关 ----------
// 关掉的能力，固件会直接拒绝，不会假装支持。
#define CAP_STATUS 1
#define CAP_LED 1
#define CAP_SERVO 1
#define CAP_SENSOR 1
#define CAP_AUDIO 0
#define CAP_STOP 1

// ---------- 通道数量 ----------
// 电脑端按这里的数字校验参数范围，写错会点错灯、转错舵机。
#define LED_CHANNELS 2
#define SERVO_CHANNELS 2
#define SENSOR_CHANNELS 1

// ---------- 驱动种类 ----------
// 0 = 模拟驱动（只记日志，不碰引脚），1 = 真实驱动。
// 型号没确认之前保持 0，改这里比改接线安全。
#define LED_KIND 0
#define SERVO_KIND 0
#define DISTANCE_KIND 0
#define AUDIO_KIND 0

// ---------- 机械范围 ----------
// 这三个数字是安全边界，必须按实际结构填。默认值很保守，不是给舵机用的。
#define SERVO_MIN_ANGLE 10
#define SERVO_MAX_ANGLE 170
// 最快的转速和单次最长动作时间，防止一直顶着结构件转。
#define SERVO_MAX_SPEED_DEG_S 180
#define SERVO_MAX_DURATION_MS 2000
// 舵机 PWM：50Hz 是常见舵机的规格。
#define SERVO_PWM_HZ 50
#define SERVO_PULSE_MIN_US 500
#define SERVO_PULSE_MAX_US 2500

// ---------- 引脚 ----------
// 按实际接线改。LED_KIND/SERVO_KIND 为 0 时这些值不会被用到。
#define LED_PIN_0 2
#define LED_PIN_1 4
#define SERVO_PIN_0 13
#define SERVO_PIN_1 12
#define DISTANCE_TRIG_PIN 5
#define DISTANCE_ECHO_PIN 18

// ---------- 串口 ----------
#define SERIAL_BAUD 115200
// 一行报文最多多少字节，超过就整行丢掉。要和电脑端的 MAX_LINE_BYTES 对得上。
#define LINE_MAX 1024

// ---------- 安全 ----------
// 这么久没收到有效指令，就把持续动作停到安全状态。
#define SAFETY_IDLE_MS 5000
// 同一单重复发来时，记住多少条 message_id 用来去重。
#define SEEN_LIMIT 32
