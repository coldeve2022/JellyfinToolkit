# 通用视频像素化检测可靠性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让通用像素化检测确实分析当前文件、及时响应停止、正确复用结果，并给出可追踪且不过度确定的画面证据。

**Architecture:** 保留 `censor` 的检测器、融合、存储和服务分层。新增独立的输入签名与评估模块；处理状态独立于画面结论。视觉能力用注册信息表示，UI 继续调用服务，不自行判断画面。

**Tech Stack:** Python >=3.9、NumPy、SQLite、FFmpeg/FFprobe、PySide6、pytest、Ruff；不增加产品运行时依赖。

**Spec:** [已确认的审查与改进设计](../specs/2026-10-21-pixelation-reliability-design.md)，用户于 2026-10-21 确认采用方案 B。

## Global Constraints

- 不识别身体部位，不联网检索版本，不下载或替换文件。
- 沿用 JellyfinToolkit 的 Qt 界面和交互风格。
- 不以“通过 CI”代替准确率验证；抽样未检出不等于全片不存在像素化。
- 保留历史人工记录；文件版本不同不能自动沿用其确认结论。
- 不修改发行归档，不自动发布、推送或更新用户媒体库。
- 在实际源码仓库实施前检查 Git 状态，并通过受控工具取得写入权限；不覆盖其他人的修改。
- 以下源码路径均相对于 `D:\JellyfinToolkit\dev\jellyfin-toolkit`。

## Review Focus

1. 文件在扫描中被替换：结果不得写成新版本文件的有效缓存；Task 1/3 验证。
2. 数据库为旧 schema，或含损坏 JSON：启动可迁移，历史标注保留，损坏记录可重算；Task 1 验证。
3. 取消发生在整帧字节到齐之前或队列满时：进程和读取线程均能结束；Task 2 验证。
4. 注册第二个视觉插件后关闭视觉：两个视觉插件都不能运行；Task 3 验证。
5. 普通棋盘、条纹与全局低清：基准必须包含这些歧义样本，不能把结构得分展示为校准概率；Task 4/5 验证。

## Task 1: 状态、文件身份与向后兼容缓存

**Files:**
- Create: `censor/identity.py`
- Modify: `censor/models.py`, `censor/store.py`
- Test: `tests/test_censor_store_identity.py`

**Interfaces:**
- Produces: `ProcessingStatus(str, Enum)`，值 `completed`, `cancelled`, `failed`, `partial`。
- Produces: `FileIdentity`，保存 `size: int`, `mtime_ns: int`, `sample_hash: str`；`identify_file(path: str | Path) -> FileIdentity`，分段内容摘要覆盖开头、中间和结尾，每段最多 65536 字节，小文件不重复读取。
- Produces: `VideoVerdict.status`, `.sampled_frames`, `.analyzed_frames`, `.file_identity`, `.analysis_signature`，为旧调用保留默认值。
- Produces: `VerdictStore.get(..., *, analysis_signature: str | None = None, file_identity: str | None = None)`；未指定新参数保留历史浏览接口，服务严格指定身份与签名。
- Produces: `set_label(..., *, file_identity: str = '')` 和 `get_label(..., *, file_identity: str | None = None)`；标签历史表保留旧版本及无身份旧记录。

- [ ] 写失败测试：旧 schema 自动增加列且保留行；损坏 evidence/candidates JSON 不使页面崩溃；`get` 严格签名不匹配返回 None；文件内容被改但 size/mtime 恢复时身份不同；`set_label` 后改变身份，严格 `get_label` 返回 None，历史记录仍存在。
- [ ] 运行 `python -m pytest tests/test_censor_store_identity.py -q`，确认失败来自缺少新行为。
- [ ] 实现上述模型、身份函数和幂等 SQLite 迁移；历史标签只用于展示/导出，不能成为新文件证据；迁移在事务内完成。
- [ ] 运行新测试及 `tests/test_censor_pipeline.py`，核对旧接口兼容性；检查 `git diff`。
- [ ] 保存本任务改动；提交仅在用户要求或项目规则要求时执行。

## Task 2: 可中断且有界的抽帧

**Files:**
- Modify: `censor/sampling.py`
- Test: `tests/test_censor_sampling.py`

