# 财务系统 架构评审续篇（增量）

| 项 | 值 |
|---|---|
| 前序评审 | `docs/architecture-review-2026-08-08.md`（高见远，基准） |
| 本次评审 | 2026-09-10 |
| 评审人 | WorkBuddy |
| 评审方式 | 只读静态分析 + 只读打开生产库（`mode=ro`）+ 实跑 pytest 与验收脚本 |
| 代码规模 | 12,336 行（22 源文件 + tests + tools） |
| 本次未修改任何业务代码 | 仅新增本报告 |

> 定位：**不是重新评审，而是验证前序结论 + 记录新增量。**
> 前序评审的 §5「不要做的事」已明确否决拆 `gui/main.py`、拆 `db/__init__.py`、引入 ORM、补 CI、重写 `build.ps1`、迁移 GUI 框架 —— **本次完全认同，不重复提出**（单用户单机、日均个位数凭证，那些动作纯亏）。

---

## 0. 一句话结论

**前序评审列的 TOP 5 技术债，现已全部关闭；代码库处于健康状态。**
剩余改进空间只有 4 项，且都是小成本（其中只有 1 项值得优先做：**备份保留窗口偏短 + 备份失败静默**）。

---

## 1. 前序技术债的关闭验证（实测）

| 前序编号 | 原问题 | 当前实测 | 结论 |
|---|---|---|---|
| **TD-1** | `C_PANEL` NameError，6 大功能页全挂 | `gui/ui_theme.py:47` 定义 `C_PANEL = C_CARD`；`gui/main.py:41` 已加入 import 列表 | ✅ **已修** |
| **TD-2** | 项目根无 `.git`，无法回滚 | `master` 分支，工作区干净，HEAD `ab44c39`（含本次核验提交） | ✅ **已修** |
| **TD-3a** | spec 入口是 `gui/main.py`，`--self-check` 会弹窗致构建挂起 | `财务系统.spec:5` → `['run_finance.py']`（带 argparse 的入口） | ✅ **已修** |
| **TD-3b** | 运行时依赖从未声明/安装，exe 静默缺库 | `build.ps1:224` 已装 `imap_tools pystray openpyxl fpdf2`；`pyproject.toml` 已声明 | ✅ **已修** |
| **TD-3c** | `self_check` 对 mail 模块是盲的 | `run_finance.py:53-65` 的 `required_modules` 已含 `mail_service`/`mail_sync`/`mail_processor`/`gui.page_mail` | ✅ **已修** |
| **TD-4** | 数据库无自动备份 | `gui/main.py:355 _run_startup_backup()`：每日首启备份 + 滚动保留（见下方 §2.1 的新发现） | ✅ **已修**（保留策略待调，见 §2.1） |
| **TD-5a** | `MailSyncEngine` 构造参数不匹配 | 调用点与签名已对齐 | ✅ **已修** |
| **TD-5b** | 附件静默丢弃 + 水位线照常前进 | `mail_processor.process_file` 已被接上；水位线推进规则已按「终态不阻塞、瞬时失败阻塞」重做 | ✅ **已修** |
| **TD-5c** | 明文密码将写入 `settings.json` | `MailBoxConfig.from_dict()` 恒置 `password=""`，明文不驻留；实测 `settings.json` 无 password 字段 | ✅ **已修** |
| **TD-5d** | SMTP 发信因未净化用户名必抛 `ValueError` | 收发两侧统一走 `username.replace('@','_at_').replace('.','_')` | ✅ **已修** |
| **§4.5** | 无 `conftest.py`，测试污染生产目录 | `tests/conftest.py` 已存在：import 前设 `FINANCE_SYSTEM_HOME` 到 `.test-home`，autouse fixture 断言 `db.DB_PATH != 真实库` | ✅ **已修** |

**质量门禁现状（实测）**：`pytest` **41 passed**（4.07s）；`tools/acceptance_check.py` **17 OK / 0 FAIL**（11 个页面全部构建成功）。

---

## 2. 新增发现（前序评审未覆盖）

### 2.1 🟠 备份：保留窗口偏短，且失败被静默吞掉

