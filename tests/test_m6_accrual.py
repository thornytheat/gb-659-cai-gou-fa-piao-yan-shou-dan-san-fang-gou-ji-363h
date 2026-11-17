"""跨期暂估冲回：关账、补票、部分冲回、跨税率、并发与中断恢复。"""

from decimal import Decimal

import pytest

from app.errors import DomainError
from app.modules.m6_batches import accounting
from app.modules.m6_batches.accounting import (
    close_period,
    invoice_trace,
    period_report,
    post_invoice,
    register_receipt,
    reopen_period,
)
from app.modules.m6_batches.periods import ensure_period, get_period
from app.store import read, transaction
import app.store as store


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "m6.sqlite3")
    with transaction() as db:
        ensure_period(db, "2026-11")
    register_receipt({"receipt_id": "GR1", "period": "2026-10", "sku": "SKU-A", "tax_rate": "0.13", "amount": "600.00"})
    register_receipt({"receipt_id": "GR2", "period": "2026-10", "sku": "SKU-A", "tax_rate": "0.13", "amount": "530.00"})
    register_receipt({"receipt_id": "GR3", "period": "2026-10", "sku": "SKU-B", "tax_rate": "0.09", "amount": "545.00"})
    return tmp_path


def _line(report, key):
    return next(line for line in report["lines"] if line["business_key"] == key)


def test_close_accrues_uninvoiced_without_touching_received(ledger):
    closed = close_period("2026-10", expected_version=0)
    assert closed["status"] == "CLOSED" and closed["version"] == 1
    report = closed["report"]
    assert _line(report, "SKU-A|0.13")["accrued"] == "1130.00"
    assert _line(report, "SKU-B|0.09")["accrued"] == "545.00"
    assert report["totals"]["difference"] == "0.00"
    assert sorted(closed["accrued_receipts"]) == ["GR1", "GR2", "GR3"]


def test_closed_report_is_immutable_after_late_invoice(ledger):
    close_period("2026-10")
    before = period_report("2026-10")

    post_invoice({"invoice_id": "IV1", "receipt_id": "GR1", "amount": "400.00"})

    after = period_report("2026-10")
    assert before["stored"] and after["stored"]
    assert before["totals"] == after["totals"]
    assert _line(after, "SKU-A|0.13")["accrued"] == "1130.00"
    assert _line(after, "SKU-A|0.13")["reversed"] == "0.00"


def test_late_invoice_lands_in_first_open_period(ledger):
    close_period("2026-10")
    result = post_invoice({"invoice_id": "IV1", "receipt_id": "GR1", "amount": "400.00"})

    assert result["source_period"] == "2026-10"
    assert result["adjustment_period"] == "2026-11"
    assert result["cross_period"] is True
    assert result["reversal_amount"] == "400.00"
    assert result["actual_amount"] == "400.00"

    november = _line(period_report("2026-11"), "SKU-A|0.13")
    assert november["reversed"] == "400.00"
    assert november["actual"] == "400.00"
    assert november["net_cost"] == "0.00"
    assert november["difference"] == "0.00"


def test_partial_invoice_reverses_only_that_part(ledger):
    close_period("2026-10")
    first = post_invoice({"invoice_id": "IV1", "receipt_id": "GR1", "amount": "400.00"})
    assert first["remaining_accrual"] == "200.00"

    second = post_invoice({"invoice_id": "IV2", "receipt_id": "GR1", "amount": "200.00"})
    assert second["remaining_accrual"] == "0.00"

    november = _line(period_report("2026-11"), "SKU-A|0.13")
    assert november["reversed"] == "600.00"
    assert november["actual"] == "600.00"


def test_partial_invoice_leaves_accrual_for_next_period(ledger):
    close_period("2026-10")
    post_invoice({"invoice_id": "IV1", "receipt_id": "GR1", "amount": "100.00"})
    with read() as db:
        assert get_period(db, "2026-11")["status"] == "OPEN"

    # 冲回只作用于调整月，原月暂估剩余由验收行体现
    source = period_report("2026-10")
    assert source["totals"]["uninvoiced_accrual"] == "1675.00"
    november = _line(period_report("2026-11"), "SKU-A|0.13")
    assert november["reversed"] == "100.00" and november["actual"] == "100.00"


