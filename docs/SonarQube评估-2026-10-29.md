# SonarQube 评估：它是什么、对本项目值不值得装

> 评估日期：2026-10-29
> 结论：**SonarQube 能给的东西本项目基本已有；而且当前环境装不起来**（Docker 依赖的 WSL 被安全策略列入黑名单）。下面写清它是什么、代价多少、以及等价替代。

---

## 一、SonarQube 是什么

一句话：**代码质量的"体检中心"**——把静态分析结果变成可量化的技术债、质量门禁和历史趋势。

| 维度 | 说明 |
| --- | --- |
| 定位 | 代码质量 + 安全的静态分析**平台**（不是单个 linter） |
| 组成 | SonarQube Server（自建）/ SonarQube Cloud（SaaS）/ SonarQube for IDE = SonarLint（编辑器插件） |
| 语言 | Community Build 支持 **21 种**，**Python 全支持**（3.0–3.14，含 Django/FastAPI/Flask/Notebook） |
| 规则量 | 官方称 7,000+ 语言规则；Python 侧覆盖 bug、代码坏味、安全热点、注入类漏洞（taint 分析要商业版） |
| 输出 | 仪表盘（Bugs / Vulnerabilities / Code Smells / Coverage / Duplication）、**技术债分钟数**、质量门禁 Pass/Fail、历史趋势 |
| 免费版 | **Community Build**：LGPL v3 免费，支持 Python；**无分支分析、无 PR 装饰**（那是 Developer 版起） |
| 资源要求 | 2 CPU / 4 GB RAM 起；内嵌 Elasticsearch，Linux 需 `vm.max_map_count>=524288` |
| 部署 | Docker（推荐）或 ZIP + Java 17；Windows 的 ZIP 官方标注"仅评估用" |
| 分析方式 | `sonar-scanner` + `sonar-project.properties`（指定源码目录、排除项、覆盖率报告路径） |

**它和 ruff 不是一类东西**：ruff 是"规则引擎"，快、能自动修；SonarQube 是"平台"，管事的是**度量与门禁**——谁在退化、技术债涨了多少、这个 PR 能不能合。

---

## 二、对本项目的判断

### 2.1 技术上能用

Python 全支持，本项目 236 文件 / 5.2 万行，规模在 Community 版舒适区。

### 2.2 但当前环境装不起来（实测）

```bash
docker --version          # 28.1.1 ✅ 已安装
docker info               # ❌ error during connect: ... dockerDesktopLinuxEngine ... 不存在
wsl --status              # ❌ PROGRAM BLOCKED BY SECURITY POLICY（wsl.exe 在黑名单）
java -version             # 21.0.10 ✅
```

Docker Desktop 未运行，而它依赖 WSL2——`wsl.exe` 被 WorkBuddy 安全策略列入程序黑名单，**无法从命令侧绕过**。

**三条可行路径**（都需要你动手，代价已标）：

| 路径 | 操作 | 代价 |
| --- | --- | --- |
| A. Docker（最简） | 解除 WSL 黑名单（安全中心 → 命令安全 → 程序黑名单）→ 启动 Docker Desktop → `docker run -d -p 9000:9000 sonarqube:community` | 需要放行 WSL；容器常驻约 1–2 GB 内存 |
| B. ZIP 评估 | 下 SonarQube 2026.1 ZIP（~600 MB），Java 17 跑（你是 21，可能需要降级或用 Docker） | Windows 官方"仅评估"，数据存 H2，不适合长期用 |
| C. SonarQube Cloud | 不部署，代码推到公开仓库后在云端扫 | 免费版**要求公开仓库**——这个项目含私密内容，**不建议** |

### 2.3 收益要打折

单人在本地开发、不接 CI/CD 的用法下，SonarQube 最值钱的四件事里三件用不上：

| 它的核心价值 | 你的情况 |
| --- | --- |
| PR 装饰 / 质量门禁拦截 | ❌ Community 版不支持，且你没有 PR 流程 |
| 分支分析 | ❌ Community 版不支持 |
| 团队统一规则 | ❌ 单人 |
| 度量与趋势（技术债、重复率、覆盖率） | ✅ **这个有用** |

也就是说：花 1–2 GB 内存常驻一个服务 + 一套 PostgreSQL，换来的是**一个仪表盘**。

---

## 三、它想给你的三件事，你现在用什么

