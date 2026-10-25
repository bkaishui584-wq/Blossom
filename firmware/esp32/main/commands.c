#include "commands.h"
#include "device_config.h"
#include "drivers.h"
#include "json_wire.h"

#include <stdio.h>
#include <string.h>

#include "esp_timer.h"
#include "esp_log.h"

static const char *TAG = "deskpet";

#define REPLY_MAX 900
#define ID_MAX 72
// 记住多少条已回复的 message_id 用来去重。电脑端重发同一单时原样回上次那一帧。
#define SEEN 16

static char s_seen_id[SEEN][ID_MAX];
static char s_seen_reply[SEEN][REPLY_MAX];
static int s_seen_next = 0;
static int s_seen_count = 0;

static int64_t s_boot_us = 0;
static long s_accepted = 0;
static long s_rejected = 0;

// ---------------------------------------------------------------- 错误码

// 要和电脑端 devices/protocol.py 里的 ERROR_* 对得上。
#define EC_NONE ""
#define EC_BAD_JSON "bad_json"
#define EC_BAD_VERSION "bad_version"
#define EC_BAD_TYPE "bad_type"
#define EC_MISSING_FIELD "missing_field"
#define EC_UNKNOWN_COMMAND "unknown_command"
#define EC_BAD_PARAM "bad_param"
#define EC_OUT_OF_RANGE "out_of_range"
#define EC_UNSUPPORTED "unsupported"
#define EC_BUSY "busy"
#define EC_INTERNAL "internal"
// 下面两个固件自己不会发：链路断了它没办法回报。但错误码表两端共用，
// 少一个就会漂移，所以照样定义在这里。
#define EC_NOT_CONNECTED "not_connected"
#define EC_TIMEOUT "timeout"

static void write_reply(char *out, size_t out_size, const char *message_id, const char *status,
                        const char *error_code, const char *result_json, const char *error_text)
{
    char escaped[256];
    jw_escape(error_text == NULL ? "" : error_text, escaped, sizeof(escaped));
    if (error_text == NULL || error_text[0] == '\0') {
        snprintf(out, out_size,
                 "{\"version\":%d,\"type\":\"response\",\"message_id\":\"%s\",\"device_id\":\"%s\","
                 "\"status\":\"%s\",\"result\":%s,\"error\":null,\"error_code\":\"%s\"}",
                 PROTOCOL_VERSION, message_id, DEVICE_ID, status,
                 (result_json == NULL || result_json[0] == '\0') ? "{}" : result_json,
                 error_code == NULL ? "" : error_code);
        return;
    }
    snprintf(out, out_size,
             "{\"version\":%d,\"type\":\"response\",\"message_id\":\"%s\",\"device_id\":\"%s\","
             "\"status\":\"%s\",\"result\":%s,\"error\":\"%s\",\"error_code\":\"%s\"}",
             PROTOCOL_VERSION, message_id, DEVICE_ID, status,
             (result_json == NULL || result_json[0] == '\0') ? "{}" : result_json,
             escaped, error_code == NULL ? "" : error_code);
}

// ---------------------------------------------------------------- 能力与范围

static void append_flag(char *out, size_t out_size, int on, const char *name)
{
    if (!on) {
        return;
    }
    size_t used = strlen(out);
    if (used + strlen(name) + 5 >= out_size) {
        return;
    }
    if (used > 1) {
        strncat(out, ",", out_size - used - 1);
    }
    strncat(out, "\"", out_size - strlen(out) - 1);
    strncat(out, name, out_size - strlen(out) - 1);
    strncat(out, "\"", out_size - strlen(out) - 1);
}

const char *commands_capabilities_json(void)
{
    static char buffer[128];
    buffer[0] = '\0';
    append_flag(buffer, sizeof(buffer), CAP_STATUS, "status");
    append_flag(buffer, sizeof(buffer), CAP_LED, "led");
    append_flag(buffer, sizeof(buffer), CAP_SERVO, "servo");
    append_flag(buffer, sizeof(buffer), CAP_SENSOR, "sensor");
    // 音频只有真正接上才报出来，没接就不报。
    append_flag(buffer, sizeof(buffer), audio_supported(), "audio");
    append_flag(buffer, sizeof(buffer), CAP_STOP, "stop");
    return buffer;
}

