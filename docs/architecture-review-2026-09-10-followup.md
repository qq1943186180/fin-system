# 财务系统 架构评审续篇（增量）

| 项 | 值 |
|---|---|
| 前序评审 | `docs/architecture-review-2026-08-08.md`（高见远，基准） |
| 本次评审 | 2026-09-10 |
| 评审人 | WorkBuddy |
| 评审方式 | 只读静态分析 + 只读打开生产库（`mode=ro`）+ 实跑 pytest 与验收脚本 |
| 代码规模 | 12,336 行（22 源文件 + tests + tools） |
| 首轮交付 | 仅新增本报告，未改业务代码 |
| 后续修复 | 2026-09-10 晚按 §4 动手，**P1 已修复并提交**（`3d27438`），详见 §2.1「修复记录」 |

> 定位：**不是重新评审，而是验证前序结论 + 记录新增量。**
> 前序评审的 §5「不要做的事」已明确否决拆 `gui/main.py`、拆 `db/__init__.py`、引入 ORM、补 CI、重写 `build.ps1`、迁移 GUI 框架 —— **本次完全认同，不重复提出**（单用户单机、日均个位数凭证，那些动作纯亏）。

---

## 0. 一句话结论

**前序评审列的 TOP 5 技术债，现已全部关闭；代码库处于健康状态。**
本报告列的 5 项改进**已全部处理完毕**：P1（备份保留窗口偏短 + 备份失败静默）`3d27438`、
P2（`.gitignore` 纵深防御）`d75c8d2`、P3（`configure_dark_style` 改名）`1051a1b`、
P4（依赖三处一致性构建期门禁）`1039b2c`、P5（业务数据回补）`21ed316` + 2026-09-11 已对生产库执行。
其中 P1 修复过程中发现该缺陷比首轮报告**更严重**（见 §2.1）。

**P6（`1cf2be2`）已修复 `file_path` 恒为空的根因**：外部 agent `summarize()` 补齐
`file_path`/税号/金额三件套 + 财务系统入库时归档原件 + `load_invoices` 缺路径告警。

**收官时又挖出第 6 项、也是唯一一项「已污染生产数据」的问题** 🔴：id=6~9 四张滴滴发票的
`tax_cents` 为 0、`total_cents` 漏计进项税额，**合计少计 ¥24.53**；其中 id=9 已生成待审核凭证，
**应付账款同步少记 2.91**。

**P7（`26f2fb5`）已按确认口径修完并落到生产库**：滴滴开的是 3% 增值税电子普通发票、
进项税不可抵扣 → 成本费用按**价税合计全额**入账。4 张台账的 `tax_cents`/`total_cents` 按
`ocr_result.json` 票面真值补齐，凭证 15 的 `借 5602` / `贷 2202` 同步 96.83 → **99.74**
（借贷仍平衡），合计补计 ¥24.53，`audit_event` id 43–47，备份
`finance.db.fixamounts-20260911-010500.bak`。详见 §2.6。

**release 已重打包至含 P1/P3/P6/P7**（`build.ps1` 六步全过；exe `01:12`）。
⚠️ 同时纠正了一条**错误的验证方法**：早前"grep exe 找符号"的做法无效 —— PyInstaller 的
PYZ 是 zlib 压缩的，实测连早已存在的 `_BACKUP_NAME_RE` 也 grep 不到。现已用
`tools/verify_release_packaging.py` 解包判定，并配负对照。全量回归 **171 passed**。

> ⚠️ 首轮报告的严重度被低估了：原以为只是「窗口 5 天」，实测是「**实际完全没有保留策略**」——
> 日期前缀匹配恒失败，使「每天首启备份一次」退化成「每次启动都备份」，生产库 5 份备份**全是同一天**。

---

## 1. 前序技术债的关闭验证（实测）

| 前序编号 | 原问题 | 当前实测 | 结论 |
|---|---|---|---|
| **TD-1** | `C_PANEL` NameError，6 大功能页全挂 | `gui/ui_theme.py:47` 定义 `C_PANEL = C_CARD`；`gui/main.py:41` 已加入 import 列表 | ✅ **已修** |
| **TD-2** | 项目根无 `.git`，无法回滚 | `master` 分支，工作区干净，HEAD `ab44c39`（含本次核验提交） | ✅ **已修** |
| **TD-3a** | spec 入口是 `gui/main.py`，`--self-check` 会弹窗致构建挂起 | `财务系统.spec:5` → `['run_finance.py']`（带 argparse 的入口） | ✅ **已修** |
| **TD-3b** | 运行时依赖从未声明/安装，exe 静默缺库 | `build.ps1:224` 已装 `imap_tools pystray openpyxl fpdf2`；`pyproject.toml` 已声明 | ✅ **已修** |
| **TD-3c** | `self_check` 对 mail 模块是盲的 | `run_finance.py:53-65` 的 `required_modules` 已含 `mail_service`/`mail_sync`/`mail_processor`/`gui.page_mail` | ✅ **已修** |
| **TD-4** | 数据库无自动备份 | `gui/main.py` `_run_startup_backup()`：每日首启备份 + 分层滚动保留（首轮发现的缺陷已于 `3d27438` 修复，见 §2.1） | ✅ **已修** |
| **TD-5a** | `MailSyncEngine` 构造参数不匹配 | 调用点与签名已对齐 | ✅ **已修** |
| **TD-5b** | 附件静默丢弃 + 水位线照常前进 | `mail_processor.process_file` 已被接上；水位线推进规则已按「终态不阻塞、瞬时失败阻塞」重做 | ✅ **已修** |
| **TD-5c** | 明文密码将写入 `settings.json` | `MailBoxConfig.from_dict()` 恒置 `password=""`，明文不驻留；实测 `settings.json` 无 password 字段 | ✅ **已修** |
| **TD-5d** | SMTP 发信因未净化用户名必抛 `ValueError` | 收发两侧统一走 `username.replace('@','_at_').replace('.','_')` | ✅ **已修** |
| **§4.5** | 无 `conftest.py`，测试污染生产目录 | `tests/conftest.py` 已存在：import 前设 `FINANCE_SYSTEM_HOME` 到 `.test-home`，autouse fixture 断言 `db.DB_PATH != 真实库` | ✅ **已修** |

