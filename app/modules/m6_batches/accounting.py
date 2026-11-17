"""跨期暂估、补票冲回与账期报告。

核心约定
--------
* 关账时，本月已验收但未开票的金额记为暂估（ACCRUAL），落在本月。
* 关账后本月只读。补到的发票只能登记到「首个开放月份」：先冲回该月暂估
  （ACCRUAL_REVERSAL，负数），再登记发票实际金额（INVOICE_ACTUAL，正数）。
* 部分补票只冲回对应部分；未冲完的暂估保留给下一张发票。
* 冲回严格按 (sku, tax_rate) 分组，跨税率一律拒绝，不做互相抵消。
* 冲回与金额登记在同一事务内完成；中断即整体回滚，原暂估不动。
  重试使用确定性 posting_id + INSERT OR IGNORE，不会重复记账。
* 关账时固化报告快照（按账期 + 版本）；旧月报告永远返回当时快照。
"""

import json
from decimal import Decimal, InvalidOperation

from app.config import RULE_VERSION
from app.errors import DomainError
from app.manifest import input_digest, run_id
from app.modules.m4_tax.calculate import money
from app.store import now_iso, read, transaction
from .periods import (
    assert_open,
    bump_version,
    ensure_period,
    first_open_period_after,
    get_period,
    normalize_period,
)

ZERO = Decimal("0.00")

# 故障注入点：暂估已扣减、posting 尚未写入的同一事务中间调用。
# 默认什么也不做；测试可替换为抛错，以验证中断回滚与重试不重复记账。
reversal_hook = lambda invoice_id, receipt_id, amount: None


def _amount(value, code: str = "BAD_AMOUNT") -> Decimal:
    token = str(value).strip().replace(",", "").replace("，", "").replace("￥", "")
    try:
        result = money(Decimal(token))
    except (InvalidOperation, ValueError) as exc:
        raise DomainError(code, f"金额格式不正确：{value}", 422) from exc
    if result < ZERO:
        raise DomainError(code, f"金额不能为负：{value}", 422)
    return result


def _require(condition: bool, code: str, message: str, status: int = 409) -> None:
    if not condition:
        raise DomainError(code, message, status)


# --------------------------------------------------------------------------
# 真实验收基线
# --------------------------------------------------------------------------

def register_receipt(receipt: dict) -> dict:
    """登记验收行。已关账月份拒绝写入，避免回写旧账。"""
    period = normalize_period(receipt.get("period"))
    receipt_id = str(receipt.get("receipt_id") or "").strip()
    _require(bool(receipt_id), "BAD_RECEIPT", "验收编号不能为空", 422)
    sku = str(receipt.get("sku") or "").strip()
    tax_rate = str(receipt.get("tax_rate") or "").strip()
    _require(bool(sku) and bool(tax_rate), "BAD_RECEIPT", "货号与税率不能为空", 422)
    amount = _amount(receipt.get("amount"))
    line_no = receipt.get("line_no")

    with transaction() as db:
        ensure_period(db, period)
        assert_open(get_period(db, period), period)
        exists = db.execute("SELECT 1 FROM receipts WHERE receipt_id = ?", (receipt_id,)).fetchone()
        _require(exists is None, "DUPLICATE_RECEIPT", f"验收编号 {receipt_id} 已存在", 409)
        db.execute(
            "INSERT INTO receipts (receipt_id, line_no, sku, tax_rate, amount, period, supplier_code,"
            " received_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt_id,
                line_no,
                sku,
                tax_rate,
                str(amount),
                period,
                receipt.get("supplier_code"),
                receipt.get("received_at"),
                now_iso(),
            ),
        )
    return {"receipt_id": receipt_id, "period": period, "sku": sku, "tax_rate": tax_rate,
            "amount": str(amount), "status": "RECORDED"}


