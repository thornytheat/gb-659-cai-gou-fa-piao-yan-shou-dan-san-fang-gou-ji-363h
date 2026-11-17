# 数据字典

- run_id：规则版本与输入 SHA-256 前 12 位组成的稳定批次标识。
- line_no：从 1 开始的物理来源行号，不因隔离或排序改变。
- business_key：本项目声明的业务唯一键，始终按字符串存储。
- reason_code：隔离原因的稳定英文编码。
- accepted：进入业务计算的记录数。
- quarantined：保留证据但不参与业务计算的记录数。
- 数值：金额、税额、用量和权重使用 Decimal；对外序列化为字符串。
- 时间：存储为带偏移的 ISO 8601，分桶使用半开区间。
- 空值：缺失键、空串和业务哨兵分别记录。
- 幂等：完整输入字节与规则版本相同则复用 run_id，不新增结果。
- period：账期，`YYYY-MM`；创建即开放，关账后旧月只读。
- period.version：账期版本，每次关账 +1；并发关账用 `expected_version` 拒绝过期状态。
- accrual：关账时为已验收未开票金额建立的暂估，`accrued_amount` 原值、`reversed_amount` 累计冲回。
- ACCRUAL_REVERSAL：调整月冲回流水，金额为负；只冲发票金额对应的部分。
- INVOICE_ACTUAL：发票实际金额流水，只用正数登记一次。
- adjustment_period：补票实际入账月份，必须是原月之后的首个开放月份。
- source_period：暂估与验收所属的原月，冲回不回写该月。
- period_report(payload)：关账时的报告快照，按 (period, version) 保存，旧版本永久可查。
- business_key：报告与差异导出的分组键，恒为 `货号|税率`，跨税率不合并。

