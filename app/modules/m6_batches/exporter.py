"""差异证据清单：只从真实暂估与流水生成，不凭空补行。"""

import csv
import io
from decimal import Decimal

from app.store import read
from .periods import normalize_period


def differences_csv(rows: list[dict]) -> str:
    output = io.StringIO(newline="")
    fields = ["business_key", "line_no", "reason_code", "amount_cny"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def difference_rows(period: str) -> list[dict]:
    """导出某账期尚未开票的暂估与不守恒差异；保留原始验收行号。"""
    period = normalize_period(period)
    with read() as db:
        rows = []
        accruals = db.execute(
            "SELECT a.*, r.line_no FROM accruals a JOIN receipts r ON r.receipt_id = a.receipt_id"
            " WHERE a.source_period = ? ORDER BY r.line_no, a.receipt_id",
            (period,),
        ).fetchall()
        for accrual in accruals:
            remaining = Decimal(accrual["accrued_amount"]) - Decimal(accrual["reversed_amount"])
            if remaining > 0:
                rows.append({
                    "business_key": f"{accrual['sku']}|{accrual['tax_rate']}",
                    "line_no": accrual["line_no"] if accrual["line_no"] is not None else "",
                    "reason_code": "UNINVOICED",
                    "amount_cny": str(remaining),
                })

        balances: dict[tuple[str, str], dict] = {}
        for receipt in db.execute("SELECT sku, tax_rate, amount FROM receipts WHERE period = ?", (period,)).fetchall():
            key = (receipt["sku"], receipt["tax_rate"])
            entry = balances.setdefault(key, {"received": Decimal("0.00"), "accrued": Decimal("0.00"),
                                              "reversed": Decimal("0.00"), "actual": Decimal("0.00")})
            entry["received"] += Decimal(receipt["amount"])
        for item in accruals:
            key = (item["sku"], item["tax_rate"])
            entry = balances.setdefault(key, {"received": Decimal("0.00"), "accrued": Decimal("0.00"),
                                              "reversed": Decimal("0.00"), "actual": Decimal("0.00")})
            entry["accrued"] += Decimal(item["accrued_amount"])
        for posting in db.execute("SELECT entry_type, sku, tax_rate, amount FROM postings WHERE period = ?", (period,)).fetchall():
            key = (posting["sku"], posting["tax_rate"])
            entry = balances.setdefault(key, {"received": Decimal("0.00"), "accrued": Decimal("0.00"),
                                              "reversed": Decimal("0.00"), "actual": Decimal("0.00")})
            amount = Decimal(posting["amount"])
            if posting["entry_type"] == "ACCRUAL_REVERSAL":
                entry["reversed"] -= amount
            elif posting["entry_type"] == "INVOICE_ACTUAL":
                entry["actual"] += amount
        for (sku, tax_rate), entry in sorted(balances.items()):
            difference = entry["received"] - (entry["accrued"] - entry["reversed"] + entry["actual"])
            if difference != 0:
                rows.append({
                    "business_key": f"{sku}|{tax_rate}",
                    "line_no": "",
                    "reason_code": "RECONCILE_DIFF",
                    "amount_cny": str(difference),
                })
    return rows
