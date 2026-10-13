# 3.10.0 系统整改对照

依据 2026-10-13 审计，基线提交 `2f04726f894de808f5f696e8849741c7b58c4014`。本次覆盖 C01–C31、M01–M19、L01–L03、UI-01–UI-23、R01–R10 共 86 个审计记录；其中存在交叉项，不能当作 86 个独立缺陷。

已实施的变化包括统一用户/来源身份与反馈快照、GUI/CLI 共用评分和媒体执行、持久化事务与冲突拒绝、审核计划绑定输入哈希、全局任务管理、分组导航和小窗口布局、不可变归档及同一源码发布门禁。原有 17 类能力保留，新增任务中心，提供任务结果、取消与恢复入口。

以下为实现与回归对照。域报告中“由 root/UI/媒体负责”的跨域项已在协调整合完成；完整发布验收以构建清单 `build-manifest.json` 及其 verification 记录为准。

## 统一工作流

1. 设置明确选择观看账号；远程库需要显式路径映射才能处理本地文件。身份不明确时不根据猜测的观看行为自动删。
2. 人工“喜欢”进入画像，“保留”保护文件；旧版本无来源的正向标签先保守保护，任务中心可审阅迁移到当前账号。AI 输出先展示建议，再明确采纳。
3. 扫描生成审核计划；执行绑定本次配置、文件内容哈希和精确目标。目标变化、账号变化、计划重放或未知冲突均拒绝写入。
4. 删除进入应用隔离区；替换、NFO、归档和数据库操作先备份/记日志。任务中心支持恢复，存在新文件/新编辑时拒绝覆盖。
5. Whisper/Lada 用私有本次输出目录，失败不把旧产物算成功。跨目录同名媒体按来源身份分目录输出。复杂字幕/轨道不能安全保留时明确拒绝自动处理。

## 核心数据与评分

| 编号 | 已交付行为 | 验证 |
|---|---|---|
| C01 P1 | 媒体域改回收/隔离执行；核心不改 trash，给重复实体和保留标签保护 | 媒体域交付；核心 same_physical_file / alias protection 回归 |
| C02 P1 | 手动移除列表与最终删除目标一致由 UI 域实施 | UI 域交付；未冒称核心改 UI |
| C03 P1 | 新 scope 存储+统一标签快照，rerank 不读旧无来源标签；UI 广播和执行前重读 | snapshot_reads_scoped_feedback、actual viewer scope；UI services 协作 |
| C04 P1 | 同路径/同实体库行先折叠，别名 KEEP 实体保护 | same_physical_file、keep_on_an_alias |
| C05 P1 | API 明确/唯一用户，DB 按用户过滤，无身份未知；实际用户 scope 防唯一用户更换串用 | api_requires_explicit_viewer、db_multiuser/ambiguous/no_identity、automatic_sole_viewer_change、worker selected viewer snapshot |
| C06 P1 | 冷启动和未知行为 REVIEW；prefix 单特征不足以自动删；AI 负猜测不构成真值 | 两代引擎 cold_start、prefix_only、ai_negative_suggestion |
| C07 P1 | PLAYED_LIKE 学习并进入画像，KEEP 仅保护；UI 👍 使用明确 LIKE | 两代引擎 explicit_like_learns_while_keep、profile positive |
| C08 P1 | 厂商 DTO Name 正规化，不把 dict repr 当特征 | api_studio_objects_are_names |
| C09 P1 | 标题无番号/CD 时回退实际媒体文件名 | title_falls_back_to_file_number_and_split_index |
| C10 P1 | 实际质量事实、破解版标记进入重复排序，等质量不自动删 | cracked_file、known_resolution、api_media_stream_facts |
| C11 P1 | AI 批内索引映射真实 item_id，不用业务番号作身份 | recheck_maps_duplicate_numbers_to_batch_identity |
| C12 P2 | 核心 AI 输出建议不自动写人工标签，UI 显式采纳并统一广播 | UI 域建议采纳实现，核心 typed verdict |
| C13 P2 | score_snapshot 评分和 profile 用同一 scoped labels；UI 入口委托 | snapshot_reads_scoped_feedback |
| C14 P2 | 推荐要求有效正样本/正命中，排除 DELETE、CD、junk | recommendation_excludes_zero_hit_and_delete |
| C15 P2 | 删除预测不进入 trusted neg_features/AI taste prompt，只有真实反馈负样本 | explicit_positive_is_in_profile_and_not_invented_negative、AI negative cold start |
| C16 P2 | AI 异常/无有效 JSON 明确 finished.error；UI 不再空结果宣称安全 | AI invalid response tests；UI 域错误展示 |
| C17 P2 | Rerank 异常有完成信号；trash 日志/worker 异常由媒体域 | rerank_error_always_emits_completion；媒体域交付 |
| C18 P2 | 主题保留勾选由 UI 域实施 | UI 域 theme selection 回归 |
| C19 P2 | 核心结构化去重建议+CD删除白名单拒绝，UI 审查采纳 | ai_results.parse_dedup_json、UI 域应用建议 |
| C20 P2 | AI taste 严格 JSON 类型校验，解析失败不保存；评分无效失败 | invalid_taste_does_not_destroy_old、invalid_scoring_response |
| C21 P2 | 内部分桶25项、各桶profile输入、累计scored；UI 构造可信正样本各桶画像 | AIScoreWorker 实施；UI 域分桶批次测试 |
| C22 P2 | scope+fresh fingerprint；统一清 profile+semantic，过期值也可检测/清除 | state fingerprint/context tests、clear_ai_rolls_back、stale_state_detectable |
| C23 P2 | 写前 GET 最新标签，只删计划tokens；checkpoint真实before/after；rollback保留新增标签 | clean_recomputes_from_latest_tags、restore_preserves_tags_added、partial_failure_count |
| C24 P2 | FC2 可选 PPV 与5–7位号码统一 FC2-number | parametrized common_fc2_ppv_names |
| C25 P2 | sync 与采集共享地址补全；身份helper保留代理path case | sync_address_uses_same_normalization、pathcase两项 |
| C26 P2 | 直接 collect 取消异常+worker取消明确，无部分统计；成功删实体从模型移除由UI | direct_db_collection_cancellation、cancelled_ingest；UI 域实体过滤 |
| C27 P2 | 按当前tab/选择模型确认目标由UI域 | UI域交付；核心保留标签/实体保护 |
| C28 P3 | 百分比数字role排序由UI域实施 | UI域数值排序测试 |
| C29 P3 | summarize 共享 dedup_groups/junk/all-CD/实体规则；占比分母UI域 | overview_uses_same_junk_and_cd_group_rules |
| C30 P3 | CSV 标准 csv.writer，逗号/引号/换行可回读 | csv_preserves_comma_quote_and_newline |
| C31 P3 | library/library_source/media_server 文档边界改10.11+/10.10之前，仍探测实际schema | schema fixtures及旧Emby/Jellyfin回归 |
|---|---:|---:|

