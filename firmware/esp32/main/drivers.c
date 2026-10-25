#include "drivers.h"
#include "device_config.h"

#include <stdio.h>
#include <string.h>

#include "esp_timer.h"
#include "esp_log.h"

#if LED_KIND
#include "driver/gpio.h"
#endif
#if SERVO_KIND
#include "driver/ledc.h"
#endif
#if DISTANCE_KIND
#include "driver/gpio.h"
#include "esp_rom_sys.h"
#endif

static const char *TAG = "deskpet";

static int s_led[LED_CHANNELS];
static int s_servo_angle[SERVO_CHANNELS];
static int s_audio_playing = 0;

static void copy_why(char *why, size_t size, const char *text)
{
    if (why == NULL || size == 0) {
        return;
    }
    snprintf(why, size, "%s", text == NULL ? "" : text);
}

void drivers_init(void)
{
    for (int i = 0; i < LED_CHANNELS; i++) {
        s_led[i] = 0;
    }
    for (int i = 0; i < SERVO_CHANNELS; i++) {
        s_servo_angle[i] = -1;
    }

#if LED_KIND
    const int led_pins[LED_CHANNELS] = {LED_PIN_0, LED_PIN_1};
    for (int i = 0; i < LED_CHANNELS; i++) {
        gpio_reset_pin(led_pins[i]);
        gpio_set_direction(led_pins[i], GPIO_MODE_OUTPUT);
        gpio_set_level(led_pins[i], 0);
    }
#endif

#if SERVO_KIND
    ledc_timer_config_t timer = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .timer_num = LEDC_TIMER_0,
        .duty_resolution = LEDC_TIMER_13_BIT,
        .freq_hz = SERVO_PWM_HZ,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    ledc_timer_config(&timer);
    const int servo_pins[SERVO_CHANNELS] = {SERVO_PIN_0, SERVO_PIN_1};
    for (int i = 0; i < SERVO_CHANNELS; i++) {
        ledc_channel_config_t channel = {
            .gpio_num = servo_pins[i],
            .speed_mode = LEDC_LOW_SPEED_MODE,
            .channel = (ledc_channel_t)i,
            .timer_sel = LEDC_TIMER_0,
            .duty = 0,
            .hpoint = 0,
        };
        ledc_channel_config(&channel);
    }
#endif
    ESP_LOGW(TAG, "drivers ready, led=%d servo=%d distance=%d audio=%d",
             LED_KIND, SERVO_KIND, DISTANCE_KIND, AUDIO_KIND);
}

// ---------------------------------------------------------------- LED

int led_apply(int channel, bool on, char *why, size_t why_size)
{
    if (channel < 0 || channel >= LED_CHANNELS) {
        copy_why(why, why_size, "没有这个 LED 通道");
        return 1;
    }
    s_led[channel] = on ? 1 : 0;
#if LED_KIND
    const int led_pins[LED_CHANNELS] = {LED_PIN_0, LED_PIN_1};
    gpio_set_level(led_pins[channel], on ? 1 : 0);
#else
    ESP_LOGI(TAG, "led sim: channel %d -> %s", channel, on ? "on" : "off");
#endif
    return 0;
}

int led_state(int channel)
{
    if (channel < 0 || channel >= LED_CHANNELS) {
        return 0;
    }
    return s_led[channel];
}

// ---------------------------------------------------------------- 舵机

int servo_clamp_speed(int speed_deg_s)
{
    if (speed_deg_s <= 0) {
        return SERVO_MAX_SPEED_DEG_S;
    }
    if (speed_deg_s > SERVO_MAX_SPEED_DEG_S) {
        return SERVO_MAX_SPEED_DEG_S;
    }
    return speed_deg_s;
}

