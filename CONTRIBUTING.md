# 参与贡献

感谢愿意帮忙。这个项目是个人开发的桌面工具，没什么流程上的官僚主义，
但有几条线必须守住 —— 它们都和**用户的数据安全**直接相关。

---

## 三条硬规则

### 1. 个人数据永不入库

这是最重要的规则。这个工具会读取用户的媒体库路径、观看记录、打标偏好，
这些数据一旦进了 git，**在历史里就再也删不干净了**。

提交前请自查（比读 `.gitignore` 可靠得多）：

```bash
git init -q && git add -A -n | grep -E "config\.json|data/|\.venv|dist/|demo/|_gui.log" && echo "有泄露！"
```

以下内容**一律不得提交**：

- `config.json`（含真实库路径、服务器地址、API Key）
- `data/` 下的任何东西（`smart.db`、`trash_journal.db`、`*_journal.json`、`jellyfin_clean_backup_*.json`）
- `subtitle_state.json`、`*报表*.csv`
- `.workbuddy/`
- 任何**硬编码的本机路径**（`C:\Users\xxx`、`D:\某目录`、`H:/`）或真实人名
- 含真实数据的截图

代码里的路径示例请用中性写法：`C:\Media\Library\ArtistA\MIXX-001.mp4`、
`C:/ProgramData/Jellyfin/Server/data/jellyfin.db`。

CI 里有独立的隐私守卫 job 会做二次拦截，绕过它没有意义。

### 2. 破坏性操作必须先备份、可回滚、有日志

任何会移动/删除/覆盖用户文件的改动，都必须满足：

- 先产出新文件或先备份原件，**确认成功之后**才替换
- 失败时原文件**不受影响**（部分失败要回滚已做的部分）
- 有可查询的记录（日志/数据库），能按单个文件恢复

参考实现：`utils/merge_archive.py`（先产出、再原子替换、失败回滚、JSON 日志可单条恢复）。

### 4. 读库只有一条路：`utils.library_source`

需要"拿到全库条目"的代码**不要**自己去找 `jellyfin.db`，一律走
`utils.library_source.collect(cfg, ...)`。它负责决定数据来源（本地库文件 / REST API）、
按架构分派读取器、统一归一化。

理由：这个项目同时支持 Jellyfin 与 Emby，库文件有两种不兼容的架构
（Jellyfin 10.9+ 的 EF 架构 `jellyfin.db`，以及 Emby / Jellyfin ≤10.8 的
`TypedBaseItems` 架构 `library.db`）。如果每个页面各写一套 SQL，
必然出现"同一个库在不同页面统计数字不一样"。

同理，"原始字段 → `Item`"的归一化（番号提取、分桶、`.nfo` 兜底）只允许写在
`utils.library.build_item()` 里。已有测试
`test_three_sources_agree_on_classification` 锁住这条：同一部作品无论从哪种来源读进来，
`num / bucket / tags / studios` 必须完全一致。

### 5. 静态检查的范围是刻意收窄的

CI 跑 `ruff check .`，规则族只开 `E4 / E7 / E9 / F`（见 `pyproject.toml`）。
这不是偷懒：全开会报出 480+ 条纯风格项，导致 lint job 从第一次运行就是红的
——**红着的检查等于没有检查**。现在这几族恰好覆盖真实抓到过的缺陷类型
（未定义名字 `F821`、重复定义 `F811`、未使用变量 `F841`、变量遮蔽 `F402`）。

如果你改了 `select`，请先确认 `ruff check .` 全绿再加规则，别一次开一大片。

---

### 3. 跨机红线：默认值里不许出现本机专属信息

默认配置必须能在**任意一台干净机器**上直接跑通。

- ❌ `"merge_backup_root": "H:/"` —— 没有 H 盘的机器上直接失败
- ❌ `"ffmpeg_gpu_codec": "h264_nvenc"` —— 没有 N 卡的机器上必然报错
- ❌ 界面文案写死"备份到 H 盘" —— 应该显示**实际解析出来的路径**
- ❌ 字体写死 `Microsoft YaHei` —— 没有该字体时 Qt 静默回退，中文变方块

