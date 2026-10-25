# ESP32 设备固件（esp32_device）

这一份是 DeskPet 电脑端「设备中心」对应的设备端固件。电脑端发结构化动作，固件验一遍
再执行，并把真实结果回给电脑端。电脑端只认设备的确认，发出去没等确认不算完成。

**当前状态：这一份没有在真机上编译、烧写和接线验证过。** 接线之前请先在 Arduino IDE
里编译通过，再把 `config.h` 里的范围改成你实际机械结构的数字。没验证过的能力，固件
和电脑端都不会报成可用。

**这一份已经被 `firmware/esp32/`（ESP-IDF v5.4.4 工程）取代。** 留着是因为它短、
好读，适合对照。正式用的那一份是 ESP-IDF 工程。两者差别在协议上：这一份的报文没有
`type` 字段，舵机参数叫 `angle`，新工程按任务书叫 `angle_deg`，并且多了消息类型
校验、`rejected` 状态和错误码。电脑端现在按新协议收发，所以这一份发出去的旧格式
回复会被判成不合协议。

## 一、文件

- `esp32_device.ino`：主程序。收报文、验协议、查白名单、执行、回确认。
- `config.h`：设备身份、能力开关、通道数量、机械范围、引脚和串口参数。**要改的就是这一份**。

## 二、支持的动作

| 动作 | 参数 | 回报 |
| --- | --- | --- |
| `device.get_status` | 无 | 固件版本、能力列表、参数范围、运行秒数、灯与运动状态 |
| `led.set` | `channel` 整数、`on` 布尔 | 通道和最终状态 |
| `servo.set_angle` | `channel` 整数、`angle` 整数 | 通道和目标角度 |
| `sensor.read` | `channel` 整数 | 原始 ADC 读数（`unit: raw`），不做单位换算 |
| `device.stop` | 无 | 停掉持续运动并把所有灯关掉 |

没有登记的动作一律拒绝，并回一句失败原因。参数一律以 `config.h` 里的范围为准再验一遍，
不靠电脑端把关。

### 这一份没有实现的动作

- `device.get_capabilities`：能力只在开机和 `device.get_status` 里报一次，没有单独的问询入口。
- `audio.play`：这一份没有音频输出。

这两条会被固件当成没登记的动作拒掉，不会假装做过。完整动作白名单以
`firmware/esp32/` 为准。

## 三、协议

一行一帧 JSON，UTF-8，一行不超过 512 字节。

电脑端发：

```json
{"version":1,"message_id":"msg-abc123","device_id":"esp32-main","command":"led.set","params":{"channel":0,"on":true}}
```

固件回：

```json
{"version":1,"message_id":"msg-abc123","device_id":"esp32-main","status":"completed","result":{"channel":0,"on":true},"error":null}
```

- `message_id` 只认字母数字下划线连字符，长度 1-64。对不上单的报文不回复（回了也没法对应）。
- `status` 只会出现：`accepted`、`running`、`completed`、`failed`、`cancelled`、`timeout`、`device_disconnected`。
- 失败一定带 `error`，说明是哪一步不行。
- 协议版本不是 1、设备名对不上、命令不在白名单、参数越界，都会回 `failed` 并说明原因。

## 四、把电脑端接上

在设备清单（数据目录里的 `devices.json`）里写一条，端口填你自己的：

```json
{
  "devices": [
    {
      "name": "esp32-main",
      "kind": "esp32",
      "capabilities": ["status", "led", "servo", "stop"],
      "options": {
        "port": "COM3",
        "baudrate": 115200,
        "limits": {
          "led_channels": {"min": 0, "max": 1},
          "servo_channels": {"min": 0, "max": 1},
          "servo_angle": {"min": 10, "max": 170}
        }
      }
    }
  ]
}
```

两点要注意：

1. 真串口还要把 `DESKPET_ALLOW_REAL_DEVICES` 打开（`.env` 里设成 1），否则电脑端直接拒收真机。
2. 清单里的 `limits` 是电脑端的参数范围。固件自己还会再验一遍，两边都要改，别只改一处。

端口和范围也可以在设备中心页面上看到；固件连上后会把自己那份范围报回来，电脑端以
固件报的为准。

## 五、编译与烧写

Arduino IDE：

1. 打开 `esp32_device.ino`（`config.h` 在同一个目录里，会自动带上）。
2. 开发板选你自己的 ESP32 型号，端口选对。
3. 直接编译。**舵机是可选的**：没装 `ESP32Servo` 库时固件照样编译通过，
   舵机能力会自动关掉，`servo.set_angle` 会回「固件没有装舵机库」。
   要用舵机就在库管理器里装 `ESP32Servo` 再编译。
4. 烧写，打开串口监视器（115200）应当先看到一行 `{"boot":1,...}`，里面带着版本和能力。

PlatformIO：把这一份放进 `src/`（`.ino` 改名为 `main.cpp`，并去掉 Arduino 自动生成函数原型的
依赖）或者用 `platform = espressif32` + `framework = arduino` 直接把 sketch 目录当项目。
舵机库按需在 `lib_deps` 里加 `madhephaestus/ESP32Servo`。

## 六、安全

- **角度范围**：`SERVO_MIN_ANGLE` / `SERVO_MAX_ANGLE` 是机械安全边界。第一次接舵机时先把
  范围收窄，确认结构不会被顶到，再慢慢放宽。没确认过就不要写 0-180。
- **`device.stop`**：会停掉持续运动并把所有灯关掉，这是安全方向。它的效果取决于固件真的
  实现了这段逻辑——电脑端的「紧急停止」按钮也只做到这一步，硬件急停请另外做（断电开关或
  限位）。
- **看门狗**：现在是软件看门狗（`SAFETY_IDLE_MS`，太久没收到有效指令就把运动停到安全状态）。
  硬件看门狗（`esp_task_wdt`）没有接，请按你用的 ESP32 内核版本自己加上。
- **限位与夹伤**：会动的机构请加物理限位。电脑端的确认弹窗只是流程，不是物理保护。
- 固件不解析模型包里的任何脚本，也不接受下载指令。

## 七、还没做的

- 没有在真机上验证过（编译、烧写、协议往返都没跑过）。
- 没有做 Wi-Fi / MQTT，目前只有 USB 串口。
- `sensor.read` 只回原始 ADC 读数，没有量纲、没有标定。
- 没有做多设备寻址（`device_id` 只认 `config.h` 里那一个）。