**质量门禁现状（实测）**：`pytest` **111 passed**（P1 后 52 → P2/P3/P4 后 83 → P5 后 111）；`tools/acceptance_check.py` **17 OK / 0 FAIL**（11 个页面全部构建成功）。
新增回归用例分布：备份保留 11 例（P1）、仓库卫生 10 例（P2）、UI 主题 13 例（P3）、依赖一致性 8 例（P4）、业务数据回补 29 例（P5）。

---

## 2. 新增发现（前序评审未覆盖）

### 2.1 🟠→✅ 备份：保留窗口偏短，且失败被静默吞掉（**已修复**）

**首轮发现（2026-09-10 白天，静态分析）**

| 项 | 内容 |
|---|---|
| **位置** | `gui/main.py` `_run_startup_backup()` |
| **现状①** | `MAX_BACKUPS = 5`，配合「每天首启备份一次」= **回溯窗口约 5 天** |
| **现状②** | 整个函数包在 `except Exception: pass` 里 —— **备份失败用户完全无感** |
| **为什么是问题** | (a) 财务场景的典型故障是「月底对账时发现月初记错」，而月初的备份**已被滚掉**。(b) 更糟的是：若备份因磁盘满/文件锁/权限失败，用户以为「系统每天自动备份」，实际一份都没有 —— **这是「以为有备份」型故障，比没有备份更危险**。 |

**修复记录（2026-09-10 晚，动手实施时发现严重度被低估）**

实施前先只读打开生产库旁的 `backups/` 复核，结果推翻了首轮的判断 —— **问题不是「窗口只有 5 天」，而是「日期匹配恒失败，实际根本没有保留策略」**：

```
C:\Users\li1\AppData\Local\FinanceSystem\backups\
  finance-20260910-222249-908177.db   ← 22:22
  finance-20260910-222655-467119.db   ← 22:26
  finance-20260910-222857-099618.db   ← 22:28
  finance-20260910-223005-310212.db   ← 22:30
  finance-20260910-223256-079905.db   ← 22:32
```

**5 份备份全是同一天**（当天连续 5 次启动）。根因：

| 环节 | 代码 | 生成/比对值 |
|---|---|---|
| 备份文件名 | `db.backup_database()` → `%Y%m%d-%H%M%S-%f` | `finance-20260910-222249-908177.db` |
| 「今天是否已备份」判定 | `date.today().isoformat()` | `finance-2026-09-10` |

`finance-2026-09-10` 与 `finance-20260910-...` **永不相等** → `existing_today` 恒为 `False` → **每次启动都新建备份**；再叠加 `MAX_BACKUPS=5`，同一天的多次启动就把历史备份全部滚掉。即：**保留窗口实际退化为「最近 5 次启动」**。此外 `MAX_BACKUPS` 的淘汰用的是 `*.db` 通配，会把迁移前备份 `finance-pre-*.db` 与 `source-pre-upgrade-*.zip` 一并计入甚至删除 —— 那是 schema 变更的安全网。

**修复内容（提交 `3d27438`）**

| # | 改动 |
|---|---|
| 1 | `_BACKUP_NAME_RE = ^finance-(\d{8})-(\d{6})-(\d{6})\.db$` —— 精确匹配自动备份固定命名；迁移前备份与手工备份**不参与轮转**（不再被误删） |
| 2 | 日期判定改为 `today_prefix = "finance-%s-" % dt.date.today().strftime("%Y%m%d")`，与文件名同构 → **真正一天一份** |
| 3 | 保留策略分层：`BACKUP_KEEP_DAYS=7`（近 7 天逐日）+ `BACKUP_KEEP_WEEKS=4`（第 8~35 天每周一份）+ `BACKUP_KEEP_MONTHS=6`（第 36 天~6 月每月一份），超出约 6 个月才淘汰 |
| 4 | 失败不再静默：捕获后记入 `self._backup_error` 并 `LOG.error(..., exc_info=True)`；新增 `_backup_status_text()` |
| 5 | 设置页新增备份状态行：展示「N 份 · 最近 YYYY-MM-DD HH:MM（每天首次启动时备份）」，**上次启动备份失败时整行标红** |
| 6 | 新增 `tests/test_backup_retention.py`（11 例）：锁定文件名正则（含不误伤迁移备份）、一天一份、失败可见、分层保留、周桶去重、空目录安全 |

