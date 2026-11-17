"""M6 差异批次：页面与接口。"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from app.store import transaction
from app.web import page
from .accounting import (
    close_period,
    invoice_trace,
    period_report,
    post_invoice,
    register_receipt,
    reopen_period,
)
from .exporter import difference_rows, differences_csv
from .periods import ensure_period, normalize_period
from .service import DEMO_SOURCE, ensure_demo, overview, run, suggested_adjustment_period

router = APIRouter()


class ReceiptIn(BaseModel):
    receipt_id: str
    period: str
    sku: str
    tax_rate: str
    amount: str
    line_no: int | None = None
    supplier_code: str | None = None
    received_at: str | None = None


class InvoiceIn(BaseModel):
    invoice_id: str
    receipt_id: str
    amount: str
    sku: str | None = None
    tax_rate: str | None = None


class CloseIn(BaseModel):
    period: str
    expected_version: int | None = None


class PeriodIn(BaseModel):
    period: str


@router.get("/batches", response_class=HTMLResponse)
def view(request: Request):
    data = overview()
    periods = data["periods"]
    return page(
        request,
        "m6.html",
        "差异批次",
        "/api/reconcile",
        [],
        context={
            "periods": periods,
            "receipts": data["receipts"],
            "invoices": data["invoices"],
            "reports": [{"report": period_report(item["period"])} for item in periods],
            "suggested_adjustment": suggested_adjustment_period(periods[-1]["period"]) if periods else "",
        },
    )


@router.post("/api/reconcile")
def execute():
    return run()


@router.post("/api/periods")
def open_period(payload: PeriodIn):
    period = normalize_period(payload.period)
    with transaction() as db:
        result = ensure_period(db, period)
    return result


@router.post("/api/periods/close")
def close(payload: CloseIn):
    return close_period(payload.period, payload.expected_version)


@router.post("/api/periods/reopen")
def reopen(payload: PeriodIn):
    return reopen_period(payload.period)


@router.post("/api/receipts")
def create_receipt(payload: ReceiptIn):
    return register_receipt(payload.model_dump())


@router.post("/api/invoices")
def create_invoice(payload: InvoiceIn):
    return post_invoice(payload.model_dump())


@router.get("/api/periods")
def list_period_state():
    return overview()["periods"]


@router.get("/api/periods/{period}/report")
def report(period: str, version: int | None = None):
    return period_report(period, version)


@router.get("/api/invoices/{invoice_id}")
def trace(invoice_id: str):
    return invoice_trace(invoice_id)


@router.get("/api/export/differences.csv")
def export_differences(period: str | None = None):
    if period:
        target = normalize_period(period)
    else:
        # 未指定账期时导出样例批次（运行样例按钮的同一批数据），保持演示可用。
        ensure_demo()
        target = DEMO_SOURCE
    body = differences_csv(difference_rows(target))
    return PlainTextResponse(
        body,
        media_type="text/csv",
        headers={"content-disposition": "attachment; filename=differences.csv"},
    )
