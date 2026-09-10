# 财务系统 · 交接说明（HANDOFF）

> 单一公司账套的本地桌面财务系统。纯 Python + SQLite + tkinter，**无 Docker、无 Web 服务、数据不出本机**。
> 最后更新：2026-09-10 · 版本 1.0.0 · 分支 `master` · 核验时 HEAD `8ae405a`（核验记录提交 `48e592a`）
> 交接核验：2026-09-10 由 WorkBuddy 实机核对（源码/数据/测试基线/快捷方式全部复现，见文末「核验记录」）

---

## 1. 这是什么

高迥（杭州）贸易有限责任公司 的自托管记账软件：复式记账内核 + 账套/凭证 + 三大报表 + 往来（应收应付）+ 费用报销 + 票据（发票 OCR）接入 + 飞书审批 + 邮件自动收票，打包成单机 Windows exe（双击即用）。

---

## 2. 东西都在哪（三个地址，别混淆）

| 用途 | 路径 | 说明 |
|---|---|---|
| ① **源代码**（唯一一份） | `C:\Users\li1\fin-system` | 改代码只动这里 |
| ② **打包好的程序** | `C:\Users\li1\fin-system\release\财务系统\财务系统.exe` | 双击运行的真实程序 |
| ③ **真实账套数据**（会变） | `C:\Users\li1\AppData\Local\FinanceSystem\` | 记账/发票/配置/日志都在这 |
| 桌面入口 | `C:\Users\li1\Desktop\财务系统.lnk` | 指向 ② |

**关键点：源码和数据是分开的。**
- 改代码 → 只动 `① fin-system`
- 备份账套 → **只拷一个文件**：`③ ...\FinanceSystem\db\finance.db`（或 `backups\` 里的副本）
- 重装程序 → 覆盖 `②`，**不影响** `③` 的账套

> 全盘只有这一份 `fin-system` 源码副本（用户要求「只留一份，多余的删掉」）。

---

## 3. 源码目录结构（`C:\Users\li1\fin-system`）

```
app_paths.py          # 所有路径的唯一来源（源码在树里 / 可变数据在 AppData）
security.py           # DPAPI 加密（Windows 凭据保护，存密钥）
logging_setup.py      # 统一日志
run_finance.py        # 启动入口（把项目根加进 sys.path —— 必须用它启动）
启动财务系统.bat       # 双击启动（自动寻找 Python）
build.ps1             # 一键打包（6 步 + manifest.sha256）
财务系统.spec          # PyInstaller onedir 配置
pyproject.toml        # 依赖声明
icon.ico
gui/
  main.py             # 主窗口 + 全部页面（导航共 11 页：工作台/账务/账簿/资金/往来/
                      #   费用票据/邮件收票/进销存/报表/期末处理/设置）
  ui_theme.py         # 浅色商务风主题（clam）
  page_mail.py        # 邮件收票页
  page_inventory.py   # 进销存页
  page_closing.py     # 期末处理页
  simhei.ttf          # PDF 中文字体（导出用）
modules/
  ledger.py           # 复式记账内核（凭证头+多行分录，借贷校验）
  reports.py          # 资产负债表/利润表/现金流量表/科目余额表/序时簿
  arap.py             # 往来：客户供应商档案 + 应收应付台账
  expense.py          # 报销：员工 / 报销单（挂发票）/ 审批自动记账
  invoice_gui.py      # 票据：发票确认 → 生成凭证
  ocr_invoice.py      # OCR 调用的封装（实际引擎在外部 invoice-agent，见第 8 节）
  inventory.py        # 进销存：商品 / 出入库单 / 库存余额
  closing.py          # 期末处理：结转损益 / 年结
  integration.py      # 数据问题（data_issue）与集成任务（integration_job）
  feishu.py           # 飞书审批（创建实例 + 轮询状态）
  mail_service.py     # IMAP 收件 / SMTP 发信封装
  mail_sync.py        # 后台轮询线程（去重 + 水位线）
  mail_processor.py   # 附件 → OCR → 入库；失败归档可重试
  export_service.py   # CSV/Excel/PDF 导出
