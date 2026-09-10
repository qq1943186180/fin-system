# 财务系统 · 交接说明（HANDOFF）

> 单一公司账套的本地桌面财务系统。纯 Python + SQLite + tkinter，**无 Docker、无 Web 服务、数据不出本机**。
> 最后更新：2026-09-10 · 版本 1.0.0 · 分支 `master` · 最近提交 `99b397e`

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
  main.py             # 主窗口 + 全部页面（导航/建表/账簿/报表/往来/报销/票据/设置）
  ui_theme.py         # 浅色商务风主题（clam）
  page_mail.py        # 邮件收票页
modules/
  ledger.py           # 复式记账内核（凭证头+多行分录，借贷校验）
  reports.py          # 资产负债表/利润表/现金流量表/科目余额表/序时簿
  arap.py             # 往来：客户供应商档案 + 应收应付台账
  expense.py          # 报销：员工 / 报销单（挂发票）/ 审批自动记账
  invoice_gui.py      # 票据：发票确认 → 生成凭证
  feishu.py           # 飞书审批（创建实例 + 轮询状态）
  mail_service.py     # IMAP 收件 / SMTP 发信封装
  mail_sync.py        # 后台轮询线程（去重 + 水位线）
  mail_processor.py   # 附件 → OCR → 入库；失败归档可重试
  export_service.py   # CSV/Excel/PDF 导出
db/                   # schema + 《小企业会计准则》科目表预置
tests/                # pytest（10 个测试文件）
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
invoices\           # 收到的附件
invoices\failed\    # OCR 失败的待重试附件（GUI 有重试入口）
reports\ logs\ backups\
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

- 当前基线：**pytest 41 passed** / 验收 **17 OK、0 FAIL**。
- 打包后自检：`release\财务系统\财务系统.exe --self-check report=<路径> stderr=<路径>`（**不带参数跑是空输出、exit 0，属正常**；带参才写 `self-check.json`）。

---

## 8. 环境依赖

- Windows 11，Python **3.11.15** 位于 `C:\tmp\ocr_venv2`（含 paddleocr / paddlepaddle / pymupdf / tkinter / Pillow / openpyxl / fpdf2 / imap-tools / pystray）。
- 运行时依赖：`imap_tools, pystray, openpyxl, fpdf2`。
- 构建依赖（可选组 `build`）：`pyinstaller==6.21.0` 等。

---

## 9. 当前状态（截至 2026-09-10，均已实测）

- **阶段 0–5 全部完成**：骨架 / 记账内核 / 报表 / 往来 / 报销 / 票据+飞书+邮件收票。
- 已打包 onedir **真 exe**，真机冒烟通过；UI = 浅色商务风（`clam` 主题，含表格斑马纹）。
- 账套实际数据：科目 46 · 凭证 13 · 往来 2 · 期间 **2026-07**。
- 发票库存：**共 6 张，价税合计 ¥1,436.83**（待确认 5 张 ¥1,340.00 + 待审核 1 张 ¥96.83）。
- 邮箱：163 **已启用**（水位线 `last_uid=1691478373`）；QQ 已禁用。

---

## 10. 待办 / 明确不做

- [ ] **「全部补收进来」**：163 历史发票补收（约 60 封）—— 前置修复已就绪，**尚未执行**（会真实连邮箱、真实写库）。
- ❌ 报税/税务申报：明确**不做**。
- ❌ 多账套切换：明确**不做**（单公司账套）。

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
