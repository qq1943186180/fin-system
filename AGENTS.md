# AGENTS.md — 财务系统（可靠单机版）

> 任何 Agent 进入本仓库的**第一个入口**。开工先读这里，收工必须写回 `HANDOFF.md`。

## ⚠️ START HERE

1. `git status --porcelain` —— 有未提交改动？先弄清是什么、复核、落库，再接新任务。
2. 读根目录 **`HANDOFF.md`**（本项目唯一权威交接文档，14 节，含核验记录）。
3. 有 Linear Issue 时先读完整目标与验收条件（team `194`）。
4. 只改本任务涉及的文件，**不 `git add .`**。

## 事实源

- 代码根：`C:\Users\li1\fin-system`（分支 `master`，**无 origin remote** —— 纯本机仓库，不经 GitHub）
- 运行数据：`%LOCALAPPDATA%\FinanceSystem\`（`db\finance.db`、`reports\`、`invoices\`、`logs\`、`backups\`、`config\`）
- 单一入口：`run_finance.py`（源码与打包程序共用）；Windows 双击 `启动财务系统.bat`
- 使用方：高迥（杭州）贸易有限责任公司（税号 91330105MACRGYRN32）——这是生产账套，**不是玩具**。

## 🔴 安全边界（不可自动化）

- **数据不出本机**：无 Docker、无 Web 服务、无云端同步。任何"上传/联网"改动都必须先说明理由。
- 程序文件与用户数据**严格分离**：源码/发布目录只放程序，账套/报表/票据/日志/备份全部在 `%LOCALAPPDATA%\FinanceSystem\`。
- 客户端加密 / 飞书审批密钥走 **DPAPI**（`feishu.json`），**不得**明文落库或提交。
- 账套数据的删除、过账、期末结转等**破坏性操作**需用户显式确认。
- 测试前用 `FINANCE_SYSTEM_HOME` 把用户数据重定向到临时目录，**不得**触碰真实账套。

## 验证

```powershell
python -m pytest -q          # 或
python -m pytest tests -v
```

## 收工

1. 在 `HANDOFF.md` 追加 / 刷新对应章节（**顶部事件区只增；「当前状态」类章节必须刷新到此刻真实值**）。
2. 有 Linear Issue 则回写状态并读回确认。
3. 确认 commit 真落库 —— **没有 HANDOFF 条目的改动 = 未完成**。

## 约定

- 不重写历史；本仓无 remote，**不需要** push。
- 跨工具共享记忆：`~/.workbuddy/SHARED.md`（真源 `~/.workbuddy/MEMORY.md`）。