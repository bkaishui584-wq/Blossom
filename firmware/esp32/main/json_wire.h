// 极小的 JSON 取值与拼装工具。
//
// 不引入第三方 JSON 库：协议报文是我们自己定的，字段固定、不嵌套（params 是一层
// 平对象），够用就好。取值失败时返回 0，调用方按「这条报文不合格」处理，不猜。
#pragma once

#include <stddef.h>

// 顶层字段：字符串（自动去掉两边的引号，处理 \" 和 \\）
int jw_get_string(const char *json, const char *key, char *out, size_t out_size);

// 顶层字段：整数。
int jw_get_int(const char *json, const char *key, long *out);

// 顶层字段：布尔（只认 true / false，不认 1 / 0）。
int jw_get_bool(const char *json, const char *key, int *out);

// 顶层字段：对象原文，含花括号。用来把 params 单独拎出来当子文档解析。
int jw_get_object(const char *json, const char *key, char *out, size_t out_size);

// 把字符串转成能安全放进 JSON 的形式（去掉控制字符，转义引号和反斜杠）。
void jw_escape(const char *src, char *out, size_t out_size);

// 顶层里有没有这个键。用来判断「你没给我这个参数」和「你给了我 null」。
int jw_has(const char *json, const char *key);