const char *commands_limits_json(void)
{
    static char buffer[512];
    int used = snprintf(buffer, sizeof(buffer),
                        "\"led_channels\":{\"min\":0,\"max\":%d},"
                        "\"servo_channels\":{\"min\":0,\"max\":%d},"
                        "\"servo_angle\":{\"min\":%d,\"max\":%d},"
                        "\"servo_speed\":{\"min\":1,\"max\":%d},"
                        "\"servo_duration_ms\":{\"min\":0,\"max\":%d},"
                        "\"sensor_channels\":{\"min\":0,\"max\":%d}",
                        LED_CHANNELS - 1, SERVO_CHANNELS - 1,
                        SERVO_MIN_ANGLE, SERVO_MAX_ANGLE,
                        SERVO_MAX_SPEED_DEG_S, SERVO_MAX_DURATION_MS,
                        SENSOR_CHANNELS - 1);
    if (audio_supported() && used > 0 && (size_t)used < sizeof(buffer)) {
        snprintf(buffer + used, sizeof(buffer) - (size_t)used,
                 ",\"audio_tracks\":%s,\"audio_volume\":{\"min\":0,\"max\":100}",
                 audio_tracks_json());
    }
    return buffer;
}

static long uptime_ms(void)
{
    return (long)((esp_timer_get_time() - s_boot_us) / 1000);
}

// ---------------------------------------------------------------- 各条动作

// 每条动作自己验参数。返回 0 成功；非 0 时把错误码和原因写进 out 参数。
typedef int (*handler_t)(const char *params, char *result, size_t result_size,
                         const char **code, const char **why);

static int check_channel(const char *params, const char *name, int max_exclusive, int *out,
                         const char **code, const char **why)
{
    long value = 0;
    if (!jw_has(params, name)) {
        *code = EC_MISSING_FIELD;
        *why = "少了参数 channel";
        return 1;
    }
    if (!jw_get_int(params, name, &value)) {
        *code = EC_BAD_PARAM;
        *why = "channel 要是整数";
        return 1;
    }
    if (value < 0 || value >= max_exclusive) {
        *code = EC_OUT_OF_RANGE;
        *why = "channel 超出这台设备的通道数";
        return 1;
    }
    *out = (int)value;
    return 0;
}

static int do_get_status(const char *params, char *result, size_t result_size,
                         const char **code, const char **why)
{
    (void)params;
    (void)code;
    (void)why;
    char leds[96];
    int used = snprintf(leds, sizeof(leds), "{");
    for (int i = 0; i < LED_CHANNELS && used > 0 && (size_t)used < sizeof(leds); i++) {
        used += snprintf(leds + used, sizeof(leds) - (size_t)used, "%s\"%d\":%s",
                         i ? "," : "", i, led_state(i) ? "true" : "false");
    }
    if (used > 0 && (size_t)used < sizeof(leds)) {
        snprintf(leds + used, sizeof(leds) - (size_t)used, "}");
    }

    char servos[128];
    used = snprintf(servos, sizeof(servos), "{");
    for (int i = 0; i < SERVO_CHANNELS && used > 0 && (size_t)used < sizeof(servos); i++) {
        used += snprintf(servos + used, sizeof(servos) - (size_t)used, "%s\"%d\":%d",
                         i ? "," : "", i, servo_last_angle(i));
    }
    if (used > 0 && (size_t)used < sizeof(servos)) {
        snprintf(servos + used, sizeof(servos) - (size_t)used, "}");
    }

    snprintf(result, result_size,
             "{\"device_id\":\"%s\",\"firmware\":\"%s\",\"protocol_version\":%d,"
             "\"capabilities\":[%s],\"limits\":{%s},\"led\":%s,\"servo\":%s,"
             "\"audio\":{\"playing\":%s},\"uptime_ms\":%ld,\"accepted\":%ld,\"rejected\":%ld}",
             DEVICE_ID, FIRMWARE_VERSION, PROTOCOL_VERSION,
             commands_capabilities_json(), commands_limits_json(), leds, servos,
             audio_playing() ? "true" : "false", uptime_ms(), s_accepted, s_rejected);
    return 0;
}

