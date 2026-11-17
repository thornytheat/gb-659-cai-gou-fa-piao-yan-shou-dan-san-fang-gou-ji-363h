"""M6 差异批次：跨期暂估冲回场景与页面数据装配。"""

from decimal import Decimal

from app.errors import DomainError
from app.manifest import build_manifest
from app.store import read, record_run, transaction
from .accounting import (
    close_period,
    invoice_trace,
    list_invoices,
    list_receipts,
    source_settlement,
    period_report,
    post_invoice,
    register_receipt,
)
from .compare import reconcile_amounts
from .periods import current_period, ensure_period, first_open_period_after, get_period, list_periods, normalize_period


DEMO_SOURCE = "2026-10"
DEMO_ADJUST = "2026-11"
DEMO_RECEIPTS = [
    {"receipt_id": "GR-202610-01", "line_no": 2, "sku": "SKU-A", "tax_rate": "0.13", "amount": "600.00", "period": DEMO_SOURCE},
    {"receipt_id": "GR-202610-02", "line_no": 3, "sku": "SKU-A", "tax_rate": "0.13", "amount": "530.00", "period": DEMO_SOURCE},
    {"receipt_id": "GR-202610-03", "line_no": 4, "sku": "SKU-B", "tax_rate": "0.09", "amount": "545.00", "period": DEMO_SOURCE},
]
# 首个开放月份补到的发票：GR-01 只补 400（部分冲回），GR-02 全额 530。
DEMO_INVOICES = [
    {"invoice_id": "IV-202611-01", "receipt_id": "GR-202610-01", "amount": "400.00"},
    {"invoice_id": "IV-202611-02", "receipt_id": "GR-202610-02", "amount": "530.00"},
]


def _seed_demo() -> None:
    """幂等地准备演示批次；已存在的账期、验收与发票不重复写入。

    并发触发时以主键/编号为准：重复写入直接忽略，最终状态一致。
    """
    with transaction() as db:
        ensure_period(db, DEMO_ADJUST)
        existing_receipts = {row["receipt_id"] for row in list_receipts(db, DEMO_SOURCE)}

    for receipt in DEMO_RECEIPTS:
        if receipt["receipt_id"] in existing_receipts:
            continue
        try:
            register_receipt(receipt)
        except DomainError as error:
            if error.code not in {"DUPLICATE_RECEIPT", "PERIOD_CLOSED"}:
                raise

    with read() as db:
        source = get_period(db, DEMO_SOURCE)
    if source is not None and source["status"] == "OPEN":
        try:
            close_period(DEMO_SOURCE, expected_version=source["version"])
        except DomainError as error:
            if error.code not in {"PERIOD_ALREADY_CLOSED", "STALE_PERIOD_VERSION"}:
                raise

    with read() as db:
        posted = {row["invoice_id"] for row in list_invoices(db, DEMO_ADJUST) if row["status"] == "POSTED"}
    for invoice in DEMO_INVOICES:
        if invoice["invoice_id"] in posted:
            continue
        try:
            post_invoice(invoice)
        except DomainError as error:
            if error.code not in {"TAX_GROUP_MISMATCH", "OVER_INVOICE"}:
                raise


def ensure_demo() -> None:
    """供导出等只读入口按需准备样例批次。"""
    _seed_demo()


def run(payload: bytes = b"procurematch-sample") -> dict:
    """运行一次补票批次，返回原月与调整月的并排报告。"""
    _seed_demo()
    source_report = period_report(DEMO_SOURCE)
    adjust_report = period_report(DEMO_ADJUST)
    manifest = build_manifest(
        payload,
        accepted=len(source_report["lines"]) + len(adjust_report["lines"]),
        quarantined=0,
    )
    manifest["created"] = record_run(manifest)
    # 原月验收 = 累计已开票（含关账前开票与跨期补票）+ 截至当前仍未冲回的暂估 + 差异。
    settlement = source_settlement(DEMO_SOURCE)
    received = Decimal(source_report["totals"]["received"])
    invoiced = Decimal(settlement["invoiced_total"])
    outstanding = Decimal(settlement["outstanding_total"])
    amounts = reconcile_amounts(
        str(received),
        str(invoiced),
        str(outstanding),
        str(received - invoiced - outstanding),
    )
    return {
        **manifest,
        **amounts,
        "source_period": DEMO_SOURCE,
        "adjustment_period": DEMO_ADJUST,
        "source_report": source_report,
        "settlement": settlement,
        "adjustment_report": adjust_report,
        "trace": invoice_trace("IV-202611-01"),
    }


def demo():
    return run()


def overview() -> dict:
    """页面装配：账期、验收、发票与补票轨迹。"""
    with read() as db:
        periods = list_periods(db)
        receipts = list_receipts(db)
        invoices = list_invoices(db)
    return {"periods": periods, "receipts": receipts, "invoices": invoices}


def report_bundle(source: str, adjust: str | None = None) -> dict:
    source = normalize_period(source)
    adjust = normalize_period(adjust) if adjust else None
    return {
        "source": period_report(source),
        "adjustment": period_report(adjust) if adjust else None,
    }


def suggested_adjustment_period(source: str) -> str:
    with read() as db:
        found = first_open_period_after(db, normalize_period(source))
    return found or current_period()