**验证**：`pytest` 52 passed（原 41）；`acceptance_check.py` 17 OK / 0 FAIL；生产库只读复核仍为 46 科目 / 13 凭证 / 6 发票（未受影响）。

> 说明：`backup_database()` 本身的实现质量很高（`sqlite3` 原生在线热备 + 备份后 `integrity_check` 验证 + 失败自动 `unlink` 不留半成品）。**本次只改调用方（保留策略与失败可见性），未动 `backup_database()` 一行。**

### 2.2 ✅ `.gitignore` 纵深防御缺口（**已修复**，提交 `d75c8d2`）

| 项 | 内容 |
|---|---|
| **现状** | `.gitignore` 只忽略了 `config/feishu.json` |
| **实测缺口** | `config/credentials.json`、`config/settings.json`、`config/feishu.app_secret.blob` **均不会被忽略**（`git check-ignore` 实测） |
| **为什么是问题** | `credentials.json` 是**全部密钥的仓库**（邮箱授权码 + 飞书 secret 的加密 blob）。当前这些文件在 AppData（安全），但项目根**已存在 `config/` 目录且含 `feishu.json`** —— 说明「往项目根放配置」这个动作真实发生过。一旦有人调试时把 `credentials.json` 拷进来再 `git add .`，密钥仓库就进版本历史了。 |
| **工作量** | **S**（改动 1 行） |
| **建议** | 把 `config/feishu.json` 一行替换为 `config/*.json` + `config/*.blob`，再用 `!config/feishu.example.json` 放行示例文件（该文件已被 `test_packaging.py` 断言只含 `app_secret_ref` 不含明文）。 |
| **修复** | ✅ 已按建议改为类型覆盖：`config/*.json` + `config/*.blob` + `!config/feishu.example.json` + `db/*.db`。**规则写 `config/*.json` 而非 `config/`**——若忽略整个目录，`!` 放行规则会被父目录规则压掉，示例文件救不回来（这条坑已写进测试）。 |
| **回归** | `tests/test_repo_hygiene.py` 10 例：6 个必须被忽略（含"任意新增 `.json`/`.blob`"两个前瞻用例）、2 个必须放行、1 个断言无密钥文件被跟踪、1 个断言 `.gitignore` 没有忽略 `config/` 目录本身。旧规则下 5 例失败，新规则 10 例全绿（真实调用 `git check-ignore`，不是 Python 里手写 glob 模拟）。 |

> 说明：这是**纵深防御**，不是已发生的泄露。git 历史已核查：从未提交过任何真实凭据（仅 `feishu.example.json` 示例）。

### 2.3 ✅ `configure_dark_style` 命名与实现相反（**已修复**，提交 `1051a1b`）

| 项 | 内容 |
|---|---|
| **位置** | `gui/ui_theme.py:151` `configure_dark_style = configure_light_style`；被 `gui/main.py:283`、`:394` 调用 |
| **现状** | 函数名说「深色」，实现是**浅色**（配色 token 全为 `C_BG=#f8f9fa` / `C_CARD=#ffffff` / `C_ACCENT=#2563eb`），符合用户已确认的浅色商务风偏好 |
| **为什么是问题** | 纯命名误导。后续维护者（或 AI 助手）看到 `configure_dark_style(root)` 会以为在配深色主题，可能因此「修复」出一个深色界面 —— 而**用户已明确否决深色风**（前序技能记录：深色版曾合并跑通测试，用户看后要求整体改回浅色）。 |
| **工作量** | **S**（改名 + 2 处调用点） |
| **建议** | 重命名为 `configure_app_style` 或直接调用 `configure_light_style`，删除别名。 |
| **修复** | ✅ 删除别名（原地留注释说明删除理由），`gui/main.py` 两处调用点改名为 `configure_light_style`，并清掉 `page_closing.py` / `page_inventory.py` / `page_mail.py` 里**导入了却从未调用**的 `configure_dark_style`。 |
| **回归** | `tests/test_ui_theme.py` 13 例。分四组：别名确已消失且 `configure_light_style` 可调用；配色 token 亮度断言（surface 亮度 >200、text <128、卡片-文字反差 >100，防"白底白字"回归）；**静态**断言主题优先级序列必须是 `("clam","alt","default")`；**真实 Tk** 断言实际生效主题 == `clam`、选中页签文字可读、Treeview 背景为浅色。把优先级改成 `vista` 优先即复现失败。 |

> 说明：真实 Tk 那组用例最初**不稳定**（每次测试各自 `tk.Tk()`，连续 5 轮里有 1~2 例偶发 `TclError` 跳过）。改为**模块级单例 root + 3 次重试（`time.sleep(0.3)`）**后，连跑 6 轮稳定 13 passed / 0 skip。这是 Windows 上反复创建销毁 Tk 实例的已知抖动，不是被测代码的问题。

### 2.4 ✅ 依赖清单三处手工同步，无机器校验（**已修复**，提交 `1039b2c`）

