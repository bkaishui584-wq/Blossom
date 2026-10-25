// ESP32 设备固件：按 DeskPet 的 v1 动作协议收指令、验参数、回确认。
//
// 只认协议里那几样：version / message_id / device_id / command / params。
// 参数一律以固件里的范围为准再验一遍，不靠电脑端把关。没登记的动作直接拒绝。
//
// 这一份没有在真机上编译和烧写过，接线前请先在 Arduino IDE 里编译通过，再把
// config.h 的数字改成你实际的结构范围。没验证过的能力，固件不会报成可用。

#include "config.h"

#if CAP_SERVO
  #if defined(__has_include)
    #if __has_include(<ESP32Servo.h>)
      #include <ESP32Servo.h>
      #define HAVE_SERVO_LIB 1
    #endif
  #endif
#endif
#ifndef HAVE_SERVO_LIB
  #define HAVE_SERVO_LIB 0
#endif

// ---------------------------------------------------------------- 状态
static char lineBuf[LINE_MAX];
static size_t lineLen = 0;
static bool lineOverflow = false;

static unsigned long bootMs = 0;
static unsigned long lastCommandMs = 0;
static unsigned long movingUntilMs = 0;
static bool moving = false;

static bool ledState[LED_CHANNELS];
static long servoAngle[SERVO_CHANNELS];

static char replyBuf[512];
static char limitsJson[224];
static char capsJson[128];

#if HAVE_SERVO_LIB
static Servo servos[SERVO_CHANNELS];
static bool servoAttached[SERVO_CHANNELS];
#endif

// ---------------------------------------------------------------- 小工具
static const char *skipWs(const char *p) {
  while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
  return p;
}

// 读一个 JSON 字符串（带转义处理）。返回字符串结束后的位置，写不进就截断。
static const char *readString(const char *p, char *out, size_t cap) {
  size_t used = 0;
  p = skipWs(p);
  if (*p != '"') return p;
  p++;
  while (*p && *p != '"') {
    char c = *p;
    if (c == '\\' && p[1]) {
      p++;
      c = (*p == 'n') ? '\n' : (*p == 't' ? '\t' : *p);
    }
    if (used + 1 < cap) out[used++] = c;
    p++;
  }
  if (*p == '"') p++;
  out[used] = 0;
  return p;
}

// 找值的结束位置：跳过字符串、嵌套对象和数组，一层层数括号。
static const char *valueEnd(const char *p) {
  int depth = 0;
  bool inString = false;
  while (*p) {
    char c = *p;
    if (inString) {
      if (c == '\\' && p[1]) { p += 2; continue; }
      if (c == '"') inString = false;
      p++;
      continue;
    }
    if (c == '"') { inString = true; p++; continue; }
    if (c == '{' || c == '[') { depth++; p++; continue; }
    if (c == '}' || c == ']') {
      if (depth == 0) return p;
      depth--;
      p++;
      continue;
    }
    if (depth == 0 && c == ',') return p;
    p++;
  }
  return p;
}

// 在最外层找一个键，把值的起止位置带回来。找不到返回 false。
static bool findTop(const char *obj, const char *key, const char **start, const char **end) {
  size_t keyLen = strlen(key);
  const char *p = skipWs(obj);
  if (*p != '{') return false;
  p++;
  while (*p) {
    p = skipWs(p);
    if (*p == '}') return false;
    if (*p != '"') return false;
    const char *nameStart = p;
    char name[48];
    p = readString(p, name, sizeof(name));
    if (nameStart == p) return false;
    p = skipWs(p);
    if (*p != ':') return false;
    p = skipWs(p + 1);
    const char *valueStart = p;
    const char *valueStop = valueEnd(p);
    bool hit = (strlen(name) == keyLen) && (strncmp(name, key, keyLen) == 0);
    if (hit) {
      if (start) *start = valueStart;
      if (end) *end = valueStop;
      return true;
    }
    p = skipWs(valueStop);
    if (*p == ',') { p++; continue; }
    return false;
  }
  return false;
}

static bool readTopString(const char *obj, const char *key, char *out, size_t cap) {
  const char *start = 0;
  const char *stop = 0;
  if (!findTop(obj, key, &start, &stop)) return false;
  readString(start, out, cap);
  return true;
}

static bool readTopLong(const char *obj, const char *key, long *out) {
  const char *start = 0;
  const char *stop = 0;
  if (!findTop(obj, key, &start, &stop)) return false;
  start = skipWs(start);
  char *endPtr = 0;
  long value = strtol(start, &endPtr, 10);
  if (endPtr == start) return false;
  *out = value;
  return true;
}