db/__init__.py        # schema（CREATE TABLE）+ 《小企业会计准则》科目表预置（在 .py 里，无 .sql 文件）
tests/                # pytest（10 个测试文件，收集 41 个用例）
tools/acceptance_check.py   # 逐页 + 新模块冒烟验收
docs/                 # 截图等
release/ build/ dist/ work/ # 构建产物/中间件（可再生，已 gitignore）
```

---

## 4. 运行数据目录（`C:\Users\li1\AppData\Local\FinanceSystem`）

```
db\finance.db       ← ★ 账套本体（SQLite 单文件，备份就靠它）
config\
  settings.json     # 邮箱/公司等业务配置（不含明文密码）
  feishu.json       # 飞书应用 app_id / approval_code
  credentials.json  # DPAPI 加密的密钥仓库（密码/授权码/secret）
  feishu.app_secret.blob
invoices\           # 收到的附件（**当前实测根目录为空**）
invoices\failed\    # OCR 失败的待重试附件（GUI 有重试入口；当前 6 个 PDF 全在这里）
reports\ logs\ backups\   # reports\ 当前为空
```

> 注：项目里的 `fin-system\config\` 是**历史遗留**，程序**不读它**；真正生效的配置全在 AppData。

---

## 5. 怎么用

- **日常使用**：双击桌面 `财务系统` 快捷方式（= exe 版）。
- **开发调试**：双击 `启动财务系统.bat`，或命令行
  `"C:/tmp/ocr_venv2/Scripts/python.exe" run_finance.py`
  ⚠️ **不要**直接跑 `python gui/main.py`（会 `ModuleNotFoundError: No module named 'db'`）。
- **关窗口 = 最小化到系统托盘**；要真退出用托盘菜单的「退出」。
- **开机自启**：设置页勾选「开机自动启动（后台运行）」（写 HKCU 的 Run 键）。

---

## 6. 改完代码 → 重新打包

```powershell
cd C:\Users\li1\fin-system
# 1) 先杀掉正在跑的实例，否则原子替换会被文件句柄锁住
Stop-Process -Name 财务系统 -Force
# 2) （PyPI 直连抖动时）改用清华源
$env:PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
# 3) 打包
powershell -ExecutionPolicy Bypass -File build.ps1 -PythonPath "C:\tmp\ocr_venv2\Scripts\python.exe"
```

- 构建 6 步 + 生成 `release\财务系统\manifest.sha256`，原子发布到 `release\`。
- **改完代码必须重打包**，否则 `release` 里还是旧版。
- **新增三方依赖要同时改 3 处**，否则 exe 会静默缺库：① `pyproject.toml` 的 `dependencies`；② `build.ps1` 里的 `pip install` 列表；③ `财务系统.spec` 的 `hiddenimports`。

---

## 7. 怎么验收（改完务必跑）

```powershell
"C:/tmp/ocr_venv2/Scripts/python.exe" -m pytest tests/ -q        # 单元测试
"C:/tmp/ocr_venv2/Scripts/python.exe" tools/acceptance_check.py  # 逐页 + 新模块冒烟
```

- 当前基线：**pytest 52 passed** / 验收 **17 OK、0 FAIL**。（41 → 52 是 2026-09-10 新增 `tests/test_backup_retention.py` 的 11 例）
- 打包后自检：`release\财务系统\财务系统.exe --self-check report=<路径> stderr=<路径>`（**不带参数跑是空输出、exit 0，属正常**；带参才写 `self-check.json`）。

---

## 8. 环境依赖

- Windows 11，Python **3.11.15** 位于 `C:\tmp\ocr_venv2`（含 paddleocr / paddlepaddle / pymupdf / tkinter / Pillow / openpyxl / fpdf2 / imap-tools / pystray）。
- 运行时依赖：`imap_tools, pystray, openpyxl, fpdf2`。
- 构建依赖（可选组 `build`）：`pyinstaller==6.21.0` 等。

### ⚠️ 第 4 个路径：OCR 引擎在**外部项目**里（交接必知）

发票 OCR **不是** fin-system 自己的代码，`settings.json` 里是硬编码的绝对路径：

```json
"invoice_agent_command": ["C:/tmp/ocr_venv2/Scripts/python.exe",
                          "C:/Users/li1/invoice-agent/ocr_invoice.py"],
