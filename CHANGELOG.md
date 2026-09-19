# 更新日志

本文件记录**面向使用者**的变化。
每条尽量写清"改了什么 + 为什么"，而不是只列提交摘要。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [3.8.0] — 2026-09-19

两个新功能页，以及一个小窗口布局修复。

### 新增

- **✍️ 批量生成字幕** —— 批量给视频生成字幕（识别 + 翻译），可选生成后去重 / 拆长句；
  可一键挑出库里缺字幕的视频。依赖第三方 `infer.exe`（Faster-Whisper 打包版）。
- **🔓 马赛克破解** —— 批量调用 Lada，支持并发、显存门控、产出时长校验；
  可一键挑出库里判定为有码的视频，产物 `xxx.restored.mp4` 直接交给「破解视频替换」。
  依赖第三方 `lada-cli.exe`。
- 两者的参数都**按工具实际支持的选项**发（读 `--help` / `--list-*`，不写死），
  工具路径留空即自动查找。下载与前置条件见 `docs/第三方工具集成.md`。

### 修复

- **窗口较小时控件被挤压重叠**：默认窗口 1100x720 下内容区不到 900px，
  密集参数行被压到比最小尺寸还小（实测 30 个控件）。
  所有页面统一改为滚动承载，三档尺寸 × 17 页面复测为 0 个。

### 测试

414 项通过（新增 `tests/test_integrations.py` 32 条）；
另对移植的字幕后处理做了差分测试，与原实现逐条一致（64 项）。


## [3.7.0] — 2026-09-19

这一版的主题是**同时适配 Jellyfin 与 Emby**，并顺手修掉几个"点一下就崩"的存量缺陷。

### 新增

- **Emby 支持**。Jellyfin 与 Emby 的 REST API 同源，现在两者共用一套实现：
  - 服务端类型**自动识别**：读 `/System/Info/Public` 的 `ProductName` 即可区分
    （该接口不需要 API Key），也可在「设置 → 媒体库数据源 → 服务端类型」手动指定；
  - 认证头统一用 `X-Emby-Token` —— 两个服务器都认，因此标签清洗、全库刷新、
    库数据采集都不需要按类型分支；
  - **「服务器 API」数据来源**：不依赖本机库文件，NAS / Docker / 服务器在另一台
    机器上的部署同样可用（Emby 的常见部署方式）。
- **库数据来源可选**（`library_source`：`auto` / `db` / `api`）。
  `auto` 的判定顺序刻意保持老用户行为不变：**能读到库文件就用库文件**，
  读不到再走服务器 API，并在日志里说明用的是哪一个。
- **数据库路径改为自动检测**。默认值不再写死 `C://ProgramData//Jellyfin//...`
  —— 那个路径在 Linux / macOS 或非默认安装的 Windows 上是无效值。
  现在默认留空，程序会扫描 Jellyfin 与 Emby 在各平台的官方默认安装位置；
  设置页提供「自动检测」按钮，并显示**当前实际生效**的数据来源。
- **库文件架构自动识别**：Jellyfin 10.9+ 的 `jellyfin.db`（EF Core 架构）与
  Jellyfin ≤10.8 / Emby 的 `library.db`（`TypedBaseItems` 架构）都能读。
  传统架构的核心元数据在 `data` 列的 JSON 里，按候选键宽容取值，取不到就回退
  同目录 `.nfo`；日志会说明「Emby 的播放记录在独立的 `users.db`，仅凭库文件读不到」，
  而不是把读不到的数据伪装成 0。
- 新增 `--doctor` 输出里的库文件探测结果会随「自动检测」一起体现（设置页可见）。

### 修复

**四处「按钮点一下就崩」的未定义名字**（存量缺陷，静态看完全正常，只有真的点一次才会暴露）：

