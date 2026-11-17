# 采购三方勾稽报告

面向真实数据复核的中型数据工程工具，包含六个可独立调用的业务模块。规则、失败原因与证据字段见 docs/DATA-DICTIONARY.md，陷阱和测试映射见 docs/DEPTH.md。

## 启动

docker compose up --build

浏览器访问 http://localhost:18218。测试：docker compose exec -T app pytest -q

## 模块

- M1 单据采信：页面 /documents，接口 /api/documents/import
- M2 主数据映射：页面 /master，接口 /api/master/resolve
- M3 数量勾稽：页面 /quantity，接口 /api/quantity/match
- M4 税额核验：页面 /tax，接口 /api/tax/check
- M5 多对多匹配：页面 /matching，接口 /api/matches/solve
- M6 差异批次：页面 /batches，接口 /api/reconcile

## 关账与跨期暂估冲回

- 关账：POST /api/periods/close，可带 `expected_version`；过期版本返回 409。
- 补票：POST /api/invoices。原月已关账时，先在首个开放月份冲回对应暂估，再登记实际金额；
  部分补票只冲对应部分，跨税率拒绝。
- 旧月报告：GET /api/periods/{period}/report 返回关账快照（可用 `?version=` 查历史版本），
  不因后续补票变化。批次页可并排核对原月与调整月。
- 冲回与实际金额在同一事务内完成；中断则原账保留，重试按发票号只记一次。

## 运行规则

同一输入内容生成相同运行标识，重复执行复用已有批次。隔离记录包含原因码、原始行号与原值；业务导出只保留一份证据清单。

