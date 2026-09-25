# 财务系统 · 交接说明（HANDOFF）

> 高迥（杭州）贸易有限责任公司的自托管记账软件。Python + SQLite + tkinter，**无 Docker、无 Web 服务、数据不出本机**。
> 最后更新 **2026-09-26 04:10** · WorkBuddy · 分支 `master` · HEAD `2c7e281` · **无 origin remote（纯本机仓库，不需 push）**
> 上一版 880 行 → 本轮蒸馏 ≤250 行。历史执行记录（P1~P10、残留清理、首次接手核验）移 `docs/handoff-history/`，见文末指针。

---

## 0. 当前快照

| 项 | 实测值（2026-09-26 04:10 活体查证） |
|---|---|
| 测试基线 | **pytest 267 passed** / 0 failed |
| 账套科目 | `account` **46** |
| 凭证 | **12**（`posted` 11 + `pending_review` 1） |
| 发票台账 | **63 张 / ¥13,745.20** |
| 报销单 | `expense` 4（`2c7e281` 真机从飞书拉取写入） |
| 往来档案 | `partner` 2 ｜ `arap_item` 0 |
| 记账期间 | `2026-07` |
| 借贷平衡 | **0 张失衡** |
| schema `user_version` | **5** ⚠️ 见下（列已补齐、版本号待升） |
| HEAD | `2c7e281`（2026-09-26 04:02） |

**🔴 schema 待迁移（04:10 复测刷新）**：`25b5000`/`2c7e281` 的依赖都是 `SCHEMA_VERSION = 6` 的 `_migration_6`（给 `expense` 补 `instance_code` 列）。但生产库 `PRAGMA user_version` 实测仍是 **5** —— 而 `instance_code` 列**已经存在**、`expense` 已有 4 行真机数据（说明此前被手工/迁移补过列，写库早于版本号升级）。下次程序启动时 `current(5) < SCHEMA_VERSION(6)` 会触发迁移，`_add_column` 幂等、已补列的老库无副作用，`user_version` 届时才升到 6。**这是预期的历史欠账，不是故障。**

**时间线（近 3 次提交）**：

| 提交 | 日期 | 内容 |
|---|---|---|
| `2c7e281` | 09-26 04:02 | feat(报销): 员工飞书自助提单 + 系统反向拉取（新增 `modules/feishu_pull.py`，删 GUI「提交飞书审批」按钮），pytest 252→267 |
| `25b5000` | 09-26 03:48 | 修飞书审批同步永远扫不到新单（状态字面量错配 + `instance_code` 列缺失），pytest 244→252 |
| `0b9c0f9` | 09-26 03:31 | 新增 `AGENTS.md` 入口（START HERE / 安全边界 / 权责分层 / 收工双写） |
| `ed24a23` | 09-11 13:51 | 阻止「保存集成设置」打坏正在工作的飞书配置 |

---

## 1. 这是什么

高迥（杭州）贸易有限责任公司 的自托管记账软件：复式记账内核 + 账套/凭证 + 三大报表 + 往来（应收应付）+ 费用报销 + 票据（发票 OCR）接入 + 飞书审批 + 邮件自动收票，打包成单机 Windows exe（双击即用）。

使用方是**生产账套，不是玩具** —— 破坏性操作见 `AGENTS.md` 安全边界。

---

## 2. 东西都在哪（三个地址，别混淆）

| 用途 | 路径 | 说明 |
|---|---|---|
| ① **源代码**（唯一一份） | `C:\Users\li1\fin-system` | 改代码只动这里 |
| ② **打包好的程序** | `C:\Users\li1\fin-system\release\财务系统\财务系统.exe` | 双击运行的真实程序 |
| ③ **真实账套数据**（会变） | `C:\Users\li1\AppData\Local\FinanceSystem\` | 记账/发票/配置/日志都在这 |
| 桌面入口 | `C:\Users\li1\Desktop\财务系统.lnk` | 指向 ② |

**关键点：源码和数据是分开的。**
- 改代码 → 只动 `①`
- 备份账套 → **只拷一个文件**：`③ ...\FinanceSystem\db\finance.db`
- 重装程序 → 覆盖 `②`，**不影响** `③`

> 全盘只有这一份 `fin-system` 源码副本。

---

## 3. 源码目录骨架（`C:\Users\li1\fin-system`）

```
app_paths.py        # 所有路径的唯一来源（源码在树里 / 可变数据在 AppData）
security.py         # DPAPI 加密（Windows 凭据保护）
logging_setup.py    # 统一日志
run_finance.py      # ★ 启动入口（把项目根加进 sys.path —— 必须用它启动）
启动财务系统.bat     # 双击启动（自动寻找 Python）
build.ps1           # 一键打包（6 步 + manifest.sha256）
财务系统.spec        # PyInstaller onedir 配置
gui/
  main.py           # 主窗口 + 全部页面（导航 11 页）
  ui_theme.py       # 浅色商务风主题（clam）
  page_mail.py / page_inventory.py / page_closing.py / simhei.ttf