**Interfaces:**
- Produces: `SamplingCancelled(SamplingError)`、`SamplingTimeout(SamplingError)`。
- Preserves: `FrameSampler.sample(...) -> list[SampledFrame]`；取消/超时抛对应异常，不将半完成抽帧伪装成完整成功。
- Produces: `FrameSampler.last_sampled_count: int`，失败/取消时也可取得已采样数量。

- [ ] 写失败测试：用 Python 子进程模拟持续沉默、分段输出、输出后非零退出、半帧 EOF；设置 timeout=0.1 时沉默进程在 2 秒内结束并抛超时；停止回调在 2 秒内取消；正常短片可少于请求数量，但零帧成功退出仍为错误。
- [ ] 写失败测试：队列满时取消、外部进程提前退出、重复调用后无存活的读取线程或子进程；无效尺寸/count/NaN 时长在启动前报错。
- [ ] 运行 `python -m pytest tests/test_censor_sampling.py -q`，确认失败。
- [ ] 用 `Queue(maxsize=2)` 和后台读取线程组装完整帧；控制线程每最多 0.05 秒检查取消和 `time.monotonic()`。清理先终止进程、必要时 kill、wait、关闭流，再等待线程结束；错误诊断保存有界 stderr 尾部，避免 stderr 管道填满。
- [ ] 检查 FFmpeg 退出码；维持 EOF/错误/取消的独立语义；保持原有采样算法，输出文字注明时间点为抽样目标时间而非精确源关键帧 PTS。
- [ ] 运行新增抽帧测试；检测到可用 FFmpeg 时，用非敏感合成视频补充实际解码测试。

## Task 3: 按能力编排、严格签名与文件级结论

**Files:**
- Modify: `censor/base.py`, `censor/registry.py`, `censor/__init__.py`, `censor/service.py`, `censor/fusion.py`
- Modify: `tests/test_censor_pipeline.py`, `tests/test_censor_fusion.py`
- Test: `tests/test_censor_service_reliability.py`

**Interfaces:**
- Consumes: Task 1 身份与状态、Task 2 取消和超时异常。
- Produces: `BaseDetector.version: str = '1'`, `BaseDetector.capabilities: tuple[str, ...] = ()`；内置视觉插件声明 `visual`, `ffmpeg`。
- Produces: `CensorService._analysis_signature(...) -> str`，稳定 JSON+SHA256，包含启用插件/版本/能力、融合策略、有效检测配置、服务模式、服务器标签和 NFO 内容摘要；顺序可忽略的集合先排序。
- Preserves: `probe`, `probe_many`, `label` 外部调用方式。

- [ ] 写失败测试：关闭视觉生成缓存后开启视觉必须运行；改参数、插件版本、NFO/服务器标签后重算；相同签名命中缓存；禁用缓存不读写自动结果。
- [ ] 写失败测试：`detectors=[]` 启用零个；dict/object 配置一致；未知或构造失败的显式插件可见报错；任意 `visual` 插件都遵守视觉开关，`ffmpeg` 插件都能取得工具。
- [ ] 写失败测试：取消、失败不写完整缓存；部分检测器失败为 partial+待确认；替换后旧人工结论不生效；扫描期间文件改变拒绝缓存；只存在元数据时不自动确认文件画面无像素化。
- [ ] 运行新增测试，确认失败。
- [ ] 实现状态汇总、签名校验和能力编排，移除对具体视觉插件名字的比较。视觉模式不因系列/标签跳过画面；旧跳过选项保留兼容但不能默认为文件级视觉确证。
- [ ] 调整融合：元数据仅提供线索，视觉未检出不提高元数据置信度；异常/取消强制待确认；多个视觉插件产生冲突时保留冲突依据并待确认。人工确认只在文件身份匹配时生效。
- [ ] 运行全部 `tests/test_censor_*.py`；更新旧测试中与确认设计冲突的断言，并保留测试解释。

## Task 4: 通用、多尺度的像素化候选

**Files:**
- Modify: `censor/mosaic_cv.py`, `censor/models.py`, `config.py`, `config.example.json`
- Modify: `tests/test_censor_mosaic.py`
- Test: `tests/test_censor_visual_evidence.py`