def list_receipts(db, period: str | None = None) -> list[dict]:
    sql = (
        "SELECT r.*, a.accrued_amount, a.reversed_amount FROM receipts r"
        " LEFT JOIN accruals a ON a.receipt_id = r.receipt_id"
    )
    params: tuple = ()
    if period:
        sql += " WHERE r.period = ?"
        params = (normalize_period(period),)
    sql += " ORDER BY r.period, r.line_no, r.receipt_id"
    rows = []
    for row in db.execute(sql, params).fetchall():
        item = dict(row)
        accrued = Decimal(row["accrued_amount"] or "0.00")
        reversed_amount = Decimal(row["reversed_amount"] or "0.00")
        item["accrued_amount"] = str(accrued)
        item["reversed_amount"] = str(reversed_amount)
        item["remaining_accrual"] = str(accrued - reversed_amount)
        rows.append(item)
    return rows


def _receipt(db, receipt_id: str):
    return db.execute("SELECT * FROM receipts WHERE receipt_id = ?", (receipt_id,)).fetchone()


def _accrual_row(db, receipt_id: str):
    return db.execute("SELECT * FROM accruals WHERE receipt_id = ?", (receipt_id,)).fetchone()


def _invoiced_against(db, receipt_id: str) -> Decimal:
    rows = db.execute(
        "SELECT amount FROM postings WHERE receipt_id = ? AND entry_type = 'INVOICE_ACTUAL'",
        (receipt_id,),
    ).fetchall()
    return sum((Decimal(item["amount"]) for item in rows), start=ZERO)


def _remaining_accrual(db, receipt_id: str) -> Decimal:
    row = _accrual_row(db, receipt_id)
    if row is None:
        return ZERO
    return Decimal(row["accrued_amount"]) - Decimal(row["reversed_amount"])


