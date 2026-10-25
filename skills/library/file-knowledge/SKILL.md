---
name: file-knowledge
version: 1.0.0
description: 搜索本地文件、读取内容、整理项目笔记，默认只读
permission: READ
entry: impl.py
timeout: 90
---

# File & Knowledge

## 说明

默认只读。批量改动和移动文件之前先给预览。
整理出的笔记要标明来源文件和行号。

## 触发条件

- 找文件
- 搜一下本地
- 项目笔记
- 之前那个
- 历史项目

## 输入

- query：要找的内容
- path：从哪个目录开始（可选）
- remember：要不要把结论记进记忆（可选）

## 步骤

1. 按关键词搜索 | tool=search_files
2. 读取最相关文件 | tool=read_file
3. 列出所在目录 | tool=list_dir

## 允许工具

- search_files
- read_file
- list_dir

## 输出格式

命中的文件与行号清单，外加一句结论。

## 验收标准

- 每条结论带文件路径
- 什么都没找到时如实说没找到

## 失败处理

- 搜索表达式非法：说明表达式问题
- 文件读不出：说明读不出，不猜内容
