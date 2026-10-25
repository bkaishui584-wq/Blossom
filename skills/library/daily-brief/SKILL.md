---
name: daily-brief
version: 1.0.0
description: 把今天的日期、待办、日程、天气和机器状态拼成一段开场简报，纯本地规则
permission: READ
entry: impl.py
timeout: 45
---

# Daily Brief

## 说明

给用户一段今天的简报：几号星期几、有什么待办、有什么日程、天气怎么样、
机器状态如何。全部走本地工具和已有服务，规则拼装，不需要模型。
哪一项读不到就跳过并说明，不编内容。

## 触发条件

- 今日简报
- 今天有什么
- 今天要做什么
- 帮我看看今天
- 今天的安排
- 每日总结

## 输入

- city：天气看哪个城市（可选，不给就试默认城市）

## 步骤

1. 读今天日期 | tool=local_time
2. 列本地待办 | tool=todo_list
3. 列日程 | tool=list_calendar_events
4. 查天气 | tool=get_weather
5. 读机器状态 | tool=system_status

## 允许工具

- local_time
- todo_list
- list_calendar_events
- get_weather
- system_status

## 输出格式

几行短句，按日期、待办、日程、天气、机器的顺序。没有的项不写。

## 验收标准

- 每一项都来自工具返回，不自己编日期和天气
- 工具失败时明确跳过，并说明是哪一项没读到
- 不联网也能给出日期、待办、日程和机器状态

## 失败处理

- 天气服务不可用：说明天气没取到，不伪造一条天气
- 待办库不可用：说明待办暂时读不到，其它项照常给