def _invoice_row(db, invoice_id: str):
    return db.execute("SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,)).fetchone()


# --------------------------------------------------------------------------
# 补票：冲回暂估 + 登记实际金额
# --------------------------------------------------------------------------

def post_invoice(invoice: dict) -> dict:
    """在首个开放月份冲回暂估并登记发票实际金额。

    同一 invoice_id 重复调用幂等；事务中断则原暂估保持不动，重试只记一次账。
    """
    invoice_id = str(invoice.get("invoice_id") or "").strip()
    receipt_id = str(invoice.get("receipt_id") or "").strip()
    _require(bool(invoice_id) and bool(receipt_id), "BAD_INVOICE", "发票号与验收编号不能为空", 422)
    amount = _amount(invoice.get("amount"))

    with transaction() as db:
        existing = _invoice_row(db, invoice_id)
        if existing is not None:
            _require(
                Decimal(existing["amount"]) == amount and existing["receipt_id"] == receipt_id,
                "INVOICE_CONFLICT",
                f"发票 {invoice_id} 已按 {existing['amount']} 登记，不能用不同金额或验收重放",
                409,
            )
            if existing["status"] == "POSTED":
                return _posted_summary(db, existing)
            source_period = existing["source_period"]
            adjustment_period = existing["adjustment_period"]
            amount = Decimal(existing["amount"])
            receipt_id = existing["receipt_id"]
            receipt = _receipt(db, receipt_id)
            _require(receipt is not None, "UNKNOWN_REFERENCE", f"验收行 {receipt_id} 不存在", 422)
        else:
            receipt = _receipt(db, receipt_id)
            _require(receipt is not None, "UNKNOWN_REFERENCE", f"验收行 {receipt_id} 不存在", 422)
            source_period = receipt["period"]
            source = get_period(db, source_period)
            _require(source is not None, "UNKNOWN_PERIOD", f"验收账期 {source_period} 不存在", 422)
            if source["status"] == "CLOSED":
                adjustment_period = first_open_period_after(db, source_period)
                _require(adjustment_period is not None, "NO_OPEN_PERIOD",
                         "没有开放账期可登记补票，请先开账", 409)
            else:
                # 账期仍开放：本期直接登记实际金额，不构成跨期冲回。
                adjustment_period = source_period
            ensure_period(db, adjustment_period)

        sku = str(invoice.get("sku") or receipt["sku"])
        tax_rate = str(invoice.get("tax_rate") or receipt["tax_rate"])
        _require(sku == receipt["sku"] and Decimal(tax_rate) == Decimal(receipt["tax_rate"]),
                 "TAX_GROUP_MISMATCH",
                 f"发票 {invoice_id} 的 {sku}|{tax_rate} 与验收 {receipt['sku']}|{receipt['tax_rate']} 不同组，不可冲回",
                 422)

        if existing is None:
            db.execute(
                "INSERT INTO invoices (invoice_id, receipt_id, sku, tax_rate, amount, source_period,"
                " adjustment_period, reversal_amount, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, '0.00', 'PENDING', ?)",
                (invoice_id, receipt_id, sku, tax_rate, str(amount), source_period,
                 adjustment_period, now_iso()),
            )

        invoiced_already = _invoiced_against(db, receipt_id)
        if existing is None:
            _require(
                invoiced_already + amount <= Decimal(receipt["amount"]),
                "OVER_INVOICE",
                f"发票累计 {invoiced_already + amount} 超过验收金额 {receipt['amount']}",
                422,
            )

        reversal = min(_remaining_accrual(db, receipt_id), amount)
        if reversal > ZERO:
            current = _accrual_row(db, receipt_id)
            db.execute(
                "UPDATE accruals SET reversed_amount = ? WHERE receipt_id = ?",
                (str(Decimal(current["reversed_amount"]) + reversal), receipt_id),
            )
            # 半途故障注入：此后的 posting 未写入，事务回滚，暂估回到原值。
            reversal_hook(invoice_id, receipt_id, str(reversal))
            db.execute(
                "INSERT OR IGNORE INTO postings (posting_id, entry_type, period, source_period, batch_id,"
                " receipt_id, invoice_id, sku, tax_rate, amount, created_at)"
                " VALUES (?, 'ACCRUAL_REVERSAL', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"REV:{adjustment_period}:{invoice_id}", adjustment_period, source_period,
                 f"ADJ:{adjustment_period}", receipt_id, invoice_id, sku, tax_rate,
                 str(-reversal), now_iso()),
            )

        db.execute(
            "INSERT OR IGNORE INTO postings (posting_id, entry_type, period, source_period, batch_id,"
            " receipt_id, invoice_id, sku, tax_rate, amount, created_at)"
            " VALUES (?, 'INVOICE_ACTUAL', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"INV:{invoice_id}", adjustment_period, source_period, f"ADJ:{adjustment_period}",
             receipt_id, invoice_id, sku, tax_rate, str(amount), now_iso()),
        )

        key = f"{adjustment_period}:{invoice_id}"
        db.execute(
            "INSERT INTO adjustments (adjustment_key, invoice_id, receipt_id, source_period,"
            " adjustment_period, invoice_amount, reversal_amount, status, created_at, posted_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'POSTED', ?, ?)"
            " ON CONFLICT(adjustment_key) DO UPDATE SET invoice_amount = excluded.invoice_amount,"
            " reversal_amount = excluded.reversal_amount, status = 'POSTED', posted_at = excluded.posted_at",
            (key, invoice_id, receipt_id, source_period, adjustment_period, str(amount),
             str(reversal), now_iso(), now_iso()),
        )
        db.execute(
            "UPDATE invoices SET status = 'POSTED', reversal_amount = ?, posted_at = ? WHERE invoice_id = ?",
            (str(reversal), now_iso(), invoice_id),
        )
        return {
            "invoice_id": invoice_id,
            "receipt_id": receipt_id,
            "source_period": source_period,
            "adjustment_period": adjustment_period,
            "invoice_amount": str(amount),
            "reversal_amount": str(reversal),
            "actual_amount": str(amount),
            "remaining_accrual": str(_remaining_accrual(db, receipt_id)),
            "cross_period": adjustment_period != source_period,
            "status": "POSTED",
            "created": existing is None,
        }


def _posted_summary(db, invoice_row) -> dict:
    return {
        "invoice_id": invoice_row["invoice_id"],
        "receipt_id": invoice_row["receipt_id"],
        "source_period": invoice_row["source_period"],
        "adjustment_period": invoice_row["adjustment_period"],
        "invoice_amount": invoice_row["amount"],
        "reversal_amount": invoice_row["reversal_amount"],
        "actual_amount": invoice_row["amount"],
        "remaining_accrual": str(_remaining_accrual(db, invoice_row["receipt_id"])),
        "cross_period": invoice_row["adjustment_period"] != invoice_row["source_period"],
        "status": "POSTED",
        "created": False,
    }


# --------------------------------------------------------------------------
# 关账
# --------------------------------------------------------------------------

