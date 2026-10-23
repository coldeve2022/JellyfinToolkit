# System Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成已批准方案 B，修复 18 项缺陷并贯通检测、检索、替换、自动化与可定制工作区。

**Architecture:** 保留现有纯 Python 领域服务与 Qt 页面分层。用版本化文件身份、作品键、明确执行状态、统一配置快照和事件串联功能；复用已有可恢复媒体事务。

**Tech Stack:** Python >=3.9、PySide6 >=6.6,<6.12、NumPy、SQLite、标准库 HTTP、FFmpeg；不新增产品依赖。

**Spec:** `docs/superpowers/specs/2026-10-23-system-optimization-design.md`（用户已批准 B 及在当前会话逐阶段实施）。

## Global Constraints

- 工作在用户指定的当前目录和 `codex/pixelation-reliability` 分支，保留现有数据；不发布、不推送、不操作真实服务器媒体。
- 检测结论、作品版本可得性、执行结果保持独立；临时失败和取消不得覆盖成功事实或伪装为“未见”。
- 同文件不同版本的人工结论不可自动继承。算法参数变化失效；主题、并发和布局变化不导致重扫。
- 每个任务先写行为测试并验证失败，再实现；阶段结束运行相关回归，最终运行完整套件。
- 保持配置/缓存向后读取能力，错误记录单条隔离；有效证据与事务备份不能误清理。

## Review Focus

1. 一源成功一源失败时，“未见”不得变成全面否定（任务 1）。
2. 名称否定、相似番号、同条目混合磁链不能成为错误下载默认项（任务 2）。
3. 扫描中改配置或换文件，旧结果不得回填新上下文（任务 3/5）。
4. 排序/筛选/关闭窗口与后台回填重叠，不能串行或丢线程所有权（任务 4/6/9）。
5. 取消、失败、重启和恢复后的队列/证据仍应可用（任务 1/7/10）。

## Task 1: 查询执行与 HTTP 调度

**Files:** `censor/releases/{models,service,store,transport}.py`; `tests/test_system_optimization_release.py`。

**Interfaces:** `ReleaseReport.status: ProcessingStatus`、`cache_hit: bool`；`HttpTransport.query_scope(should_stop=...)` 为每作品预算上下文；`ReleaseStore.record_attempt(report)` 保留尝试，`pending/retry_failed` 负责续跑；`ReleaseService.fresh(number, options=None)` 统一有效缓存。

- [ ] RED：跨查询额度重置、取消保留缓存/待办、临时失败重试、禁用零请求、详情 limit、构造失败诊断、部分成功不产生全面未见。
- [ ] 实现查询级预算、可打断退避/限速、每实际请求计数、独立执行状态与尝试记录；成功缓存与失败日志分离。
- [ ] GREEN：上述测试及 `test_release_service.py` / `test_release_remediation.py`。

## Task 2: 作品匹配、证据和下载入口

**Files:** `censor/releases/{models,base,javdb,javbus,supjav,store}.py`、`censor/tag_signals.py`；同任务 1 测试文件及 `test_release_sources/index.py`。

**Interfaces:** `contains_number(text, number)` 完整作品边界；`classify_tag` 共用否定/归一化；`ReleaseCandidate.best_magnet` 只选可确认属于该版本的入口；`ReleaseReport.best` 只从无码候选默认选择。

- [ ] RED：标题软线索 <0.8、Not Uncensored 为否定、返回页番号校验、XNCRA 不匹配 NCRA、只有有码不提供默认复制；磁链实体正常解码。
- [ ] 实现公共语言/作品匹配工具与站点证据分级，升级解析器/判定签名版本。
- [ ] GREEN：源夹具、版本模型与传输回归。

## Task 3: 文件版本、缓存与批量读取

**Files:** `censor/{service,store}.py`、`censor/releases/store.py`；`tests/test_system_optimization_state.py`。

**Interfaces:** `VerdictStore.confirmed_censored(verify_identity=True)` 核验实际文件；`get_many` 批量查询；`CensorService.refresh_tools()` 清依赖；`remove_label(path)` 撤回当前版本标签，历史保留；签名仅含有效分析选项。

- [ ] RED：原路径替换/移除不进入有效确认清单，改 workers 不重扫、改阈值重扫；工具配置刷新；损坏 JSON 单条跳过；撤回不覆盖算法证据。
- [ ] 实现身份与分析参数契约、批量读取及显式连接关闭，保持旧缓存兼容。
- [ ] GREEN：身份/标签/任务历史与新状态回归。

## Task 4: 检索页有效刷新与稳定表格更新

**Files:** `ui/pages/censor_replace.py`、`ui/censor_release.py`、`ui/services.py`；`tests/test_system_optimization_ui.py`。

**Interfaces:** `events.censor_changed = Signal(list)` 发布文件路径；检索页 `refresh()` 由事件/show 触发，保持选择；`lookup_targets` 使用 fresh 契约；行更新冻结排序。

- [ ] RED：标记事件后新行出现、标签撤回后消失；过期/失败重查；128 组合排序回填；候选索引不累积；状态颜色随主题刷新。
- [ ] 实现 stable-key 回填、选中与详情同步、候选生命周期、数字排序。
- [ ] GREEN：新 UI 回归、现有替换页与证据面板测试。

## Task 5: 配置快照与公共工具逻辑