modules/
  ledger.py         # 复式记账内核（凭证头+多行分录，借贷校验）
  reports.py        # 资产负债表/利润表/现金流量表/科目余额表/序时簿
  arap.py           # 往来：客户供应商档案 + 应收应付台账
  expense.py        # 报销：员工 / 报销单（挂发票）/ 审批自动记账
  invoice_gui.py    # 票据：发票确认 → 生成凭证
  ocr_invoice.py    # OCR 调用封装（引擎在外部 invoice-agent，见 §4）
  inventory.py      # 进销存：商品 / 出入库单 / 库存余额
  closing.py        # 期末处理：结转损益 / 年结
  integration.py    # 数据问题（data_issue）与集成任务（integration_job）
  feishu.py         # 飞书审批 legacy「推」路径（GUI 入口已在 2c7e281 删除）
  feishu_pull.py    # ★ 现行主路径「拉」：列实例→取详情→解析 form→建本地单（幂等，instance_code 唯一键）
  mail_service.py   # IMAP 收件 / SMTP 发信封装
  mail_sync.py      # 后台轮询线程（去重 + 水位线）
  mail_processor.py # 附件 → OCR / 数电票 XML → 入库；失败归档可重试
  einvoice.py       # ★ 附件类型单一事实来源 + 数电票 XML/OFD 解析（不走 OCR）
  export_service.py # CSV/Excel/PDF 导出
db/__init__.py      # schema（CREATE TABLE）+ 《小企业会计准则》科目表预置（在 .py 里，无 .sql）
tests/              # pytest（当前 267 用例）
tools/              # acceptance_check.py / backfill_* / fix_invoice_amounts / verify_release_packaging
docs/               # 专题文档 + handoff-history/
release/ build/ dist/ work/   # 构建产物/中间件（可再生，已 gitignore）
icon.ico
```

---

## 4. 运行数据目录 + 外部 OCR（交接必知）

```
%LOCALAPPDATA%\FinanceSystem\
  db\finance.db       ← ★ 账套本体（SQLite 单文件，备份就靠它）
  config\settings.json    # 邮箱/公司等业务配置（不含明文密码）
  config\feishu.json     # 飞书应用 app_id / approval_code
  config\credentials.json # DPAPI 加密的密钥仓库
  invoices\               # 收到的附件
  invoices\failed\        # OCR 失败待重试（GUI 有重试入口）
  reports\ logs\ backups\
```

> 项目里 `fin-system\config\` 是**历史遗留**，程序**不读它**；真正生效的配置全在 AppData。

**⚠️ 第 4 个路径：OCR 引擎在外部项目** `C:\Users\li1\invoice-agent\` ——
`settings.json` 里硬编码了绝对路径：

```json
"invoice_agent_command": ["C:/tmp/ocr_venv2/Scripts/python.exe",
                          "C:/Users/li1/invoice-agent/ocr_invoice.py"],