static int do_get_capabilities(const char *params, char *result, size_t result_size,
                               const char **code, const char **why)
{
    (void)params;
    (void)code;
    (void)why;
    snprintf(result, result_size,
             "{\"device_id\":\"%s\",\"firmware\":\"%s\",\"protocol_version\":%d,"
             "\"capabilities\":[%s],\"limits\":{%s}}",
             DEVICE_ID, FIRMWARE_VERSION, PROTOCOL_VERSION,
             commands_capabilities_json(), commands_limits_json());
    return 0;
}

static int do_led(const char *params, char *result, size_t result_size,
                  const char **code, const char **why)
{
    int channel = 0;
    if (check_channel(params, "channel", LED_CHANNELS, &channel, code, why)) {
        return 1;
    }
    int on = 0;
    if (!jw_has(params, "on")) {
        *code = EC_MISSING_FIELD;
        *why = "少了参数 on";
        return 1;
    }
    if (!jw_get_bool(params, "on", &on)) {
        *code = EC_BAD_PARAM;
        *why = "on 要是 true 或 false";
        return 1;
    }
    char detail[48];
    if (led_apply(channel, on != 0, detail, sizeof(detail)) != 0) {
        *code = EC_UNSUPPORTED;
        *why = "这一路灯打不开";
        return 1;
    }
    snprintf(result, result_size, "{\"channel\":%d,\"on\":%s}", channel, on ? "true" : "false");
    return 0;
}

static int do_servo(const char *params, char *result, size_t result_size,
                    const char **code, const char **why)
{
    int channel = 0;
    if (check_channel(params, "channel", SERVO_CHANNELS, &channel, code, why)) {
        return 1;
    }
    long angle = 0;
    if (!jw_has(params, "angle_deg")) {
        *code = EC_MISSING_FIELD;
        *why = "少了参数 angle_deg";
        return 1;
    }
    if (!jw_get_int(params, "angle_deg", &angle)) {
        *code = EC_BAD_PARAM;
        *why = "angle_deg 要是整数";
        return 1;
    }
    if (angle < SERVO_MIN_ANGLE || angle > SERVO_MAX_ANGLE) {
        *code = EC_OUT_OF_RANGE;
        *why = "超出机械范围";
        return 1;
    }
    long speed = 0;
    if (jw_has(params, "speed_deg_s") && !jw_get_int(params, "speed_deg_s", &speed)) {
        *code = EC_BAD_PARAM;
        *why = "speed_deg_s 要是整数";
        return 1;
    }
    long duration = 0;
    if (jw_has(params, "duration_ms") && !jw_get_int(params, "duration_ms", &duration)) {
        *code = EC_BAD_PARAM;
        *why = "duration_ms 要是整数";
        return 1;
    }
    if (duration < 0 || duration > SERVO_MAX_DURATION_MS) {
        *code = EC_OUT_OF_RANGE;
        *why = "持续时间超出允许范围";
        return 1;
    }
    char detail[48];
    int rc = servo_apply(channel, (int)angle, (int)speed, duration, detail, sizeof(detail));
    if (rc != 0) {
        *code = (rc == 2) ? EC_OUT_OF_RANGE : EC_UNSUPPORTED;
        *why = detail[0] ? detail : "这一路舵机转不了";
        return 1;
    }
    snprintf(result, result_size, "{\"channel\":%d,\"angle_deg\":%ld,\"speed_deg_s\":%d}",
             channel, angle, servo_clamp_speed((int)speed));
    return 0;
}

