# 归档：财务系统 · 接手核验记录（2026-09-10 实机复核）

> **从 `HANDOFF.md` 蒸馏移出** · 2026-09-26 · 执行者 WorkBuddy
> 原文 = 旧 HANDOFF §14「核验记录」，由 WorkBuddy 于 **2026-09-10** 实机复核、落库提交 `48e592a`。
> 这是首次接手这台机器时的全量核对，每条均为「声明值 vs 实测值」的对照，用来判断后续文档里哪些数字可信。
> **保留作证据基线**；本文中的数字已过时，最新活性值请新 HANDOFF §0 当前快照。

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