"invoice_summary_path": "C:/Users/li1/invoice-agent/results/summary.json"
```

**移动/删除 `invoice-agent` 或 `C:\tmp\ocr_venv2` → 邮件收票链路直接失效，且不报错**，只会一直把附件丢进 `invoices\failed\`。打包 exe 时 OCR 引擎不被包含，是运行时外部调用。

---

## 5. 日常使用 / 重打包 / 验收 / 环境

- **日常使用**：双击桌面 `财务系统` 快捷方式（= exe 版）
- 开发调试：双击 `启动财务系统.bat`，或 `"C:/tmp/ocr_venv2/Scripts/python.exe" run_finance.py`
- ⚠️ **不要**直接跑 `python gui/main.py`（`ModuleNotFoundError: No module named 'db'`）
- 关窗口 = 最小化到系统托盘；真退出用托盘菜单「退出」
- **开机自启**：设置页勾选「开机自动启动（后台运行）」（写 **HKCU 的 Run 键**）

**重打包**（改完代码必须做，否则 `release` 是旧版）

```powershell
cd C:\Users\li1\fin-system
Stop-Process -Name 财务系统 -Force          # 否则原子替换被文件句柄锁住
# PyPI 直连抖动时：$env:PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
powershell -ExecutionPolicy Bypass -File build.ps1 -PythonPath "C:\tmp\ocr_venv2\Scripts\python.exe"
```

⚠️ **新增三方依赖要同时改 3 处**，否则 exe 静默缺库：① `pyproject.toml` 的 `dependencies`；② `build.ps1` 的 pip install 列表；③ `财务系统.spec` 的 `hiddenimports`。

**验收**（改完务必跑）

```powershell
"C:/tmp/ocr_venv2/Scripts/python.exe" -m pytest tests/ -q         # 当前 267 passed
"C:/tmp/ocr_venv2/Scripts/python.exe" tools/acceptance_check.py   # 逐页 + 新模块冒烟 17 OK
```

打包后自检：`财务系统.exe --self-check report=<路径> stderr=<路径>`（**不带参数是空输出 + exit 0，属正常**）。

**环境**：Windows 11 + Python **3.11.15**，venv 位于 `C:\tmp\ocr_venv2`（含 paddleocr / paddlepaddle / pymupdf / tkinter / Pillow / openpyxl / fpdf2 / imap-tools / pystray）。
运行时依赖 `imap_tools, pystray, openpyxl, fpdf2`；构建依赖（可选组 `build`）`pyinstaller==6.21.0`。
构建工具链：本机无 `py` 启动器，3.11 宿主在 `C:\Users\li1\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe`，须用 `-PythonPath` 显式传入（`build.ps1` 找不到 `py -3.11` 会直接抛错）。

---

## 6. 活跃待办 + 明确不做

- [ ] 🟡 **P10 方案 A / D —— 等哥哥人工，明确「先别干」**（2026-09-11 哥哥原话）
  - **哥哥要做的第一步**：登录电子税务局 →「税务数字账户 → 全量发票查询 → 取得发票」→ 导出 **Excel**（2023-01 ~ 2026-09）丢进 `work/`。**拿到之前不要写 A/D 代码。**
  - 拿到后我写**只读**对账工具，出「漏票 / 多票 / 金额不符 / 重复」报告（方案 D）。
  - 🔴 **边界**：系统/agent **无法程序化接税局**（无 API）。合法直连只有「乐企」，门槛（营收≥5000 万 / 开票量 / 信用 A/B 等）**高迥一条都不满足**。所以 A = 哥哥人工导出，系统只解析。爬税局/模拟登录一律不做。
  - 方案 C（PaddleOCR→RapidOCR，减依赖）可做不急 —— 109 个附件全有文本层，PDF 兜底 OCR 从未触发，继续调引擎边际收益 ≈ 0。
  - 评估文档：`docs/invoice-ocr-options-2026-09-11.md`
- [ ] 🟡 **飞书配一个独立审批人 open_id**（治理更干净，需哥哥提供另一个飞书账号；当前 `initiator == approver` + `allow_self_approval=True`）
- ℹ️ **`release\财务系统.exe` 需重打包**：`2c7e281` 改了源码（含 GUI 按钮增删），exe 还是旧的。双击运行的用户看到的是旧行为。
- ℹ️ 飞书配置分散三处且都在生效路径（`feishu.json` → 数据根 `feishu.json` → `settings.json.feishu` **最后 merge 覆盖** = 实际生效值）。去重收益低风险高，**暂不动**。
- ❌ **报税 / 税务申报：明确不做。** ❌ **多账套切换：明确不做**（单公司账套）。

---

## 7. 踩过的坑速查（完整版见技能 `sme-finance-desktop`）

1. **启动必须走 `run_finance.py`**（否则 `import db` 失败）。
2. **DPAPI secret name 不能含 `@`**（`security.py` 只允许 `._:-`）：`a@b.com` → `a_at_b_com`。
3. **ttk 主题必须用 `clam`**：`vista` 忽略背景映射 → 选中页签白底白字看不见。
4. **表格斑马纹会盖掉选中高亮**：需专用 `sel` tag（`_ZebraTree` 已处理）。
5. **改代码后必须重打包**，否则 `release` 是旧版。
6. **构建前先杀 `财务系统.exe`**（MSYS 下别用 `taskkill //F`，用 PowerShell `Stop-Process`）。
7. **本机 GUI 截图不可靠**（DPI 缩放 + 窗口遮挡）→ 用程序化断言（读 tag / style 值）验证。
8. **`summarize()` 让「金额」兼职两种含义**（`价税合计 or 金额合计`）+ summary 写 `税额` 而下游找 `税额合计` → **进项税静默归零**。教训：一个字段只承载一种含义；跨模块字段名必须对齐；金额一律「三件套 + 自校验」。

---

## 8. 快捷索引