正确做法：留空 + 自动探测/回退，并在界面上显示实际生效的值。

---

## 开发环境

```bash
python -m venv .venv
# Windows
.venv\Scripts\pip install -r requirements-dev.txt
# macOS / Linux
.venv/bin/pip install -r requirements-dev.txt

pytest -q          # 全部测试，离屏运行，不会弹窗
ruff check .       # 静态检查
```

需要 FFmpeg 时，装到 PATH 里即可；不装的话相关测试会自动跳过
（CI 里会装真 ffmpeg，所以别指望本地跳过就等于没问题）。

## 常用脚本

| 命令 | 作用 |
| --- | --- |
| `python tools/dev/seed_demo.py` | 生成虚构演示库到 `./demo/` |
| `python main.py --data-dir ./demo` | 用演示库启动界面 |
| `python main.py --doctor` | 打印环境自检结果 |
| `python tools/dev/make_screenshots.py` | 重新生成 README 截图（输出 `docs/images/`） |
| `python tools/make_icon.py` | 重新生成图标 |
| `python tools/make_version_info.py` | 重新生成 exe 版本资源 |
| `python scripts/build_release.py` | 一键打包（含测试） |
| `python tools/dev/smoke_exe.py` | 验证打包产物真能启动 |

---

## 代码约定

### 分层

```
ui/         PySide6 界面。允许 import utils/workers，不写业务逻辑
workers/    QThread 后台任务。包装纯函数 + 发信号，不 import ui
utils/      纯逻辑层。与 Qt 完全解耦，方便单测
automation/ 无界面编排。复用 utils/workers，不 import ui
```

`utils/` 里不要 `import PySide6` —— 一旦引入，整个纯逻辑层就没法脱离 GUI 测试了
（`workers/ffmpeg.py` 里那个 `QThread` 是例外，它本来就是 worker）。

### 一些具体要求

- **颜色一律走 `ui/theme.py` 的 token**，不要在页面里硬编码色值。切换主题靠重新应用 QSS。
- **不要写模块级的路径/工具常量**。`DATA_DIR = ...`、`_FFMPEG = find_binary(...)`
  这类在 import 时固化的值，会导致运行时改了配置也不生效，测试里也没法重定向。
  用函数懒解析（见 `utils/tools.resolve_tool()`）。
- **新增页面**要同步 `ui/main_window.py` 的 `NAV_ITEMS` 与 `tests/test_gui_smoke.py` 的
  `PAGE_SPECS` —— 测试里有断言守着，漏了会红。
- **手动脚本放 `tools/dev/`**，不要用 `gui_smoke.py` 这类"看着像测试"的名字放在 `tests/` 下 ——
  pytest 只收 `test_*.py` / `*_test.py`，那种文件 CI 永远不会跑。
- 注释写**为什么**，不写**是什么**。尤其是"这里为什么不能那样写"的坑，值得留一行。

### 关于硬件能力的判定（踩过的坑，别改回去）

不要用 `ffmpeg -encoders` / `-hwaccels` 判断本机支不支持某个硬件编码器。
那两条命令列出的是**这份 ffmpeg 编译时带了什么**，与有没有对应显卡无关 ——
官方构建会同时列出 `h264_nvenc` / `h264_qsv` / `h264_amf`，
于是没有 Intel 核显的机器会显示"支持 QSV"，一执行就报 MFX 错误。

必须**真跑一次**才行（见 `utils/tools.probe_encoder()`）。
`tests/test_portability.py::test_compiled_list_is_not_used_as_ground_truth`
就是防止这个判定被改回读列表的。

---

## 提交

- 提交信息写清动机。**"修好了"不如"修了什么、为什么之前会坏"**。
- 一个提交做一件事。顺手改的无关代码拆开。
- 改到用户可见行为（尤其破坏性操作、默认值）请同步更新 `CHANGELOG.md`。
- 破坏性操作的改动，请说明回滚路径。

## 提 Issue

- **Bug**：请附 `python main.py --doctor` 的输出（或界面「🩺 环境自检」的截图），
  以及复现步骤。没有环境信息很难判断。
- **安全/隐私问题**：不要开公开 issue，见 [SECURITY.md](SECURITY.md)。