## 媒体执行与文件事务

| 编号 | 状态及实施位置 | 可复查的验收行为 |
|---|---|---|
| M01 | 协调域 root：统一自动化入口/计划接口；媒体提供共享核心 | 自动化入口与空库契约由 root orchestration 回归验收。 |
| M02 | 完成；[FFmpeg worker](../workers/ffmpeg.py:348)、[file_ops](../utils/file_ops.py:291) | 真实 MKV 与既有独立 MP4 共存，转码拒绝覆盖；不同格式成功转码保留源，原位修复保留旧目标备份。 |
| M03 | 完成；FFmpeg worker 使用 root `proc.run_streaming` | 静默真实 Python 子进程绝对 deadline 到期回收，0.2秒限制不等自然1.3秒退出；取消/超时保留原件。 |
| M04 | 完成；[NFO业务](../utils/nfo.py:118) | 源缺失、已有目标均不预删；预览身份改变拒绝旧计划；改名/内容写入记录备份，半项取消逆序撤回。 |
| M05 | 完成；[字幕解析/清理](../utils/subtitle_clean.py:298) | VTT mm:ss、cue ID/settings、NOTE/STYLE保留；无序号SRT解析；空/部分解析拒绝写回，严格解码与可恢复写入。 |
| M06 | 完成；[数据库转移](../workers/database.py:19) | 新库有效播放/收藏/评分等保持；只填空状态；旧重复用户记录按日期/有效状态确定选取；取消回滚整个事务。 |
| M07 | 完成；[SQLite逻辑快照](../utils/file_ops.py:70)、workers.database、utils.censor_tags | 活跃WAL中已提交行包含于快照，integrity_check通过；不覆盖已有快照；独立关系表schema拒绝不安全本地标签写入。 |
| M08 | 完成；[修复 worker](../workers/merge_repair.py:121)、utils.merge_repair | final与第一分集同路径时优先有效backup，final禁止作为原始输入；修复产物/字幕整组失败逆序恢复。 |
| M09 | 完成；utils.merge_archive、utils.merge_repair | session UUID分组，同番号不同目录不混组；旧记录按目录兼容；未完成归档不假装完整修复任务。 |
| M10 | 完成；[归档业务](../utils/merge_archive.py:248)、OperationJournal | 意图日志持久化早于媒体变更；每步跟踪operation/session；日志首写失败零媒体mutation；失败补救记录保留，session恢复支持冲突拒绝。 |
| M11 | 完成；[Whisper worker](../workers/whisper_gen.py:121) | 旧字幕与本次新产物隔离；非零/启动失败不清理旧字幕；既有且不覆盖计skipped；无新字幕不算生成成功；第二格式解析失败撤回第一格式已安装字幕。 |
| M12 | 完成；[Lada工具](../utils/lada_tool.py:486)、workers.lada_restore | code非零明确失败，旧恢复产物不算本次成功；私有目录产物通过校验后才落位，失败无活动目录坏输出。 |
| M13 | 协调域 root；共享 [merge_group_ffmpeg](../utils/merge_executor.py:119) 已提供 | GUI/CLI委托同一纯核心，阶段/每分集/runner统一取消；root负责编排无取消后新执行。 |
| M14 | 完成；共享merge executor | 真实HEVC不同尺寸分集归一为H264，按目标probe校验，不再用旧HEVC codec拒绝；HDR未经明确色彩转换拒绝自动归一。 |
| M15 | 完成；共享merge executor + root统一file_scan | 所有norm/partial/intermediate存私有TemporaryDirectory，失败/取消finally清理；扫描排除应用临时与备份目录。 |
| M16 | 完成软件逻辑；[GPU门控](../utils/gpu.py:75)、workers.lada_restore | cuda:1查询第二设备；high/low滞回序列11→9→7直到低水位才恢复；真实显存/模型并发仍需设备验收。 |
| M17 | root负责proc进程树边界 | 媒体消费root绝对deadline/on_started/取消接口；Windows控制进程测试由root完成；POSIX回归在本机跳过，待Linux。 |
| M18 | 完成；[稳定输出映射](../utils/file_ops.py:24)、Whisper/Lada workers | 集中输出始终来源canonical路径SHA12子目录；同批与分两次单片任务的同basename不串写；find_subtitles同时发现新映射和旧平铺。 |
| M19 | 完成；共享merge executor、归档 | 真实双音轨与内嵌字幕保留；外部SRT/VTT按累计时长拼接；mov_text保留MP4/MOV，其它内嵌字幕选MKV；不安全格式/轨道组合明确拒绝；归档不移走缺少合并副本的外挂字幕。 |