| 我要… | 去哪 |
|---|---|
| 启动程序 | `启动财务系统.bat` ／ 桌面 `财务系统` |
| 找账套 / 备份 | `%LOCALAPPDATA%\FinanceSystem\db\finance.db` ／ `backups\` |
| 改邮箱 / 公司配置 | `%LOCALAPPDATA%\FinanceSystem\config\settings.json` |
| 看日志 | `%LOCALAPPDATA%\FinanceSystem\logs\` |
| 重试失败发票 | 程序「票据」页重试入口（源文件在 `invoices\failed\`） |
| 重新打包 | `build.ps1 -PythonPath "C:\tmp\ocr_venv2\Scripts\python.exe"` |
| 验证 exe 含某改动 | `tools/verify_release_packaging.py`（**别用 grep**，PYZ 是压缩的） |
| 补发票关联 / 公司抬头 | `tools/backfill_business_data.py`（默认只读） |
| 修正发票金额口径 | `tools/fix_invoice_amounts.py`（默认只读） |
| 补收历史发票 | `tools/backfill_history_invoices.py`（默认只读；**绝不改 `last_uid`**） |
| 体检 OCR 质量 | `tools/audit_history_invoice_ocr.py`（只读，出 JSON 到 `logs/`） |
| 数电票 XML/OFD 解析 | `modules/einvoice.py`（白名单单一来源；回归 `tests/test_einvoice_xml.py`） |
| OCR 回归自检 | `C:/tmp/ocr_venv2/Scripts/python.exe C:/Users/li1/invoice-agent/ocr_invoice.py --self-check "C:\Users\li1\fin-system\work\ocr_samples" --company-tax-no 91330105MACRGYRN32` ⚠️ **目录参数必须 Windows 路径**，填 `/c/...` 会静默假阴性 |
| 飞书配置怎么读 / 写 | 读 `effective_config()`；写 `build_settings_section(edited, allow_self_approval)` —— **别直接改 `settings.json`**（它不是飞书配置全集） |
| 残留隔离区 / 还原 | `%LOCALAPPDATA%\FinanceSystem\backups\cleanup-20260911-0230\`（含 manifest + SHA256） |
| 跑测试 / 冒烟 | `tests/` + `tools/acceptance_check.py` |
| 了解全部设计 / 坑 | 技能 `sme-finance-desktop` |

---

## 9. 最近交接事件（最新在最上）

| 日期 | 事件 |
|---|---|
| 2026-09-26 04:10 | **提交蒸馏成果**：HANDOFF 880→238 行归档落库；快照值按 `2c7e281` 刷新（pytest 267、expense 4 行）；补记 `2c7e281` 漏写的交接 |
| 2026-09-26 03:56 | **蒸馏 HANDOFF** 880 → 242 行；P1~P10 执行记录 + 首次接手核验移 `docs/handoff-history/`；补记 `25b5000` 漏写的交接；新增「schema 待迁移」快照项 |
| 2026-09-26 03:48 | `25b5000` 飞书审批同步修复（状态字面量错配 + `instance_code` 列缺失）—— **该提交当时漏写 HANDOFF**，本节补上 |
| 2026-09-26 03:31 | `0b9c0f9` 新增 `AGENTS.md` 入口 |
| 2026-09-11 02:34 | 残留清理 + 删除废弃草稿凭证 id 11 → 凭证 13→12 |

---

## 10. 归档指针（历史证据，不要删）

| 文件 | 内容 |
|---|---|
| `docs/handoff-history/tasks-2026-09-11.md` | 旧 §10 待办全量执行记录（P1~P10 含 10.1~10.5 子节）+ §11 残留清理。**全部已完成**，含生产库执行记录、审计 ID、备份文件名 |
| `docs/handoff-history/snapshot-2026-09-11.md` | 旧 §9「当前状态」原文。保留**打包验证方法（别用 grep，PYZ 是 zlib 压缩的）、P6 符号清单、`manifest.sha256`/exe 哈希、执照其余字段（法定代表人/注册资本/登记机关）、163 邮箱水位线**等长尾证据 |
| `docs/handoff-history/verification-2026-09-10.md` | 旧 §14 首次接手实机核验（声明值 vs 实测值）。**数字已过时**，最新活性值见 §0 |
| `docs/architecture-review-2026-08-08.md` | 架构体检基准；TOP 5 技术债**已全部关闭** |
| `docs/architecture-review-2026-09-10-followup.md` | 核验 + 增量；含「不建议做」清单（拆 main.py / 引 ORM / 补 CI）防过度工程 |
| `docs/invoice-ocr-options-2026-09-11.md` | P10 方案评估（A/B/C/D） |

> **收工纪律**：改完代码按 `AGENTS.md` 收工三步走（更新 HANDOFF → 回写 Linear → 确认 commit 落库）。
> **没有 HANDOFF 条目的改动 = 未完成。** 体积守门：`bash ~/.workbuddy/tools/check-handoff.sh HANDOFF.md`（≤250 行）；全仓体检 `bash ~/.workbuddy/tools/check-all-handoffs.sh`。
