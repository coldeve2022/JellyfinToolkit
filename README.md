<div align="center">

<img src="assets/icon.png" width="120" alt="Jellyfin Toolkit">

# Jellyfin Toolkit

**影视库整理工具** — 一个 Windows 桌面程序，把 **Jellyfin / Emby** 影视库里那些「该删哪个、怎么合、缺什么、哪儿坏了」的判断和操作，收进一个窗口。

[![tests](https://github.com/coldeve2022/JellyfinToolkit/actions/workflows/tests.yml/badge.svg)](https://github.com/coldeve2022/JellyfinToolkit/actions/workflows/tests.yml)
[![Release](https://img.shields.io/github/v/release/coldeve2022/JellyfinToolkit)](https://github.com/coldeve2022/JellyfinToolkit/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/downloads/)

</div>

---

## 这是什么

它不管播放，管的是播完之后的事：库里哪些东西该删、哪些该合、哪些缺东西、哪些坏了。这些活平时要么靠翻文件夹和数据库，要么靠手敲 ffmpeg 命令行 —— 这个程序把它们做成了有预览、有记录、能撤回的界面操作。

数据来源两条路都通：**直读服务器上的库文件**，或者**走服务器 API**（服务器装在 NAS、Docker、另一台机器上都行）。Jellyfin 与 Emby 的接口同源，两者共用同一套实现，不用装两个版本。

批量操作前建议先备份媒体库 —— 程序会把删掉的东西移进隔离区并留下记录，但文件操作终究有风险。

## 每个页面在解决什么

| 页面 | 你什么时候会用到它 | 它具体做什么 |
| --- | --- | --- |
| 🚀 新手引导 | 第一次打开，不知道从哪下手 | 6 步向导，每步可跳转到对应功能页 |
| 📊 库概览 | 想知道库里到底堆了多少东西 | 可筛选排序的分布表 + 占比条形图 + 指标卡；库条目 / 唯一视频路径 / 作品分组三个数分开算 |
| ❤️ 喜好画像 | 「该删哪些」心里没底 | 从收藏与观看行为算命中度，输出未看推荐；Top 演员 / 类型 / 标签条形图，可导 CSV，可接本地 AI 复核 |
| 🏷️ 快速打标 | 一张张点太慢 | 卡片流只推待复核的片子，喜欢 / 不喜欢 / 跳过 / 删除四键，反馈实时参与重排 |
| 🗑️ 删除暂存 | 怕点错删掉不该删的 | 先审阅目标和理由，再移进隔离区；任务中心可恢复 |
| 🔁 重复处理 | 同一个番号好几个版本 / 分辨率 | 按番号分组，留优删差；CD 分卷不会被当成重复项 |
| 🎬 视频有码检测 | 分不清哪些有码、哪些是破解版 | 拖入文件或文件夹，按关键词、文件名、标签自动分类，可按演员清理未知项 |
| 📝 字幕缺失检测 | 不知道哪些片子没字幕 | 按「同名 + 字幕扩展名」判定，不限语言标记写法，认 `Subs/` 子目录 |
| ✍️ 批量生成字幕 | 缺的太多，一个个补不现实 | 驱动 Faster-Whisper / TransWithAI，可选生成后去重、拆长句 |
| 🔄 破解视频替换 | 修复完的产物怎么换回原位 | 先配对校验时长与音轨，同名歧义不会自动选错，保留可恢复备份 |
| 🔓 马赛克破解 | 要批量跑 Lada | 并发 + 显存门控 + 产出时长校验，产物直接交给「破解视频替换」 |
| 🔧 视频修复转码 | 下载下来就是坏的 / Jellyfin 播不了 | 坏帧修复与兼容性转码放在同一页，可用显卡加速 |
| 📋 NFO 关联修复 | `.ts` 转 `.mp4` 之后信息全乱 | 修正 NFO 与关联资源对不上的问题 |
| 🗄️ 数据库工具 | 要查表结构、搬播放记录 | 跨库转移播放记录 + 检查表结构 |
| 🧩 分集合并 | 一集散成好几个文件，看一次切一次 | 树形列表：番号做父节点、CD 分集做子节点，勾选即合并，原片归档可回滚 |
| 🤖 自动化工作台 | 每期都要重复点同样的流程 | 先审阅计划，再按同一范围执行；支持步骤模板与中途取消 |
| ⚙️ 设置 | 连服务器、指路径、配外部工具 | 观看用户、服务器路径映射、评分口径、FFmpeg 与第三方工具 |
| 📌 任务中心 | 「刚才那一大批操作到底干了什么」 | 任务状态与历史、文件恢复、旧版反馈逐项迁移 |

**两处需要自备第三方程序**：「批量生成字幕」用 Faster-Whisper 打包版，「马赛克破解」用 Lada 的 `lada-cli.exe`。都不随仓库分发，在「设置 → 第三方工具」里指定位置即可，详见 [docs/第三方工具集成.md](docs/第三方工具集成.md)。

## 下载与运行

**用打包好的 exe（Windows）**

1. 到 [Releases](https://github.com/coldeve2022/JellyfinToolkit/releases) 下载 `JellyfinToolkit-vX.Y.Z-win64.zip`
2. 解压到任意目录
3. 双击 `JellyfinToolkit.exe`

**从源码运行**

```bash
git clone https://github.com/coldeve2022/JellyfinToolkit.git
cd JellyfinToolkit
start.bat          # Windows：自动建 .venv 装依赖并启动
./start.sh         # macOS / Linux
```

也可以手动来：`python -m venv .venv` → 装 `requirements.txt` → `python main.py`。Windows 上双击 `start-silent.vbs` 或 `main.pyw` 可以不带控制台窗口启动。

命令行带四个开关：

| 参数 | 作用 |
| --- | --- |
| `--doctor` | 打印环境自检结果后退出（数据目录、ffmpeg 路径、实测可用的编码器与硬解方式），不启动界面 |
| `--version` | 打印版本号 |
| `--portable` | 强制便携模式 |
| `--data-dir <路径>` | 指定数据目录，等价于环境变量 `JELLYFIN_TOOLKIT_DATA_DIR` |

## 系统要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11 为主要目标平台（发布包只提供 win64）；macOS / Linux 可源码运行，未做发布验证 |
| Python | 3.9 以上（推荐 3.11 ~ 3.13），仅源码运行时需要 |
| 依赖 | `PySide6`（6.6 ~ 6.11；6.12 有 Qt 侧回归，已锁上限） |
| 外部工具 | **FFmpeg / ffprobe** —— 只有修复、转码、分集合并用到，其余功能不装也能用 |
| 可选 | **Jellyfin 或 Emby** 服务器 + API Key（读库数据、清洗标签、触发全库刷新） |
| 可选 | 本地大模型（Ollama / LM Studio 等 OpenAI 兼容接口），用于 AI 复核 |

FFmpeg 不必装进 PATH：可以在「设置 → FFmpeg 转码设置」里指定，或把 `ffmpeg.exe` / `ffprobe.exe` 丢进程序目录的 `bin/`。装完点「🩺 环境自检」确认路径生效。

## 接上你的服务器

程序读库有两种方式，`library_source` 三选一：

| 值 | 行为 |
| --- | --- |
| `auto`（默认） | 能读到本机库文件就用库文件，读不到再走 API |
| `db` | 只读本地库文件，不依赖服务器在线 |
| `api` | 只走 REST API，库文件不在本机也能用（Emby 推荐） |

几个容易卡住的点：

- **服务端类型留 `auto`**：读 `/System/Info/Public` 的 `ProductName` 自动分辨 Jellyfin 还是 Emby，这个接口不需要 Key
- **API Key 去哪拿**：Jellyfin → 控制台 → 高级 → API 密钥；Emby → 设置 → 高级 → API 密钥。两边认证头相同（`X-Emby-Token`），填一处通用
- **必须指定观看用户**：只有一个账号时自动确认，多个账号要选一个用于画像。标签、画像、AI 缓存按「库 + 用户」隔离，不会串
- **库文件路径留空 = 自动检测**：会扫 Jellyfin 与 Emby 在各平台的默认安装位置，两种库架构（新版 EF Core 的 `jellyfin.db`、旧版与 Emby 的 `library.db`）都能识别
- **远程路径要映射**：API 能读到元数据，不代表本机能操作文件。设置里配好 remote / local 映射再处理本地文件

## 数据存在哪

| 内容 | 位置 |
| --- | --- |
| 配置 | `<数据目录>/config.json` |
| 打标记录、画像缓存 | `<数据目录>/data/smart.db` |
| 删除与替换留痕 | `<数据目录>/data/trash_journal.db` |
| 分集合并归档日志 | `<数据目录>/data/merge_archive_journal.json` |
| 合并原片备份 | `<数据目录>/backups/_merged_originals/<番号>/` |
| 标签清洗备份 | `<数据目录>/data/jellyfin_clean_backup_*.json` |
| 运行错误日志 | `<数据目录>/data/app-error.log` |

`<数据目录>` 默认是 `%APPDATA%\JellyfinToolkit`（源码运行则是项目目录）。想做成绿色版，在程序目录放一个空的 `portable.txt`，数据就跟着程序走；环境变量 `JELLYFIN_TOOLKIT_DATA_DIR` 优先级最高。

配置项的完整含义见 [`config.example.json`](config.example.json)，复制成 `config.json` 放进数据目录可做默认值预设。常用的几项：`library_source`（数据来源）、`merge_backup_root`（合并备份放哪，留空就用数据目录）、`ffmpeg_gpu_codec` / `ffmpeg_hardware_accel`（留空 = 自动实测）、`exclude_path_keywords`（哪些目录不参与分析）、`min_video_size_mb`（小于该体积的文件跳过，默认 10）。

## 常见问题

<details>
<summary><b>提示找不到 ffmpeg</b></summary>

三选一：「设置 → FFmpeg 转码设置 → 浏览」直接指定；把 `ffmpeg.exe` / `ffprobe.exe` 放进程序目录 `bin/`；或装进系统 PATH。改完点「🩺 环境自检」确认。
</details>

<details>
<summary><b>选了硬件编码器，一执行就报错（MFX session / amfrt64.dll / nvenc not found）</b></summary>

这台机器没有对应的显卡或驱动。把编码器改成「自动」，程序会真跑一次编码来确认可用性（不是读 `ffmpeg -encoders` 的编译期列表，那份列表在多数机器上都会虚报 Intel / AMD 硬编可用）；手工选了不可用的，执行时也会回退 `libx264` 并给出提示。
</details>

<details>
<summary><b>用 Emby，播放记录读出来是空的</b></summary>

Emby 把收藏与播放次数放在独立的 `users.db` 里，只给程序库文件是读不到的 —— 这种情况日志会明确说明，而不是把数据当成 0。改用「服务器 API」数据来源即可读到完整行为数据。
</details>

<details>
<summary><b>删掉或合并掉的片子还能找回来吗</b></summary>

能。删除只是移进隔离区并写一条记录，在「任务中心 → 文件恢复」里按记录还原；分集合并会先把原片挪到 `backups/_merged_originals/<番号>/`，再合并成单文件，归档日志可按单个文件回滚。目标路径已被新文件占用时会拒绝覆盖并说明冲突。
</details>

<details>
<summary><b>界面中文显示成方块</b></summary>

系统缺中文字体。Linux 上装一套即可：`sudo apt install fonts-noto-cjk`。程序会按平台探测可用字体，但系统一个中文字体都没有时无能为力。
</details>

<details>
<summary><b>AI 复核连不上</b></summary>

AI 功能是可选的，连不上不影响其他功能。默认地址 `http://localhost:11434/v1` 是 Ollama；没装 Ollama 或没拉模型时，把「启用」关掉即可。设置页有「启动本机 Ollama 并重试」按钮，只会启动本机服务，不会替你下载模型。
</details>

## 项目结构

```
jellyfin-toolkit/
├── main.py / main.pyw       # 入口（--doctor / --version / --portable / --data-dir）
├── config.py                # 配置与数据目录解析（跨平台 + 便携模式）
├── version.py               # 版本唯一来源
├── ui/                      # PySide6 界面：main_window 路由 + pages/ 18 个页面 + theme/styles/widgets
├── workers/                 # QThread 后台任务（扫描 / 分析 / 转码 / 合并 / 生成字幕…）
├── utils/                   # 与 Qt 解耦的纯逻辑层，可单测
│   ├── library*.py          #   库采集：库文件（两种架构）/ REST API / 统一数据源
│   ├── scoring*.py          #   命中度评分引擎
│   ├── merge*.py            #   分集合并与安全归档、回滚
│   ├── subtitle_clean.py    #   「有没有字幕」的唯一判定实现
│   └── tools.py             #   外部工具定位 + 硬件能力实测
├── automation/              # 无界面编排流水线（采集 → 评分 → 执行）
├── tools/                   # make_icon / make_version_info；dev/ 下是手动脚本（pytest 不收集）
├── scripts/                 # build_release / version_manage / release_flow / verify_release / patch_whisper_encoding
└── tests/                   # pytest（离屏运行，不弹窗）
```

## 开发

```bash
pip install -r requirements-dev.txt

pytest -q                              # 全部测试（离屏）
ruff check .                           # 静态检查
python tools/dev/seed_demo.py          # 生成虚构演示库，不需要真实媒体库
python tools/dev/make_screenshots.py   # 重新生成 README 截图
python scripts/build_release.py        # 打包：图标 + 版本资源 + 测试 + PyInstaller + zip + SHA256
python tools/dev/smoke_exe.py          # 验证打包产物真能启动
```

版本号规则与发版流程见 [docs/版本管理.md](docs/版本管理.md)；两条对账命令：`python scripts/version_manage.py status`（版本号一致性）、`python scripts/release_flow.py status`（发行包归档）。约定与红线见 [CONTRIBUTING.md](CONTRIBUTING.md)，改动记录见 [CHANGELOG.md](CHANGELOG.md)。

## 许可

[MIT](LICENSE)。本工具与 Jellyfin 官方无关联，为社区第三方工具。