## 页面与交互

| 编号 | 已交付行为 | 实现 | 验证 |
|---|---|---|---|
| UI-01 完成 | 移除手动目标后不再执行；以可见模型和当前tab计划为准 | [ui/pages/delete.py:309](../ui/pages/delete.py:309) | [removed_manual_target_never_appears_in_execution_plan](../tests/test_ui_remediation.py:30) |
| UI-02 完成 | 扫描缓存只影响当前队列，新增未扫描输入保留；空缓存由调用方处理当前输入 | [ui/pages/repair.py:267](../ui/pages/repair.py:267) | [repair_plan_uses_current_queue_including_unscanned_inputs](../tests/test_ui_remediation.py:53) |
| UI-03 完成 | 主题只刷新颜色，取消勾选与组展开/滚动状态保留 | [ui/pages/dedup.py:567](../ui/pages/dedup.py:567) | [dedup_theme_preserves_cancelled_deletion](../tests/test_ui_remediation.py:65) |
| UI-04 完成 | 运行中拒绝覆盖扫描引用；共享过滤器后台收集并检查停止 | [ui/pages/censorship.py:182](../ui/pages/censorship.py:182) | [censorship_repeated_input_does_not_replace_running_thread](../tests/test_ui_remediation.py:121) |
| UI-05 完成 | 引擎默认空值保留；编辑模型/格式优先于旧itemData | [ui/pages/subtitle_gen.py:669](../ui/pages/subtitle_gen.py:669) | [whisper_default_and_edited_parameters_roundtrip](../tests/test_ui_remediation.py:95) |
| UI-06 完成 | scoped LabelStore + 全局事件同步；执行前重读保留标签，旧分析反馈不符即拒绝 | [ui/services.py:68](../ui/services.py:68) | [global_label_notification_refreshes_delete](../tests/test_ui_remediation.py:215) |
| UI-07 完成 | 统一强引用活线程；窗口拒绝关闭直至实际线程及关联流程结束，Settings纳入stop | [ui/main_window.py:306](../ui/main_window.py:306) | [shutdown_requests_stop_and_close_waits_for_threads](../tests/test_ui_remediation.py:225) |
| UI-08 完成 | stopping状态保持输入冻结；停止不是线程已结束，等待真实终止和结果投递 | [ui/services.py:378](../ui/services.py:378) | [task_manager_waits_for_actual_thread_end](../tests/test_ui_remediation.py:182) |
| UI-09 完成 | 只读预览→明确路径可取消确认→同一approved_plan_id执行；配置选项变化失效 | [ui/pages/automation.py:143](../ui/pages/automation.py:143) | [automation_executes_only_reviewed_plan](../tests/test_ui_remediation.py:323) |
| UI-10 完成 | 默认重置使用实际combo/edit setter，完成恢复并保留保存步骤 | [ui/pages/settings.py:1128](../ui/pages/settings.py:1128) | [settings_reset_and_ffmpeg_persistence](../tests/test_ui_remediation.py:111) |
| UI-11 完成 | FFmpeg路径写入cfg，保存失败回滚内存cfg并显示失败 | [ui/pages/settings.py:1035](../ui/pages/settings.py:1035) | [settings_reset_and_ffmpeg_persistence](../tests/test_ui_remediation.py:111) |
| UI-12 完成 | 目录删除同步唯一队列；缺字幕列表重查文件与输出目录，去重保留结果 | [ui/pages/subtitle.py:129](../ui/pages/subtitle.py:129) | [generated_subtitle_rechecked_even_after_old_scan](../tests/test_ui_remediation.py:253) |
| UI-13 完成 | 百分比剥离%后存数值role，排序按真正数字 | [ui/pages/review.py:1011](../ui/pages/review.py:1011) | [review_percent_sort_numeric](../tests/test_ui_remediation.py:140) |
| UI-14 完成 | 分布占比使用全库总作品分母，不再把最大条目当100% | [ui/pages/insight.py:326](../ui/pages/insight.py:326) | [insight_bucket_percent_is_population_share](../tests/test_ui_remediation.py:154) |
| UI-15 完成 | 无码偏好组件加入布局；画像6指标改3列，Top图纵排，小窗可读 | [ui/pages/profile.py:63](../ui/pages/profile.py:63) | 18页截图 + [scoring_pages_render_with_synthetic_data](../tests/test_gui_smoke.py:414) |
| UI-16 完成 | 生成字幕/Lada统一add_item路径协议；删除/count同步，直接音频拒收 | [ui/pages/subtitle_gen.py:137](../ui/pages/subtitle_gen.py:137) | [generated_queue_uses_path_role](../tests/test_ui_remediation.py:164) |
| UI-17 完成 | 只读检测checkbox生效；错误/取消不显示成功；旧int,int facade读取真实stats | [ui/services.py:378](../ui/services.py:378) | [legacy_worker_facade_reports_actual_failed_stats](../tests/test_ui_remediation.py:766) |
| UI-18 完成 | 目录枚举、硬件/能力探测、模型列表、服务器请求、NFO内容读取、替换索引、归档和旧合并发现后台化 | [ui/services.py:526](../ui/services.py:526) | [censorship_only_classifies_filtered_media](../tests/test_ui_remediation.py:466) |
| UI-19 完成 | 扫描来源和实际user统一snapshot；目录复用iter_media_files，cfg路径映射/排除一致 | [ui/services.py:150](../ui/services.py:150) | [stale_analysis_cannot_rebind_changed_server](../tests/test_ui_remediation.py:455) |
| UI-20 完成 | AI建议结构化显示，明确采纳可撤销；区分喜欢与仅保留；文件名推测/应用隔离文案准确 | [ui/pages/review.py:889](../ui/pages/review.py:889) | [ai_adoption_is_explicit_and_undoable](../tests/test_ui_remediation.py:348) |
| UI-21 完成 | 侧栏按能力分组并滚动，主动作/进度固定footer；Review换行网格、Settings映射行表及小窗零水平滚动 | [ui/layout.py:103](../ui/layout.py:103) | [navigation_is_scrollable_and_task_center_registered](../tests/test_ui_remediation.py:244) |
| UI-22 完成 | 跨库转移要求用户明确选择jellyfin.db，拒绝自动替换为邻居文件；检查只读SQLite且可interrupt | [ui/pages/db_tools.py:293](../ui/pages/db_tools.py:293) | 静态文件名拒绝分支 + [db_tools_inspect_worker_stops](../tests/test_gui_smoke.py:343) |
| UI-23 完成 | 全局任务管理锁定输入快照、写操作互斥、真实阶段/历史；移除/清空/主题/错误/取消统一约束 | [ui/services.py:243](../ui/services.py:243) | [task_failed_result_arrives_after_thread_return](../tests/test_ui_remediation.py:576) |