| 位置 | 症状 |
| --- | --- |
| `SettingsPage._browse_ffmpeg()` | 用了 `sys.platform` 但模块里没 `import sys` → 点「浏览」选 ffmpeg 即 `NameError` |
| `SettingsPage._probe_hardware()` | 用了 `QApplication.setOverrideCursor` 但没导入 → 点「实测本机能力」即崩 |
| `NFOPage._ai_check_nfo()` | 用了 `QMessageBox` 但没导入；且 `__init__` **从未保存 `cfg`** → 点「AI 检查冲突」必崩 |
| `DeletePage._on_ai_recheck_done()` | 用了 `QColor` 但没导入 → AI 复核完成后标黄行时崩 |

- **`MergePage._backup_root_text()`：3 处调用、全仓没有定义** ——
  分集合并的「确认替换原分集」流程一点就 `AttributeError`。已补回实现，
  并改为显示**解析后**的备份路径（留空 / 盘符失效都会回退，与实际落盘位置一致）。
- 清理 4 处方法内的重复导入（`AIScoreWorker` / `AIRecheckWorker`）。
- `utils/merge.py` 里循环变量 `field` 遮蔽 `dataclasses.field`。

### 变更

- Jellyfin 数据库默认路径由写死的 `C://ProgramData//...` 改为**空 + 自动检测**。
- **CI 的 lint 范围收窄到「能真的拦住 bug」的规则族**（`E4/E7/E9/F`）。
  此前配置选了九族规则，对已成型的代码库会报出 480+ 条纯风格项（"请用 pathlib"、
  "请用 `X | None`" 之类）——**lint job 在第一次运行就会红，等于没有静态检查**。
  收窄后恰好覆盖本次真实抓到的缺陷类别：未定义名字、重复定义、未使用变量、变量遮蔽。
- README 补充「媒体库数据源（Jellyfin / Emby）」章节与 Emby FAQ。

### 性能

- **封面查找的目录列举加了缓存**：同一目录里的几十上百个条目共用一份 `os.listdir` 结果。
  媒体库放在网络盘（NAS）上时每次 `listdir` 都是几十毫秒的网络往返，上千条目就是几十秒。
  缓存只在**单次采集**内有效，不会出现"新增海报后刷新不出现"的陈旧问题。
- 三个采集器（EF 库 / 传统库 / REST API）的条目归一化合并为同一个 `build_item()`，
  消除重复实现带来的"同一部作品换个读法分数就变了"的风险。

### 测试

309 → **370**（全绿）。新增两个文件：

- `tests/test_emby.py`：服务端类型识别、两种库架构的采集、REST API 采集
  （分页 / 分桶 / 演员 / 行为数据 / 排除关键词）、统一来源解析、本机库文件定位、
  **三种读法的判定一致性**、目录列举缓存；
- `tests/test_ui_wiring.py`：**真的把曾经崩掉的按钮各点一次**，
  外加两条系统性守卫 —— 模块必备名字、以及**所有 `self._私有方法()` 调用都必须有定义**
  （后者正是漏掉 `_backup_root_text` 的那类缺陷）。

---

## [3.6.0] — 2026-09-19

这一版的主题是**可移植性**与**发布准备**：把"只在我这台机器上能跑"变成
"换任何一台机器都能跑，而且可以公开"。

### 新增

- **应用图标**：多尺寸 `assets/icon.ico`（16→256，小尺寸单独绘制简化版），
  由 `tools/make_icon.py` 生成，可重复运行。
- **exe 版本资源**：右键「属性 → 详细信息」可以看到产品名/版本/版权/许可，
  由 `tools/make_version_info.py` 从 `version.py` 生成。
- **`--doctor` 环境自检**：`python main.py --doctor` 打印数据目录、实际生效的
  ffmpeg/ffprobe 路径，以及**实测可用**的编码器与硬解方式。界面「设置」页也有入口。
- **便携模式**：程序目录放一个空 `portable.txt`（或设 `JELLYFIN_TOOLKIT_PORTABLE=1`），
  配置与数据就写在程序目录下的 `AppData/`，整个文件夹可拷到 U 盘直接用。
- **自定义数据目录**：`--data-dir` 或环境变量 `JELLYFIN_TOOLKIT_DATA_DIR`。
- **虚构演示库生成器** `tools/dev/seed_demo.py`：不需要真实媒体库就能试用/截图。
- **单文件启动脚本**：`start.bat`（Windows）/ `start.sh`（macOS、Linux），
  首次运行自动建 `.venv` 并装依赖。