| SonarQube 的产出 | 你现在有的 | 状态 |
| --- | --- | --- |
| Bugs / 代码坏味 | **ruff**（配置已收敛到 `E4/E7/E9/F`——恰好是真会崩的那些：未定义名字 F821、重复定义 F811、未使用变量 F841） | ✅ 已接入 CI/打包 |
| 复杂度 / 上帝类 / 长函数 | **Desloppify** + `tools/dev/code_metrics.py` | ✅ 已跑（见 `docs/代码健康度评估-2026-10-29.md`） |
| 循环依赖 / 孤儿模块 | Desloppify `cycles`（0 个）/ `orphaned`（10 个） | ✅ 已跑 |
| 架构分层越界 | **import-linter**（3 条契约，0 违规） | ✅ 已接入 |
| 重复率 | Desloppify 重复维度 **97.9%** | ✅ 已跑 |
| **测试覆盖率** | `pytest --cov`（本次补测，见下） | ⚠️ 之前没量，现已补 |
| 技术债趋势 | 无 | ❌ 只有 SonarQube 类平台能给 |

**为什么不全开 ruff 规则**（这也是"为什么不需要 SonarQube 全套规则"的实证）：

```bash
ruff check . --select "B,C4,SIM,S,PIE,RET,ARG,PL,RUF,UP" --statistics
# Found 15847 errors.
```

1.5 万条里绝大多数是 `PTH`（请用 pathlib）、`UP045`（请用 X | None）这类纯风格项。
`pyproject.toml` 里对此有明确记录：全开等于**没有静态检查**（第一次跑就红，然后大家学会忽略）。
SonarQube 的 Python 规则集同样会产出大量这类"坏味"，取舍逻辑是一样的。

---

## 四、覆盖率的实测结果（本次补测）

装了 `pytest-cov`，跑完整测试套件（1,700+ 用例，9 分钟）：

| 模块 | 代码行 | 覆盖率 |
| --- | --- | --- |
| `censor/`（**核心业务**） | 4,883 | **87.0%** |
| `utils/` | 8,406 | 82.3% |
| `automation/` | 453 | 73.3% |
| `ui/`（界面） | 14,011 | 69.9% |
| `workers/` | 1,493 | **58.4%** |
| **合计** | 29,246 | **75.8%** |

判断：

- **核心业务 87% 是很健康的数字**（多数项目 60–70% 就算合格）；
- `ui/` 69.9% 偏低但合理——PySide6 交互靠 `qtbot` 驱动，成本高、收益低；
- `workers/` 58.4% 是**最该补**的一块（后台任务出错最难查）；
- 76% 总覆盖率放到 SonarQube 看板里属于"及格偏好"，不会触发质量门禁红灯。

这条数据之前**没有**（只有测试数量，没有覆盖率），现在是有了基线，可以设"不许低于 75%"。

复现：

```bash
pytest tests -q --cov=censor --cov=utils --cov=ui --cov=automation --cov=workers --cov-report=term
coverage report | tail -1     # TOTAL 29246 7085 76%
```

---

## 五、建议

**不建议现在装 SonarQube。** 理由按重要性：

1. **装不上**：WSL 被黑名单，Docker 起不来；ZIP 版在 Windows 只算评估用。
2. **用不上它最值钱的部分**（质量门禁/PR 装饰/分支分析），单人本地开发没有这些环节。
3. **它想解决的问题你已经覆盖了**：ruff（真 bug）+ Desloppify（结构/复杂度/重复）+ import-linter（分层）+ coverage（覆盖率）。这四件加起来**零常驻内存**，且都在打包流程里。

**如果你仍然想要一个"体检仪表盘"**，最省事的是路径 A（放行 WSL → Docker 跑 community 版），我可以帮你写好 `docker-compose.yml` 与 `sonar-project.properties`（含排除 `.venv/dist/data/build`、接入 coverage.xml）。

**真正值得先做的**（比装平台收益大）：

1. 把 `import-linter` 接进 `build_release.py` 的验证步骤（<2 秒，防分层退化）
2. 补覆盖率阈值（现在有了基线，可以设"不许低于当前值"）
3. 处理 Desloppify 报的 10 个孤儿文件 + `_batch_review_navigation` 死代码
4. 拆 `CensorshipPage`（这是"修 A 坏 B"的根源，见健康度评估）

---

## 附：三条工具的定位对照（避免重复投入）

| 工具 | 一句话 | 本项目状态 |
| --- | --- | --- |
| ruff | 快、能自动修的规则引擎（真 bug 优先） | ✅ 已接入 |
| Desloppify | 机械检测（死代码/重复/复杂度/孤儿）+ 主观评审框架 + 分数 | ✅ 已跑 |
| import-linter | 只做一件事：禁止 A 依赖 B | ✅ 已接入 |
| SonarQube | 平台：度量 + 门禁 + 趋势 + 团队规则 | ⛔ 未装（见上） |
| dependency-cruiser | JS/TS 的依赖图与规则校验 | ⛔ **不适用**（只认 JS/TS） |