## 协调整合与发布

| 编号 | 处理结果 | 验证入口 |
|---|---|---|
| M01 / L01 | 自动化、CLI与GUI共享 roots 范围、扫描和评分；明确区分无本地映射的远程元数据能力；删除前再次读取保护标签/观看状态 | test_orchestration_remediation.py、test_system_http_integration.py |
| M13 / L02 | CLI复用纯媒体合并核心；停止信号贯穿各阶段/文件，取消后不开始新写操作；文件与目录统一展开 | test_orchestration_remediation.py、test_media_remediation_execution.py |
| M17 | Windows Job Object 管理后代，POSIX 独立进程组；停止/超时/主进程早退仍回收子进程 | test_process_lifecycle_remediation.py、test_proc.py |
| L03 | 健康分析不导入 Qt；显式 ffprobe 配置贯穿本次执行，缺工具为失败 | test_system_foundations.py、test_orchestration_remediation.py |
| R01 | Windows入口重定向改为原生 nul，批处理 ASCII+CRLF | test_release_remediation.py |
| R02 | 解释器与 py 的 -3 参数分别处理，支持带空格路径 | test_release_remediation.py |
| R03 | 烟测与报障模板使用真实 data/app-error.log；缺产物、异常日志、提前退出均失败 | test_release_remediation.py、tools/dev/smoke_exe.py |
| R04 | 提升版本同步字符串/固定元组/pyproject，构建验证 PE 四份版本值 | test_release_remediation.py、scripts/version_manage.py check、scripts/build_release.py |
| R05 | True/False/None 三态保存加载保持；非法数值、枚举和配置关联校验 | test_system_foundations.py |
| R06 | 发布依赖同一 tag 的完整测试矩阵；本地验证记录绑定 commit 与全源码摘要，构建后再次核验 | test_release_remediation.py、scripts/verify_release.py、.github/workflows/release.yml |
| R07 | 同版本同哈希仅校验；不同内容拒绝，既有运行目录和便携数据不重建；首次归档私有 stage 提交 | test_release_remediation.py、test_release_flow.py |
| R08 | 下载校验 SHA256；历史来源使用真实 tag，未知来源保持未知，不冒记当前 HEAD | test_release_remediation.py、scripts/release_flow.py |
| R09 | 索引运行路径相对归档根包含版本前缀，未发布快照与正式版分开 | test_release_remediation.py |
| R10 | E2E/截图复用 main.PAGE_SPECS，覆盖全部18页；演示数据仅写独立目录，拒绝覆盖已有库 | test_release_remediation.py、tools/dev/e2e_check.py、tools/dev/make_screenshots.py |

## 测试范围与实际使用边界

Windows 本机验证包含完整 pytest、ruff、compileall、版本检查、虚构库真实 Qt 后台线程、全部页面导航/关闭、100%/125%/150%缩放、真实短片 FFmpeg 音轨/字幕/时长、进程生命周期、本机回环 HTTP 请求、真实 C/D 跨盘替换与恢复，以及已安装 Whisper/Lada 的只读 CLI 能力探测。构建产物另做完整采样时长启动烟测和 PE 资源核验。测试使用独立虚构数据，未对真实库批量写入。

真实 Jellyfin/Emby 实例的写入、全部服务端版本、Linux/macOS实际GUI、真实GPU模型推理及长电影任务未在本次本机验收中执行。Jellyfin 普通 GET→POST 接口没有条件更新，最新读取与提交间其它客户端的极短写入竞争不能由客户端完全消除；本次已通过最新合并、checkpoint 和差异恢复降低影响。应用恢复不覆盖恢复期间用户新建/新改文件；复杂轨道/HDR等无法保证保真的场景会明确拒绝而保留原件。以上是已实现功能的边界，不能表述成“所有外部环境绝无问题”。
