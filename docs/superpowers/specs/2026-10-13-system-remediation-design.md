# JellyfinToolkit 全面整改设计

用户已批准2026-10-13审计报告的全部修复与优化，要求按风险优先实施并完成全系列验证。本文件把报告转成执行约束，不再为既有授权增加重复确认。

## 目标

修复C01-C31、M01-M19、L01-L03、UI-01-UI-23、R01-R10，保留现有有用能力；所有显示、选择、保留、取消与实际文件/服务器操作一致。提供可运行、可测试、可恢复的Windows软件与源码，明确实际验证范围。

## 不可违反的约束

- 不使用真实媒体、真实服务器凭据或用户观看数据库做破坏性测试；测试样本和配置均隔离。
- 默认不进行永久删除；无法保证系统回收的目标使用可追溯的应用隔离归档，或拒绝执行。所有替换先留备份，有逐项记录与恢复入口。
- 原文件、既有目标、备份、执行计划、日志与输出身份独立建模；任何失败不得无记录丢失可恢复的原件。
- 同一实体不能同时保留与删除；执行目标来自用户审核的同一计划，主题/排序/切视图不改变业务选择。
- 缺观看用户/画像/特征时返回未知或复核；人工确认喜欢进入正样本；仅保留与确认喜欢的语义清晰。
- 读取库继续统一经过library_source；DTO归一在build_item；GUI/CLI复用相同业务核心。
- Qt依赖保留在ui/workers，纯业务模块可独立测试；不新增庞大运行依赖，Python最低版本保持3.9。
- 配置变动明确持久化与通知；新增jellyfin_user_id默认为空，library_path_mappings默认为空列表。映射条目为{"remote": str, "local": str}，按路径段匹配而非任意子串替换。
- 日志不得输出凭据；真实路径、数据、产物不能进入Git；提交文档中仅用中性样例。
- 所有长任务有真实running/stopping/cancelled/failed/succeeded状态；旧任务结束前不允许覆盖引用或启动冲突任务。
- AI结果用稳定候选id/索引，验证所属批次与结果结构；失败不改旧有效状态；模型推断不能自动成为人工真值；完整回滚清除相关AI状态。
- 保留v1/v2为计算策略，统一其数据、样本定义和执行约束；统一扫描范围/扩展名/附属/缓存失效。
- 发布必须验证同一commit，版本字符串/元组/PE/tag一致；旧发行归档不可覆盖或重写。

## 产品结构

导航按首页与连接、内容审核、版本与分集、字幕管理、媒体处理、任务中心分组；设置与高级维护单独可见。原17种能力通过分组、标签页和明确步骤保留，不能通过删除按钮来假装功能完成。

共用工作流：选择范围→检测/计划→逐项审阅→执行→校验→结果/恢复。字幕检查可直接转生成任务；Lada产物可转配对审核/替换；只读API但本地路径不可达时显示能力限制而非“零结果/媒体损坏”。任务主操作与进度固定可见，高级参数折叠。

## 实施隔离与接口

独立Git分支codex/toolkit-system-remediation；四个明确文件所有权领域，互不同时修改同一文件。跨领域接口先发消息并在实施记录保存；原公开函数可保留兼容包装，但不得继续复制核心逻辑。

- 核心数据域：library、library_api、library_source、scoring、scoring_v2、profile、labels、ai_profile、ai_scoring、jellyfin_api、ingest、scorer、ai、ai_score、jellyfin_sync、deck_state、naming、tag_norm。
- 媒体事务域：nfo、subtitle_clean、merge/merge_archive/merge_repair、censor_tags、lada_tool、whisper_tool、gpu及对应ffmpeg/database/merge/whisper/lada/trash workers；trash删除核心。新增无Qt文件事务、替换、合并执行核心。
- UI域：ui全部、GUI入口main.py；负责列表模型、配置广播、标签同步、任务生命周期、导航/布局、执行确认与恢复展示，不改纯业务模块。
- 协调域：config、version、media_types、proc、自动化与CLI、发布/构建/启动、跨域集成、文档、全系列验证及打包。

共享合并核心接口目标：merge_group_ffmpeg(group, ffmpeg, ffprobe, options=None, log=print, should_stop=None) -> tuple[bool,str]；GUI与CLI均调用它。如既有测试需要命令runner注入，作为明确可选参数保留，不能为通过测试重新复制实现。

新增公共媒体扫描接口iter_media_files(paths, video_exts=None, exclude_keywords=None, should_stop=None, log=None)，由协调域提供；返回路径迭代器，去重、过滤附属/临时产物、配置扩展名、错误明细与取消一致。扫描worker/各页面与自动化逐步委托。

## 验证

每个已复现缺陷先写针对预期行为的回归测试并记录原始失败，再修复、通过相关测试。集成后运行全部pytest、ruff、compileall、版本/隐私检查；使用真实ffmpeg/ffprobe创建小型虚构影片测试转码、合并、轨道、时长、取消和碰撞；Qt离屏检查17能力及导航/选择/运行中退出；真实受控子进程检验静默超时和进程树停止。构建新的独立发行包并检查PE资源、依赖和启动。

Windows、当前ffmpeg与Qt的实际验证不能宣称覆盖所有操作系统/GPU或上游版本。无法实测的真实服务schema/多GPU等要以兼容适配和边界测试实现，明确验收证据与未实测范围，不以“已全绿”冒充真实平台证明。