static bool readTopBool(const char *obj, const char *key, bool *out) {
  const char *start = 0;
  const char *stop = 0;
  if (!findTop(obj, key, &start, &stop)) return false;
  start = skipWs(start);
  if (strncmp(start, "true", 4) == 0) { *out = true; return true; }
  if (strncmp(start, "false", 5) == 0) { *out = false; return true; }
  return false;
}

// message_id 只认字母数字下划线连字符，长度 1-64。对不上就不回复（没法对单）。
static bool idLooksOk(const char *value) {
  size_t len = strlen(value);
  if (len == 0 || len > 64) return false;
  for (size_t i = 0; i < len; i++) {
    char c = value[i];
    bool ok = (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
              c == '_' || c == '-';
    if (!ok) return false;
  }
  return true;
}

static bool capsHas(const char *name) {
  size_t len = strlen(name);
  const char *p = capsJson;
  while ((p = strstr(p, name)) != 0) {
    bool leftOk = (p == capsJson) || (p[-1] == '"');
    bool rightOk = (p[len] == '"');
    if (leftOk && rightOk) return true;
    p += len;
  }
  return false;
}

static void buildCapabilities() {
  size_t used = 0;
  capsJson[used++] = '[';
  bool first = true;
  struct { const char *name; int on; } items[] = {
    {"status", CAP_STATUS},
    {"led", CAP_LED},
    {"servo", CAP_SERVO && HAVE_SERVO_LIB},
    {"sensor", CAP_SENSOR},
    {"stop", CAP_STOP},
  };
  for (size_t i = 0; i < sizeof(items) / sizeof(items[0]); i++) {
    if (!items[i].on) continue;
    int wrote = snprintf(capsJson + used, sizeof(capsJson) - used, "%s\"%s\"", first ? "" : ",", items[i].name);
    if (wrote <= 0 || (size_t)wrote >= sizeof(capsJson) - used) break;
    used += (size_t)wrote;
    first = false;
  }
  snprintf(capsJson + used, sizeof(capsJson) - used, "]");
}

static void buildLimits() {
  snprintf(
      limitsJson, sizeof(limitsJson),
      "{\"led_channels\":{\"min\":0,\"max\":%d},\"servo_channels\":{\"min\":0,\"max\":%d},"
      "\"servo_angle\":{\"min\":%d,\"max\":%d},\"sensor_channels\":{\"min\":0,\"max\":%d}}",
      LED_CHANNELS - 1, SERVO_CHANNELS - 1, SERVO_MIN_ANGLE, SERVO_MAX_ANGLE,
      SENSOR_CHANNELS - 1);
}

static void sendReply(const char *messageId, const char *status, const char *resultJson, const char *error) {
  int wrote = snprintf(
      replyBuf, sizeof(replyBuf),
      "{\"version\":1,\"message_id\":\"%s\",\"device_id\":\"%s\",\"status\":\"%s\",\"result\":%s,\"error\":%s}",
      messageId, DEVICE_ID, status, resultJson ? resultJson : "{}",
      error ? error : "null");
  if (wrote <= 0 || (size_t)wrote >= sizeof(replyBuf)) {
    Serial.println("{\"version\":1,\"device_id\":\"" DEVICE_ID "\",\"status\":\"failed\",\"result\":{},\"error\":\"回复太长，没有发出去\"}");
    return;
  }
  Serial.println(replyBuf);
}

static void sendFailed(const char *messageId, const char *reason) {
  char escaped[160];
  size_t used = 0;
  for (const char *p = reason; *p && used + 1 < sizeof(escaped); p++) {
    if (*p == '"' || *p == '\\') escaped[used++] = '\\';
    escaped[used++] = *p;
  }
  escaped[used] = 0;
  char payload[192];
  snprintf(payload, sizeof(payload), "\"%s\"", escaped);
  sendReply(messageId, "failed", "{}", payload);
}

// ---------------------------------------------------------------- 动作
static void doStop() {
  ledState[0] = false;
  pinMode(LED_PIN_0, OUTPUT);
  digitalWrite(LED_PIN_0, LOW);
#if LED_CHANNELS > 1
  ledState[1] = false;
  pinMode(LED_PIN_1, OUTPUT);
  digitalWrite(LED_PIN_1, LOW);
#endif
  moving = false;
  movingUntilMs = 0;
}

static void handleGetStatus(const char *messageId) {
  char result[420];
  int wrote = snprintf(
      result, sizeof(result),
      "{\"version\":\"%s\",\"capabilities\":%s,\"limits\":%s,\"uptime_s\":%lu,"
      "\"led\":{\"0\":%s,\"1\":%s},\"moving\":%s}",
      FIRMWARE_VERSION, capsJson, limitsJson, (millis() - bootMs) / 1000UL,
      ledState[0] ? "true" : "false",
      (LED_CHANNELS > 1 && ledState[1]) ? "true" : "false",
      moving ? "true" : "false");
  if (wrote <= 0 || (size_t)wrote >= sizeof(result)) {
    sendFailed(messageId, "状态太长，没能组装出来");
    return;
  }
  sendReply(messageId, "completed", result, 0);
}

static void handleLed(const char *messageId, const char *params) {
  long channel = 0;
  bool on = false;
  if (!readTopLong(params, "channel", &channel)) {
    sendFailed(messageId, "缺少参数 channel");
    return;
  }
  if (channel < 0 || channel >= LED_CHANNELS) {
    sendFailed(messageId, "channel 超出这台设备的范围");
    return;
  }
  if (!readTopBool(params, "on", &on)) {
    sendFailed(messageId, "缺少参数 on（true 或 false）");
    return;
  }
  int pin = (channel == 0) ? LED_PIN_0 : LED_PIN_1;
  pinMode(pin, OUTPUT);
  digitalWrite(pin, on ? HIGH : LOW);
  ledState[channel] = on;
  char result[120];
  snprintf(result, sizeof(result), "{\"channel\":%ld,\"on\":%s}", channel, on ? "true" : "false");
  sendReply(messageId, "completed", result, 0);
}

static void handleServo(const char *messageId, const char *params) {
#if !HAVE_SERVO_LIB
  sendFailed(messageId, "固件没有装舵机库，舵机动作不可用");
#else
  if (!capsHas("servo")) {
    sendFailed(messageId, "固件没有开舵机能力");
    return;
  }
  long channel = 0;
  long angle = 0;
  if (!readTopLong(params, "channel", &channel)) {
    sendFailed(messageId, "缺少参数 channel");
    return;
  }
  if (channel < 0 || channel >= SERVO_CHANNELS) {
    sendFailed(messageId, "channel 超出这台设备的范围");
    return;
  }
  if (!readTopLong(params, "angle", &angle)) {
    sendFailed(messageId, "缺少参数 angle");
    return;
  }
  if (angle < SERVO_MIN_ANGLE || angle > SERVO_MAX_ANGLE) {
    sendFailed(messageId, "angle 超出机械范围");
    return;
  }
  int pin = (channel == 0) ? SERVO_PIN_0 : SERVO_PIN_1;
  if (!servoAttached[channel]) {
    servos[channel].attach(pin);
    servoAttached[channel] = true;
  }
  servos[channel].write((int)angle);
  servoAngle[channel] = angle;
  moving = true;
  movingUntilMs = millis() + SERVO_MOVE_MS;
  char result[140];
  snprintf(result, sizeof(result), "{\"channel\":%ld,\"angle\":%ld}", channel, angle);
  sendReply(messageId, "completed", result, 0);
#endif
}

static void handleSensor(const char *messageId, const char *params) {
#if !CAP_SENSOR
  sendFailed(messageId, "固件没有开传感器能力");
#else
  long channel = 0;
  if (!readTopLong(params, "channel", &channel)) {
    sendFailed(messageId, "缺少参数 channel");
    return;
  }
  if (channel < 0 || channel >= SENSOR_CHANNELS) {
    sendFailed(messageId, "channel 超出这台设备的范围");
    return;
  }
  int raw = analogRead(SENSOR_PIN_34_ADC);
  char result[120];
  snprintf(result, sizeof(result), "{\"channel\":%ld,\"value\":%d,\"unit\":\"raw\"}", channel, raw);
  sendReply(messageId, "completed", result, 0);
#endif
}

// ---------------------------------------------------------------- 解析一行
static void handleLine(const char *line) {
  const char *p = skipWs(line);
  if (*p != '{') return;

  char messageId[72];
  if (!readTopString(p, "message_id", messageId, sizeof(messageId))) return;
  if (!idLooksOk(messageId)) return;  // 对不上单，只能不回

  long version = 0;
  if (!readTopLong(p, "version", &version) || version != 1) {
    sendFailed(messageId, "协议版本对不上，只认 1");
    return;
  }

  char deviceId[72];
  if (!readTopString(p, "device_id", deviceId, sizeof(deviceId))) {
    sendFailed(messageId, "缺少 device_id");
    return;
  }
  if (strcmp(deviceId, DEVICE_ID) != 0) {
    sendFailed(messageId, "这条不是发给这台设备的");
    return;
  }

  char command[48];
  if (!readTopString(p, "command", command, sizeof(command))) {
    sendFailed(messageId, "缺少 command");
    return;
  }

  const char *paramsStart = 0;
  const char *paramsStop = 0;
  char params[192];
  params[0] = '{';
  params[1] = '}';
  params[2] = 0;
  if (findTop(p, "params", &paramsStart, &paramsStop)) {
    size_t len = (size_t)(paramsStop - paramsStart);
    if (len >= sizeof(params)) {
      sendFailed(messageId, "params 太长");
      return;
    }
    memcpy(params, paramsStart, len);
    params[len] = 0;
  }

  lastCommandMs = millis();

  // 白名单：只有这里点名的动作成立，别的名字一律拒绝。
  if (strcmp(command, "device.get_status") == 0) {
    if (!CAP_STATUS) { sendFailed(messageId, "这台设备没有状态能力"); return; }
    handleGetStatus(messageId);
  } else if (strcmp(command, "led.set") == 0) {
    if (!CAP_LED) { sendFailed(messageId, "这台设备没有灯能力"); return; }
    handleLed(messageId, params);
  } else if (strcmp(command, "servo.set_angle") == 0) {
    if (!CAP_SERVO) { sendFailed(messageId, "这台设备没有舵机能力"); return; }
    handleServo(messageId, params);
  } else if (strcmp(command, "sensor.read") == 0) {
    if (!CAP_SENSOR) { sendFailed(messageId, "这台设备没有传感器能力"); return; }
    handleSensor(messageId, params);
  } else if (strcmp(command, "device.stop") == 0) {
    if (!CAP_STOP) { sendFailed(messageId, "这台设备没有停下的能力"); return; }
    doStop();
    sendReply(messageId, "completed", "{\"stopped\":true,\"leds_off\":true}", 0);
  } else {
    sendFailed(messageId, "固件不认这个动作");
  }
}

// 软件看门狗：太久没收到有效指令，就把持续动作停到安全状态。
static void serviceSafety() {
  if (!moving) return;
  unsigned long now = millis();
  if (now >= movingUntilMs) {
    moving = false;
    return;
  }
  if ((now - lastCommandMs) > SAFETY_IDLE_MS) {
    doStop();
  }
}

// ---------------------------------------------------------------- 入口
void setup() {
  Serial.begin(SERIAL_BAUD);
  bootMs = millis();
  lastCommandMs = bootMs;
  movingUntilMs = 0;
  for (int i = 0; i < LED_CHANNELS; i++) ledState[i] = false;
  for (int i = 0; i < SERVO_CHANNELS; i++) servoAngle[i] = 0;
  pinMode(LED_PIN_0, OUTPUT);
  if (LED_CHANNELS > 1) pinMode(LED_PIN_1, OUTPUT);
  digitalWrite(LED_PIN_0, LOW);
  if (LED_CHANNELS > 1) digitalWrite(LED_PIN_1, LOW);
  buildCapabilities();
  buildLimits();
  // 启动时先报一句自己是谁、会什么，电脑端连上后就能对上。
  char boot[320];
  snprintf(boot, sizeof(boot),
           "{\"boot\":1,\"device_id\":\"%s\",\"version\":\"%s\",\"capabilities\":%s,\"limits\":%s}",
           DEVICE_ID, FIRMWARE_VERSION, capsJson, limitsJson);
  Serial.println(boot);
}

void loop() {
  while (Serial.available() > 0) {
    int incoming = Serial.read();
    if (incoming < 0) break;
    char c = (char)incoming;
    if (c == '\n' || c == '\r') {
      if (lineOverflow) {
        // 上一行太长了，丢掉，等下一行重新开始。
        lineOverflow = false;
        lineLen = 0;
      } else if (lineLen > 0) {
        lineBuf[lineLen] = 0;
        handleLine(lineBuf);
        lineLen = 0;
      }
    } else if (lineLen < LINE_MAX - 1) {
      lineBuf[lineLen++] = c;
    } else {
      lineOverflow = true;
      lineLen = 0;
    }
  }
  serviceSafety();
  delay(2);
}