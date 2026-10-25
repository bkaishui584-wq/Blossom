---
name: personal-workflow
version: 1.0.0
description: 本地日历、待办、提醒、专注与每日总结，定时交给 Scheduler
permission: LOW
entry: impl.py
timeout: 60
---

# Personal Workflow

## 说明

提醒与定时靠本地 Scheduler 和日历，不靠模型轮询。
写入日程走工具，权限由程序判断。

## 触发条件

- 提醒
- 待办
- 日程
- 安排
- 专注
- 今天做了什么

## 输入

- action：list / add / summary
- title / when：新增日程用

## 步骤

1. 查看现有日程 | tool=list_calendar_events
2. 需要时新增一条 | tool=add_calendar_event

## 允许工具

- list_calendar_events
- add_calendar_event

## 输出格式

安排清单或写入确认，外加一句下一步。

## 验收标准

- 新增的日程必须能从库里读回来
- 时间说不清就先问，不瞎填

## 失败处理

- 时间解析不了：告诉用户要写成什么样
