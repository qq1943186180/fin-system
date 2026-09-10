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

## 9. 当前状态（截至 2026-09-11 02:34，均已实测）

- **阶段 0–5 全部完成**：骨架 / 记账内核 / 报表 / 往来 / 报销 / 票据+飞书+邮件收票。
- 另有**已上线的增量模块**（第 3 节目录树已含）：进销存 `inventory.py` + 期末处理 `closing.py` + 数据问题 `integration.py`，GUI 导航共 11 页。
- 已打包 onedir **真 exe**，真机冒烟通过；UI = 浅色商务风（`clam` 主题，含表格斑马纹）。
- **release 已重打包至含 P1/P3/P6/P7**（exe `01:12`，8,863,479 B；`build.ps1` 六步全过，`BUILD_EXIT=0`）。
  - ✅ **打包验证方式已纠正**：早期记录说"grep exe 找 `_BACKUP_NAME_RE` 命中 4 次"—— **该方法无效**。PyInstaller 把字节码压进 PKG→PYZ（zlib），任何 Python 级名字都 grep 不到（对照实测：`_BACKUP_NAME_RE` / `configure_light_style` 均为 **0 次**，而它们确在包里）。现在改用 `tools/verify_release_packaging.py`：解出 PYZ，取目标模块 code object，扫 `co_name` / `co_names` / `co_varnames` / `co_consts`（含**字符串元组**，3.11 的关键字实参名藏在这里）。实测输出 `ALL_TARGET_SYMBOLS_PRESENT`，且**负对照通过**（注入假符号必被报出）。
  - ✅ 实测含 P6：`_archive_invoice_file` / `_archive_month` / `_archive_enabled` / `_safe_name` / `invoice_archive_enabled` / `INVOICES_ARCHIVE_DIR` / `_source_filename` / `source_file` 全部命中。
  - ✅ `manifest.sha256` 999 项，`财务系统.exe` 哈希与清单一致（`09b7332…2a3cd4`）；构建期内置自检 `exit=0`。
  - ⚠️ `release\frozen-self-check.json`（`7/11` 那份）是**旧构建残留**——`build.ps1` 把自检报告写到临时目录，不回写发布根，故该文件不会更新。**已于 2026-09-11 清入隔离区**（见 §11 残留清理）。
  - ℹ️ 构建工具链：本机无 `py` 启动器，3.11 宿主为 `C:\Users\li1\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe`，需 `-PythonPath` 显式传入（`build.ps1` 找不到 `py -3.11` 会直接抛错）。
- ✅ **回归基线：`pytest` 171 passed**（P1 前 41 → P1 后 52 → P2~P5 后 111 → P6 后 130 → P7 后 164 → 打包验证器后 171）。
- 账套实际数据：科目 46 · 凭证 **12** · 往来（档案）2 · 期间 **2026-07**。
  - 补口径：凭证 12 = 已记账 **11** + 待审核 1；其中有 3 条 `test data cleanup` 冲销凭证，属**有意保留**的测试残留（已记账、与凭证 5/6/7 两两对冲，账平）。
  - **2026-09-11 02:34 删除了 1 条废弃草稿**：`voucher` id 11「冲销 2026-07-0001：123」（冲销真实的股东投资 ¥200,000，从未记账，纯残留）。13 → 12，`posted` 11 张未变、借贷平衡未变、发票台账未变；审计 `audit_event` id 105 `voucher_draft_deleted`；备份 `backups\finance-delvoucher11-20260911-023402.db`。
  - 往来 2 = `partner` 客户/供应商**档案** 2 条；应收应付台账 `arap_item` **0 条**。
  - 期间 2026-07 取自凭证的 `period` 字段；`period` 表本身 **0 行**（期末处理状态未初始化，不影响记账）。
- 公司抬头：**已补齐且已逐字复核**（`2026-09-11` 重新读取桌面 `执照.jpg`，与库内 `company` 表 1 行**完全一致**）：
  - `name` = `高迥（杭州）贸易有限责任公司` ✅
  - `tax_no` = `91330105MACRGYRN32` ✅（与执照"统一社会信用代码"逐位一致）
  - `addr` = `浙江省杭州市拱墅区东新街道东方茂商业中心1幢3层4062室` ✅（与执照"住所"逐字一致，含"1幢"的"1"为阿拉伯数字、"3层4062室"）
  - ⚠️ **早前那条"与 `执照.jpg` 一致"原来没有证据**——当时读图返回"Content filtered / 模型不支持图片"，地址实际来自别处。本次已真正读图复核，**结论碰巧是对的**，但不代表当时的方法对。以后凡"照抄证件"类写入，必须先把证件读出来、肉眼看字段再落库。
- 执照上**其余权威字段**（本次一并读出，库内暂未使用；将来开票 / 开户 / 备案要用）：
  - 类型：有限责任公司（自然人投资或控股）
  - 法定代表人：李尚远
  - 注册资本：壹佰万元整
  - 成立日期：2023-08-01
  - 登记机关：杭州市拱墅区市场监督管理局；执照打印日期 2026-01-12（换发/变更后版本）
  - ⚠️ 库内**没有**电话 / 开户行及账号字段（`bank_account` 表 0 行）。若将来要**对外开票**（销方信息需"名称+税号+地址电话+开户行账号"），这两项仍缺，需补。