| 项 | 内容 |
|---|---|
| **现状** | 新增三方依赖必须同时改 ① `pyproject.toml` ② `build.ps1:224` 的 `pip install` 列表 ③ `财务系统.spec` 的 `hiddenimports` |
| **为什么是问题** | `build.ps1` **不读** `pyproject.toml`；PyInstaller 对缺失的 hiddenimport **只发 WARNING 不报错** → 构建「成功」但 exe 点开该功能才崩。当前靠人记（已写入技能坑列表），机器无校验。 |
| **工作量** | **S~M** |
| **建议** | 在 `build.ps1` 装完依赖后加一段断言：解析 `pyproject.toml` 的 `dependencies`，逐个 `python -c "import <mod>"` 验证；再 `grep` spec 的 `hiddenimports` 覆盖同一集合，不一致即 `throw` 中止构建。把「三处一致」变成构建期门禁而非人的记忆。 |
| **修复** | ✅ 已按建议在 `[2/6]` 装包后新增 `Assert-DependencyParity` 四项检查：① 脚本 `$RuntimePackages` == `pyproject.toml` 声明；② 每个声明项都在 spec `hiddenimports` 里；③ 每个包在**当前构建环境**里真实可 `import`；④ 反向检查（仅告警不阻断）spec 里出现未声明项。pip 名/import 名映射（`fpdf2`→`fpdf`）集中到 `$script:DependencyImportNames` 一处，不再散落在人脑里。 |
| **附带修复** | 同轮把 `finally` 与 `Publish-ReleaseAtomically` 里删旧版本的 `Remove-Item` 改为**非致命**：新版已上线时删不掉旧目录只占磁盘，不该让整个构建报「失败」（本机端点防护会拦住单次删数百文件的操作）。改为 `Write-Warning` 并提示手动清理路径。 |
| **约束** | `build.ps1` 有 Windows PowerShell 5.1 ANSI 解码约束 → 新增代码**必须纯 ASCII**。已加静态测试守住（`test_build_script_is_pure_ascii`）。 |
| **回归** | `tests/test_dependency_parity.py` 8 例：三方一致、映射与 `build.ps1` 同源、`fpdf2`→`fpdf` 具体映射、`build.ps1` 纯 ASCII、`requires-python` 与 3.11 对齐、门禁已定义**且**已调用、调用顺序在 install 之后 PyInstaller 之前。只往 `pyproject.toml` 注入 `requests` → 2 例失败；还原后 8 例全绿。 |

> 实跑验证：完整 `build.ps1` 输出 `dependency parity OK: imap_tools, pystray, openpyxl, fpdf`——证明 `fpdf2`→`fpdf` 映射端到端生效（这正是当初静默缺库的那个包）。

### 2.5 🟢 数据层遗留（业务数据，非代码缺陷）

| # | 现象 | 影响 | 状态 |
|---|---|---|---|
| a | `company` 表 1 行但 `name/tax_no/addr/note` **全为空** | 报表抬头的公司名为空 | ✅ **已执行**（`21ed316`，2026-09-11 00:44；源文件 = 桌面 `执照.jpg`） |
| b | 6 张发票 `file_path`/`file_hash` 均为空串 | 发票记录与原始 PDF 未关联 | ✅ **已执行**，6/6 归档落地并逐行 `os.path.exists` 复核 |
| c | `partner.type` 中英混用（一条中文「客户」+ 一条英文 `supplier`） | 代码已用 `type IN (?,?)` 兼容，**非 bug** | ℹ️ 可不处理；若要统一，`add_partner` 已自动归一化新数据 |
| d | 3 条 `test data cleanup` 冲销凭证 + 3 个早期 `failed_invoice_*.pdf` | 测试残留，占位不占逻辑 | ⬜ 可选清理（**先确认清单**） |
| e | `release/frozen-self-check.json` 是 2026-07-11 旧文件 | 陈旧，无功能影响 | ✅ 重打包已刷新 |
| f | 飞书配置在 `settings.json` 与 `feishu.json` 里重复一份 | 双份配置有分叉风险 | ⬜ 明确单一来源（`feishu.json` 为主） |
| g | `config/feishu.app_secret.blob` 孤儿文件（当前代码无任何 `.blob` 读写） | 旧版残留，易误导维护者去查「blob 链路」 | ⬜ 可删 |
| h | ~~`invoice-agent\invoice_agent.py::summarize()` 丢字段~~（上一轮新发现） | `file_path` 恒为空 + 税号丢失 | ✅ **已修**（`1cf2be2`，见下方「根因修复已实施」） |
| i | 🔴 **`summarize()` 的 `金额` 实为价税合计，下游却当不含税金额用**（本轮新发现） | id=6~9 丢弃进项税、价税合计少计 **合计 ¥24.53**；id=9 已生成凭证，应付账款同步少记 2.91 | 🔴 **未修**，会计口径待确认，见 §2.6 |

#### a/b 的根因与工具（提交 `21ed316`）