static int do_sensor(const char *params, char *result, size_t result_size,
                     const char **code, const char **why)
{
    int channel = 0;
    if (check_channel(params, "channel", SENSOR_CHANNELS, &channel, code, why)) {
        return 1;
    }
    int value = 0;
    char detail[64];
    detail[0] = '\0';
    int valid = distance_read(channel, &value, detail, sizeof(detail));
    if (!valid) {
        // 读不到就如实说读不到，不拿 0 冒充测量值。
        snprintf(result, result_size,
                 "{\"channel\":%d,\"valid\":false,\"value\":null,\"unit\":\"mm\","
                 "\"measured_at_ms\":%ld,\"error\":\"%s\"}",
                 channel, uptime_ms(), detail[0] ? detail : "这次没测到");
        return 0;
    }
    snprintf(result, result_size,
             "{\"channel\":%d,\"valid\":true,\"value\":%d,\"unit\":\"mm\","
             "\"measured_at_ms\":%ld,\"error\":\"\"}",
             channel, value, uptime_ms());
    return 0;
}

static int do_audio(const char *params, char *result, size_t result_size,
                    const char **code, const char **why)
{
    if (!audio_supported()) {
        *code = EC_UNSUPPORTED;
        *why = "这一版没有音频输出";
        return 1;
    }
    char track[48];
    if (!jw_get_string(params, "track_id", track, sizeof(track))) {
        *code = EC_MISSING_FIELD;
        *why = "少了参数 track_id";
        return 1;
    }
    long volume = 0;
    int has_volume = jw_has(params, "volume");
    if (has_volume && !jw_get_int(params, "volume", &volume)) {
        *code = EC_BAD_PARAM;
        *why = "volume 要是整数";
        return 1;
    }
    if (has_volume && (volume < 0 || volume > 100)) {
        *code = EC_OUT_OF_RANGE;
        *why = "音量超出 0-100";
        return 1;
    }
    char detail[48];
    if (audio_play(track, (int)volume, detail, sizeof(detail)) != 0) {
        *code = EC_UNSUPPORTED;
        *why = detail[0] ? detail : "放不了这段提示音";
        return 1;
    }
    snprintf(result, result_size, "{\"track_id\":\"%s\",\"playing\":true}", track);
    return 0;
}

static int do_stop(const char *params, char *result, size_t result_size,
                   const char **code, const char **why)
{
    (void)params;
    (void)code;
    (void)why;
    drivers_stop_all();
    snprintf(result, result_size, "{\"stopped\":true,\"leds_off\":true}");
    return 0;
}

typedef struct {
    const char *name;
    int capability;
    handler_t run;
} command_t;

static const command_t COMMANDS[] = {
    {"device.get_status", CAP_STATUS, do_get_status},
    {"device.get_capabilities", CAP_STATUS, do_get_capabilities},
    {"sensor.read", CAP_SENSOR, do_sensor},
    {"led.set", CAP_LED, do_led},
    {"audio.play", CAP_AUDIO, do_audio},
    {"servo.set_angle", CAP_SERVO, do_servo},
    {"device.stop", CAP_STOP, do_stop},
};

static const command_t *find_command(const char *name)
{
    for (size_t i = 0; i < sizeof(COMMANDS) / sizeof(COMMANDS[0]); i++) {
        if (strcmp(COMMANDS[i].name, name) == 0) {
            return &COMMANDS[i];
        }
    }
    return NULL;
}

// ---------------------------------------------------------------- 去重

static const char *seen_lookup(const char *message_id)
{
    for (int i = 0; i < SEEN; i++) {
        if (s_seen_id[i][0] && strcmp(s_seen_id[i], message_id) == 0) {
            return s_seen_reply[i];
        }
    }
    return NULL;
}

static void seen_store(const char *message_id, const char *reply)
{
    int slot = s_seen_next;
    snprintf(s_seen_id[slot], ID_MAX, "%s", message_id);
    snprintf(s_seen_reply[slot], REPLY_MAX, "%s", reply);
    s_seen_next = (slot + 1) % SEEN;
    if (s_seen_count < SEEN) {
        s_seen_count++;
    }
}