- 完整开源发布物料：`LICENSE`、`README.md`、`CHANGELOG.md`、`CONTRIBUTING.md`、
  `SECURITY.md`、`pyproject.toml`、`config.example.json`、`.gitignore`、`.gitattributes`、
  CI 工作流与 Issue 模板。

### 修复

- **硬件编码/硬解能力判定方式错误（影响所有非本机用户）**。
  旧实现读 `ffmpeg -encoders` / `-hwaccels` 的输出，而这两条命令列出的是
  *这份 ffmpeg 编译时带了什么*，与**本机有没有对应硬件**无关。
  任何官方 ffmpeg 构建都会同时列出 `h264_nvenc` / `h264_qsv` / `h264_amf`，
  于是没有 Intel 核显或 AMD 显卡的机器会看到"支持 QSV"，
  一执行就报 `Error creating a MFX session` / `amfrt64.dll failed to open`。
  → 改为**真跑一次**（3 帧 320x240 黑场编/解）来判定，结果带缓存
  （键含工具路径 + 大小 + mtime + 缓存版本，换 ffmpeg 自动失效）。
- **编码器参数不分厂商（QSV/AMF 直接报错）**。
  旧实现对所有硬件编码器都用 `-preset p4 -cq N`，那是 NVENC 专属写法，
  Intel QSV 会报 `Unable to parse "preset" option value "p4"`。
  → 按厂商分支：`libx264` 用 `-crf`，NVENC 用 `-preset p4 -cq`，
  QSV 用 `-global_quality`，AMF 用 `-qp_i/-qp_p`，未知编码器只给 `-c:v` 不猜参数。
- **备份根目录写死 `H:/`**。没有 H 盘的机器上 `mkdir` 直接失败，整个「确认替换」功能报废。
  → 默认值改为空；留空/盘符不存在/不可写时统一回退到数据目录下的 `backups/`。
  界面文案也从"备份到 H 盘"改为显示**实际生效**的路径。
- **配置的 ffmpeg 路径形同虚设**。界面里能填，但 `find_binary()` 只看 PATH，
  且 `_FFMPEG` 是模块级常量（import 时固化），改了配置也不生效；
  分集合并页的解析顺序还是 PATH 优先。
  → 统一为「用户配置 > 程序目录及 `bin/` > PATH > 各平台常见安装位置」，
  并支持运行时刷新（`configure_tools()`），保存设置后立即生效。
- **`merge_ffmpeg_path` 有配置项但设置页没有输入框**，非 PATH 安装的用户无从填写。
  → 补上输入框 + 浏览 + 自动检测 + 实际路径回显。
- **界面字体写死 `Microsoft YaHei`**。没有该字体的机器上 Qt 会静默回退，
  中文可能渲染成方块且没有任何报错。
  → 按平台给出候选列表，并用 `QFontDatabase` 校验字体真实存在。
- **测试会往项目目录写运行时文件**。`ToolkitConfig.save()`、`LabelStore()`、
  `DeleteJournal()` 的默认路径都指向项目目录，跑一次测试就在仓库里留下
  `config.json`、`data/*.db`。
  → `tests/conftest.py` 做会话级重定向；同时新增 `config.set_data_root()`，
  它会同步那些 `from config import DATA_DIR`（**导入期绑定**）的模块 ——
  只改 `config.DATA_DIR` 对它们无效。
- **GUI 冒烟测试从未被 CI 执行**。文件叫 `tests/gui_smoke.py`，
  但 pytest 只收集 `test_*.py` / `*_test.py`。
  而它恰恰是能挡住"打包后主窗口不显示、进程却活着"这类问题的测试。
  → 改造成 `tests/test_gui_smoke.py` 的 27 个真用例，并新增
  「侧边栏条目数 == 页面数」的断言（防止新增页面时静默导航到错误页面）。
- **数据库工具页会卡住进程退出**。`DBInspectWorker` 没有中止手段，
  大库逐个 `COUNT(*)` 很慢，关窗时线程停不下来会触发
  `QThread: Destroyed while thread is still running` 直接崩掉进程。
  → 加 `stop()` 与循环内中止检查，`shutdown()` 真正把它停干净。