def _accrue_period(db, period: str) -> list[str]:
    """为账期内已验收未开票的金额建立暂估。"""
    created = []
    batch_id = f"CLOSE:{period}"
    receipts = db.execute(
        "SELECT * FROM receipts WHERE period = ? ORDER BY line_no, receipt_id", (period,)
    ).fetchall()
    for receipt in receipts:
        if _accrual_row(db, receipt["receipt_id"]) is not None:
            continue
        remaining = Decimal(receipt["amount"]) - _invoiced_against(db, receipt["receipt_id"])
        if remaining <= ZERO:
            continue
        db.execute(
            "INSERT INTO accruals (receipt_id, source_period, batch_id, sku, tax_rate,"
            " accrued_amount, reversed_amount, created_at) VALUES (?, ?, ?, ?, ?, ?, '0.00', ?)",
            (receipt["receipt_id"], period, batch_id, receipt["sku"], receipt["tax_rate"],
             str(remaining), now_iso()),
        )
        db.execute(
            "INSERT OR IGNORE INTO postings (posting_id, entry_type, period, source_period, batch_id,"
            " receipt_id, invoice_id, sku, tax_rate, amount, created_at)"
            " VALUES (?, 'ACCRUAL', ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (f"ACR:{receipt['receipt_id']}", period, period, batch_id, receipt["receipt_id"],
             receipt["sku"], receipt["tax_rate"], str(remaining), now_iso()),
        )
        created.append(receipt["receipt_id"])
    return created


def close_period(period: str, expected_version: int | None = None) -> dict:
    """关账并固化报告。expected_version 用于拒绝基于过期状态的并发关账。"""
    period = normalize_period(period)
    with transaction() as db:
        ensure_period(db, period)
        current = get_period(db, period)
        if current["status"] == "CLOSED":
            raise DomainError("PERIOD_ALREADY_CLOSED", f"{period} 已关账", 409)
        if expected_version is not None and int(expected_version) != current["version"]:
            raise DomainError(
                "STALE_PERIOD_VERSION",
                f"{period} 版本已变化（当前 {current['version']}，请求 {expected_version}），拒绝过期关账",
                409,
            )

        accrued = _accrue_period(db, period)
        new_version = current["version"] + 1
        report = _compute_report(db, period, version=new_version)
        db.execute(
            "INSERT INTO period_reports (period, version, payload, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(period, version) DO UPDATE SET payload = excluded.payload,"
            " created_at = excluded.created_at",
            (period, new_version, json.dumps(report, ensure_ascii=False, sort_keys=True), now_iso()),
        )
        db.execute(
            "UPDATE periods SET status = 'CLOSED', version = ?, closed_at = ?, batch_id = ? WHERE period = ?",
            (new_version, now_iso(), f"CLOSE:{period}:{new_version}", period),
        )
        return {
            "period": period,
            "status": "CLOSED",
            "version": new_version,
            "accrued_receipts": accrued,
            "report": report,
            "run_id": run_id(f"{RULE_VERSION}:{period}:{new_version}".encode("utf-8")),
            "input_sha256": input_digest(f"{period}:{new_version}".encode("utf-8")),
        }


def reopen_period(period: str) -> dict:
    """重开账期用于纠错；历史版本快照仍保留。"""
    period = normalize_period(period)
    with transaction() as db:
        current = get_period(db, period)
        _require(current is not None, "UNKNOWN_PERIOD", f"账期 {period} 不存在", 404)
        _require(current["status"] == "CLOSED", "PERIOD_ALREADY_OPEN", f"{period} 尚未关账", 409)
        version = bump_version(db, period)
        db.execute("UPDATE periods SET status = 'OPEN', closed_at = NULL WHERE period = ?", (period,))
        return {"period": period, "status": "OPEN", "version": version}


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------

def _empty_line(sku: str, tax_rate: str) -> dict:
    return {
        "business_key": f"{sku}|{tax_rate}",
        "sku": sku,
        "tax_rate": tax_rate,
        "received": ZERO,
        "accrued": ZERO,
        "reversed": ZERO,
        "actual": ZERO,
        "uninvoiced": ZERO,
    }