// ---------------------------------------------------------------- 入口

void commands_boot_marker(void)
{
    s_boot_us = esp_timer_get_time();
}

// message_id 只认字母数字下划线连字符，和电脑端一个规则。
static int id_is_valid(const char *text)
{
    size_t len = strlen(text);
    if (len == 0 || len > 64) {
        return 0;
    }
    for (size_t i = 0; i < len; i++) {
        char c = text[i];
        int ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')
                 || c == '_' || c == '-';
        if (!ok) {
            return 0;
        }
    }
    return 1;
}

int commands_handle_line(const char *line, char *out, size_t out_size)
{
    if (line == NULL || out == NULL || out_size == 0) {
        return 0;
    }
    out[0] = '\0';

    char message_id[ID_MAX];
    if (!jw_get_string(line, "message_id", message_id, sizeof(message_id))
        || !id_is_valid(message_id)) {
        ESP_LOGW(TAG, "no usable message_id, staying silent");
        return 0;
    }

    char kind[16];
    if (!jw_get_string(line, "type", kind, sizeof(kind)) || strcmp(kind, "command") != 0) {
        s_rejected++;
        write_reply(out, out_size, message_id, "rejected", EC_BAD_TYPE, NULL,
                    "消息类型对不上，只认 command");
        return 1;
    }

    long version = 0;
    if (!jw_get_int(line, "version", &version) || version != PROTOCOL_VERSION) {
        s_rejected++;
        write_reply(out, out_size, message_id, "rejected", EC_BAD_VERSION, NULL,
                    "协议版本对不上");
        return 1;
    }

    char device_id[ID_MAX];
    if (!jw_get_string(line, "device_id", device_id, sizeof(device_id))
        || !id_is_valid(device_id)) {
        s_rejected++;
        write_reply(out, out_size, message_id, "rejected", EC_MISSING_FIELD, NULL,
                    "device_id 不合法");
        return 1;
    }
    if (strcmp(device_id, DEVICE_ID) != 0) {
        s_rejected++;
        write_reply(out, out_size, message_id, "rejected", EC_MISSING_FIELD, NULL,
                    "这条不是发给这台设备的");
        return 1;
    }

    // 同一单重发：把上次那一帧原样还回去，不重新执行。
    const char *cached = seen_lookup(message_id);
    if (cached != NULL) {
        ESP_LOGW(TAG, "duplicate message_id, replaying the previous reply");
        snprintf(out, out_size, "%s", cached);
        return 1;
    }

    char command[48];
    if (!jw_get_string(line, "command", command, sizeof(command))) {
        s_rejected++;
        write_reply(out, out_size, message_id, "rejected", EC_MISSING_FIELD, NULL,
                    "少了 command");
        return 1;
    }
    const command_t *entry = find_command(command);
    if (entry == NULL) {
        s_rejected++;
        write_reply(out, out_size, message_id, "rejected", EC_UNKNOWN_COMMAND, NULL,
                    "固件不认这个动作");
        return 1;
    }
    if (!entry->capability) {
        s_rejected++;
        write_reply(out, out_size, message_id, "rejected", EC_UNSUPPORTED, NULL,
                    "这台设备没有这个能力");
        return 1;
    }

    char params[512];
    if (!jw_get_object(line, "params", params, sizeof(params))) {
        snprintf(params, sizeof(params), "{}");
    }

    char result[REPLY_MAX];
    const char *code = EC_NONE;
    const char *why = "";
    if (entry->run(params, result, sizeof(result), &code, &why) != 0) {
        s_rejected++;
        write_reply(out, out_size, message_id, "rejected", code, NULL, why);
        seen_store(message_id, out);
        return 1;
    }
    s_accepted++;
    write_reply(out, out_size, message_id, "completed", EC_NONE, result, NULL);
    seen_store(message_id, out);
    return 1;
}