- **计时型测试不可靠**。`worker.start(); sleep(0.1); stop()` 测"中断"，
  小文件往往在 stop 之前就跑完了，测的根本不是中断路径，机器一变快就退化成空测。
  → 改成注入"挂起到被 kill 为止"的假子进程，保证命中中断分支，
  并断言确实观察到了子进程。
- **配置写入非原子**。断电或被强杀会留下坏掉的 `config.json`。
  → 改为临时文件 + `os.replace` 原子落盘，并加 `fsync`。
- **配置里残留失效盘符不会自愈**。换机器后指向已不存在盘符的路径会一直报错。
  → 加载时检测并清空该字段（备份根目录），同时给出说明。
- **源码里写死了本机路径**。`启动.vbs` 里是本机 venv 的绝对路径；
  `ui_preview.py`、`e2e_check.py`、`docs/_gui_shot.py` 里是本机项目绝对路径；
  分集合并/入库/UI 文案里散落着「H 盘」和具体的媒体目录名。
  → 全部清除，脚本改为按 `__file__` 推导项目根并统一放到 `tools/dev/`。
- **测试夹具含真实媒体路径**（`test_junk_attachment.py` 里的本机下载目录与人名）。
  → 换成中性虚构路径。

### 变更（可能影响既有使用者）

- **版本号收敛到 `version.py` 单一来源**。窗口标题、侧边栏、exe 版本资源、
  构建脚本全部引用它，不再各处手写（旧版出现"界面 v3.5、文件属性 v3.5、实际 3.5"的漂移）。
- **`ffmpeg_gpu_codec` / `ffmpeg_hardware_accel` 默认值改为空（= 自动探测）**。
  旧默认是 `h264_nvenc` + `cuda`，在没有 NVIDIA 显卡的机器上必然报错。
- **`merge_backup_root` 默认值从 `H:/` 改为空**（回退到数据目录）。
  原来填了自定义备份盘的配置会保留原值；若该盘符在本机不存在，会被自动清空。
- **`exclude_path_keywords` 默认值从 `["动漫"]` 改为 `[]`**。
  这是作者个人的偏好，不该作为所有人的默认行为。需要排除就在设置页里填。
- **启动脚本改名**：`启动.vbs` → `start-silent.vbs`（ASCII 文件名，跨平台更友好），
  且不再写死 python 路径，改为按「exe → 本地 venv → py 启动器 → PATH」顺序查找。
- **数据目录规范化**。打包运行改为按平台规范写入
  `%APPDATA%` / `~/Library/Application Support` / `$XDG_CONFIG_HOME`（旧版本已经是 `%APPDATA%`，
  这里统一了 macOS/Linux 行为并补上便携模式）。
- 移除了配置加载时的"过时模型名自动回退"逻辑 —— 那是针对特定用户环境的硬编码，
  对其他用户是意外行为。
- 打包配置 `JellyfinToolkit.spec`：`upx=False`（UPX 压缩过的 exe 极易被国产安全软件误报）、
  补上图标与版本资源、排除未使用的重型依赖（QtWebEngine/QtQml/numpy/matplotlib 等）。

### 移除

- `tests/gui_smoke.py`（→ `tests/test_gui_smoke.py`）
- `ui_preview.py`、`e2e_check.py`、`docs/_gui_shot.py`、`docs/alg_bench.py`
  （→ `tools/dev/` 下并按需重写）
- 仓库中的个人数据：`config.json`、`data/`、`subtitle_state.json`、
  `喜好画像报表.csv`、`.workbuddy/`、含真实数据的 `docs/_shots/` 截图

### 测试

- 309 项 pytest 全部通过（上一版为 238 项）。
- 新增 `tests/test_portability.py`（43 项），专门锁住上面这些"换台机器就废掉"的问题，
  其中包含一条直接针对"退化成读编译期列表"的回归断言。

---

## 更早的版本

3.5 及以前的版本未在此文件中逐条记录。