- 发票库存：**共 14 张，账面价税合计 ¥2,672.42**（其中 13 张「待确认」+ 1 张「待审核」）。
  - **原 6 张（2026 年）**：`file_path` / `file_hash` / 税号已全部补齐，原件归档于 `invoices\archive\<开票月份>\`（提交 `21ed316`）；`tax_cents` / `total_cents` 已按票面真值修正（少计 ¥24.53，提交 `26f2fb5`）。详见 §10.2。
  - ✅ **新增 8 张（2024 年，2026-09-11 01:41 试收自 163 邮箱，价税合计 ¥1,211.06）**：见 §10.3。
    购买方全部正确为「高迥（杭州）贸易有限责任公司」`91330105MACRGYRN32`，金额自洽，
    原件归档至 `invoices\archive\2024-06\`（2 个）与 `2024-09\`（6 个），`audit_event` id 48–55。
- 邮箱：163 **已启用**（水位线 `last_uid=1691478373`）；QQ 已禁用。

---

## 10. 待办 / 明确不做

- [x] ✅ **163 历史发票补收 —— 全量已完成**（试收 2026-09-11 01:41；全量 2026-09-11 02:02）。详见 §10.3。
  - ✅ 前置条件全部就绪（P6 归档 + P7 口径 + P5 回补），端到端链路**已验证可用**。
  - ✅ **P9（OCR 缺陷）先修，再全量补收**：改外部 `ocr_invoice.py` 用**坐标锚点**替代「文本流顺序」，
    发票类可入库率 34/72(47%) → **64/72**，真发票 **64/64 = 100%**，买卖方错配 **0**。
  - ✅ 全量结果：49 封邮件 / 新入库 **49 张** / 失败 **0** / 台账 14 → **63 张** / 价税合计 **¥13,745.20** /
    凭证 **13 → 12 张**（补收本身只写台账不影响账务，13 张未变；**13 → 12 与补收无关**，是 2026-09-11 02:34 单独删除 id 11 废弃草稿所致，见 §10 首条）/ 审计 `invoice_backfilled` 累计 57 条。
  - 工具：`tools/backfill_history_invoices.py`（默认只读 + OCR 预筛）、`tools/audit_history_invoice_ocr.py`（质量体检）。
- [x] ✅ **P9：修外部 OCR 的「乱序文本层」抽取缺陷 —— 已完成（2026-09-11）**，详见 §10.3 §六。
  改 `C:/Users/li1/invoice-agent/ocr_invoice.py`（改动前备份 `ocr_invoice.py.bak-20260911-015923`）：
  新增 `cn_upper_to_num` / `_nearest_by_label` / `_split_by_layout` / `amount_triple_from_row`，
  金额抽取改六级优先级（含**大写金额校正**），买卖方按**版面区域**配对（不再按出现顺序）。
  新增 `--self-check <DIR>` 自检与 `--company-tax-no` 可选锚点；CLI 契约不变。
  回归基线：`work/ocr_samples/`（109 个真实附件 + `baseline.json`），**零回退**（老版能过的 17 张数值一致）。
- [x] ✅ **P1 备份缺陷（已修复，提交 `3d27438`）**：原 `gui/main.py` `_run_startup_backup()` 有两处真实缺陷 —— ①「今天是否已备份」用 `date.today().isoformat()`（`finance-2026-09-10`）去匹配备份文件名 `finance-20260910-...` 的 `%Y%m%d` 前缀，**永不相等**，使「每天首启一次」退化成「每次启动都备份」，叠加 `MAX_BACKUPS=5` 后同一天的多次启动把历史全滚掉（**生产库实测 5 份全是 9/10 同一天，等于实际没有保留策略**）；②整函数 `except Exception: pass`，备份失败用户无感。现已改为：正则精确匹配自动备份命名（迁移前备份不参与轮转）+ 分层保留（7 日 / 4 周 / 6 月，超期才淘汰）+ 失败记 `LOG.error` 并在设置页标红。详见 `docs/architecture-review-2026-09-10-followup.md` §2.1。
- [x] ✅ **P2 `.gitignore` 纵深防御缺口（已修复，提交 `d75c8d2`）**：原只忽略 `config/feishu.json`，而 `config/credentials.json`（全部密钥的仓库）／`config/settings.json`／`config/*.blob` **都不被忽略**（`git check-ignore` 实测）。已改为按类型覆盖 `config/*.json` + `config/*.blob` + `!config/feishu.example.json` + `db/*.db`——**以后新增的配置文件默认安全**。注意规则写 `config/*.json` 而非 `config/`：忽略整个目录会让 `!` 放行规则失效。回归测试 `tests/test_repo_hygiene.py`（10 例，真实调用 `git check-ignore`）；旧规则下 5 例失败，新规则全绿。
- [x] ✅ **P3 `configure_dark_style` 命名与实现相反（已修复，提交 `1051a1b`）**：该别名实为 `configure_light_style`（`gui/ui_theme.py:151`），配色全浅色，会误导后人去「修」成深色（而深色风已明确否决）。已删除别名并把 `gui/main.py` 两处调用点、三个页面里未使用的 import 全部改名。回归测试 `tests/test_ui_theme.py`（13 例，含真实 Tk 断言实际生效主题为 `clam`）；把主题优先级改成 `vista` 优先可复现失败。
- [x] ✅ **P4 依赖三处手工同步（已修复，提交 `1039b2c`）**：`pyproject.toml` / `build.ps1` / `spec.hiddenimports` 三处靠人脑同步，而 PyInstaller 对缺失 hiddenimport 只发 WARNING → exe 静默缺库（openpyxl / fpdf2 导出报表时已真实发生）。已在 `build.ps1` `[2/6]` 装包后新增 `Assert-DependencyParity` 门禁：脚本清单 vs pyproject、每个声明项必须在 spec hiddenimports 里、每个包必须真实可 import，命中漂移即中止构建。pip 名/import 名映射（`fpdf2`→`fpdf`）集中到一处。回归测试 `tests/test_dependency_parity.py`（8 例）；实跑构建输出 `dependency parity OK: imap_tools, pystray, openpyxl, fpdf`。
- [x] ✅ **P5 业务数据回补（已对生产库执行，提交 `21ed316`，执行于 2026-09-11 00:44）**：详见下方 §10.1。
- [x] ✅ **P6 发票原件归档 + `file_path` 落地 + 金额口径纠正（已修复，提交 `1cf2be2`）**：`invoice_doc.file_path` 恒为空的根因已修 —— 外部 agent `summarize()` 补齐 `file_path`/税号/金额三件套，财务系统入库时把原件归档进自己的数据根，`load_invoices` 对缺路径行记 WARNING。回归 `tests/test_invoice_archive.py`（18 例）。详见 §10.2。
- [x] ✅ **P7 历史金额口径修正（已对生产库执行，提交 `26f2fb5`，执行于 2026-09-11 01:05）**：`summarize()` 早年让 `金额` 字段兼职两种含义且 key 名与下游不匹配，导致进项税静默归零、4 张发票价税合计少计 ¥24.53，其中 id=9 的凭证亦连带少记应付。已按确认口径（3% 电子普票、不可抵扣、全额入成本）修正台账与凭证。工具 `tools/fix_invoice_amounts.py`（默认只读）+ 回归 `tests/test_fix_invoice_amounts.py`（34 例）。详见 §10.2。
- [x] ✅ **测试残留清理 —— 已执行（2026-09-11 02:30~02:34），详见 §11 残留清理**。
  - 文件类 **10 个已移入可逆隔离区**（`invoices\failed\` 6 + `archive\2026-09\` 孤儿副本 2 + `release\frozen-self-check.json` + `config\feishu.app_secret.blob`），**未删除**，带 `manifest.json` + SHA256，可整体还原。
  - 凭证类：`voucher` id 11（废弃草稿）**已删除**；3 条 `test data cleanup` 冲销凭证（12/13/14）**有意保留** —— 它们是 `posted`，DB 触发器硬拦（`posted voucher is immutable`），且与 5/6/7 两两对冲，**只删冲销项会让测试费用复活进账（¥1,164.88）**。
  - ⚠️ 同时纠正一处误判：`archive\2026-09\` 里**带 epoch 后缀**的文件才是台账 `file_path` 指向的正本（id 10/11），无后缀的是回填孤儿 —— 方向与早前记录相反。
- [x] ✅ **可选小清理 —— 已执行（2026-09-11 02:30）**：`config/feishu.app_secret.blob`（已核 `security.py` 只读写 `credentials.json`）、`release/frozen-self-check.json` 均已清入隔离区。
- ℹ️ 未处理（低优先）：飞书配置**分散在三处**且都在生效代码路径上 —— `modules/feishu.py` 先读安装根 `config/feishu.json`（`_legacy_config()`）→ 再读数据根 `config/feishu.json` → 最后把 `settings.json` 的 `feishu` 段 **merge 覆盖**上去（`modules/feishu.py:63/80/89`）；GUI 只写 `settings.json`。即 **`settings.json` 优先级最高，是实际生效值**。去重需先确认调用路径与优先级，**收益低风险高，暂不动**。
- ℹ️ `partner.type` 中英混用（中文「客户」+ 英文 `supplier`）：**代码已用 `type IN (?,?)` 兼容，非 bug**，新数据由 `add_partner` 自动归一化。可不处理。
- ❌ 报税/税务申报：明确**不做**。
- ❌ 多账套切换：明确**不做**（单公司账套）。

> 架构层面的完整体检见 `docs/architecture-review-2026-08-08.md`（基准）与
> `docs/architecture-review-2026-09-10-followup.md`（核验 + 增量）。
> 前者的 TOP 5 技术债**现已全部关闭**；后者明确列出「不建议做」的事项（拆 main.py / 引 ORM / 补 CI 等），避免过度工程。


### 10.1 P5 业务数据回补（✅ 已于 2026-09-11 00:44 对生产库执行）

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

#### 执行记录（✅ 已完成）

```bash
# 1) 只读看方案（先跑过，6 行全部可关联、无模糊项）
python tools/backfill_business_data.py

# 2) 写入（自动备份 → 归档文件 → 事务写库 → 写审计）—— 实际执行命令
python tools/backfill_business_data.py \
  --company-name "高迥（杭州）贸易有限责任公司" \
  --company-tax-no "91330105MACRGYRN32" \
  --company-addr "浙江省杭州市拱墅区东新街道东方茂商业中心1幢3层4062室" \
  --apply
```

**执行前**：确认「财务系统」exe 未运行（`Get-Process` 无匹配，DB 最后写入 16:51）、两个 `python.exe` 均为 hermes gateway 与账套无关。

**执行结果（实测复核）**：

| 项目 | 结果 |
|---|---|
| 自动备份 | `db\backups\finance.db.backfill-20260911-004425.bak`（在线备份 API + `integrity_check` + 行数校验） |
| 归档文件 | 6 个，`invoices\archive\2026-07\` ×4 + `2026-09\` ×2 |
| 归档存在性 | 逐行 `os.path.exists` → **6/6 EXISTS** |
| 发票关联 | 6 行 `file_path`/`file_hash` 全部落地；id=6~9 另补 `seller_tax_no`/`buyer_tax_no` |
| 公司抬头 | `name` / `tax_no` / `addr` 三字段写入，`note` 保持空 |
| 审计留痕 | `audit_event` 新增 6 条 `invoice_file_linked` + 3 条 `company_field_updated`（id 34–42） |
| 忽略项 | 3 个 25 字节 `failed_invoice_*.pdf` 占位符 + 1 份结算单（非发票）—— 均正确跳过 |

**⚠️ 执行中额外发现的口径问题**：`ocr_result.json` 显示 id=6~9 的
`税额合计` 非零（17.74 / 0.27 / 3.61 / 2.91），但库里 `tax_cents` 全为 0、
`total_cents` = `net_cents`。回补工具**按要求只填 `file_path`/`file_hash`/税号**，
未触碰金额列 —— 金额修正单列为 §10.2。

#### 为什么 id=6~9 的「金额」会错（根因链）

`invoice_agent.py::summarize()` 原先把 `金额` 写成
`f.get("价税合计") or f.get("金额合计")`。而 OCR 对滴滴票**抽不到**「价税合计」
（正则只认 `价税合计…¥数字` 的同行形态），于是 `金额` 落到了 `金额合计`（**不含税**值）。
财务系统 `normalize_invoice()` 又把 summary 的 `金额` 当 `net_amount_cents`，
`税额合计` 因 key 名不匹配（summary 里叫「税额」）取到 0，最终
`total = net + tax = net` —— **进项税被静默丢弃**。该漏洞已在 P6 一并修掉
（`summarize()` 现输出 `金额合计`/`税额合计`/`价税合计` 三个独立字段，
价税合计缺失时用「不含税 + 税额」兜底）。


### 10.2 P6 根因修复 + P7 历史金额修正（✅ 均已完成）

#### 已完成（提交 `1cf2be2`）

| 步骤 | 改动 | 落点 |
|---|---|---|
| ① | `summarize()` 补齐 `file_path`（绝对）/`销售方税号`/`购买方税号`/`金额合计`/`税额合计`/`价税合计`；价税合计缺失时用「不含税 + 税额」兜底；原 6 个中文字段保留 | 外部 `C:\Users\li1\invoice-agent\invoice_agent.py` |
| ② | 新增 `_archive_invoice_file()`，在 `normalize_invoice()` 内把原件复制到 `<数据根>\invoices\archive\<开票月份>\<发票号>_<原名>`；同名不同内容加 `(2)` 后缀不覆盖；失败降级沿用原路径、**绝不阻断记账**；`settings.invoice_archive_enabled=false` 可关 | `modules/invoice_gui.py` |
| ③ | `mail_processor` 在临时文件被删除前把它作为 `source_file` 传入入库（此前只取 OCR 的 `fields`、丢掉 `file` 键，原件随 `finally` 一起消失） | `modules/mail_processor.py` |
| ④ | `load_invoices` 对缺 `file_path` 的行记 `LOG.warning` | `modules/invoice_gui.py` |
| ⑤ | `INVOICES_ARCHIVE_DIR` 常量 + `ensure_app_dirs()` 创建 | `app_paths.py` |

回归 `tests/test_invoice_archive.py` 18 例；**变异验证**：移除归档调用或 `source_file`
传递 → 4 例失败（证明测试真在保护行为）。全量 **130 passed**。

> ⚠️ `logging_setup` 里 `logger.propagate = False`，故 `caplog` 抓不到这些告警，
> 测试改为直接断言 `LOG.warning` 调用；日志本身正常写入 `logs\` 下文件。

#### ✅ P7 已执行：id=6~9 的历史金额修正（提交 `26f2fb5`）

口径已确认：滴滴开的是 **3% 增值税电子普通发票**，进项税**不可抵扣** → 成本费用按
**价税合计全额**入账（**台账 ≠ 分录**：`invoice_doc.tax_cents` 仍如实记录票面税额，
但凭证不拆 `2221`，而是把原金额整体抬到价税合计）。

| id | 发票号码 | 修正前 net/tax/total | 修正后 net/tax/total | 凭证处理 | 补计 |
|---|---|---|---|---|---|
| 6 | 26337000000663360916 | 591.11 / 0 / 591.11 | 591.11 / **17.74** / **608.85** | 无凭证，仅台账 | ¥17.74 |
| 7 | 26447000001378352112 | 8.78 / 0 / 8.78 | 8.78 / **0.27** / **9.05** | 无凭证，仅台账 | ¥0.27 |
| 8 | 26217000000285335773 | 120.15 / 0 / 120.15 | 120.15 / **3.61** / **123.76** | 无凭证，仅台账 | ¥3.61 |
| 9 | 26117000001021765684 | 96.83 / 0 / 96.83 | 96.83 / **2.91** / **99.74** | voucher 15 同步：`借 5602` / `贷 2202` 均 96.83 → **99.74** | ¥2.91 |

合计补计价税 **¥24.53**。

工具：`tools/fix_invoice_amounts.py`（**默认只读**，`--apply` 才写）。

| 安全层 | 做法 |
|---|---|
| 真值来源 | 只认 `invoice-agent/results/ocr_result.json` 的票面三件套；`不含税 + 税额 ≠ 价税合计` 时整条丢弃 |
| 动手门槛 | **仅**当 `tax_cents == 0 && total_cents == net_amount_cents` 且不含税与票面一致；其它形态一律**报冲突、不猜** |
| 凭证拒绝 | 非「一借一贷」/ 含 `2221` / 已 `posted` / 分录金额与发票原额不符 → 全部拒绝并报告 |
| 备份 | SQLite **在线备份 API**（WAL 下 `copy2` 不可靠）+ `integrity_check` + 5 张关键表行数校验 |
| 事务 | `BEGIN IMMEDIATE` + 写前 TOCTOU 复核 + 改后借贷自校验；任一步失败**整体回滚** |
| 留痕 | 每处改动一条 append-only `audit_event`（含前后值、证据来源、`tool` 名不混用） |
| 幂等 | 现值已等于真值 → 跳过；复跑输出 `[无需处理]` |

执行证据（生产库）：备份 `finance.db.fixamounts-20260911-010500.bak`（240 KB）；
`audit_event` id **43~47**；`voucher 15` 调整后借贷 **9974 = 9974**；全库无失衡凭证。
独立复核：`pytest -q` → **164 passed**；工具复跑 → `[无需处理]`（已收敛）。

> 🔴 新增踩坑（已写入技能）：`summarize()` 早年让 `金额` 字段**兼职两种含义**
> （`价税合计 or 金额合计`），且 summary 写 `税额`、下游却找 `税额合计` —— key 名不匹配
> 让进项税**静默归零**。教训：一个字段只承载一种含义；跨模块字段名必须对齐，
> 金额类字段一律用「三件套 + 自校验（不含税+税额=价税合计）」兜底。

---

### 10.3 163 历史发票补收（✅ 全量完成：63 张台账；外部 OCR 缺陷已根治）

**工具**
- `tools/backfill_history_invoices.py` —— 补收（默认只读；`--apply` 才写库）
- `tools/audit_history_invoice_ocr.py` —— OCR 质量体检（只读）
- `tools/invoice_ocr_probe.py` —— 共用的 OCR 探测/分级模块
- **测试**：`tests/test_backfill_history_invoices.py`（34 例）

#### 一、为什么会有「历史发票收不到」

邮箱水位线 `last_uid` **只增不减**，且当前是 `1691478373` = INBOX 倒数第二封
（UID 范围 1691477904~1691478374）。`fetch_attachments` 在 `last_uid > 0` 时只查
`uid = last_uid+1 : *`，于是 **66 封历史发票邮件（2023-09 ~ 2026-09）对正常增量轮询永久不可见**。
实测：候选 66 封，其中 `> 水位线` 的 **0 封**，`<= 水位线`（需补收）的 **66 封**。

> 🔴 **补收工具绝不修改 `last_uid`**。补收是**旁路**：若把水位线降到历史区间再处理，
> ①30 分钟轮询线程会立刻把 66 封一起收进来（就不是"试收 3 封"了）；
> ②只降一部分还会让老邮件被永久跳过。所以补收**不碰水位线**，幂等性靠
> `register_invoice` 的**发票号唯一约束**保证（重复票返回 `BUSINESS_REJECTED`，不写坏数据）。

> ⚠️ **顺序陷阱**：补收必须**从最旧开始**（`--order oldest` 为默认）。若从最新往回收
> 并推进水位线，中间的老邮件会被永久跳过。

#### 二、试收结果（2026-09-11 01:41，`--limit 3 --apply`）

| 项 | 结果 |
|---|---|
| 备份 | `finance-20260911-014108-425256.db`（在线备份 API + integrity_check） |
| 选中 | 3 封邮件（一嗨 2024-06-19 / 滴滴 2024-09-11 / 花小猪 2024-09-11） |
| 待入库发票 | **8 张，全部 SUCCESS**（0 重复、0 失败） |
| 预筛跳过 | 7 个行程单/结算单（辅助凭证，不送 OCR、**不产生 `invoices/failed/` 占位文件**） |
| 发票总数 | 6 → **14 张**；价税合计 ¥1,461.36 → **¥2,672.42** |
| 归档 | `invoices\archive\2024-06\`（2）+ `2024-09\`（6），命名 `<20位票号>_<原名>` |
| 凭证 | **13 张不变** —— 只写发票台账（`status='待确认'`），**不自动生成凭证、不影响账务**（注：`13` 为本次操作**当时**的凭证数；2026-09-11 02:34 删除废弃草稿 id 11 后现为 **12**，与补收无关，见 §11.2） |
| 留痕 | `audit_event` id **48~55**（`invoice_backfilled`，含 mail_uid/主题/附件名/票号） |
| 独立复核 | 8 张票**购买方全部为「高迥（杭州）贸易有限责任公司」+ 正确税号**；净额+税额=价税合计自洽 |

> ℹ️ 首次试收时工具尚未实现审计写入，id 48~55 系事后补录（`details_json` 里标了
> `recorded_retroactively: true` + `ingested_at`）。**现已内置**：`record_backfill_audit()`
> 在每次入库成功后写一条 `invoice_backfilled`。

#### 三、🔴 全量补收的真实障碍：OCR 识别能力不足

体检全部 110 个附件（`audit_history_invoice_ocr.py`）：

| 附件性质 | 数量 | 说明 |
|---|---|---|
| 发票类 | **73** | 其中 ✅完整可入库 **34（47%）**、⚠️部分缺失 15（21%）、❌基本抽不到 23（32%）、🔥错误 1 |
| 辅助凭证（行程单/结算单等） | 37 | 本就不含发票字段，**不该算识别失败** |

按年份看发票类完整率：**2023 年 0/7 · 2024 年 10/46 · 2025 年 11/27 · 2026 年 13/30**。

**三类失败（`_source` 字段可区分根因）**：

1. **同程系（2023–2024，`_source=textlayer`）** —— 最典型也最可修。
   PDF 文本层是**乱序**的（先输出所有"值"、再输出所有"标签"）：
   ```
   第  8 行:  91320105MA1MXUCP1F      ← 南京同游天下（销售方）税号
   第  9 行:  91330105MACRGYRN32      ← 高迥杭州（购买方）税号
   第 23 行:  高迥（杭州）贸易有限责任公司   ← 购买方名称
   第 24 行:  南京同游天下汽车租赁有限公司   ← 销售方名称
   第 28 行:  发票号码:                ← 只有标签，值在第 1 行（661829583058）
   第 16 行:  ¥ 146.27                ← 价税合计
   ```
   → ①`发票号码[:：]数字` 正则抽不到（值/标签分离）；
   ②金额正则同样失效；③**最危险**：`extract_fields` 的乱序兜底假设
   **名称与税号的出现顺序一致**（都购买方在前），但此票**名称是购买方在前、税号是销售方在前**
   → **购买方/销售方税号张冠李戴**（输出"购买方名称=高迥 + 购买方税号=南京同游"）。

2. **滴滴 `DZFP_240_*.pdf`（2024-09-11，`_source=textlayer`）** —— 同样文本层抽不到票号/日期/金额
   （买卖方正常）。形态与①类似，大概率一并可修。

3. **`invoice_2.pdf`（2025–2026）** —— ~~文本层为空，走图像 OCR 兜底仍全无字段，属扫描件/图片型 PDF~~
   🔴 **此判断是错的，已在 §六 纠正**：该文件**有完整文本层**，其内容是
   **「费用结算单 / Expense Statement」**（租车结算单，含姓名/取还车时间/车牌），
   **本来就不是增值税发票**，抽不到发票字段是**正确拒绝**而非识别失败。
   原文判成"图像型 PDF"是因为旧 `_source` 判定用了 `"发票号码" in blob`，
   结算单里没有这四个字 → 被误标成 `ocr`。新版已把 `_source` 改为按**文本层是否为空**判定。

4. **噪音**：`logo2x.png`（邮件签名里的 logo 图片）被当作附件抓取，OCR `exit 1`。
   `allowed_ext` 含 `.png` 是必要的（手机拍票），但对**小尺寸签名图**应加过滤。

> ⚠️ **下游护栏救了这批数据**：`normalize_invoice` 要求 `发票号码`/`开票日期` 非空、
> 且 `净额+税额==价税合计`，否则 `raise ValueError` → `BUSINESS_REJECTED`。
> 所以**残缺票不会入库**（实测：若不做预筛直接 `--limit 5 --apply`，
> 前 6 封会全部被拒、一张票进不来）。**这正是"试收"要先做 OCR 预筛的原因。**

#### 四、应对：补收工具内置 OCR 预筛

`--limit N` 的语义是「收 **N 封真的能入库**的邮件」。工具按 uid 顺序**逐封探测 OCR**，
累积"**至少有一个非辅助附件的判定为 OK**"的邮件，凑够 N 封即停（不做全量扫描，通常只探测十几封）。
`--no-probe` 可关闭。

所以试收命令是：`python tools/backfill_history_invoices.py --limit 3 --apply`

#### 五、下一步决策（已选 A 并执行完毕）

| 选项 | 做法 | 能收多少 | 成本 |
|---|---|---|---|
| **A（推荐）先修 OCR 再全量补收** ✅ **已选并完成** | 改 `C:/Users/li1/invoice-agent/ocr_invoice.py`，改用**坐标锚点**抽取（详见 §六） | 34 → **64 张（+88%）** | 改外部项目 + 全量回归 |
| ~~B 先收能收的~~ | `--limit 50 --apply`，预筛自动跳过不可识别的 | 34 张（47%） | — |
| ~~C 全量硬收~~ | `--no-probe --limit 50 --apply` | 仍只 34 张 + ~39 个 `failed` 占位文件 | 不推荐 |

#### 六、✅ OCR 根治（P9）：把「按文本流顺序」换成「按版面坐标」

**修复对象**：`C:/Users/li1/invoice-agent/ocr_invoice.py`（外部项目，无 git，改动前已备份
`ocr_invoice.py.bak-20260911-015923`）。

**为什么必须改坐标**：这些 PDF 的文本层是**乱序**的（先吐所有"值"、再吐所有"标签"），
所以任何「按出现顺序配对」的写法都不可靠 —— 实测同一张票里**名称是买方在前、
税号却是卖方在前**，顺序假设必然导致张冠李戴。

**票面不变量（2023–2026 各类版式实测一致，票面全国统一）**：

| 锚点 | 事实 | 用途 |
|---|---|---|
| 标签同行 | 「发票号码:」「开票日期:」与其**值在同一 y、值在标签右侧** | 定位票号/日期 |
| 三栏同行 | **金额 \| 税率 \| 税额** 三栏同一 y | 定位整行后按 x 取净额/税率/税额；`*`/免税 ⇒ `tax=0` |
| 区域分离 | 购买方信息区与销售方信息区**空间分离**（老式票上下分区、数电票左右分栏） | 名称/税号按坐标二分配对 |
| 大写金额 | 「壹佰肆拾陆圆贰角柒分」= 146.27，是**票面权威值** | 独立校验 + 定 total |

**实现要点**：
- 新增 `cn_upper_to_num()`（中文大写金额解析）、`_nearest_by_label()`（同行右侧锚点）、
  `_split_by_layout()`（按 x/y 跨度大的轴二分，自动适配上下分区/左右分栏）、
  `amount_triple_from_row()`（由「税率」表头定位整行）。
- 金额改为**六级优先级**：坐标三元组自洽 → **大写金额校正 total** → 纯数值自洽三元组
  → 大写金额单锚 → 免税票 `net=total,tax=0` → 税率反推。
- **兼容性**：`extract_fields(blob)` 单参数调用仍可用（图像 OCR 路径退化为纯正则）；
  `process_pdf(path)` 兼容；CLI 的 `<文件>` / `<目录> --out` 契约不变（`mail_processor`
  以位置参数调用，未受影响）。
- 新增两个**可选**开关：`--company-tax-no`（本企业税号，仅作买卖方翻转兜底；
  **不传也已 100% 正确**）、`--self-check <DIR>`（回归自检）。
- 顺手修掉老版一个瑕疵：`开票人:` 的 `\s*` 跨行匹配会误抓到下一行的「销售方:(章)」，
  改为不跨行 + 排除栏目词，取不到就不输出（不猜）。

**验证（`work/ocr_samples/` 109 个真实附件，已导出 + `baseline.json` 基线）**：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 发票类附件可入库 | 34 / 72（47%） | **64 / 72** |
| 真发票通过率 | — | **64 / 64 = 100%** |
| 不可入账的 8 个 | — | 全是**非发票**（7×费用结算单 + 1×运单明细），**正确拒绝** |
| 大写金额独立校验 | — | **64 / 64 一致**（用票面大写金额交叉验证 total） |
| 买卖方错配 | 有（同程系税号互换） | **0**（购买方税号全部 = 本企业） |
| 老版通过的 17 张 | — | **数值完全一致，零回退** |
| 2023 年完整率 | 0 / 7 | **全部可入库** |
| 纯图片型文件 | 0 个（原以为有 7 个） | 同样 0 个 —— 109 个附件**全部有文本层** |

复现命令：`C:/tmp/ocr_venv2/Scripts/python.exe C:/Users/li1/invoice-agent/ocr_invoice.py --self-check C:/Users/li1/fin-system/work/ocr_samples --company-tax-no 91330105MACRGYRN32`

#### 七、✅ 全量补收执行结果（2026-09-11 02:02，`--limit 50 --apply`）

| 项 | 结果 |
|---|---|
| 备份 | `finance-20260911-020234-444259.db`（在线备份 API + integrity_check） |
| 处理 | 49 封邮件 / 提交 64 个附件 |
| 新入库 | **49 张**（含审计留痕 49 条） |
| 重复/业务拒绝 | 15（8 张为试收已入 + 7 张同票重复附件） |
| 预筛跳过 | 45（行程单/结算单/结账单等辅助凭证，**不产生 `failed` 占位文件**） |
| 失败 | **0** |
| 台账规模 | 14 → **63 张**；价税合计 ¥2,672.42 → **¥13,745.20** |
| 凭证 | **13 张不变**（补收只写发票台账，**不生成凭证、不影响账务**）（注：`13` 为本次操作**当时**的凭证数；02:34 删除废弃草稿 id 11 后现为 **12**，与补收无关，见 §11.2） |
| 审计 | `invoice_backfilled` 累计 **57 条**（id 48~55 试收 + 本次 49 条） |
| 覆盖期间 | 2023-11 ~ 2026-09（10 个月度） |

**入库后质量核查（全部通过）**：

| 检查项 | 结果 |
|---|---|
| 购买方税号 ≠ 本企业 | **0** |
| 销售方税号 = 本企业（错配） | **0** |
| 购买方/销售方税号为空 | **0 / 0** |
| `file_path` 为空 / 指向文件缺失 | **0 / 0** |
| `净额 + 税额 ≠ 价税合计` | **0** |
| 重复发票号 | **无** |
| 发票号长度分布 | 8 位 14 张（老式票）+ 20 位 49 张（数电票）—— 老式 8 位票号也被正确抽取 |
| 金额区间 | ¥3.00 ~ ¥2,501.30，均值 ¥218.18 |
| 销售方抽样 | 同程、滴滴各分公司、一嗨、曹操(优行)、如祺(祺宸)、中国移动辽宁、顺丰、耀东方、哈啰、华住 —— 全部为真实票务/出行/通信主体 |

> ℹ️ `partner` 档案仍为 2 行 —— 补收**只入发票台账**，不自动建供应商。
> 后续要生成凭证时，按既有流程先在「往来」页建供应商（`invoice_to_voucher` 要求 `partner_id` 非空）。

---


- 邮箱密码/授权码、飞书 secret —— **只经 DPAPI 加密**存 `credentials.json`，**绝不落明文、绝不进仓库/二进制**。
- `config/feishu.json`、`db/*.db`、`data/`、`release/` 均已 gitignore。

---

## 11. 残留清理（2026-09-11 02:30~02:34，已执行）

清理原则：**隔离而非删除** —— 文件一律 `move` 到带时间戳的隔离区 + `manifest.json`（原路径/新路径/大小/SHA256），可整体还原；**已记账凭证一律不碰**。

### 11.1 文件类：10 个 → 已移入隔离区（未删除）

隔离区：`%LOCALAPPDATA%\FinanceSystem\backups\cleanup-20260911-0230\`
DB 快照：`backups\finance-cleanup-20260911-0230.db`（SQLite 在线备份 API，`integrity_check=ok`）

| 来源 | 内容 |
|---|---|
| `invoices\failed\` | 6 个：3× 25 字节占位符（`failed_invoice_1785993548/598/608.pdf`）+ 2× 一嗨真实发票副本 + 1× 结算单 |
| `invoices\archive\2026-09\` | 2 个**孤儿副本**（无 epoch 后缀那两份） |
| `release\` | `frozen-self-check.json`（2026-07-11 旧构建残留，335 B） |
| `config\` | `feishu.app_secret.blob`（旧版密钥存储，262 B） |

**复验**：`invoices\failed` 剩 **0 个**；台账 **63/63 个 `file_path` 全部仍在盘上**；`pytest` → **205 passed**。

彻底删除（确认无需还原后）：
```powershell
Remove-Item -Recurse "$env:LOCALAPPDATA\FinanceSystem\backups\cleanup-20260911-0230"
```

### 11.2 凭证类：删 1 条、留 6 条

**✅ 已删除：`voucher` id 11** —— `draft`「冲销 2026-07-0001：123」，冲销**真实**的股东投资 ¥200,000，从未记账，纯残留。
- 删除 1 行 `voucher` + 2 行 `voucher_line`（`1002` 贷 200,000 / `3001` 借 200,000）
- 审计：`audit_event` id **105** `voucher_draft_deleted`
- 备份：`backups\finance-delvoucher11-20260911-023402.db`
- 结果：`voucher` **13 → 12**（`posted` 11 + `pending_review` 1）；`posted` 数量、借贷平衡、发票台账（63 张 / ¥13,745.20）**全部未变**
- ⚠️ 副作用：释放了 `idempotency_key='reversal:1'`（唯一索引 `ux_voucher_source_idempotency`）→ 日后可重新对凭证 1 发起冲销（这是**期望行为**）

**🔴 有意保留：`voucher` 12 / 13 / 14**（`posted`「冲销 …：test data cleanup」）。**它们不该删**：
1. DB 触发器 `posted_voucher_no_delete` 硬拦，实测直接 `RAISE(ABORT,'posted voucher is immutable')` —— 绕过它 = **篡改已记账凭证**，不是清理；
2. 它们对冲的 5/6/7 也是 `posted`（`5602 飞书审批测试 888.88` / `1401 测试费用 199.00` / `1401 测试费用无科目 77.00`，合计 **¥1,164.88**）。**只删冲销项 = 让测试费用复活进账**，凭空多出 ¥1,164.88 费用 + 等额应付；
3. 六张两两对冲、**账是平的**，留着只是台账里多几行 —— 成本远低于篡改风险。

### 11.3 ⚠️ 一处方向性误判（已纠正）

早前记录写「`archive\2026-09\` 里带 epoch 后缀的 PDF 是重复副本，可清」——**恰好相反**：

| 文件名形态 | 实际身份 |
|---|---|
| `26337000000601987996_【一嗨-403.00元】…(1)_1789029248.pdf` | ✅ **正本** —— `invoice_doc.file_path`（id 10/11）指向它 |
| `26337000000601987996_【一嗨-403.00元】…(1).pdf` | 🗑️ **孤儿** —— 回填时新落盘，无任何行引用 |

**教训**：判副本前必须先 `SELECT file_path FROM invoice_doc` 建引用集合逐个比对，**别按文件名形态猜**。照旧记录执行会打断 2 条台账的文件引用。

### 11.4 清理后复验（全绿）

| 项 | 结果 |
|---|---|
| `invoices\failed` | 0 个 |
| 台账 `file_path` 引用完整性 | 63/63 存在 |
| `voucher` | 12（posted 11 / pending_review 1） |
| 借贷平衡（全部 `posted` 凭证） | 0 张失衡 |
| 发票台账 | 63 张 / ¥13,745.20（未变） |
| `pytest` | **205 passed** |

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
| 重新打包 | `build.ps1 -PythonPath "C:\Users\li1\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe"` |
| **验证 exe 里是否含某改动** | `tools/verify_release_packaging.py <exe> <临时目录>`（**别用 grep**，PYZ 是压缩的） |
| 回补发票原件关联 / 公司抬头 | `tools/backfill_business_data.py`（默认只读，`--apply` 才写） |
| 修正发票金额口径 | `tools/fix_invoice_amounts.py`（默认只读，`--apply` 才写） |
| **补收历史发票**（水位线之外的邮件） | `tools/backfill_history_invoices.py`（默认只读；`--limit N --apply` 才写；**绝不改 `last_uid`**） |
| 体检 OCR 识别质量 | `tools/audit_history_invoice_ocr.py`（只读，出 JSON 到 `logs/`） |
| **OCR 回归自检**（改了外部 OCR 后必跑） | `C:/tmp/ocr_venv2/Scripts/python.exe C:/Users/li1/invoice-agent/ocr_invoice.py --self-check "C:\Users\li1\fin-system\work\ocr_samples" --company-tax-no 91330105MACRGYRN32` ⚠️ **目录参数必须写 Windows 路径**；填 `/c/Users/...` 不报错但输出「样本 0 张」（静默假阴性）。基线：`样本 109 张：可入账 64 / 不可入账 45（其中无文本层 0）` |
| **残留隔离区 / 还原** | `%LOCALAPPDATA%\FinanceSystem\backups\cleanup-20260911-0230\`（含 `manifest.json` + SHA256，可整体还原；见 §11） |
| 清理后的 DB 快照 | `backups\finance-cleanup-20260911-0230.db`（清理前）／`backups\finance-delvoucher11-20260911-023402.db`（删凭证 11 前） |
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
| 凭证数 | 12 | `voucher` = **12**（11 posted / 1 pending_review）；2026-09-11 02:34 删除废弃草稿 id 11 后由 13 降为 12 | ✅ |
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

**遗留风险（截至 2026-09-11 02:34）**：

| # | 项 | 状态 |
|---|---|---|
| 1 | `company` 表全空 → 报表抬头公司名为空 | ✅ **已修**（`21ed316` 工具已执行，抬头三字段写入） |
| 2 | `partner.type` 中文「客户」/ 英文 `supplier` 混用 | ℹ️ 代码已兼容 `type IN (?,?)`，**非 bug** |
| 3 | 6 张发票 `file_path` 空 → 与原始 PDF 无关联 | ✅ **已修**（6/6 归档落地，逐行 `os.path.exists` 复核） |
| 4 | `invoices\failed\` 残留 + 3 条 `test data cleanup` 冲销凭证 | ✅ **文件 6 个已清入隔离区**（2026-09-11 02:30）；凭证 12/13/14 **有意保留**（posted + 两两对冲，删了会让测试费用复活 ¥1,164.88）；废弃草稿 id 11 **已删**。详见 §11 |
| 5 | `release\frozen-self-check.json` 是 2026-07-11 旧文件 | ✅ **已清入隔离区**（2026-09-11 02:30） |
| 6 | 配置双份：`settings.json` 的 `feishu` 段与 `feishu.json` 重复 | ⚠️ **优先级与早前记录相反**：`modules/feishu.py` 先读 `feishu.json`、再用 `settings.json.feishu` **覆盖**（`:63/80/89`）→ **实际生效的是 `settings.json`**。未去重（低收益高风险） |
| 7 | 对外开票要素不全：`bank_account` 0 行、`company` 无电话 | ℹ️ 只做进项不受影响；将来要开票需补 |
| 8 | ~~**根因未修**：`summarize()` 丢字段 → 新收发票 `file_path` 依旧为空~~ | ✅ **已修**（`1cf2be2`，①~⑤ 全部落地 + 18 例回归） |
| 9 | ~~财务系统**没有"成功归档"目录**，OCR 成功的 PDF 从未落盘~~ | ✅ **已修**（`1cf2be2`，新增 `invoices\archive\<月份>\` 并在入库时归档） |
| 10 | ~~🔴 **新发现**：id=6~9 的 `tax_cents`/`total_cents` 漏计进项税（合计 ¥24.53）；id=9 已生成待审核凭证、应付账款同样少记 2.91~~ | ✅ **已修**（`26f2fb5` 工具已执行：4 张台账 + 凭证 15，二审借贷平衡，见 §10.2） |
| 11 | ~~⚠️ `release\财务系统\` 仍是 P1/P3 时的构建，**不含 P6 归档逻辑**~~ | ✅ **已重打包**（`01:12`，`build.ps1` 六步全过；拆包实测 P6 符号全命中，见第 9 节） |

**P5/P6/P7 实机复核（2026-09-11）**：

| 核验项 | 实测 | 结论 |
|---|---|---|
| P5 写库已执行 | 6 行 `file_path` 全部指向真实存在的归档文件（逐行 `os.path.exists` → 6/6 EXISTS） | ✅ |
| P5 公司抬头 | `company` = 高迥（杭州）贸易有限责任公司 / 91330105MACRGYRN32 / 浙江省杭州市拱墅区东新街道东方茂商业中心1幢3层4062室 | ✅ |
| P5 审计留痕 | `audit_event` id 34–42：6 条 `invoice_file_linked` + 3 条 `company_field_updated` | ✅ |
| P5 备份可恢复 | `finance.db.backfill-20260911-004425.bak`（在线备份 API + `integrity_check` + 行数校验） | ✅ |
| P5 拒绝垃圾文件 | 3 个 25 字节 `failed_invoice_*.pdf` 占位符 + 1 份结算单被正确忽略 | ✅ |
| P6 金额口径自证 | 四张滴滴票 `不含税/价税合计` ≈ 0.9709（3% 征收率），与 OCR 抽取字段吻合 | ✅ |
| P6 归档不覆盖 | 同票命名但内容不同 → `(2)` 后缀；已有归档字节未变 | ✅ |
| P6 归档不阻断 | 归档失败降级沿用原路径（开关 + 异常路径均有用例覆盖） | ✅ |
| P6 原件保全 | 邮件链临时文件删除前已复制归档（端到端用例断言目标文件存在） | ✅ |
| P6 变异验证 | 移除归档调用或 `source_file` 传递 → 4 例失败；还原后 18 例全绿 | ✅ |
| **P7 写库已执行** | 4 张发票 `tax_cents`/`total_cents` 按票面真值补齐：6→60885 / 7→905 / 8→12376 / 9→9974 | ✅ |
| **P7 凭证同步** | voucher 15：`借 5602` 与 `贷 2202` 均 9683 → **9974**，借贷平衡 9974 = 9974 | ✅ |
| **P7 全库平衡** | `HAVING SUM(debit_cents) <> SUM(credit_cents)` → 无失衡凭证 | ✅ |
| **P7 审计留痕** | `audit_event` id 43–47：5 条（4 台账 + 1 凭证），含前后值与证据来源 | ✅ |
| **P7 备份可恢复** | `finance.db.fixamounts-20260911-010500.bak`（在线备份 API + `integrity_check` + 5 表行数校验） | ✅ |
| **P7 幂等** | 复跑 dry-run → `[无需处理]`，无二次写入 | ✅ |
| **P7 变异验证** | 去掉"缺陷形态门槛" / "已记账拒绝" / "2221 拒绝" → 对应用例失败 | ✅ |
| **重打包** | `build.ps1` 六步全过 `BUILD_EXIT=0`；exe `01:12` / 8,863,479 B | ✅ |
| **打包内容验证** | PYZ 解包：P6 全部符号命中（`_archive_invoice_file` 等）；负对照注入假符号必被报出 | ✅ |
| **发布完整性** | `manifest.sha256` 999 项；exe 哈希 `09b7332…2a3cd4` 与清单一致 | ✅ |
| **全量回归** | `pytest tests -q` → **171 passed**（130 → +34 P7 → +7 打包验证器） | ✅ |

