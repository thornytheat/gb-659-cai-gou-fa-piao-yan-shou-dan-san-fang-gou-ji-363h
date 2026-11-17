# 深度登记 采购三方勾稽报告

- 规模：中型
- 核心规则：采购、分批验收和合并发票按货号与税率分组，数量和金额使用 Decimal 守恒。
- 使用者与工作流：应付会计导入三类单据，映射主数据，核对数量税额，多对多匹配并导出差异。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 单据采信 | 导入三类单据并隔离冲突 | POST /api/documents/import | `app/modules/m1_documents/parser.py` `app/modules/m1_documents/money.py` | `tests/test_m1_documents.py` |
| M2 主数据映射 | 归一货号与旧字段 | POST /api/master/resolve | `app/modules/m2_master/aliases.py` `app/modules/m2_master/evolve.py` | `tests/test_m2_master.py` |
| M3 数量勾稽 | 统一包装单位并核对未到量 | POST /api/quantity/match | `app/modules/m3_quantity/units.py` `app/modules/m3_quantity/match.py` | `tests/test_m3_quantity.py` |
| M4 税额核验 | 重算净额、税额与含税额 | POST /api/tax/check | `app/modules/m4_tax/calculate.py` `app/modules/m4_tax/verify.py` | `tests/test_m4_tax.py` |
| M5 多对多匹配 | 在相同货号税率内组合匹配 | POST /api/matches/solve | `app/modules/m5_matching/candidates.py` `app/modules/m5_matching/solver.py` | `tests/test_m5_matching.py` |
| M6 差异批次 | 关账、补票冲回并并排核对原月与调整月 | POST /api/reconcile | `app/modules/m6_batches/accounting.py` `app/modules/m6_batches/periods.py` | `tests/test_m6_batches.py` `tests/test_m6_accrual.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-05 | 单据缺少字段 | 静默补空进入匹配 | 隔离并保留行号 | `app/modules/m1_documents/parser.py` | `tests/test_m1_documents.py::test_missing_field_quarantined` |
| TRAP-06 | 金额带千分位 | 直接转换失败 | 清理分隔符后使用 Decimal | `app/modules/m1_documents/money.py` | `tests/test_m1_documents.py::test_thousands_amount_uses_decimal` |
| TRAP-07 | 空值哨兵 | 被当作零金额 | 空值集合统一拒绝 | `app/modules/m1_documents/money.py` | `tests/test_m1_documents.py::test_missing_field_quarantined` |
| TRAP-08 | 供应商编码前导零 | 转整数后主数据失配 | 编码始终按字符串保形 | `app/modules/m2_master/aliases.py` | `tests/test_m2_master.py::test_supplier_code_preserves_leading_zero` |
| TRAP-10 | 税额浮点尾差 | 分项和总额差一分 | Decimal 半入到分 | `app/modules/m4_tax/calculate.py` | `tests/test_m4_tax.py::test_half_up_to_cent` |
| TRAP-14 | 重复单号值冲突 | 后写覆盖金额 | 单独隔离冲突版本 | `app/modules/m1_documents/parser.py` | `tests/test_m1_documents.py::test_conflicting_duplicate_detected` |
| TRAP-17 | 旧列名仍在使用 | 新版读取报错 | 受控字段迁移并报告冲突 | `app/modules/m2_master/evolve.py` | `tests/test_m2_master.py::test_old_columns_migrate` |
| TRAP-19 | 组合候选顺序变化 | 匹配结果随输入顺序变化 | 按差额、数量与编号稳定择一 | `app/modules/m5_matching/solver.py` | `tests/test_m5_matching.py::test_choice_is_order_independent` |
| TRAP-23 | 相邻税率边界 | 不同税率金额互相抵消 | 货号与税率共同分组 | `app/modules/m4_tax/verify.py` | `tests/test_m4_tax.py::test_different_tax_rates_never_offset` |
| TRAP-26 | 同批重复执行 | 重复新增对账结果 | 输入摘要作为运行唯一键 | `app/store.py` | `tests/test_m6_batches.py::test_idempotent_record` |
| TRAP-27 | 关账后补到发票 | 回写旧月改账 | 旧月只读，补票落首个开放月份 | `app/modules/m6_batches/accounting.py` | `tests/test_m6_accrual.py::test_late_invoice_lands_in_first_open_period` |
| TRAP-28 | 补票只记新票 | 暂估与实际金额重复确认成本 | 先冲回对应暂估再登记实际金额 | `app/modules/m6_batches/accounting.py` | `tests/test_m6_accrual.py::test_partial_invoice_reverses_only_that_part` |
| TRAP-29 | 部分补票 | 一次冲光全部暂估 | 仅冲发票金额对应的部分 | `app/modules/m6_batches/accounting.py` | `tests/test_m6_accrual.py::test_partial_invoice_reverses_only_that_part` |
| TRAP-30 | 跨税率补票 | 13% 发票抵消 9% 暂估 | 按货号与税率分组，跨组拒绝 | `app/modules/m6_batches/accounting.py` | `tests/test_m6_accrual.py::test_different_tax_rate_cannot_offset` |
| TRAP-31 | 并发关账 | 两人同时关账重复取数 | 写锁 + 版本号，过期状态拒绝 | `app/modules/m6_batches/periods.py` `app/store.py` | `tests/test_m6_accrual.py::test_concurrent_close_only_one_wins` |
| TRAP-32 | 冲回半途中断 | 暂估已扣、金额未记 | 单事务回滚，重试确定性记账一次 | `app/modules/m6_batches/accounting.py` | `tests/test_m6_accrual.py::test_interrupted_reversal_keeps_original_books_and_retry_is_once` |
| TRAP-33 | 重开关账 | 旧月快照被覆盖 | 快照按版本保存，旧版仍可查 | `app/modules/m6_batches/accounting.py` | `tests/test_m6_accrual.py::test_reopen_keeps_old_snapshot_queryable` |

## 对账规则

- 采购数量 = 净验收数量 + 未到数量。
- 净验收含税额 = 已开票含税额 + 未开票金额 + 已解释差异。
- 不同税率的行不可相互抵消。
- 关账时：原月验收 = 原月暂估 + 原月已开票；关账后旧月账本冻结。
- 跨期补票：原月验收 = 跨期累计已开票 + 未冲回暂估；冲回与实际金额都只记在首个开放月份。
- 部分补票只冲减发票金额对应的暂估，剩余暂估留待后续发票。

## 账期接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/periods | 开账（幂等） |
| POST | /api/periods/close | 关账，可带 expected_version；过期版本返回 409 |
| POST | /api/periods/reopen | 重开纠错，保留历史快照 |
| POST | /api/receipts | 登记真实验收，已关账月份拒绝 |
| POST | /api/invoices | 补票：首个开放月份冲回暂估并登记实际金额 |
| GET | /api/periods/{period}/report | 已关账返回关账快照，可加 ?version= 指定历史版本 |
| GET | /api/invoices/{invoice_id} | 单张发票的跨期冲回轨迹 |
| GET | /api/export/differences.csv | 从真实暂估与流水导出的差异证据清单 |

## 已知不覆盖

- 不处理外币汇兑，样例与输出均为人民币。
- 不改动既有付款流程；不扩展商品映射表。

