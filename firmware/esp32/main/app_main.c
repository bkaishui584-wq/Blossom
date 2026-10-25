// DeskPet ESP32 设备固件主程序。
//
// 干的事就一件：从串口一行一行收报文，交给命令层，再把回复原样写回去。
// 电脑端只认这里发出去的确认，所以这边绝不"先回完成再做事"。
#include "commands.h"
#include "device_config.h"
#include "drivers.h"
#include "json_wire.h"

#include <stdio.h>
#include <string.h>

#include "driver/uart.h"
#include "esp_log.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"

static const char *TAG = "deskpet";

#define UART_PORT UART_NUM_0
#define RX_BUFFER 2048

static char s_line[LINE_MAX];
static size_t s_used = 0;
static int s_overflow = 0;
static int64_t s_last_command_us = 0;

static void emit_line(const char *line)
{
    if (line == NULL || line[0] == '\0') {
        return;
    }
    uart_write_bytes(UART_PORT, line, strlen(line));
    uart_write_bytes(UART_PORT, "\n", 1);
}

static void uart_setup(void)
{
    const uart_config_t config = {
        .baud_rate = SERIAL_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    // 收发缓冲都给上，收慢一点也不会丢行。
    uart_driver_install(UART_PORT, RX_BUFFER, RX_BUFFER, 0, NULL, 0);
    uart_param_config(UART_PORT, &config);
    uart_set_pin(UART_PORT, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE,
                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
}

// 开机先把身份报出来：电脑端不用猜对面是什么固件、会做什么。
static void announce_boot(void)
{
    char line[512];
    snprintf(line, sizeof(line),
             "{\"version\":%d,\"type\":\"event\",\"device_id\":\"%s\",\"event\":\"device.state\","
             "\"data\":{\"boot\":true,\"firmware\":\"%s\",\"protocol_version\":%d,"
             "\"capabilities\":[%s],\"limits\":{%s}}}",
             PROTOCOL_VERSION, DEVICE_ID, FIRMWARE_VERSION, PROTOCOL_VERSION,
             commands_capabilities_json(), commands_limits_json());
    emit_line(line);
}

static void handle_line(void)
{
    if (s_used == 0) {
        return;
    }
    s_line[s_used] = '\0';
    s_used = 0;

    char reply[LINE_MAX];
    if (commands_handle_line(s_line, reply, sizeof(reply))) {
        emit_line(reply);
        s_last_command_us = esp_timer_get_time();
    }
    s_line[0] = '\0';
}

void app_main(void)
{
    commands_boot_marker();
    drivers_init();
    uart_setup();

    // 看门狗：卡死了要能自己复位，别让持续动作一直挂着。
    esp_task_wdt_add(NULL);
    esp_task_wdt_reset();

    s_last_command_us = esp_timer_get_time();
    announce_boot();

    unsigned char chunk[128];
    while (1) {
        int count = uart_read_bytes(UART_PORT, chunk, sizeof(chunk), pdMS_TO_TICKS(20));
        for (int i = 0; i < count; i++) {
            unsigned char c = chunk[i];
            if (c == '\n' || c == '\r') {
                if (s_overflow) {
                    // 超长的那一行整行丢掉，不拿半截报文去执行。
                    ESP_LOGW(TAG, "line too long, dropped");
                    s_overflow = 0;
                    s_used = 0;
                    continue;
                }
                handle_line();
                continue;
            }
            if (s_overflow) {
                continue;
            }
            if (s_used + 1 >= sizeof(s_line)) {
                s_overflow = 1;
                s_used = 0;
                continue;
            }
            s_line[s_used++] = (char)c;
        }

        // 太久没有有效指令：把持续动作收掉。
        if ((esp_timer_get_time() - s_last_command_us) / 1000 > SAFETY_IDLE_MS) {
            drivers_safety_idle();
            s_last_command_us = esp_timer_get_time();
        }
        esp_task_wdt_reset();
    }
}