int servo_apply(int channel, int angle_deg, int speed_deg_s, long duration_ms, char *why, size_t why_size)
{
    if (channel < 0 || channel >= SERVO_CHANNELS) {
        copy_why(why, why_size, "没有这个舵机通道");
        return 1;
    }
    if (angle_deg < SERVO_MIN_ANGLE || angle_deg > SERVO_MAX_ANGLE) {
        copy_why(why, why_size, "超出机械范围");
        return 2;
    }
    int speed = servo_clamp_speed(speed_deg_s);
    long window = duration_ms;
    if (window < 0) {
        window = 0;
    }
    if (window > SERVO_MAX_DURATION_MS) {
        window = SERVO_MAX_DURATION_MS;
    }

    int previous = s_servo_angle[channel];
    s_servo_angle[channel] = angle_deg;

#if SERVO_KIND
    long span = SERVO_PULSE_MAX_US - SERVO_PULSE_MIN_US;
    long pulse = SERVO_PULSE_MIN_US + span * (angle_deg - SERVO_MIN_ANGLE) / (SERVO_MAX_ANGLE - SERVO_MIN_ANGLE);
    long period = 1000000L / SERVO_PWM_HZ;
    uint32_t duty = (uint32_t)((pulse * 8191L) / period);
    ledc_set_duty(LEDC_LOW_SPEED_MODE, (ledc_channel_t)channel, duty);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, (ledc_channel_t)channel);
#else
    ESP_LOGI(TAG, "servo sim: channel %d %d -> %d deg (speed %d, %ldms)",
             channel, previous, angle_deg, speed, window);
#endif
    return 0;
}

int servo_last_angle(int channel)
{
    if (channel < 0 || channel >= SERVO_CHANNELS) {
        return -1;
    }
    return s_servo_angle[channel];
}

// ---------------------------------------------------------------- 距离

int distance_read(int channel, int *value_mm, char *why, size_t why_size)
{
    if (value_mm != NULL) {
        *value_mm = 0;
    }
    if (channel < 0 || channel >= SENSOR_CHANNELS) {
        copy_why(why, why_size, "没有这个传感器通道");
        return 0;
    }
#if DISTANCE_KIND
    // HC-SR04 这一类：拉低再拉高触发，量回响高电平持续多久。
    gpio_set_direction(DISTANCE_TRIG_PIN, GPIO_MODE_OUTPUT);
    gpio_set_direction(DISTANCE_ECHO_PIN, GPIO_MODE_INPUT);
    gpio_set_level(DISTANCE_TRIG_PIN, 0);
    esp_rom_delay_us(4);
    gpio_set_level(DISTANCE_TRIG_PIN, 1);
    esp_rom_delay_us(10);
    gpio_set_level(DISTANCE_TRIG_PIN, 0);

    int64_t start = esp_timer_get_time();
    while (gpio_get_level(DISTANCE_ECHO_PIN) == 0) {
        if (esp_timer_get_time() - start > 30000) {
            copy_why(why, why_size, "传感器没有回响");
            return 0;
        }
    }
    int64_t echo_start = esp_timer_get_time();
    while (gpio_get_level(DISTANCE_ECHO_PIN) == 1) {
        if (esp_timer_get_time() - echo_start > 60000) {
            copy_why(why, why_size, "回响时间超长，读不到有效距离");
            return 0;
        }
    }
    int64_t micros = esp_timer_get_time() - echo_start;
    if (value_mm != NULL) {
        // 声速按 343 m/s 算：每厘米约 58 微秒。
        *value_mm = (int)(micros / 58);
    }
    return 1;
#else
    copy_why(why, why_size, "这一版没有接距离传感器，读不到数");
    return 0;
#endif
}

// ---------------------------------------------------------------- 音频

int audio_supported(void)
{
    return CAP_AUDIO && AUDIO_KIND;
}

const char *audio_tracks_json(void)
{
#if CAP_AUDIO && AUDIO_KIND
    return "[\"boot\",\"confirm\",\"done\"]";
#else
    return "[]";
#endif
}

int audio_play(const char *track_id, int volume, char *why, size_t why_size)
{
    if (!audio_supported()) {
        copy_why(why, why_size, "这一版没有音频输出");
        return 1;
    }
    (void)volume;
    ESP_LOGI(TAG, "audio sim: play %s", track_id == NULL ? "(空)" : track_id);
    s_audio_playing = 1;
    return 0;
}

int audio_playing(void)
{
    return s_audio_playing;
}

// ---------------------------------------------------------------- 安全

void drivers_stop_all(void)
{
    for (int i = 0; i < LED_CHANNELS; i++) {
        char why[32];
        led_apply(i, false, why, sizeof(why));
    }
    s_audio_playing = 0;
    ESP_LOGW(TAG, "stop: leds off, audio stopped");
}

void drivers_safety_idle(void)
{
    // 长时间没有有效指令：把持续动作收掉。舵机不动它，避免"停"反而动一次。
    s_audio_playing = 0;
    ESP_LOGW(TAG, "safety idle: 长时间没有指令，持续动作已收掉");
}