| 项 | 内容 |
|---|---|
| **位置** | `gui/main.py:355-391 _run_startup_backup()` |
| **现状①** | `MAX_BACKUPS = 5`，配合「每天首启备份一次」= **回溯窗口约 5 天** |
| **现状②** | 整个函数包在 `except Exception: pass` 里 —— **备份失败用户完全无感** |
| **为什么是问题** | (a) 财务场景的典型故障是「月底对账时发现月初记错」，而月初的备份**已被滚掉**。(b) 更糟的是 (b)：若备份因磁盘满/文件锁/权限失败，用户以为「系统每天自动备份」，实际一份都没有 —— **这是「以为有备份」型故障，比没有备份更危险**。 |
| **工作量** | **S**（约 1 小时） |
| **建议** | ①保留策略改分层：近 7 天逐日 + 近 4 周每周一份 + 近 6 月每月一份（或简单点：`MAX_BACKUPS = 30`）。②失败不再静默：错误写 `logging`，并复用已有的 `issue_bar` 机制（`gui/main.py:341`）在工作台显示告警，让用户看见「今日备份失败」。 |

> 补充：`backup_database()` 本身的实现质量很高（`sqlite3` 原生在线热备 + 备份后 `integrity_check` 验证 + 失败自动 `unlink` 不留半成品）。**缺的只是保留策略与失败可见性，不需要重写。**

### 2.2 🟡 `.gitignore` 纵深防御缺口

| 项 | 内容 |
|---|---|
| **现状** | `.gitignore` 只忽略了 `config/feishu.json` |
| **实测缺口** | `config/credentials.json`、`config/settings.json`、`config/feishu.app_secret.blob` **均不会被忽略**（`git check-ignore` 实测） |
| **为什么是问题** | `credentials.json` 是**全部密钥的仓库**（邮箱授权码 + 飞书 secret 的加密 blob）。当前这些文件在 AppData（安全），但项目根**已存在 `config/` 目录且含 `feishu.json`** —— 说明「往项目根放配置」这个动作真实发生过。一旦有人调试时把 `credentials.json` 拷进来再 `git add .`，密钥仓库就进版本历史了。 |
| **工作量** | **S**（改动 1 行） |
| **建议** | 把 `config/feishu.json` 一行替换为 `config/*.json` + `config/*.blob`，再用 `!config/feishu.example.json` 放行示例文件（该文件已被 `test_packaging.py` 断言只含 `app_secret_ref` 不含明文）。 |

> 说明：这是**纵深防御**，不是已发生的泄露。git 历史已核查：从未提交过任何真实凭据（仅 `feishu.example.json` 示例）。

### 2.3 🟡 `configure_dark_style` 命名与实现相反

| 项 | 内容 |
|---|---|
| **位置** | `gui/ui_theme.py:151` `configure_dark_style = configure_light_style`；被 `gui/main.py:283`、`:394` 调用 |
| **现状** | 函数名说「深色」，实现是**浅色**（配色 token 全为 `C_BG=#f8f9fa` / `C_CARD=#ffffff` / `C_ACCENT=#2563eb`），符合用户已确认的浅色商务风偏好 |
| **为什么是问题** | 纯命名误导。后续维护者（或 AI 助手）看到 `configure_dark_style(root)` 会以为在配深色主题，可能因此「修复」出一个深色界面 —— 而**用户已明确否决深色风**（前序技能记录：深色版曾合并跑通测试，用户看后要求整体改回浅色）。 |
| **工作量** | **S**（改名 + 2 处调用点） |
| **建议** | 重命名为 `configure_app_style` 或直接调用 `configure_light_style`，删除别名。 |

### 2.4 🟢 依赖清单三处手工同步，无机器校验

| 项 | 内容 |
|---|---|
| **现状** | 新增三方依赖必须同时改 ① `pyproject.toml` ② `build.ps1:224` 的 `pip install` 列表 ③ `财务系统.spec` 的 `hiddenimports` |
| **为什么是问题** | `build.ps1` **不读** `pyproject.toml`；PyInstaller 对缺失的 hiddenimport **只发 WARNING 不报错** → 构建「成功」但 exe 点开该功能才崩。当前靠人记（已写入技能坑列表），机器无校验。 |
| **工作量** | **S~M** |
| **建议** | 在 `build.ps1` 装完依赖后加一段断言：解析 `pyproject.toml` 的 `dependencies`，逐个 `python -c "import <mod>"` 验证；再 `grep` spec 的 `hiddenimports` 覆盖同一集合，不一致即 `throw` 中止构建。把「三处一致」变成构建期门禁而非人的记忆。 |

### 2.5 🟢 数据层遗留（业务数据，非代码缺陷）