def test_different_tax_rate_cannot_offset(ledger):
    close_period("2026-10")
    with pytest.raises(DomainError) as error:
        post_invoice({"invoice_id": "IV9", "receipt_id": "GR3", "amount": "545.00", "tax_rate": "0.13"})
    assert error.value.code == "TAX_GROUP_MISMATCH"
    # 拒绝后暂估一分未动
    assert period_report("2026-11")["lines"] == []


def test_over_invoice_rejected(ledger):
    close_period("2026-10")
    with pytest.raises(DomainError) as error:
        post_invoice({"invoice_id": "IV1", "receipt_id": "GR1", "amount": "600.01"})
    assert error.value.code == "OVER_INVOICE"


def test_write_to_closed_month_rejected(ledger):
    close_period("2026-10")
    with pytest.raises(DomainError) as error:
        register_receipt({"receipt_id": "GRX", "period": "2026-10", "sku": "SKU-A", "tax_rate": "0.13", "amount": "1.00"})
    assert error.value.code == "PERIOD_CLOSED"


def test_concurrent_close_rejects_stale_version(ledger):
    close_period("2026-10", expected_version=0)
    with pytest.raises(DomainError) as error:
        close_period("2026-10", expected_version=0)
    assert error.value.code in {"PERIOD_ALREADY_CLOSED"}


def test_stale_version_rejected_on_open_period(ledger):
    with transaction() as db:
        ensure_period(db, "2026-12")
    with pytest.raises(DomainError) as error:
        close_period("2026-12", expected_version=7)
    assert error.value.code == "STALE_PERIOD_VERSION"
    with read() as db:
        assert get_period(db, "2026-12")["status"] == "OPEN"


def test_interrupted_reversal_keeps_original_books_and_retry_is_once(ledger):
    close_period("2026-10")

    def blow_up(invoice_id, receipt_id, amount):
        raise RuntimeError("simulated crash")

    original = accounting.reversal_hook
    accounting.reversal_hook = blow_up
    try:
        with pytest.raises(RuntimeError):
            post_invoice({"invoice_id": "IV1", "receipt_id": "GR1", "amount": "400.00"})
    finally:
        accounting.reversal_hook = original

    with read() as db:
        accrual = db.execute("SELECT * FROM accruals WHERE receipt_id = 'GR1'").fetchone()
        assert accrual["reversed_amount"] == "0.00"
        assert db.execute("SELECT COUNT(*) FROM postings WHERE invoice_id = 'IV1'").fetchone()[0] == 0
    assert period_report("2026-11")["lines"] == []

    first = post_invoice({"invoice_id": "IV1", "receipt_id": "GR1", "amount": "400.00"})
    replay = post_invoice({"invoice_id": "IV1", "receipt_id": "GR1", "amount": "400.00"})
    assert first["reversal_amount"] == replay["reversal_amount"] == "400.00"

    with read() as db:
        postings = db.execute("SELECT COUNT(*) FROM postings WHERE invoice_id = 'IV1'").fetchone()[0]
    assert postings == 2  # 一笔冲回 + 一笔实际金额
    assert _line(period_report("2026-11"), "SKU-A|0.13")["reversed"] == "400.00"


def test_reopen_keeps_old_snapshot_queryable(ledger):
    close_period("2026-10")
    snapshot = period_report("2026-10", version=1)
    reopen_period("2026-10")
    close_period("2026-10")
    assert period_report("2026-10", version=1)["totals"] == snapshot["totals"]
    assert period_report("2026-10")["snapshot_version"] == 3


def test_adjustment_report_pairs_source_and_adjustment_rows(ledger):
    close_period("2026-10")
    post_invoice({"invoice_id": "IV1", "receipt_id": "GR1", "amount": "400.00"})
    november = period_report("2026-11")
    source = next(row for row in november["source_lines"] if row["receipt_id"] == "GR1")
    assert source["original_period"] == "2026-10"
    assert source["original_accrued"] == "600.00"
    assert source["reversed_here"] == "400.00"
    assert source["actual_here"] == "400.00"
    assert november["balanced"]


def test_invoice_trace_shows_both_periods(ledger):
    close_period("2026-10")
    post_invoice({"invoice_id": "IV1", "receipt_id": "GR1", "amount": "400.00"})
    trace = invoice_trace("IV1")
    assert trace["source_period"] == "2026-10"
    assert trace["adjustment_period"] == "2026-11"
    assert {row["entry_type"] for row in trace["postings"]} == {"ACCRUAL_REVERSAL", "INVOICE_ACTUAL"}
    assert trace["reversal_amount"] == "400.00"


