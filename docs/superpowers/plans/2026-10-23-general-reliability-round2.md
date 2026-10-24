# 通用视频流程第二轮审计实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复全项目静态遍历与回归中发现的可复现缺陷，强化通用视频取样、历史复用、预处理及界面可靠性。

**Architecture:** 沿用现有服务、SQLite、FFmpeg 和 Qt 组件，修复公共入口，避免复制第二套流程。独立取样和展示缺陷按并行排错技能分开处理，最终统一验证。

**Tech Stack:** Python、PySide6、NumPy、SQLite、FFmpeg、pytest。

**Spec:** 用户本轮十二项要求，以及此前已批准设计中通用检测、历史与 UI 契约。本轮不开发或验证专用色情影片版本检索渠道。

## Global Constraints

- 当前目录与用户数据保持原位；测试使用隔离目录和合成媒体。
- 不按 FC2 或其他来源名称自动确认内容；预清洗只处理无效输入、重复计算及已有有效证据。
- 不自动修改用户媒体，不联网访问媒体站点，不发布、不提交 Git。
- 成功历史保留，取消与失败不能覆盖成功分析；人工记录必须匹配当前文件版本。
- 以新失败回归驱动修复；原有兼容入口保留，公共逻辑收敛。

## Review Focus

- 非整帧时间、长 GOP、非零时间起点的视频应标出真实解码位置。
- 文件替换、缓存坏行、撤回确认不能混入旧文件的证据。
- 静止、黑场、重复画面不能算作独立覆盖，也不反复执行同一 CV 分析。
- 损坏偏好文件不能阻止界面显示；小窗、缺失证据图均应可操作。
- 大历史库不得逐条打开数据库；并发扫描不能共享可变结果缓存。

## Tasks

- [x] 1. 全项目文件/AST/重复实现清单与基线静态检查、隔离全量测试。
- [x] 2. `censor/sampling.py`：真实时间戳、统一单帧生命周期与旧缩略图残留。测试 `tests/test_sampling_round2.py`。
- [x] 3. `censor/service.py` / `censor/store.py`：坏行隔离、有效历史与新人工确认、批量复核读取、无关调度设置不失效缓存。测试 `tests/test_history_round2.py`。
- [x] 4. `censor/mosaic_cv.py`：重复像素分析复用、候选限额与均匀总览，保持低覆盖为待复核。测试 `tests/test_analysis_round2.py`。
- [x] 5. `ui/workspace_preferences.py` / `ui/censor_gallery.py`：偏好独立恢复与规范化、小屏证据布局及缺图重绘。测试 `tests/test_workspace_round2.py`。
- [x] 6. 通用文件发现/预清洗、任务与跨页事件回归；仅对已复现问题修改，不按内容来源新增自动结论。
- [x] 7. 定向及全量测试、性能对照、版本/变更说明与十二项要求映射报告。

## 实施裁决

用户已要求逐项解决并提供现有目录。本计划细化已授权的通用修复，直接在当前干净基线 `58f512d` 上执行，不额外等待同范围方案批准。

Ruling: 取样与 UI 的已定位独立缺陷按 dispatching-parallel-agents 分别处理，其余当前会话实施，最后一次 fresh-context 只读审查；不按子任务重复派审查或创建工作树。

独立审查两项 Important 已完成失败回归及修复：失败重查不能借人工确认覆盖成功分析；坏历史页按数据库实际读取行推进游标。另补旧错误缓存拒绝复用和队列连接关闭/回滚回归。

最终主套件 1434 passed + 1 skipped（212.20 秒，exit 0），独立回环 HTTP 1 passed（exit 0），新增 72 项回归；Ruff、compileall、版本一致性与 diff-check 通过。源码版本 3.20.1，未提交或重新打包。完整映射见 ../../通用审计第二轮-2026-10-23.md。