**`file_path` 恒为空的根因不在财务系统**，而在外部发票识别 agent：
`C:\Users\li1\invoice-agent\invoice_agent.py` 的 `summarize()` 把 OCR 结果的
`fields` 压缩成 6 个中文字段（发票号码/开票日期/销售方/金额/税额/购买方），
**丢掉了 `file`（文件名）以及 `销售方税号` / `购买方税号` / `价税合计`**。
财务系统 `register_invoice(raw, raw.get("file_path") or raw.get("file"))` 因此
永远拿不到 `source_file` → `file_path` 只能是空串；同一原因也让 2026-07-12
入库的 4 张滴滴发票 `seller_tax_no` / `buyer_tax_no` 为空。

**关键发现：`results/ocr_result.json` 里 `file` 与 `发票号码` 是成对的**
（`[{"file": "滴滴电子发票A.pdf", "fields": {...}}]`），所以 id=6~9 可以按
**发票号精确 join**，完全不需要按文件名或顺序猜：

| 库内 id | 发票号码 | 原始文件 | 证据来源 |
|---|---|---|---|
| 6 | 26337000000663360916 | `invoice-agent\invoices\滴滴电子发票A.pdf` | `ocr_result.json` |
| 7 | 26447000001378352112 | `…\滴滴电子发票B.pdf` | `ocr_result.json` |
| 8 | 26217000000285335773 | `…\滴滴电子发票C.pdf` | `ocr_result.json` |
| 9 | 26117000001021765684 | `…\滴滴电子发票D.pdf` | `ocr_result.json` |
| 10 | 26337000000601987996 | `FinanceSystem\invoices\failed\【一嗨-403.00元】…pdf` | agent 单文件模式重新识别 |
| 11 | 26317000002656636842 | `…\【一嗨-216.96元】…pdf` | 同上 |