def source_settlement(period: str) -> dict:
    """原月验收的结算进度：累计已开票（含关账前后、跨期补票）与未冲回暂估。

    恒等式：验收金额 = 累计已开票 + 未冲回暂估（差额记录在 difference）。
    """
    period = normalize_period(period)
    with read() as db:
        receipts = {
            row["receipt_id"]: Decimal(row["amount"])
            for row in db.execute("SELECT receipt_id, amount FROM receipts WHERE period = ?", (period,)).fetchall()
        }
        invoiced = {receipt_id: ZERO for receipt_id in receipts}
        for row in db.execute("SELECT receipt_id, amount FROM postings WHERE entry_type = 'INVOICE_ACTUAL'").fetchall():
            if row["receipt_id"] in invoiced:
                invoiced[row["receipt_id"]] += Decimal(row["amount"])

        outstanding: dict[tuple[str, str], Decimal] = {}
        for row in db.execute(
            "SELECT sku, tax_rate, accrued_amount, reversed_amount FROM accruals WHERE source_period = ?",
            (period,),
        ).fetchall():
            key = (row["sku"], row["tax_rate"])
            outstanding[key] = (
                outstanding.get(key, ZERO) + Decimal(row["accrued_amount"]) - Decimal(row["reversed_amount"])
            )

        return {
            "period": period,
            "invoiced_total": str(sum(invoiced.values(), start=ZERO)),
            "outstanding_total": str(sum(outstanding.values(), start=ZERO)),
            "outstanding_lines": [
                {"business_key": f"{sku}|{rate}", "sku": sku, "tax_rate": rate, "remaining_accrual": str(amount)}
                for (sku, rate), amount in sorted(outstanding.items())
            ],
        }


def _compute_report(db, period: str, version: int | None = None) -> dict:
    lines: dict[tuple[str, str], dict] = {}
    sources: dict[str, dict] = {}

    def line_for(sku: str, tax_rate: str) -> dict:
        return lines.setdefault((sku, tax_rate), _empty_line(sku, tax_rate))

    for receipt in db.execute("SELECT * FROM receipts WHERE period = ?", (period,)).fetchall():
        line_for(receipt["sku"], receipt["tax_rate"])["received"] += Decimal(receipt["amount"])

    # 本账期确认的暂估（按暂估所属账期，不随冲回月份移动）。
    for accrual in db.execute("SELECT * FROM accruals WHERE source_period = ?", (period,)).fetchall():
        line = line_for(accrual["sku"], accrual["tax_rate"])
        line["accrued"] += Decimal(accrual["accrued_amount"])
        line["uninvoiced"] += Decimal(accrual["accrued_amount"])

    for posting in db.execute("SELECT * FROM postings WHERE period = ? ORDER BY posting_id", (period,)).fetchall():
        line = line_for(posting["sku"], posting["tax_rate"])
        amount = Decimal(posting["amount"])
        if posting["entry_type"] == "ACCRUAL_REVERSAL":
            line["reversed"] += -amount
            # 仅当冲回作用于本月自己的暂估时，才减少本月的未开票余额。
            if posting["source_period"] == period:
                line["uninvoiced"] += amount
            source = sources.setdefault(
                posting["receipt_id"],
                {
                    "receipt_id": posting["receipt_id"],
                    "business_key": line["business_key"],
                    "original_period": posting["source_period"],
                    "original_accrued": "0.00",
                    "reversed_here": "0.00",
                    "actual_here": "0.00",
                },
            )
            source["reversed_here"] = str(Decimal(source["reversed_here"]) + (-amount))
        elif posting["entry_type"] == "INVOICE_ACTUAL":
            line["actual"] += amount
            if posting["source_period"] and posting["source_period"] != period:
                source = sources.setdefault(
                    posting["receipt_id"],
                    {
                        "receipt_id": posting["receipt_id"],
                        "business_key": line["business_key"],
                        "original_period": posting["source_period"],
                        "original_accrued": "0.00",
                        "reversed_here": "0.00",
                        "actual_here": "0.00",
                    },
                )
                source["actual_here"] = str(Decimal(source["actual_here"]) + amount)

    # 原月已确认的暂估金额与当前剩余，用于调整月并排核对。
    for source in sources.values():
        accrual = _accrual_row(db, source["receipt_id"])
        if accrual is not None:
            source["original_accrued"] = accrual["accrued_amount"]
            source["remaining_accrual"] = str(
                Decimal(accrual["accrued_amount"]) - Decimal(accrual["reversed_amount"])
            )
        else:
            source["remaining_accrual"] = "0.00"

    rows = []
    for (sku, tax_rate) in sorted(lines):
        line = lines[(sku, tax_rate)]
        received, accrued = line["received"], line["accrued"]
        reversed_amount, actual = line["reversed"], line["actual"]
        net_cost = accrued - reversed_amount + actual
        difference = received - net_cost
        rows.append({
            "business_key": f"{sku}|{tax_rate}",
            "sku": sku,
            "tax_rate": tax_rate,
            "received": str(received),
            "accrued": str(accrued),
            "reversed": str(reversed_amount),
            "actual": str(actual),
            "uninvoiced_accrual": str(line["uninvoiced"]),
            "net_cost": str(net_cost),
            "difference": str(difference),
            "balanced": difference == ZERO,
        })

    totals = {field: str(sum((Decimal(row[field]) for row in rows), start=ZERO)) for field in (
        "received", "accrued", "reversed", "actual", "uninvoiced_accrual", "net_cost", "difference"
    )}
    current = get_period(db, period)
    return {
        "period": period,
        "version": version if version is not None else (current or {}).get("version", 0),
        "status": (current or {}).get("status"),
        "lines": rows,
        "source_lines": sorted(sources.values(), key=lambda item: (item["original_period"], item["receipt_id"])),
        "totals": totals,
        "balanced": all(row["balanced"] for row in rows),
        "generated_at": now_iso(),
    }


