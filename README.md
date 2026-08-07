# 财务系统（可靠单机版）

本项目使用 Python 3.11、Tkinter 和 SQLite。程序文件与用户数据严格分离：源码或发布目录只保存程序，账套、报表、票据、日志、备份和本机配置统一保存在：

```text
%LOCALAPPDATA%\FinanceSystem\
├─ db\finance.db
├─ reports\
├─ invoices\
├─ invoices\failed\       <- OCR 失败的附件存档
├─ logs\
├─ backups\
└─ config\
```

自动化测试可在进程启动前设置 `FINANCE_SYSTEM_HOME`，把整套用户数据重定向到临时目录。

## 从源码启动

安装 Python 3.11 后双击 `启动财务系统.bat`，或执行：

```powershell
python .\run_finance.py
```

`run_finance.py` 是源码和打包程序共用的唯一入口。启动失败时，脱敏后的详细日志写入 `%LOCALAPPDATA%\FinanceSystem\logs`。

## 功能模块

### 1. 账套与凭证
- 预置《小企业会计准则》45 科目
- 复式记账凭证录入、审核、过账
- 资产负债表、利润表、现金流量表自动生成

### 2. 往来管理
- 客户/供应商管理
- 应收/应付账款登记与核销

### 3. 费用报销 + 飞书审批
- 报销单录入与费用明细
- 飞书审批集成（需配置 `feishu.json` + DPAPI 密钥）
- 审批状态自动回写

### 4. 费用票据 OCR
- 支持本地目录发票识别
- 调用 `invoice_agent_command`（配置在 `settings.json`）
- 识别后自动入库，状态为「待确认」

### 5. 邮件收票（新增）
- 配置多个企业邮箱（IMAP）
- 定时自动收邮件，提取发票附件
- 调用 OCR 识别，自动入库
- 支持 SMTP 发信
- 失败附件自动保存到 `invoices/failed/` 目录，支持重试

### 6. 系统托盘
- 关闭窗口最小化到托盘
- 托盘菜单：显示/隐藏/退出
- 后台持续运行，定时收件

## 配置说明

### 基础配置
编辑 `%LOCALAPPDATA%\FinanceSystem\config\settings.json`：

```json
{
  "invoice_agent_command": [
    "C:/tmp/ocr_venv2/Scripts/python.exe",
    "C:/Users/li1/invoice-agent/invoice_agent.py"
  ],
  "mail_poll_interval_minutes": 30,
  "mail_boxes": [
    {
      "name": "高迥贸易-主邮箱",
      "imap_host": "imap.qq.com",
      "imap_port": 993,
      "username": "a13757178707@agent.qq.com",
      "password_encrypted": true,
      "use_ssl": true,
      "enabled": true,
      "last_uid": 0,
      "folder": "INBOX",
      "subject_filter": "发票|电子发票|invoice",
      "allowed_ext": [".pdf", ".jpg", ".jpeg", ".png"],
      "smtp_host": "smtp.qq.com",
      "smtp_port": 465,
      "smtp_use_ssl": true
    }
  ]
}
```

密码通过 GUI 输入后自动加密存储到 `credentials.json`（DPAPI），不会明文保存。

### 飞书配置
1. 将 `config\feishu.example.json` 复制到 `%LOCALAPPDATA%\FinanceSystem\config\feishu.json`，填写非敏感标识。
2. 通过隐藏输入把密钥写入当前 Windows 用户的 DPAPI 仓库：

   ```powershell
   python .\security.py set feishu.app_secret
   ```

3. 用 `python .\security.py status feishu.app_secret` 检查是否已配置。

不要把 `app_secret` 写回 JSON、日志、命令参数或安装目录。

### OCR 配置
`invoice_agent_command` 指定 OCR 子进程命令，示例：

```json
[
  "C:/tmp/ocr_venv2/Scripts/python.exe",
  "C:/Users/li1/invoice-agent/invoice_agent.py"
]
```

OCR 失败的附件会自动保存到 `invoices/failed/` 目录，可手动重试。

## 隔离构建与发布

构建脚本创建临时 Python 3.11 虚拟环境，安装固定版本的 PyInstaller，生成 onedir 包并依次执行运行时自检、敏感文件/真实密钥扫描和 SHA-256 清单生成。全部成功后才原子发布到 `release\财务系统`。

```powershell
.\build.ps1 -PythonPath "C:\Path\To\Python311\python.exe"
```

第一次验证发布物时不改桌面快捷方式。确认通过后重新构建并显式更新：

```powershell
.\build.ps1 -PythonPath "C:\Path\To\Python311\python.exe" -UpdateDesktopShortcut
```

任何构建或自检失败都不会覆盖已发布版本，也不会改桌面快捷方式。发布目录不得出现 `finance.db`、`feishu.json`、`credentials.json`、`.env` 或临时虚拟环境。

## 验证

```powershell
python -m pytest
python .\run_finance.py --self-check --self-check-report .\self-check.json
```

自检需要数据库层同样使用 `app_paths.DB_PATH`；检测到数据仍写入安装目录时会失败。

## 安全说明

- 所有敏感密钥（飞书 app_secret、邮箱密码）均通过 Windows DPAPI 加密存储
- 配置文件和日志中不会出现明文密钥
- 数据库文件（`finance.db`）存储在 `%LOCALAPPDATA%`，不受版本控制
- 打包发布前会自动扫描并排除敏感文件

## 技术栈

- **语言**: Python 3.11
- **GUI**: Tkinter + ttk
- **数据库**: SQLite3
- **ORM**: 原生 SQL（无外部 ORM）
- **审批**: 飞书开放平台 API
- **OCR**: 外部子进程（PaddleOCR）
- **邮件**: imap-tools + smtplib
- **打包**: PyInstaller (onedir)