def test_no_open_period_rejects_late_invoice(ledger):
    close_period("2026-10")
    with transaction() as db:
        db.execute("UPDATE periods SET status = 'CLOSED' WHERE period = '2026-11'")
    with pytest.raises(DomainError) as error:
        post_invoice({"invoice_id": "IV1", "receipt_id": "GR1", "amount": "400.00"})
    assert error.value.code == "NO_OPEN_PERIOD"


def test_invoice_in_open_period_posts_no_reversal(ledger):
    post_invoice({"invoice_id": "IV0", "receipt_id": "GR1", "amount": "300.00"})
    close_period("2026-10")
    # 开放期内已开票 300，关账只暂估剩余 300
    assert _line(period_report("2026-10"), "SKU-A|0.13")["accrued"] == "830.00"
    assert _line(period_report("2026-10"), "SKU-A|0.13")["actual"] == "300.00"


def test_partial_accrual_survives_second_close(ledger):
    close_period("2026-10")
    post_invoice({"invoice_id": "IV1", "receipt_id": "GR1", "amount": "400.00"})
    with transaction() as db:
        ensure_period(db, "2026-12")
    close_period("2026-11")
    # 十一月关账，十月剩余 200 暂估不被搬动
    post_invoice({"invoice_id": "IV2", "receipt_id": "GR1", "amount": "200.00"})
    december = _line(period_report("2026-12"), "SKU-A|0.13")
    assert december["reversed"] == "200.00" and december["actual"] == "200.00"
    assert period_report("2026-10")["totals"]["uninvoiced_accrual"] == "1675.00"


def test_concurrent_close_only_one_wins(ledger):
    from concurrent.futures import ThreadPoolExecutor

    with transaction() as db:
        ensure_period(db, "2026-12")

    def attempt(_):
        try:
            close_period("2026-12", expected_version=0)
            return "closed"
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(attempt, range(2)))

    assert outcomes.count("closed") == 1
    assert set(outcomes) - {"closed"} <= {"PERIOD_ALREADY_CLOSED", "STALE_PERIOD_VERSION"}
    with read() as db:
        assert get_period(db, "2026-12")["version"] == 1
        assert db.execute("SELECT COUNT(*) FROM period_reports WHERE period = '2026-12'").fetchone()[0] == 1


def test_source_settlement_identity_holds_after_partial_invoice(ledger):
    close_period("2026-10")
    post_invoice({"invoice_id": "IV1", "receipt_id": "GR1", "amount": "400.00"})
    settlement = accounting.source_settlement("2026-10")
    assert settlement["invoiced_total"] == "400.00"
    # 原月验收 1675 = 已开票 400 + 未冲回暂估 1275
    assert settlement["outstanding_total"] == "1275.00"
    with read() as db:
        received = sum(
            Decimal(row["amount"]) for row in db.execute("SELECT amount FROM receipts WHERE period = '2026-10'").fetchall()
        )
    assert received == Decimal(settlement["invoiced_total"]) + Decimal(settlement["outstanding_total"])


def test_open_period_invoice_counts_in_settlement(ledger):
    post_invoice({"invoice_id": "IV0", "receipt_id": "GR1", "amount": "600.00"})
    close_period("2026-10")
    settlement = accounting.source_settlement("2026-10")
    assert settlement["invoiced_total"] == "600.00"
    assert settlement["outstanding_total"] == "1075.00"


def test_same_invoice_id_with_different_amount_rejected(ledger):
    close_period("2026-10")
    post_invoice({"invoice_id": "IV1", "receipt_id": "GR1", "amount": "400.00"})
    with pytest.raises(DomainError) as error:
        post_invoice({"invoice_id": "IV1", "receipt_id": "GR1", "amount": "500.00"})
    assert error.value.code == "INVOICE_CONFLICT"
    assert _line(period_report("2026-11"), "SKU-A|0.13")["reversed"] == "400.00"


def test_report_snapshot_contains_no_later_reversals(ledger):
    close_period("2026-10")
    post_invoice({"invoice_id": "IV1", "receipt_id": "GR1", "amount": "400.00"})
    # 原月快照不因后续冲回变化
    october = period_report("2026-10")
    assert _line(october, "SKU-A|0.13")["reversed"] == "0.00"
    assert _line(october, "SKU-A|0.13")["actual"] == "0.00"
    # 调整月报告显示冲回与实际金额
    november = _line(period_report("2026-11"), "SKU-A|0.13")
    assert november["reversed"] == november["actual"] == "400.00"