"invoice_summary_path": "C:/Users/li1/invoice-agent/results/summary.json"
```

- 依赖目录：`C:\Users\li1\invoice-agent\`（有独立 GUI「发票管家」，桌面有 `发票管家.lnk`）。
- **移动/删除 `C:\Users\li1\invoice-agent` 或 `C:\tmp\ocr_venv2` → 邮件收票链路直接失效**，且程序不会报错，只会一直把附件丢进 `invoices\failed\`。
- 打包 exe 时 OCR 引擎不会被包含进去，它是运行时外部调用。

---

## 9. 当前状态（截至 2026-09-10，均已实测）

- **阶段 0–5 全部完成**：骨架 / 记账内核 / 报表 / 往来 / 报销 / 票据+飞书+邮件收票。
- 另有**已上线的增量模块**（第 3 节目录树已含）：进销存 `inventory.py` + 期末处理 `closing.py` + 数据问题 `integration.py`，GUI 导航共 11 页。
- 已打包 onedir **真 exe**，真机冒烟通过；UI = 浅色商务风（`clam` 主题，含表格斑马纹）。
- **release 已随 P1/P3 一起重打包**（exe `00:07`，8.86 MB）：实测含 P1 备份修复（`_BACKUP_NAME_RE` 出现 4 次）与 P3 改名（`configure_light_style` 3 次、无 `configure_dark_style =` 别名）；`财务系统.exe --self-check` 返回 `{"ok": true}`（13 项运行时组件）。P4 的 `build.ps1` 门禁改动**不进二进制**，但同一轮构建实跑验证过。
- ✅ **回归基线：`pytest` 111 passed**（P1 前 41 → P1 后 52 → P2/P3/P4/P5 后 111）。
- 账套实际数据：科目 46 · 凭证 13 · 往来（档案）2 · 期间 **2026-07**。
  - 补口径：凭证 13 = 已记账 11 + 草稿 1 + 待审核 1；其中有 3 条 `test data cleanup` 冲销凭证 + 1 条草稿冲销，属测试残留。
  - 往来 2 = `partner` 客户/供应商**档案** 2 条；应收应付台账 `arap_item` **0 条**。
  - 期间 2026-07 取自凭证的 `period` 字段；`period` 表本身 **0 行**（期末处理状态未初始化，不影响记账）。
- 发票库存：**共 6 张，价税合计 ¥1,436.83**（待确认 5 张 ¥1,340.00 + 待审核 1 张 ¥96.83）。
  - ⚠️ 6 张发票的 `file_path` / `file_hash` 均为空串 —— 发票记录与原始 PDF **未建立关联**，原始文件只在 `invoices\failed\`。
- 邮箱：163 **已启用**（水位线 `last_uid=1691478373`）；QQ 已禁用。

---

## 10. 待办 / 明确不做

- [ ] **「全部补收进来」**：163 历史发票补收（约 60 封）—— 前置修复已就绪，**尚未执行**（会真实连邮箱、真实写库）。
- [x] ✅ **P1 备份缺陷（已修复，提交 `3d27438`）**：原 `gui/main.py` `_run_startup_backup()` 有两处真实缺陷 —— ①「今天是否已备份」用 `date.today().isoformat()`（`finance-2026-09-10`）去匹配备份文件名 `finance-20260910-...` 的 `%Y%m%d` 前缀，**永不相等**，使「每天首启一次」退化成「每次启动都备份」，叠加 `MAX_BACKUPS=5` 后同一天的多次启动把历史全滚掉（**生产库实测 5 份全是 9/10 同一天，等于实际没有保留策略**）；②整函数 `except Exception: pass`，备份失败用户无感。现已改为：正则精确匹配自动备份命名（迁移前备份不参与轮转）+ 分层保留（7 日 / 4 周 / 6 月，超期才淘汰）+ 失败记 `LOG.error` 并在设置页标红。详见 `docs/architecture-review-2026-09-10-followup.md` §2.1。
- [x] ✅ **P2 `.gitignore` 纵深防御缺口（已修复，提交 `d75c8d2`）**：原只忽略 `config/feishu.json`，而 `config/credentials.json`（全部密钥的仓库）／`config/settings.json`／`config/*.blob` **都不被忽略**（`git check-ignore` 实测）。已改为按类型覆盖 `config/*.json` + `config/*.blob` + `!config/feishu.example.json` + `db/*.db`——**以后新增的配置文件默认安全**。注意规则写 `config/*.json` 而非 `config/`：忽略整个目录会让 `!` 放行规则失效。回归测试 `tests/test_repo_hygiene.py`（10 例，真实调用 `git check-ignore`）；旧规则下 5 例失败，新规则全绿。
- [x] ✅ **P3 `configure_dark_style` 命名与实现相反（已修复，提交 `1051a1b`）**：该别名实为 `configure_light_style`（`gui/ui_theme.py:151`），配色全浅色，会误导后人去「修」成深色（而深色风已明确否决）。已删除别名并把 `gui/main.py` 两处调用点、三个页面里未使用的 import 全部改名。回归测试 `tests/test_ui_theme.py`（13 例，含真实 Tk 断言实际生效主题为 `clam`）；把主题优先级改成 `vista` 优先可复现失败。
- [x] ✅ **P4 依赖三处手工同步（已修复，提交 `1039b2c`）**：`pyproject.toml` / `build.ps1` / `spec.hiddenimports` 三处靠人脑同步，而 PyInstaller 对缺失 hiddenimport 只发 WARNING → exe 静默缺库（openpyxl / fpdf2 导出报表时已真实发生）。已在 `build.ps1` `[2/6]` 装包后新增 `Assert-DependencyParity` 门禁：脚本清单 vs pyproject、每个声明项必须在 spec hiddenimports 里、每个包必须真实可 import，命中漂移即中止构建。pip 名/import 名映射（`fpdf2`→`fpdf`）集中到一处。回归测试 `tests/test_dependency_parity.py`（8 例）；实跑构建输出 `dependency parity OK: imap_tools, pystray, openpyxl, fpdf`。
- [x] ✅ **P5 业务数据回补（工具与测试已就绪，提交 `21ed316`；⚠️ 尚未对生产库执行）**：详见下方 §10.1。
- [ ] 测试残留数据清理：`invoices\failed\` 6 个 PDF（含 3 个 `failed_invoice_*.pdf` 早期样本，25 字节占位符）、3 条 `test data cleanup` 冲销凭证。**清理前先确认清单。**（回补工具已把 3 个 25 字节占位符自动识别并忽略。）
- [ ] 可选小清理：`config/feishu.app_secret.blob`（旧版孤儿文件，当前代码无 `.blob` 读写）、`release/frozen-self-check.json`（7/11 旧文件）、`settings.json` 与 `feishu.json` 里重复的飞书配置段。
- ℹ️ `partner.type` 中英混用（中文「客户」+ 英文 `supplier`）：**代码已用 `type IN (?,?)` 兼容，非 bug**，新数据由 `add_partner` 自动归一化。可不处理。
- ❌ 报税/税务申报：明确**不做**。
- ❌ 多账套切换：明确**不做**（单公司账套）。

> 架构层面的完整体检见 `docs/architecture-review-2026-08-08.md`（基准）与
> `docs/architecture-review-2026-09-10-followup.md`（核验 + 增量）。
> 前者的 TOP 5 技术债**现已全部关闭**；后者明确列出「不建议做」的事项（拆 main.py / 引 ORM / 补 CI 等），避免过度工程。


### 10.1 P5 业务数据回补（工具已就绪，待确认后执行）

**工具**：`tools/backfill_business_data.py`（默认只读；`--apply` 才写库）
**测试**：`tests/test_backfill_business_data.py`（29 例，全绿）

#### 排查结论

1. **`company` 表**：恒为 1 行且 `name/tax_no/addr/note` 全空 → 报表/打印抬头取不到公司名。**只有抬头是直接可补的，需要一个你确认的注册地址。**
2. **`invoice_doc.file_path` 恒为空串**。根因**不在财务系统**，而在**外部发票识别 agent**：
   `C:\Users\li1\invoice-agent\invoice_agent.py` 的 `summarize()` 把 OCR 结果压缩成
   6 个中文字段（发票号码/开票日期/销售方/金额/税额/购买方），**丢掉了 `file`（文件名）
   以及 `销售方税号` / `购买方税号` / `价税合计`**。财务系统
   `register_invoice(raw, raw.get("file_path") or raw.get("file"))` 因此永远拿不到
   `source_file`，`file_path` 只能是空串；同一原因也让 2026-07-12 入库的 4 张滴滴发票
   税号为空。**根因不修，以后新收的发票照样是空。**

#### 6 行发票全部可硬关联，无一行靠猜

| 库内 id | 发票号码 | 原始文件 | 证据来源 |
|---|---|---|---|
| 6 | 26337000000663360916 | `invoice-agent\invoices\滴滴电子发票A.pdf` | `results/ocr_result.json` |
| 7 | 26447000001378352112 | `…\滴滴电子发票B.pdf` | 同上 |
| 8 | 26217000000285335773 | `…\滴滴电子发票C.pdf` | 同上 |
| 9 | 26117000001021765684 | `…\滴滴电子发票D.pdf` | 同上 |
| 10 | 26337000000601987996 | `FinanceSystem\invoices\failed\【一嗨-403.00元】…pdf` | 单文件重新识别 |
| 11 | 26317000002656636842 | `…\【一嗨-216.96元】…pdf` | 同上 |

关键点：`ocr_result.json` 里 `file` 与 `发票号码` 是**成对的**，所以 id=6~9 是按发票号
**精确 join**，不是按顺序猜。id=10/11 用识别 agent 的单文件模式重新识别取号，
号码与库内**完全一致**（含 `seller_tax_no`）。归档到
`FinanceSystem\invoices\archive\<开票月份>\<发票号>_<原名>`，路径由库自己持有，
不依赖 agent 的暂存目录。

同时发现：`FinanceSystem\invoices\failed\` **没有"成功归档"目录**，历史上 OCR 成功的
PDF 从未落盘到财务系统侧（只有元数据进库）；该目录里 3 个 25 字节的
`failed_invoice_*.pdf` 是早期失败占位符（工具已自动忽略），另 1 个
`订单…结算单.pdf` 是结算单不是发票（识别不出发票号，正确忽略）。

#### 执行方式（需你确认）

```bash
# 1) 只读看方案（已跑过，6 行全部可关联）
python tools/backfill_business_data.py

# 2) 确认后写入（自动备份 → 归档文件 → 事务写库 → 写审计）
python tools/backfill_business_data.py \
  --company-name "高迥（杭州）贸易有限责任公司" \
  --company-tax-no "91330105MACRGYRN32" \
  --company-addr "<待你确认的注册地址>" \
  --apply
```

写入前后可自查：`invoice_doc.file_path` 应指向真实存在的文件、
`audit_event` 新增 `invoice_file_linked` / `company_field_updated`。
**执行前请先退出「财务系统」程序**（已确认当前未在运行）。

#### 根因修复方案（未实施，需你点头）

| 步骤 | 改动 | 风险 |
|---|---|---|
| ① | `invoice-agent\invoice_agent.py::summarize()` 的 row 里补上 `file_path`（绝对路径）与 `销售方税号`/`购买方税号`/`价税合计` | 低，纯**新增** key，现有 6 个中文字段不变，下游 `_text()` 查找兼容 |
| ② | 财务系统入库时把源文件**归档进自己的数据根**、`file_path` 指向归档副本 | 中，改动 `register_invoice` 的写入行为，属非平凡改动，需先过方案 |
| ③ | 给 `load_invoices` 加一条告警：summary 行缺 `file_path` 时记 `LOG.warning` | 低，纯观测 |

①③ 只是止血与可观测；②才让链路长期自洽。建议 ①②③ 一起做，但 ② 想先听你的意见。


---

## 11. 安全红线

- 邮箱密码/授权码、飞书 secret —— **只经 DPAPI 加密**存 `credentials.json`，**绝不落明文、绝不进仓库/二进制**。
- `config/feishu.json`、`db/*.db`、`data/`、`release/` 均已 gitignore。

---

## 12. 踩过的坑速查（完整版见技能 `sme-finance-desktop`）

1. **启动必须走 `run_finance.py`**（否则 `import db` 失败）。
2. **DPAPI secret name 不能含 `@`**（`security.py` 只允许字母数字 `._:-`）：`a@b.com` → `a_at_b_com`。
3. **ttk 主题必须用 `clam`**：`vista` 会忽略背景映射 → 选中页签**白底白字看不见**。
4. **表格斑马纹会盖掉选中高亮**：需专用 `sel` tag（已在 `_ZebraTree` 处理）。
5. **改代码后必须重打包**，否则 `release` 是旧版。
6. **构建前先杀 `财务系统.exe` 进程**（MSYS 下别用 `taskkill //F`，用 PowerShell `Stop-Process`）。
7. **本机 GUI 截图不可靠**（DPI 缩放 + 窗口被遮挡）→ 用程序化断言（读 tag / style 值）验证，别靠看图下结论。

---

## 13. 快捷索引

| 我要… | 去哪 |
|---|---|
| 启动程序 | `启动财务系统.bat` ／ 桌面 `财务系统` |
| 找账套文件（备份/迁移） | `%LOCALAPPDATA%\FinanceSystem\db\finance.db` |
| 改邮箱/公司配置 | `%LOCALAPPDATA%\FinanceSystem\config\settings.json` |
| 看运行日志 | `%LOCALAPPDATA%\FinanceSystem\logs\` |
| 重试失败发票 | 程序里「票据」页的重试入口（源文件在 `invoices\failed\`） |
| 重新打包 | `build.ps1` |
| 跑测试 | `tests/` + `tools/acceptance_check.py` |
| 了解全部设计/坑 | 技能 `sme-finance-desktop`（Hermes） |

---

## 14. 核验记录（2026-09-10，WorkBuddy 接手时实机复核）

> 本节的每一条都是**实测**，不是转述。复核方式：直读文件系统 + 只读打开 SQLite（`mode=ro`）+ 实跑测试。

| 核验项 | 声明 | 实测 | 结论 |
|---|---|---|---|
| ① 源码路径 | `C:\Users\li1\fin-system` | 存在，全盘搜索**仅此一份** | ✅ |
| ② 打包程序 | `release\财务系统\财务系统.exe` | 存在，8.8 MB，`_internal\` + `manifest.sha256` | ✅ |
| ③ 数据目录 | `%LOCALAPPDATA%\FinanceSystem\` | db/config/invoices/logs/reports/backups 齐全 | ✅ |
| 桌面入口 | `桌面\财务系统.lnk` → ② | 目标 = `%USERPROFILE%\fin-system\release\财务系统\财务系统.exe`，工作目录已设 | ✅ |
| `db\finance.db` | 账套本体 | 存在，241,664 字节，25 张表 | ✅ |
| 科目数 | 46 | `account` = **46**（全部 `is_leaf=1`） | ✅ |
| 凭证数 | 13 | `voucher` = **13**（11 posted / 1 draft / 1 pending_review） | ✅ |
| 往来数 | 2 | `partner` = **2**；`arap_item` = 0（口径见第 9 节） | ⚠️ 需注明口径 |
| 期间 | 2026-07 | 全部凭证 `period='2026-07'`；`period` 表 0 行 | ✅ 有附注 |
| 发票张数/金额 | 6 张 / ¥1,436.83 | 6 张，`sum(total_cents)` = 143,683 → **¥1,436.83** | ✅ |
| 待确认子集 | 5 张 ¥1,340.00 | 5 张 134,000 分 → **¥1,340.00** | ✅ |
| 待审核子集 | 1 张 ¥96.83 | 1 张 9,683 分 → **¥96.83** | ✅ |
| 邮箱状态 | 163 启用 / QQ 禁用 | 163 `enabled=true, last_uid=1691478373`；QQ `enabled=false` | ✅ |
| pytest 基线 | 41 passed | 核验时 **41 passed**；P1 后 **52**；P2/P3/P4 后 **83**；P5 后 **111 passed** | ✅ |
| 验收基线 | 17 OK / 0 FAIL | **合计：17 OK / 0 FAIL**（11 个页面全构建成功） | ✅ |
| git 状态 | 分支 master | `master`；核验时 HEAD = `8ae405a` → P1 `3d27438` → P2 `d75c8d2` → P3 `1051a1b` → P4 `1039b2c` → P5 `21ed316` | ✅ |
| 用户喜好 | 源码/数据分离 | 结构确实分离；测试有 `.test-home` 隔离守卫（`conftest.py` 防写生产库） | ✅ |

**复核中发现并已修正的文档错误**：目录树漏了 `closing/inventory/integration/ocr_invoice/page_closing/page_inventory/simhei.ttf` 与 3 个导航页；「最近提交」写的是 `99b397e`（实际 `8ae405a`）；第 8 节漏了外部 OCR 项目 `C:\Users\li1\invoice-agent`；第 9 节未注明「往来 2」是档案数。

**顺手验证：`docs/architecture-review-2026-08-08.md` 列的 TOP 5 技术债已全部关闭** —— TD-1（`C_PANEL` NameError）、TD-2（无 git）、TD-3（构建流水线断裂，含 spec 入口 / 依赖声明 / self_check 清单三处）、TD-4（无自动备份）、TD-5（邮件收票 4 个缺陷）逐项对照源码确认已修。增量发现与「不建议做」清单见 `docs/architecture-review-2026-09-10-followup.md`。

**P1 备份缺陷修复复核（2026-09-10 晚，提交 `3d27438`）**：

| 核验项 | 实测 | 结论 |
|---|---|---|
| 缺陷坐实 | `%LOCALAPPDATA%\FinanceSystem\backups\` 下 5 份备份**全部**为 `finance-20260910-*`（22:22/22:26/22:28/22:30/22:32，同一天 5 次启动） | ⚠️ 原「5 天窗口」实为「最近 5 次启动」，**无保留策略** |
| 根因 | 判定值 `date.today().isoformat()` = `finance-2026-09-10`；文件名 = `finance-20260910-...` → 前缀恒不匹配 | ✅ 已定位 |
| 修复后测试 | `tests/test_backup_retention.py` 11 例全绿（正则不误伤迁移备份 / 一天一份 / 失败可见 / 分层保留 / 周桶去重 / 空目录安全） | ✅ |
| 质量门禁 | `ocr_venv2` 解释器实跑：pytest **52 passed**、acceptance **17 OK / 0 FAIL** | ✅ |
| 生产库未受影响 | 修复前后 `mode=ro` 统计均为 46 科目 / 13 凭证 / 6 发票（价税合计 `total_cents`=143,683 → ¥1,436.83） | ✅ |
| 迁移前备份受保护 | `finance-pre-*.db` 与 `source-pre-upgrade-*.zip` 不匹配新正则 → 不参与轮转删除 | ✅ |

> 注：现存那 5 份同日备份，在**下次启动程序**时会被新策略收敛为 1 份（同一天只留最新）。这是预期行为，不是数据丢失 —— 它们内容完全相同（同为当日账套快照）。

**P2~P5 复核（2026-09-10 深夜）**：

| 核验项 | 实测 | 结论 |
|---|---|---|
| P2 忽略规则 | `git check-ignore -v` 实测：`credentials.json` / `settings.json` / `feishu.json` / `*.blob` / 任意新 `.json` 全部命中；`feishu.example.json` / `README.md` 放行 | ✅ |
| P2 无密钥入库 | `git add -A -n` 只显示 `.gitignore`，无任何凭据文件 | ✅ |
| P3 源码残留 | 全仓 `configure_dark_style` 源码引用为 0（仅剩解释性注释） | ✅ |
| P3 真实主题 | 真实 Tk 断言实际生效主题 = `clam`，选中页签可读；改成 `vista` 优先即失败 | ✅ |
| P4 门禁存在且被接线 | 静态断言 `Assert-DependencyParity` 已定义、已调用，且顺序在 install 之后、PyInstaller 之前 | ✅ |
| P4 实跑 | 完整 `build.ps1` 输出 `dependency parity OK: imap_tools, pystray, openpyxl, fpdf`（证明 `fpdf2`→`fpdf` 映射端到端生效） | ✅ |
| P4 变异性 | 只往 `pyproject.toml` 注入 `requests` → 2 例失败；还原后全绿 | ✅ |
| P5 只读不改库 | 不加 `--apply` 对生产库跑：6 行全部规划、库内 `file_path` 仍为空串、`audit_event` 无新增、无 `backups\` 目录产生 | ✅ |
| P5 关联正确性 | 6 行全部按发票号精确 join，金额二次校验通过；id=10/11 重新识别出的号码与库内**完全一致** | ✅ |
| P5 变异性 | 同时移除「执行前复核」与「SQL 空列守卫」→ `test_apply_blocks_overwriting_a_path_filled_after_planning` 必定失败 | ✅ |
| P5 备份 WAL 安全 | 未 checkpoint 的 WAL 数据必须出现在备份里（专项用例验证通过） | ✅ |
| P5 未执行写库 | `company` 仍全空、6 张发票 `file_path` 仍空 → **确实没有擅自改生产数据** | ✅ |

**遗留风险（未修，仅记录）**：
1. `company` 表全空 → 报表抬头公司名为空（**P5 工具已备好，待确认地址后执行**）。
2. `partner.type` 中文「客户」/ 英文 `supplier` 混用（**代码已兼容 `type IN (?,?)`，非 bug**）。
3. 6 张发票 `file_path` 空 → 与原始 PDF 无关联（**P5 工具已备好，6 行全部可硬关联**）。
4. `invoices\failed\` 6 个 PDF + 3 条 `test data cleanup` 冲销凭证属测试残留（其中 3 个 25 字节占位符）。
5. `release\frozen-self-check.json` 是 2026-07-11 的旧文件（`logs\` 下另有一份）。
6. 配置双份：`settings.json` 里有 `feishu` 段，`feishu.json` 里又有一份同内容。
7. **根因未修**：`invoice-agent\invoice_agent.py::summarize()` 丢字段 → 以后新收发票 `file_path` 依旧为空、税号依旧丢失（方案见 §10.1）。
8. 财务系统**没有"成功归档"目录**，历史上 OCR 成功的 PDF 从未落盘到财务系统侧，只有元数据进库。