id=10/11 重新识别出的号码、销售方税号与库内**完全一致**。另发现财务系统侧
**没有"成功归档"目录**——历史上 OCR 成功的 PDF 从未落盘到财务系统，
只有元数据进库；`invoices\failed\` 里 3 个 25 字节 `failed_invoice_*.pdf`
是早期失败占位符（工具自动忽略），1 个 `订单…结算单.pdf` 是结算单非发票。

**工具** `tools/backfill_business_data.py`（默认只读，`--apply` 才写）：

- **默认只读**：不加 `--apply` 只打印方案，不写库、不复制文件。
- **写前备份**：改用 **SQLite 在线备份 API**（生产库是 WAL 模式，直接
  `shutil.copy2` 有可能连同未 checkpoint 的页一起拷走、拿到不一致快照），
  并做 `integrity_check` + 关键表行数校验。专项用例验证「未 checkpoint 的
  WAL 数据必须出现在备份里」。
- **硬证据关联**：一律按发票号码 join；文件名里的金额只作**第二道校验**，
  金额不符即拒绝。
- **拒绝模糊**：一个号码命中多份文件、多行指向同一文件 → 只报告不猜。
- **三层防覆盖**：执行前复核列现状 + SQL 仍要求目标列为空 + 只填充空列，
  绝不覆盖已有值，也不会留下没人引用的**孤儿归档文件**。
- **幂等**：只填目标列为空的行，重复运行不产生第二次写入。
- **留痕**：每次改动写一条 append-only 的 `audit_event`
  （`invoice_file_linked` / `company_field_updated`）。
- **归档位置**：`<数据根>\invoices\archive\<开票月份>\<发票号>_<原名>`，
  路径由库自己持有，不依赖 agent 的暂存目录。

**根因修复（✅ 已实施，提交 `1cf2be2`）**：

| 步骤 | 改动 | 落点 |
|---|---|---|
| ① | `summarize()` 补齐 `file_path`（绝对）/`销售方税号`/`购买方税号`/`金额合计`/`税额合计`/`价税合计`；价税合计缺失时用「不含税 + 税额」兜底；原 6 个中文字段保留（下游 `_text()` 兼容） | 外部 `invoice_agent.py` |
| ② | 新增 `_archive_invoice_file()`，在 `normalize_invoice()` 内把原件复制到 `<数据根>\invoices\archive\<开票月份>\<发票号>_<原名>`；同名不同内容加 `(2)` 后缀不覆盖；失败降级沿用原路径、**绝不阻断记账**；`settings.invoice_archive_enabled=false` 可关 | `modules/invoice_gui.py` |
| ③ | `mail_processor` 在临时文件被删除前把它作为 `source_file` 传入入库（此前只取 `fields`、丢掉 `file` 键，原件随 `finally` 消失） | `modules/mail_processor.py` |
| ④ | `load_invoices` 对缺 `file_path` 的行记 `LOG.warning` | `modules/invoice_gui.py` |
| ⑤ | `INVOICES_ARCHIVE_DIR` 常量 + `ensure_app_dirs()` 创建 | `app_paths.py` |

回归 `tests/test_invoice_archive.py`（18 例）。**变异验证**：移除归档调用或 `source_file`
传递 → 4 例失败；还原后全绿。全量 **130 passed**。
（`logging_setup` 里 `logger.propagate = False`，`caplog` 抓不到告警，测试改为直接断言
`LOG.warning` 调用；日志本身正常落 `logs\`。）

### 2.6 ✅ 已污染生产数据并已修完：金额口径错位（提交 `26f2fb5`）

**问题**：`summarize()` 把 summary 的 `金额` 写成
`f.get("价税合计") or f.get("金额合计")`。OCR 对滴滴票**抽不到**「价税合计」
（正则只认 `价税合计…¥数字` 同行形态），于是 `金额` 落到 `金额合计`（**不含税**值）。
`normalize_invoice()` 又把它当 `net_amount_cents`，`税额合计` 因 key 名不匹配
（summary 里叫「税额」）取到 0 → `total = net + tax = net`，**进项税被静默丢弃**。

| id | 发票号码 | 现值 net/tax/total | 票面真值（`ocr_result.json`） | 偏差 | 入账状态 |
|---|---|---|---|---|---|
| 6 | 26337000000663360916 | 591.11 / 0 / 591.11 | 591.11 / **17.74** / **608.85** | 少计 17.74 | 待确认，无凭证 |
| 7 | 26447000001378352112 | 8.78 / 0 / 8.78 | 8.78 / **0.27** / **9.05** | 少计 0.27 | 待确认，无凭证 |
| 8 | 26217000000285335773 | 120.15 / 0 / 120.15 | 120.15 / **3.61** / **123.76** | 少计 3.61 | 待确认，无凭证 |
| 9 | 26117000001021765684 | 96.83 / 0 / 96.83 | 96.83 / **2.91** / **99.74** | 少计 2.91 | **凭证 15 待审核**，未记账 |

**口径自证**：四张票的 `不含税 / 价税合计` 均 ≈ `0.9709`（3% 征收率），与 OCR 抽出的
`金额合计`/`税额合计` 完全吻合 —— 证实 `金额合计` 是不含税值。

**为什么修起来要分两种**：id=6/7/8 未入账，改台账即可；id=9 已生成凭证 15
（`借 5602 成本 96.83 / 贷 2202 应付 96.83`），**应付账款同样少记 2.91**（实付 99.74），
必须连带处理分录。**口径已确认**：滴滴开的是 3% 增值税电子普通发票、进项税**不可抵扣**
→ 成本费用按**价税合计全额**入账（分录整体抬到价税合计，不拆 2221）。
无论哪种口径，`invoice_doc` 都按**票面真值**记录（**台账 ≠ 分录**）。

#### 修复结果（对生产库已执行）

| id | 修正后 net/tax/total | 凭证处理 | 补计 |
|---|---|---|---|
| 6 | 591.11 / **17.74** / **608.85** | 无凭证，仅台账 | ¥17.74 |
| 7 | 8.78 / **0.27** / **9.05** | 无凭证，仅台账 | ¥0.27 |
| 8 | 120.15 / **3.61** / **123.76** | 无凭证，仅台账 | ¥3.61 |
| 9 | 96.83 / **2.91** / **99.74** | voucher 15：`借 5602` / `贷 2202` 96.83 → **99.74** | ¥2.91 |

合计补计 **¥24.53**。工具 `tools/fix_invoice_amounts.py`（**默认只读**）。

| 安全层 | 实现 |
|---|---|
| 真值来源 | 只认 `invoice-agent/results/ocr_result.json` 的票面三件套；`不含税 + 税额 ≠ 价税合计` 整条丢弃 |
| 动手门槛 | **仅** `tax_cents == 0 && total_cents == net_amount_cents` 且不含税与票面一致；其它形态**报冲突、不猜** |
| 凭证拒绝 | 非「一借一贷」/ 含 `2221` / 已 `posted` / 分录金额与发票原额不符 → 全部拒绝 |
| 备份 | SQLite **在线备份 API**（WAL 下 `copy2` 不可靠）+ `integrity_check` + 5 表行数校验 |
| 事务 | `BEGIN IMMEDIATE` + 写前 TOCTOU 复核 + 改后借贷自校验；任一步失败**整体回滚** |
| 留痕 | 每处改动一条 append-only `audit_event`（前后值 + 证据来源） |
| 幂等 | 现值已等于真值 → 跳过；复跑输出 `[无需处理]` |

实测：`audit_event` id 43–47；voucher 15 借贷 **9974 = 9974**；全库无失衡凭证；
备份 `finance.db.fixamounts-20260911-010500.bak`。回归 `tests/test_fix_invoice_amounts.py`
（34 例）。**变异验证**：去掉"缺陷形态门槛" / "已记账拒绝" / "2221 拒绝" → 对应用例失败。

> 🔴 **沉淀教训**：`金额` 一个字段兼职两种含义（`价税合计 or 金额合计`），叠加
> summary 写 `税额`、下游找 `税额合计` 的 key 名不匹配 —— 两个小疏忽合起来让**进项税静默归零**，
> 且没有任何地方报错。规则：**一个字段只承载一种含义**；跨模块字段名必须对齐；
> 金额类字段一律"三件套 + 自校验（不含税 + 税额 = 价税合计）"。

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
| 备份实现质量 | ✅ 原生在线热备 + 完整性校验 + 失败清理（保留策略与失败可见性已在 `3d27438` 补齐，见 §2.1）。 |
| 构建脚本 | ✅ 隔离 venv、可复现构建、密钥字节级扫描、SHA256 清单、原子发布带回滚。 |
| exe 与源码一致性 | ✅ 已重打包至含 P1/P3/P6/P7（exe `01:12`，8,863,479 B；`build.ps1` 六步全过）。**验证方式已纠正**：`grep exe` 无效（PYZ 是 zlib 压缩的，实测连既有的 `_BACKUP_NAME_RE` 也 0 命中）；改用 `tools/verify_release_packaging.py` 解 PYZ 判 `co_name`/`co_names`/`co_varnames`/`co_consts`（含字符串元组）→ `ALL_TARGET_SYMBOLS_PRESENT`，并配负对照。`manifest.sha256` 999 项、exe 哈希一致。 |

---

## 4. 建议的动作顺序

| 优先级 | 动作 | 工作量 | 状态 | 理由 |
|---|---|---|---|---|
| **P1** | 备份保留策略改分层 + 失败不再静默 | S | ✅ **已完成**（`3d27438`） | 唯一涉及**数据可恢复性**的项；「以为有备份」最危险。修复中另发现日期匹配恒失败，实际无保留策略 |
| **P2** | `.gitignore` 补 `config/*.json` + `config/*.blob`（放行 example） | S | ✅ **已完成**（`d75c8d2`） | 一行改动，挡住密钥进仓库的唯一现实路径 |
| **P3** | 重命名 `configure_dark_style` | S | ✅ **已完成**（`1051a1b`） | 防止后人被名字误导去做用户已否决的深色风 |
| **P4** | 依赖三处一致性加构建期断言 | S~M | ✅ **已完成**（`1039b2c`） | 把已知地雷变成机器门禁 |
| **P5** | 补 `company` 信息 / 回填发票 `file_path` | S | ✅ **已完成**（`21ed316`，2026-09-11 00:44 执行） | 业务数据完整性，非代码问题；已按「只读→确认→备份→留痕」流程执行 |
| **P6** | 修 `invoice_agent.py::summarize()` 丢字段的根因 | S~M | ✅ **已完成**（`1cf2be2`） | 不修根因，以后新收的发票 `file_path` 照样是空 |
| **P7** | 🔴 修 id=6~9 被污染的金额口径（含 id=9 的凭证 15） | S | ✅ **已完成**（`26f2fb5`，2026-09-11 01:05 执行） | **唯一一项已污染生产数据的缺陷**：进项税额、成本与应付账款均少记；已按确认口径（3% 电子普票、不可抵扣、全额入成本）修正 |
| **P8** | exe 打包内容验证方法（grep 无效） | S | ✅ **已完成**（随 `tools/verify_release_packaging.py` 落地） | 早前"grep exe 找符号"的验证是**假阳性安全**：旧结论"删掉 P3 别名已验证"实际无证据；现改为解 PYZ 判定 + 负对照 |

> **不建议做的（延续前序 §5）**：拆 `gui/main.py`（2845 行）、拆 `db/__init__.py`、引入 ORM、补 CI/覆盖率、重写 `build.ps1`、迁移 GUI 框架。这些在当前规模下是负收益。

---

## 5. 证据清单

| 结论 | 验证方式 |
|---|---|
| TOP5 全部关闭 | 逐项 grep 源码 + `git log` + `财务系统.spec`/`run_finance.py` 实读 |
| 52 passed / 17 OK | `pytest tests/ -q`、`tools/acceptance_check.py` 实跑 |
| 备份窗口 = 5 天（首轮） | `grep -n MAX_BACKUPS gui/main.py` → 381 行；读 355-391 行确认「每天一次」 |
| 备份失败静默 | 读 `gui/main.py:389-391` → `except Exception: pass` |
| **备份实际无保留策略** | `ls -la %LOCALAPPDATA%\FinanceSystem\backups\` → 5 份**全部** `finance-20260910-*`（同一天）；对照 `date.today().isoformat()` 与文件名 `%Y%m%d` 格式确认前缀恒不匹配 |
| **P1 修复后行为** | `pytest tests/test_backup_retention.py -q` → 11 passed；全量 52 passed |
| **修复未污染生产库** | 修复前后 `mode=ro` 统计均为 46 科目 / 13 凭证 / 6 发票 |
| `.gitignore` 缺口 | `git check-ignore` 实测 `config/credentials.json` 等 3 个文件**不被忽略** |
| SQL 拼接无注入路径 | 逐处追溯 `db/__init__.py:168/174/609/826`、`modules/ledger.py:561` 的插值来源 |
| git 无密钥泄露 | `git log --all --diff-filter=A --name-only` 过滤 credential/secret/blob/db |
| 生产库只读体检 | `mode=ro` URI 打开，统计表行数（未写入） |
| P2 修复有效 | `git check-ignore -v -- config/credentials.json config/settings.json config/feishu.json config/feishu.app_secret.blob config/anything-new.json config/whatever.blob` → 全部命中；`config/feishu.example.json`、`README.md` → 未命中 |
| P2 无密钥入库 | `git add -A -n` → 只显示 `.gitignore` 自身 |
| P3 别名已消失 | 全仓 `grep -rn "configure_dark_style" --include=*.py .` → 仅剩 1 处解释性注释，无源码引用 |
| P3 主题真实生效 | 真实 `tk.Tk()` + `ttk.Style().theme_use()` 断言 == `clam`；改优先级后复现失败 |
| P4 门禁实跑 | 完整 `build.ps1` → `dependency parity OK: imap_tools, pystray, openpyxl, fpdf` |
| P4 变异性 | `pyproject.toml` 注入 `requests` → 2 例失败；还原 → 8 passed |
| P5 只读不改库 | 不加 `--apply` 跑生产库：`invoice_doc.file_path` 仍空串、`audit_event` 无新增、`backups\` 未生成 |
| P5 关联正确性 | 用 `C:\tmp\ocr_venv2`（PyMuPDF 1.28.0）对 `invoices\failed\` 两个真 PDF 跑 agent 单文件模式 → 号码与库内 id=10/11 **完全一致** |
| P5 备份 WAL 安全 | 专项用例：数据写入 WAL 未 checkpoint（保持写连接不关）时执行备份 → 备份中能读到该数据 |
| P5 防覆盖变异性 | 同时移除「执行前复核」与「SQL 空列守卫」→ `test_apply_blocks_overwriting_a_path_filled_after_planning` 必定失败 |
| P5 写库实际生效 | `os.path.exists` 逐行复核 → 6/6 归档文件 EXISTS；`audit_event` id 34–42 新增 9 条 |
| P5 备份可恢复 | `finance.db.backfill-20260911-004425.bak`（在线备份 API + `integrity_check` + 关键表行数校验） |
| P6 归档不覆盖 | 同票命名但内容不同 → `(2)` 后缀；已有归档字节未变（用例断言） |
| P6 原件保全 | 邮件链端到端用例：临时文件 `finally` 删除后，归档副本仍存在且文件名含原附件名 |
| P6 金额口径自证 | 四张滴滴票 `不含税/价税合计` ≈ 0.9709（3% 征收率），与 OCR 抽取字段吻合 |
| P6 变异性 | 移除归档调用或 `source_file` 传递 → 4 例失败；还原后 18 例全绿 |
| P6 全量回归 | `pytest tests -q` → **130 passed**（P5 后 111 → P6 后 130） |
| P7 缺陷证据 | 生产库 `invoice_doc` 实测 id=6~9 `tax_cents=0`；`ocr_result.json` 实测 `税额合计` 非零；凭证 15 分录 `借 5602 96.83 / 贷 2202 96.83` |
| P7 只读不改库 | 不加 `--apply` 跑生产库：`invoice_doc` 金额未变、`audit_event` 无新增、`backups\` 未生成 |
| P7 写库实际生效 | `invoice_doc` id=6~9 → 60885 / 905 / 12376 / 9974；`tax_cents` 同步 1774 / 27 / 361 / 291 |
| P7 凭证同步 | voucher 15：`借 5602` = `贷 2202` = **9974**；`HAVING SUM(debit_cents) <> SUM(credit_cents)` → 全库无失衡 |
| P7 审计留痕 | `audit_event` id 43–47（4 条 `invoice_amount_corrected` + 1 条 `voucher_amount_corrected`），含前后值与证据来源 |
| P7 备份可恢复 | `finance.db.fixamounts-20260911-010500.bak`（在线备份 API + `integrity_check` + 5 表行数校验） |
| P7 幂等 | 复跑 dry-run → `[无需处理]`，无二次写入 |
| P7 变异性 | 去掉"缺陷形态门槛" / "已记账拒绝" / "2221 拒绝" → 对应用例失败；还原后 34 例全绿 |
| 打包实跑 | `build.ps1` `[1/6]`~`[6/6]` 全过，`BUILD_EXIT=0`；`[4/6]` 冻结自检 `exit=0` |
| 打包内容判定 | `tools/verify_release_packaging.py` 解 PYZ → P6 符号全命中；**负对照**注入假符号必被报出 |
| grep 方法无效（对照） | 对新 exe `grep -a -c _BACKUP_NAME_RE` → **0**；`configure_light_style` → **0**（两者确在包内）→ 证明"grep 命中 N 次"类结论不可采信 |
| 发布完整性 | `manifest.sha256` 999 项；`sha256sum 财务系统.exe` == 清单值（`09b7332…2a3cd4`） |
| 全量回归 | `pytest tests -q` → **171 passed**（130 → +34 P7 → +7 打包验证器） |

> 首轮评审全程只读，未修改 `fin-system` 任何业务代码，仅新增本报告并在 `HANDOFF.md` 第 10 节补待办。
>
> **后续执行记录**：§2.1~§2.4 的修复（`3d27438` / `d75c8d2` / `1051a1b` / `1039b2c`）均在同一会话内完成并各自带回归用例；
> §2.5 的回补工具（`21ed316`）**已于 2026-09-11 00:44 对生产库执行**（备份 + 归档 + 留痕，逐行复核）；
> §2.5 的根因修复与 §2.6 的口径纠正方向一并落地为提交 `1cf2be2`（回归 18 例）；
> **§2.6 的历史数据修正（P7）已于 2026-09-11 01:05 执行完毕**（提交 `26f2fb5`，回归 34 例）；
> release 已重打包（`01:12`）并用拆包方式验证 P6/P7 代码确在 exe 内。**本轮全部待办已清零**，
> 唯一剩余的是业务动作：163 历史发票补收（约 60 封，需真实连邮箱写库）。
