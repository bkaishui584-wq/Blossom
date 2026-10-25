# DeskPet ESP32 设备固件（ESP-IDF）

这一份是 DeskPet 电脑端「设备中心」对应的设备端固件，ESP-IDF v5.4.4 工程。
电脑端发结构化动作，固件自己再验一遍参数，执行完把真实结果回过去。
电脑端只认设备回过来的确认，发出去没等到确认不算完成。

**当前状态：编译通过，没有在真机上烧写过。** 也就是没接过舵机、没接过传感器、
没接过喇叭。所有外设驱动默认是模拟实现（只记日志、不动引脚），要接真硬件得先把
`main/device_config.h` 里的型号、引脚和机械范围填对。

## 一、文件

| 文件 | 干什么 |
| --- | --- |
| `CMakeLists.txt` / `main/CMakeLists.txt` | 工程骨架 |
| `sdkconfig.defaults` | 关掉 IDF 自带控制台，串口只跑协议；打开任务看门狗 |
| `main/device_config.h` | 设备身份、能力开关、通道数、机械范围、引脚。**要改的就是这一份** |
| `main/app_main.c` | 收串口、按行分帧、分发、看门狗、安全空闲 |
| `main/commands.c` | 动作白名单、独立参数校验、错误码、去重 |
| `main/drivers.c` | LED / 舵机 / 距离 / 音频，模拟与真实两套实现 |
| `main/json_wire.c` | 极小的 JSON 取值与拼装，不引第三方库 |

## 二、编译

**先记住一件事：工程路径不能有中文。** ESP-IDF 的 kconfig 处理不了非 ASCII 路径，
放在 `…\ChatGPT\项目\deskpet\firmware\esp32` 下会直接配置失败。编译时请把工程复制到
纯英文路径（例如 `C:\esp\deskpet-esp32`），或者在英文路径下检出这份代码。

```powershell
# 1. 让 IDF 用自带的那份 Python 3.11（系统默认若是 3.13，export 会找不到环境）
$env:PATH = "D:\Espressif\tools\idf-python\3.11.2;" + $env:PATH
. D:\Espressif\frameworks\esp-idf-v5.4.4\export.ps1

# 2. 在纯英文路径里编译
cd C:\esp\deskpet-esp32
idf.py set-target esp32
idf.py build
```

本工程不负责烧写。刷机请自己确认端口和板子型号：

```powershell
idf.py -p COM7 flash monitor
```

## 三、串口与日志分开

`sdkconfig.defaults` 里设了 `CONFIG_ESP_CONSOLE_UART_NONE=y`：IDF 自己的控制台不占
UART0，UART0 完全归应用，只发 JSON，日志不会和协议挤在同一行上。

要看日志时把这一行改成 `CONFIG_ESP_CONSOLE_UART_DEFAULT=y`，日志会和协议混在一条线上。
电脑端会把看不懂的行记一笔（不影响功能），但不干净。

## 四、支持的动作

| 动作 | 参数 | 回报 |
| --- | --- | --- |
| `device.get_status` | 无 | 固件版本、能力、范围、灯的开关、舵机上次角度、运行毫秒 |
| `device.get_capabilities` | 无 | 协议版本、固件版本、能力列表、参数范围 |
| `sensor.read` | `channel` | `valid`、`value`、`unit`、`measured_at_ms`、`error` |
| `servo.set_angle` | `channel`、`angle_deg`，可选 `speed_deg_s`、`duration_ms` | 通道、最终角度、实际用的速度 |
| `audio.play` | `track_id`，可选 `volume` | 曲目名（只放已登记的名字） |
| `led.set` | `channel`、`on` | 通道和最终状态 |
| `device.stop` | 无 | 停掉持续动作并关灯 |

没登记的动作回 `rejected` 加错误码，不会照做。参数在固件里再验一遍，不靠电脑端把关。

## 五、安全

- 舵机角度、速度、持续时间都有上限，全部在 `device_config.h` 里。默认值很保守，
  必须按实际机械结构改。
- 5 秒没收到有效指令就把持续动作收掉（`SAFETY_IDLE_MS`）。
- 任务看门狗 10 秒，卡死就复位，不让舵机一直顶在某个位置。
- 同一单重发只回上次那一帧，不重复执行。
- 命令层不碰引脚：引脚只在驱动层出现。

**`device.stop` 不是硬件急停。** 它只让固件把当前动作收掉，救不了固件本身卡死、
电源掉电或者舵机被结构件卡住这种情况。真要急停请另外加断电开关。

## 六、外设怎么接

四种外设各有 `*_KIND` 开关，默认全是 0（模拟驱动）。填型号之前不要改成 1。

| 外设 | 开关 | 要准备的信息 |
| --- | --- | --- |
| LED | `LED_KIND` | 几个灯、接哪几个 GPIO、高电平亮还是低电平亮 |
| 舵机 | `SERVO_KIND` | 型号、供电电压、信号 GPIO、机械能转的安全角度范围 |
| 距离传感器 | `DISTANCE_KIND` | 型号（超声波还是激光）、TRIG/ECHO 或 I2C 接线 |
| 音频 | `AUDIO_KIND` | 功放型号（I2S 还是 PWM）、喇叭、音量、要用哪几段预录音 |