| # | 现象 | 影响 | 建议 |
|---|---|---|---|
| a | `company` 表 1 行但 `name/tax_no/addr/note` **全为空** | 报表抬头的公司名为空 | 补录公司信息 |
| b | 6 张发票 `file_path`/`file_hash` 均为空串 | 发票记录与原始 PDF 未关联；原件只在 `invoices/failed/`（6 个 PDF） | OCR 成功的 PDF 回填 `file_path` |
| c | `partner.type` 中英混用（一条中文「客户」+ 一条英文 `supplier`） | 代码已用 `type IN (?,?)` 兼容，**非 bug** | 可不处理；若要统一，`add_partner` 已自动归一化新数据 |
| d | 3 条 `test data cleanup` 冲销凭证 + 3 个早期 `failed_invoice_*.pdf` | 测试残留，占位不占逻辑 | 可选清理（**先确认清单**） |
| e | `release/frozen-self-check.json` 是 2026-07-11 旧文件 | 陈旧，无功能影响 | 重跑 `build.ps1` 即刷新 |
| f | 飞书配置在 `settings.json` 与 `feishu.json` 里重复一份 | 双份配置有分叉风险 | 明确单一来源（`feishu.json` 为主） |
| g | `config/feishu.app_secret.blob` 孤儿文件（当前代码无任何 `.blob` 读写） | 旧版残留，易误导维护者去查「blob 链路」 | 可删 |

---

## 3. 复核确认「无需改动」的部分（避免后人重复劳动）

以下是本次**专门复核并确认健康**的项目，前序评审的结论依然成立：

| 检查项 | 结论 |
|---|---|
| SQL 注入 | ✅ 全部 f-string/`%` 拼 SQL 的插值均为**代码内部常量**（迁移表名列表、硬编码元组 `("expense","cash_doc","invoice_doc")`、整数版本号、报表别名），**无用户输入路径**。已逐处追溯调用链确认。 |
| git 密钥泄露 | ✅ 历史从未提交过真实凭据；当前无敏感文件被跟踪。 |
| 账务内核数据完整性 | ✅ 数据库级触发器管控（审计只追加、已记账不可变、状态机强约束、借贷必平、整数分精度、幂等索引）依然完整。 |
| 分层结构 | ✅ `entry → gui → modules → db → infra` 无循环、无反向依赖。 |
| 测试隔离护栏 | ✅ `conftest.py` 已建立，越界写库即 fail。 |
| 备份实现质量 | ✅ 原生在线热备 + 完整性校验 + 失败清理（仅保留策略待调，见 §2.1）。 |
| 构建脚本 | ✅ 隔离 venv、可复现构建、密钥字节级扫描、SHA256 清单、原子发布带回滚。 |
| exe 与源码一致性 | ✅ exe（22:32）比源码最新改动（22:25）新，`release/` 是当前版本。 |

---

## 4. 建议的动作顺序

| 优先级 | 动作 | 工作量 | 理由 |
|---|---|---|---|
| **P1** | 备份保留策略改分层（或提到 30 份）+ 失败不再静默 | S | 唯一涉及**数据可恢复性**的项；「以为有备份」最危险 |
| **P2** | `.gitignore` 补 `config/*.json` + `config/*.blob`（放行 example） | S | 一行改动，挡住密钥进仓库的唯一现实路径 |
| **P3** | 重命名 `configure_dark_style` | S | 防止后人被名字误导去做用户已否决的深色风 |
| **P4** | 依赖三处一致性加构建期断言 | S~M | 把已知地雷变成机器门禁 |
| **P5** | 补 `company` 信息 / 回填发票 `file_path` | S | 业务数据完整性，非代码问题 |

> **不建议做的（延续前序 §5）**：拆 `gui/main.py`（2845 行）、拆 `db/__init__.py`、引入 ORM、补 CI/覆盖率、重写 `build.ps1`、迁移 GUI 框架。这些在当前规模下是负收益。

---

## 5. 证据清单

| 结论 | 验证方式 |
|---|---|
| TOP5 全部关闭 | 逐项 grep 源码 + `git log` + `财务系统.spec`/`run_finance.py` 实读 |
| 41 passed / 17 OK | `pytest tests/ -q`、`tools/acceptance_check.py` 实跑 |
| 备份窗口 = 5 天 | `grep -n MAX_BACKUPS gui/main.py` → 381 行；读 355-391 行确认「每天一次」 |
| 备份失败静默 | 读 `gui/main.py:389-391` → `except Exception: pass` |
| `.gitignore` 缺口 | `git check-ignore` 实测 `config/credentials.json` 等 3 个文件**不被忽略** |
| SQL 拼接无注入路径 | 逐处追溯 `db/__init__.py:168/174/609/826`、`modules/ledger.py:561` 的插值来源 |
| git 无密钥泄露 | `git log --all --diff-filter=A --name-only` 过滤 credential/secret/blob/db |
| 生产库只读体检 | `mode=ro` URI 打开，统计表行数（未写入） |

> 本次评审全程只读，未修改 `fin-system` 任何业务代码。仅新增本报告，并在 `HANDOFF.md` 第 10 节补充待办条目。