**Files:** `ui/release_settings.py`（新增）、`ui/pages/{censorship,censor_replace,settings}.py`、`ui/censor_scan.py`、`utils/{tool_search,lada_tool,whisper_tool,library,media_types}.py`。

**Interfaces:** `ReleaseSettings` 编辑副本，`apply(cfg)` 校验保存并广播；worker deep-copy 服务 cfg；公共 `parse_cli_options(text)` 与 `drives()`。

- [ ] RED：取消设置不改变 cfg；保存后 transport/tool 立即刷新；启动后改 cfg 不改变当前任务；公共工具发现/选项解析保留功能。
- [ ] 实现共享设置部件与委托，检测页仅保留检测参数，检索页可编辑全部有效网络/来源设置。
- [ ] GREEN：新配置行为测试、现有工具集成/设置回归。

## Task 6: 统一任务、自动检索与媒体库输入

**Files:** `ui/services.py`、`ui/censor_release.py`、两核心页、`main.py`、`censor/workflow.py`（新增）。

**Interfaces:** `confirmed_work_items(censor_service)` 提供有效作品清单；`queue_confirmed(...)` 公共入队；检索 worker 使用 TaskManager，stats 区分 ok/fail/cancelled；检测页 `receive_paths` / `import_library` 复用采集范围。

- [ ] RED：人工确认后自动检索可开关；检索不阻塞另一页只读任务；失败/取消任务正确呈现；当前库无映射有明确结果。
- [ ] 实现事件与任务接线、队列续跑、检索所选/失败重试与库范围导入。
- [ ] GREEN：真实 Qt 线程的虚构端到端测试，原任务生命周期回归。

## Task 7: 本地候选配对、替换与失效传播

**Files:** `utils/replacement.py`、`ui/pages/{censor_replace,replace}.py`、`main.py`、`ui/services.py`。

**Interfaces:** `preview_release_replacements(candidate_paths, originals, should_stop=None)` 返回既有 replacement plan 结构；`events.replacement_ready(list,list)`；替换成功发送 `media_changed`，检测页旧记录失效并可重查。

- [ ] RED：同番号多副本 ambiguous、唯一版本匹配、未审核不执行、替换后不沿用旧人工结论、失败保留原件、恢复有效。
- [ ] 实现审阅配对与现有事务共用；文件操作与元数据同步分开计结果。
- [ ] GREEN：媒体事务、替换执行、新闭环测试。

## Task 8: 自动化步骤复用

**Files:** `automation/{orchestrator,pipeline,plans}.py`、`ui/pages/automation.py`、`censor/workflow.py`。

**Interfaces:** 公共 `run_censor_workflow(cfg, paths, *, scan, lookup, should_stop, log, progress)`，支持自动化步骤 `pixelation_scan`、`release_lookup`；只读步骤与既有写计划分离。

- [ ] RED：同范围、同缓存/身份、取消不启动后续步骤、失败摘要准确，CLI/GUI 用同服务。
- [ ] 实现新增步骤与选项、计划/执行/报告状态。
- [ ] GREEN：自动化回归与公共流程测试。

## Task 9: 可恢复工作区与自定义 UI

**Files:** `ui/workspace_preferences.py`（新增）、`ui/table_tools.py`、两核心页、`ui/censor_gallery.py`、`ui/theme.py`。

**Interfaces:** `WorkspacePreferences(key)` 管理表格、splitter、字体/密度/快捷键/自动下一条与画廊大小；`attach_workspace(...)` 提供公共偏好菜单与保存/恢复/默认；偏好不进入算法签名。

- [ ] RED：列显隐/顺序/宽度和分隔条重启恢复、筛选保留、默认重置、窄窗操作可达、快捷键和自动下一条实际生效。
- [ ] 实现公共偏好、可伸缩布局、按宽度调整画廊与主题 token；保留稳定选中与操作身份。
- [ ] GREEN：UI 行为、截图/控件边界和缩放验收。

## Task 10: 流式分析与资源预算

**Files:** `censor/{sampling,mosaic_cv,service}.py`、`ui/censor_scan.py`、批量 store 接口。

**Interfaces:** `FrameSampler.iter_samples(...)` 精确时间戳流；检测器边取边分析/写必要证据；`CensorService.cleanup_thumbnails(max_bytes=...)` 只清未引用目录；并发按内存预算限制。

- [ ] RED：分析首帧先于最后取样、取消不开始下一帧、长 GOP 不重复、图像证据一致、清理保留有效引用、内存预算限制并发。
- [ ] 实现流式帧生命周期、较少证据图子进程、后台缓存分区；保留 list `sample` 兼容接口。
- [ ] GREEN：同条件合成性能对比与所有采样/算法回归，不用少取帧冒充加速。

## Task 11: 完整回归、独立审查与交付

**Files:** `docs/系统审计-2026-10-23.md`、现行使用文档、`CHANGELOG.md`、实施状态记录。

- [ ] 完整 pytest、Ruff、编译、版本一致性检查；回环网络测试在允许的环境单独验证。
- [ ] 19 页导航/关闭，100%/125%/150% 缩放与小窗验证；所有媒体操作只用合成数据。
- [ ] 按 executing-plans 要求派发一次独立最终代码审查；重要问题 RED→GREEN 修复后重跑套件。
- [ ] 把 18 项缺陷与新增能力逐项映射到实际测试，记录性能与真实环境边界，交付当前目录更改。
