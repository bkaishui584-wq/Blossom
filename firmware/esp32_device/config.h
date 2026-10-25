// ESP32 设备固件配置。
//
// 每一个数字都要照着你的实际接线和机械结构填，不要照抄这一份：
// 舵机转不出这个范围会顶到结构件，LED 通道写错会点错灯。
#pragma once

// 设备身份。要和电脑端设备清单里的名字对得上。
#define DEVICE_ID "esp32-main"
#define FIRMWARE_VERSION "1.0.0"

// 能力开关。关掉的能力，固件会直接拒绝对应动作，不会假装支持。
#define CAP_STATUS 1
#define CAP_LED 1
#define CAP_SERVO 1
#define CAP_SENSOR 1
#define CAP_STOP 1

// 通道数量。电脑端会按这里的数字校验参数范围。
#define LED_CHANNELS 2
#define SERVO_CHANNELS 2
#define SENSOR_CHANNELS 1

// 机械范围（度）。这两个数字是安全边界，必须按实际结构填。
#define SERVO_MIN_ANGLE 10
#define SERVO_MAX_ANGLE 170
// 转到目标角度最多等多久，超了就按失败回报。
#define SERVO_MOVE_MS 900UL

// 引脚。按实际接线改。
#define LED_PIN_0 2
#define LED_PIN_1 4
#define SERVO_PIN_0 13
#define SERVO_PIN_1 12
#define SENSOR_PIN_34_ADC 34

// 串口参数。要和电脑端设备清单里的 baudrate 一致。
#define SERIAL_BAUD 115200
// 一行报文最长多少字节，超了直接丢。
#define LINE_MAX 256

// 软件看门狗：这么久没收到有效指令，就把持续动作停到安全状态。
#define SAFETY_IDLE_MS 5000UL