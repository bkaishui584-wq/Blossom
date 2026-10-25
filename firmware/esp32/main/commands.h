// 命令白名单与参数校验。
//
// 两个原则：
//   1. 没登记的动作一律拒绝，不认识的一律不动。
//   2. 参数在这里再验一遍，不靠电脑端把关。电脑端少了哪一样，这里也拦得住。
#pragma once

#include <stddef.h>

// 处理一行请求。要回的话写进 out（一行 JSON，不带换行），返回 1 表示有回复。
// 没有可用的 message_id 时不回复（对不上单，回了也没用）。
int commands_handle_line(const char *line, char *out, size_t out_size);

// 固件自己报的能力名单，拼成 JSON 数组的样子。
const char *commands_capabilities_json(void);
// 固件的参数范围表，拼成 JSON 对象的样子。
const char *commands_limits_json(void);
// 开机时记下启动时刻，后面的运行时长以它为基准。
void commands_boot_marker(void);