**Interfaces:**
- Produces: `MosaicParams.windows: tuple[int, ...] = (80, 160, 240)`、`candidate_gap_seconds: float = 10.0`，支持 `censor_windows`、`censor_candidate_gap_seconds` 配置；保留 `window` 兼容单尺度调用。
- Extends: `analyze_frame(frame, params=None, *, should_stop=None) -> MosaicHit`；每个滑窗检查停止。
- Preserves: `analyze_frames(...) -> tuple[list[FrameCandidate], float]`；候选按结构得分筛选、用时间间隔去重；`top=0` 返回空候选但不破坏最高分计算。
- Produces: `ProbeContext.shared` 的 `sampled_frames`、`analyzed_frames` 和 `visual_summary` 供服务聚合。

- [ ] 写失败测试：尾部边缘区域被扫描；适合小窗口的低分辨率输入可分析；输入通道/字节数不匹配、非法 stride、min/max 倒置、非有限阈值均显式拒绝。
- [ ] 写失败测试：同样灰度结构的不同颜色帧不因肤色改变排序；候选遵守 review_score；时间上相邻高分帧不挤满面板；窗口内取消可中断。
- [ ] 运行新增测试，确认失败。
- [ ] 补齐各尺度滑窗末尾位置，使用完整配置参数且统一校验；移除肤色计算/排序，旧 skin_ratio 字段仅兼容反序列化。
- [ ] 使用多帧独立时间证据支持自动确认；只出现一次强结构记作疑似，不将分数映射为概率。文案用“抽样检出/未检出”“疑似像素化”“覆盖不足”。
- [ ] 运行全部图像测试；记录多尺度默认配置相对单尺度的时间与候选差异，避免不加测量地声称提速或更准。

## Task 5: UI 状态、合成评估和完整检查

**Files:**
- Modify: `ui/pages/censorship.py`, `docs/有码检测.md`
- Create: `censor/evaluation.py`, `tools/dev/evaluate_pixelation.py`
- Test: `tests/test_censor_evaluation.py`, `tests/test_censor_ui_status.py`

**Interfaces:**
- Consumes: Task 1/3 的状态与摘要，Task 4 的视觉候选。
- Produces: `evaluate_cases(cases: Iterable[EvaluationCase], params: MosaicParams) -> dict`；每条保存 case_id、split、expected、score、elapsed、参数与算法版本，输出类别分组统计，不把合成结果称为实际媒体准确率。
- Produces: CLI `python tools/dev/evaluate_pixelation.py --output <path>`，输出 JSON；合成样本分 `development` / `validation`，seed 不重叠。

- [ ] 写失败测试：UI 在取消/失败/部分完成时显示处理状态和实际帧数，错误可见；仍能筛选与人工复核；没有画面证据时不展示视觉置信概率。
- [ ] 写失败测试：合成场景覆盖噪声/渐变/文本/条纹/棋盘/黑边/全局低清/局部块平均，含位置和尺寸变化；歧义场景单列而非强填负例；输出可 JSON roundtrip，验证集和调参集 seed 分离。
- [ ] 运行新增测试，确认失败。
- [ ] 接入状态和证据摘要，维持已有布局、配色、表格和缩略图；有界提交批量任务，停止后取消排队任务，所有完成路径恢复按钮状态。
- [ ] 实现评估与文档，替换“单一网格能证明文件事实”等过度确定的描述，说明抽样时间精度和同结构歧义。
- [ ] 执行相关测试、`ruff check .`、`python scripts/version_manage.py check`；再运行完整 `python -m pytest tests -q` 和编译检查（pycache 重定向到临时目录）。区分已有失败、新失败和环境跳过。
- [ ] 检查最终 diff、工作区污染和依赖变化；用任务 1–4 的回归及评估输出完成整体审查。若采用 native 执行，按技能要求在末尾由独立 reviewer 检查整组改动。

## 执行与交付

推荐由当前会话直接实施（Native）：五项任务共享模型和接口，按顺序落地更容易保持迁移与 UI 一致。最终交付源码 diff、测试结果、合成评估 JSON、已知限制和运行说明。产品源码修改前仍需用户审阅本计划并选择执行方式；不将方案 B 的设计确认代替计划审阅。

## 计划自查

- 设计三个阶段均由上述任务覆盖，所有五项已复现问题有对应回归。
- 没有新增下载、版本检索或媒体库写回功能。
- 每项明确生产/消费的状态与接口，旧接口有兼容路径。
- 原始 pytest 基线尚待测试依赖准备完成后运行；没有把未执行的检查标成通过。
