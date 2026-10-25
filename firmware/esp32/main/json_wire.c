#include "json_wire.h"

#include <stdio.h>
#include <string.h>

// 跳过空白。
static const char *skip_space(const char *p)
{
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') {
        p++;
    }
    return p;
}

// 找一个顶层键。返回它的值起始位置；找不到返回 NULL。
// 顶层的意思是花括号深度为 1，所以 params 里面的同名键不会被误认。
static const char *find_value(const char *json, const char *key)
{
    if (json == NULL || key == NULL) {
        return NULL;
    }
    size_t key_len = strlen(key);
    int depth = 0;
    int in_string = 0;
    int escaped = 0;
    const char *p = json;

    while (*p) {
        char c = *p;
        if (in_string) {
            if (escaped) {
                escaped = 0;
            } else if (c == '\\') {
                escaped = 1;
            } else if (c == '"') {
                in_string = 0;
                // 刚读完一个字符串：顶层的话，它可能是一个键。
                if (depth == 1) {
                    const char *after = skip_space(p + 1);
                    if (*after == ':' && (size_t)(p - json) >= key_len) {
                        const char *start = p - key_len;
                        if (start > json && start[-1] == '"' && strncmp(start, key, key_len) == 0) {
                            return skip_space(after + 1);
                        }
                    }
                }
            }
            p++;
            continue;
        }
        if (c == '"') {
            in_string = 1;
        } else if (c == '{' || c == '[') {
            depth++;
        } else if (c == '}' || c == ']') {
            depth--;
            if (depth < 0) {
                return NULL;
            }
        }
        p++;
    }
    return NULL;
}

int jw_has(const char *json, const char *key)
{
    return find_value(json, key) != NULL;
}

int jw_get_string(const char *json, const char *key, char *out, size_t out_size)
{
    if (out == NULL || out_size == 0) {
        return 0;
    }
    out[0] = '\0';
    const char *p = find_value(json, key);
    if (p == NULL || *p != '"') {
        return 0;
    }
    p++;
    size_t used = 0;
    while (*p && *p != '"') {
        char c = *p;
        if (c == '\\' && p[1]) {
            p++;
            c = *p;
            if (c == 'n') {
                c = '\n';
            } else if (c == 't') {
                c = '\t';
            }
        }
        if (used + 1 >= out_size) {
            out[0] = '\0';
            return 0;
        }
        out[used++] = c;
        p++;
    }
    if (*p != '"') {
        out[0] = '\0';
        return 0;
    }
    out[used] = '\0';
    return 1;
}

int jw_get_int(const char *json, const char *key, long *out)
{
    const char *p = find_value(json, key);
    if (p == NULL || out == NULL) {
        return 0;
    }
    int negative = 0;
    if (*p == '-') {
        negative = 1;
        p++;
    }
    if (*p < '0' || *p > '9') {
        return 0;
    }
    long value = 0;
    while (*p >= '0' && *p <= '9') {
        value = value * 10 + (*p - '0');
        p++;
    }
    // 后面还跟着小数点或字母的，按不合法处理，不截断成整数。
    if (*p == '.' || (*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z')) {
        return 0;
    }
    *out = negative ? -value : value;
    return 1;
}

int jw_get_bool(const char *json, const char *key, int *out)
{
    const char *p = find_value(json, key);
    if (p == NULL || out == NULL) {
        return 0;
    }
    if (strncmp(p, "true", 4) == 0) {
        *out = 1;
        return 1;
    }
    if (strncmp(p, "false", 5) == 0) {
        *out = 0;
        return 1;
    }
    return 0;
}

int jw_get_object(const char *json, const char *key, char *out, size_t out_size)
{
    if (out == NULL || out_size < 3) {
        return 0;
    }
    out[0] = '\0';
    const char *p = find_value(json, key);
    if (p == NULL || *p != '{') {
        return 0;
    }
    int depth = 0;
    int in_string = 0;
    int escaped = 0;
    size_t used = 0;
    while (*p) {
        char c = *p;
        if (used + 2 >= out_size) {
            out[0] = '\0';
            return 0;
        }
        out[used++] = c;
        if (in_string) {
            if (escaped) {
                escaped = 0;
            } else if (c == '\\') {
                escaped = 1;
            } else if (c == '"') {
                in_string = 0;
            }
        } else if (c == '"') {
            in_string = 1;
        } else if (c == '{') {
            depth++;
        } else if (c == '}') {
            depth--;
            if (depth == 0) {
                out[used] = '\0';
                return 1;
            }
        }
        p++;
    }
    out[0] = '\0';
    return 0;
}

void jw_escape(const char *src, char *out, size_t out_size)
{
    if (out == NULL || out_size == 0) {
        return;
    }
    size_t used = 0;
    for (const char *p = src; p != NULL && *p; p++) {
        unsigned char c = (unsigned char)*p;
        const char *replacement = NULL;
        char plain[2] = {0, 0};
        if (c == '"') {
            replacement = "\\\"";
        } else if (c == '\\') {
            replacement = "\\\\";
        } else if (c < 0x20) {
            replacement = " ";
        } else {
            plain[0] = (char)c;
            replacement = plain;
        }
        size_t need = strlen(replacement);
        if (used + need + 1 >= out_size) {
            break;
        }
        memcpy(out + used, replacement, need);
        used += need;
    }
    out[used] = '\0';
}
