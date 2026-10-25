---
name: office-agent
version: 1.0.0
description: 读取和写出 md、txt、csv、json、docx、xlsx、pptx、pdf，写入前先查目标
permission: HIGH
entry: impl.py
timeout: 120
---

# Office Agent

## 说明

读文档用标准库，不装额外依赖。格式不支持或抽不出正文时明确说抽不出。
写入前先检查目标路径，禁止静默覆盖。

## 触发条件

- 表格
- 文档
- 简历
- 报告
- pdf
- excel
- word
- ppt

## 输入

- path：要读的文件
- out_path / content：要写出的文件与内容（可选）
- overwrite：是否允许覆盖（可选）

## 步骤

1. 读取文档正文 | tool=read_document
2. 写出结果并回读复核 | tool=write_file

## 允许工具

- read_document
- write_file
- list_dir

## 输出格式

读到什么、写成什么、复核结果。

## 验收标准

- 写入后必须回读复核
- 抽不出正文时说清是格式问题还是扫描件

## 失败处理

- 格式不支持：列出当前支持的类型
- 目标已存在且没给覆盖许可：停下来问，不覆盖
