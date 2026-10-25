---
name: git-review
version: 1.0.0
description: 读取 Git 状态与差异，审查代码、找潜在回归、整理提交说明
permission: READ
entry: impl.py
timeout: 90
---

# Git & Code Review

## 说明

只读。提交、推送、重置、删分支属于写操作，不在本 Skill 的工具白名单里，
必须走单独的授权流程。

## 触发条件

- git
- 差异
- 代码审查
- review
- 提交说明
- 改了哪些

## 输入

- path：仓库内相对目录（可选）
- staged：是否只看暂存区（可选）

## 步骤

1. 读取工作区状态 | tool=git_read
2. 读取差异统计 | tool=git_read
3. 读取提交历史 | tool=git_read

## 允许工具

- git_read
- read_file

## 输出格式

改动概览、风险点清单、建议的提交说明。

## 验收标准

- 每条风险都指到具体文件
- 没有改动时如实说工作区是干净的

## 失败处理

- 不是 Git 仓库：直接说明
- 工具被拒：说明被拒，不假装审查过