def period_report(period: str, version: int | None = None) -> dict:
    """已关账月份返回固化快照；重开或未关账则按当前流水实时计算。

    显式传入 version 时永远返回对应关账版本的快照，供旧月原样复核。
    """
    period = normalize_period(period)
    with read() as db:
        current = get_period(db, period)
        _require(current is not None, "UNKNOWN_PERIOD", f"账期 {period} 不存在", 404)
        if version is None and current["status"] == "CLOSED":
            snapshot = db.execute(
                "SELECT * FROM period_reports WHERE period = ? ORDER BY version DESC LIMIT 1",
                (period,),
            ).fetchone()
        elif version is not None:
            snapshot = db.execute(
                "SELECT * FROM period_reports WHERE period = ? AND version = ?", (period, version)
            ).fetchone()
        else:
            snapshot = None
        if snapshot is not None:
            report = json.loads(snapshot["payload"])
            report["stored"] = True
            report["snapshot_version"] = snapshot["version"]
            report["status"] = current["status"]
            return report
        report = _compute_report(db, period)
        report["stored"] = False
        report["status"] = current["status"]
        return report


def invoice_trace(invoice_id: str) -> dict:
    """一张补票的跨期轨迹：原月暂估 → 调整月冲回 → 实际金额。"""
    with read() as db:
        invoice = _invoice_row(db, invoice_id)
        _require(invoice is not None, "UNKNOWN_INVOICE", f"发票 {invoice_id} 不存在", 404)
        postings = [dict(row) for row in db.execute(
            "SELECT entry_type, period, amount FROM postings WHERE invoice_id = ? ORDER BY period, entry_type",
            (invoice_id,),
        ).fetchall()]
        return {
            "invoice_id": invoice_id,
            "receipt_id": invoice["receipt_id"],
            "source_period": invoice["source_period"],
            "adjustment_period": invoice["adjustment_period"],
            "invoice_amount": invoice["amount"],
            "reversal_amount": invoice["reversal_amount"],
            "cross_period": invoice["source_period"] != invoice["adjustment_period"],
            "status": invoice["status"],
            "postings": postings,
        }


def list_invoices(db, period: str | None = None) -> list[dict]:
    sql = "SELECT * FROM invoices"
    params: tuple = ()
    if period:
        sql += " WHERE adjustment_period = ? OR source_period = ?"
        params = (normalize_period(period), normalize_period(period))
    sql += " ORDER BY adjustment_period, invoice_id"
    return [dict(row) for row in db.execute(sql, params).fetchall()]
