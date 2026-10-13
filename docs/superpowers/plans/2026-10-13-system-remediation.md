# JellyfinToolkit System Remediation Implementation Plan

> **For agentic workers:** Use the already-authorized audit design and TDD task by task. Independent, disjoint file domains may run in parallel under dispatching-parallel-agents; integration and final review remain sequential. User explicitly authorized completing the full report without further permission gates.

**Goal:** 完成审计全部问题和产品优化，提交可验证源码与独立Windows运行包。

**Architecture:** 在现有PySide6分层上建立统一身份、范围、配置、任务与可恢复操作核心，GUI/CLI使用兼容包装共享实现。先数据安全与正确性，后工作流/UI整理，最后集成、审查和打包。

**Tech Stack:** Python3.9+、PySide6、SQLite、FFmpeg/ffprobe、pytest/pytest-qt、ruff、PyInstaller。

**Spec:** ../specs/2026-10-13-system-remediation-design.md

## Global Constraints

继承spec全部约束；测试完全隔离；不同时改同一文件；失败先证据后修复；原件/目标/日志的失败恢复可验证；所有报告条目都有修复位置与验收记录。不得改弱原有测试来掩盖回归；如安全策略有意改变旧行为，记录新契约并更新该行为测试。

## Review Focus

- 相同物理路径多条数据库记录：保留实体绝不可被删。
- 已存在目标与部分写入/日志失败：原件和目标都有可找到的恢复记录。
- 取消时已有部分计划、工具静默无输出：停止后无新写入，父进程和原件保留。
- 改服务器/观看账号/标签/模型/媒体内容：缓存与画像不能串用。
- 网络路径、中文长路径、多个同名输出：明确范围和身份，不能自动选错误文件。

## Task 1: 安全基础与配置契约

Files: config.py、utils/media_types.py、utils/proc.py、utils/file_scan.py、tests/test_system_foundations.py。

Produces: jellyfin_user_id/library_path_mappings配置；统一扫描接口；既有run_streaming契约加独立POSIX进程组；错误日志统一实际路径。

- [x] 写三态配置持久化、非法范围、映射/用户字段、静默取消/超时、扫描过滤回归，观察失败。
- [x] 实现类型与范围校验、单一默认媒体清单、可取消公共扫描及进程组隔离；保持Python3.9兼容。
- [x] 运行对应测试，记录命令结果；提交限定文件。

## Task 2: 核心数据与评分整改（核心数据域）

Files: spec列出的核心数据域及tests/test_core_remediation.py；报告要求见core-report C01-C31（UI与trash相关条目由其它域实现，核心域保留协作记录）。

Consumes: Task1配置；Produces: 用户正确的归一库、共享反馈/画像、正确DTO/身份、受保护实体和有效AI结果。

- [x] 把审计probe中的预期安全/正确结果转换回归测试并先运行失败。
- [x] 按C05-C11优先修用户、缺失画像、反馈、DTO、番号/分卷/版本与去重实体；再修AI身份/缓存/并发同步、推荐/CSV/统计口径。
- [x] 测DB/API/legacy、多用户、冷启动、重复实体、正反馈、坏JSON、过期缓存和并发标签；更新文档事实。
- [x] 报告每项C编号处理结果及接口；提交限定文件。

## Task 3: 媒体事务和工具整改（媒体事务域）

Files: spec列出的媒体域、新增utils文件事务/合并执行/替换核心及tests/test_media_remediation.py。

Produces: 可恢复的安全删除/替换/NFO/字幕/数据库/归档；共享merge_group_ffmpeg入口；本次产物有效性与工具门控。

- [x] 转码碰撞、NFO失败预删、VTT空解析、WAL快照、日志失败、旧产物、bad-final/session混组等先写失败回归。
- [x] 完成M02-M19及C01/C17；合并GUI核心提为无Qt共用实现，保留现有公共函数兼容入口。
- [x] 真实短影片验证时长/字幕/多音轨与归一目标；停止/失败无半成品参与新扫描；提供恢复函数。
- [x] 将UI/自动化消费者所需接口提前通信；提交限定文件并写逐项验收。

## Task 4: 页面状态与产品工作流整改（UI域）

Files: ui全部、main.py、tests/test_ui_remediation.py。

Consumes: 数据/媒体新接口；Produces: 单一目标模型、全局任务/配置通知、真实操作确认、分组导航与工作流移交。

- [x] UI-01/02/03/04/05/06/07/08/09先写真实控件回归并观察失败。
- [x] 修隐藏目标、勾选、缓存、默认参数、标签同步与运行生命周期；所有执行前审阅同一计划。
- [x] 修UI-10至23并实现报告产品分组、固定任务主操作、字幕/Lada移交、任务历史/恢复入口、连接用户/路径能力展示。
- [x] 测17能力、主题/视图/筛选不改选择、900x600/1100x720布局、运行中关窗、配置save-load和跨页状态。
- [x] 交付虚构数据截图、逐项UI映射；提交限定文件。

## Task 5: 自动化、CLI与发布整改（协调域）

Files: automation、auto_*、workers/automation.py、scripts、tools、启动脚本、CI、version及tests/test_orchestration_remediation.py/test_release_remediation.py。

- [x] 完整编排入口、roots限制、取消部分计划、失败退出码、JSON stdout、日志路径、版本元组、归档不可变先写失败回归。
- [x] M01/M13/L01-L03及R01-R10逐项实现；automation复用媒体执行/替换核心；dry-run与写计划契约一致。
- [x] 保证服务器写操作明确计划、最新数据合并、日志不含凭据；构建门禁同commit，缺产物严格失败。
- [x] CLI烟测、默认配置首次启动、版本资源与历史归档重入测试；提交并记录。

## Task 6: 全系列集成、独立审查与可运行交付

Files: 所有消费者整合、docs/remediation-status.md、CHANGELOG/README/config.example、最终测试与构建。

- [x] 汇总86个审计记录（交叉重复合并映射，不夸大独立bug数量），任何未完成项保持开放，不用“以后再做”算完成。
- [ ] 全量pytest、ruff、compileall、隐私/版本检查、真实FFmpeg小样本、Qt布局/生命周期、受控进程与API模拟服务器系统测试。
- [x] 独立审查全分支diff；修所有重要新发现，重跑受影响与全量必要检查。
- [ ] 新版本3.10.0独立打包、PE资源核验、隔离启动/功能烟测、SHA256；保留旧版本、用户配置与所有真实媒体。
- [ ] 交付源码、运行包、修复对照和验证报告。只有有证据的项目才能标完成。

最终全套结果、构建摘要与交付记录保存于独立验收报告；此计划随发布源码冻结，交付后不回写以保持已验证源码摘要。
