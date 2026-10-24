# 分组复核与人工标签同步实施计划

**Goal:** 减少低风险本地视频的重复人工审核，让已确认结论持久同步到 NFO 和 Jellyfin，完成可启动的 Windows 打包。

**Architecture:** 保留通用像素网格分析和版本化人工确认。来源前缀只选择分阶段取样策略，第一批覆盖全部时段；证据不足或疑似继续余下计划取样。人工状态通过独立持久任务同步，算法低风险不写确认标签。元数据任务与 UI 分开，失败分目标重试。

**Tech Stack:** 现有 Python / NumPy / FFmpeg / SQLite / PySide6 / PyInstaller。

**Authorization:** 用户已明确委托实现细节及本地打包。继续在指定目录，保留前轮未提交更改，不发布远端，不访问媒体站点；不需要再询问同范围设计批准。

## 决策与边界

- 不按 FC2 名称免检，也不推断哪处马赛克与内容“相关”。分组为可编辑前缀（默认 FC2）；默认首轮 30 个均匀时段、通过低风险门槛才暂不作为待办，随时可完整检查。
- 优先方案为“同一完整计划的分阶段取样 + 显式人工标签后台同步”。未选“所有 FC2 自动无马赛克”，会漏掉例外；未选“每个视频都人工确认”，无法减少工作量。
- 仅人工确认触发标签。默认受管标签为 `像素化:有` / `像素化:未见`，可改；未知、低风险、失败不写这些标签。
- 默认开启 NFO 和已配置 Jellyfin 的未来确认同步；未配置服务器显示待配置。旧确认通过显式补同步入口，不在升级后自动全库改写。
- NFO 使用同名文件，已有唯一归属的 fallback 可用，拒绝多人共享 NFO；创建缺失同名 NFO 可关闭。写入保留无关标签、字段及备份，校验并发变更。
- Jellyfin 使用 API 精确路径和本地映射匹配，不用作品编号猜条目、不写服务器数据库、不修改观看状态。每批只构建一次条目索引，歧义不写。
- 按当前文件身份与任务代次执行；改结论、撤回、停止、文件替换和网络失败均保留可恢复状态，不能把本地确认成功误报为两端同步成功。

## 接口约定

新增 `censor/triage.py`：`triage_plan(path, cfg, requested, *, explicit=False) -> dict`，返回 `enabled/group/initial_indices/order` 等取样计划；保持非分组默认全量。

新增 `censor/metadata_sync.py`：`MetadataSyncService(cfg, verdict_store)`；`enqueue(path, file_identity, state)`（state 为 CensorState 或 None）、`run_pending(should_stop=..., log=..., retry_failed=False) -> dict`、`status() -> dict`、`enqueue_confirmed(paths=None)`。配置键由主实现添加：`censor_sync_nfo`、`censor_sync_jellyfin`、`censor_sync_create_nfo`、`censor_sync_tag_censored`、`censor_sync_tag_uncensored`。

## Tasks

- [x] 1. RED→GREEN：可配置来源分组、均匀初检、疑似续查、不重复解码计划点、显式深入检查绕开分流，展示实际覆盖与复用指标。
- [x] 2. RED→GREEN：人工标签双目标持久队列、差量写入与撤回、文件版本/任务代次检查、未配置/歧义/失败重试，保留用户无关元数据。
- [x] 3. RED→GREEN：配置和设置 UI、标签确认/撤回入队、后台泵、状态、历史补同步入口；既有确认与视觉证据在 NFO 改动后继续保留。
- [ ] 4. 独立审查、隔离全量测试、合成 HTTP 集成与使用说明；不在测试中连接真实用户服务器或写真实媒体。
- [ ] 5. 更新源码版本，验证收据后构建 onedir/zip/SHA256；保留旧发布 zip，验证新 EXE 版本及启动。

## 封包前记录

独立审查发现的四项同步边界问题已补充回归并修复：配置变化后重排队、未获确认的 POST、撤回后退休所有权凭据、同名不同扩展视频共用 NFO。补充了批量确认的选择范围/取消测试及无声明 GBK NFO 测试，最终针对性检查为 66 passed。完整验证与封包结果以 `build/verification.json`、`dist/JellyfinToolkit/build-manifest.json` 和对应日志为准；不在生成验证摘要后修改此源码记录。
