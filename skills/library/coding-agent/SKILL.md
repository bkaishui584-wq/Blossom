---
name: coding-agent
version: 1.0.0
description: 读取项目结构、定位目标文件、追踪引用、提出最小改动并在授权后落盘和复核
permission: HIGH
entry: impl.py
timeout: 180
---

# Coding Agent

## 说明

先看清真实项目，再谈改动。所有文件读写都走工具，权限与确认由程序决定。
没有拿到明确的改动内容时，只报告事实，不假装改过文件。

## 触发条件

- 改代码
- 修改文件
- 重构
- 修复报错
- 看项目结构
- 追踪调用链

## 输入

- target：要看的文件或目录
- change_path / change_content：要写入的改动（可选）
- overwrite：是否允许覆盖已有文件（可选）
- test_command：改动后要跑的测试命令（可选）

## 步骤

1. 查看工作区信息 | tool=workspace_info
2. 列出顶层结构 | tool=list_dir
3. 读取目标文件 | tool=read_file
4. 搜索引用位置 | tool=search_files
5. 写入改动并回读复核 | tool=write_file

## 允许工具

- workspace_info
- list_dir
- read_file
- search_files
- write_file
- run_tests

## 输出格式

一行结论 + 事实清单：看到什么、改了什么、验证到什么程度。

## 验收标准

- 读到的每个结论都能指到具体文件
- 写入后必须回读复核，复核不到就说没验证过
- 工具被拒时如实说被拒，不换一种说法冒充成功

## 失败处理

- 读不到文件：说明路径不存在，不猜内容
- 写入被拒：说明权限被拒，保持原文件不动
- 测试没跑：说明没验证过
