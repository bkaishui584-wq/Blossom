---
name: system-assistant
version: 1.0.0
description: 本机时间、日期、运行状态和本地待办的查询，全靠本地工具，不联网也不需要模型
permission: READ
entry: impl.py
timeout: 30
---

# System Assistant

## 说明

只回答本机真实读数。时间来自系统时钟，状态来自系统接口，待办来自本地库。
没有网络、没有模型也能跑。读不到就说读不到，不猜。

## 触发条件

- 几点
- 现在时间
- 今天几号
- 星期几
- 开机多久
- 电量
- 内存
- 系统状态
- 待办
- 要做的事

## 输入

- action：time / status / todo（可选，缺了就按说的话猜）
- what：看时间的哪一项，all / time / date / weekday / timezone / uptime（可选）

## 步骤

1. 读本机时间 | tool=local_time
2. 读本机运行状态 | tool=system_status
3. 列本地待办 | tool=todo_list

## 允许工具

- local_time
- system_status
- todo_list

## 输出格式

一段简短读数，外加一句还能做什么。

## 验收标准

- 每个数字都来自工具返回，不自己编时间
- 工具失败时如实说失败，不假装查到了
- 不联网也能跑完

## 失败处理

- 读不到本机状态：说明这台机器上读不到，不估一个数字
- 没有待办库：说明待办暂时不可用