import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app.config import DATABASE_PATH

# 账期、验收、发票、暂估、冲回与流水共用一份 SQLite 存储。
# 关闭账期后旧月只读：新发生额一律落在“首个开放月份”，不回写旧月。
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, accepted INTEGER NOT NULL, quarantined INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS periods (
    period      TEXT PRIMARY KEY,
    status      TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
    version     INTEGER NOT NULL,
    batch_id    TEXT,
    closed_at   TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipts (
    receipt_id    TEXT PRIMARY KEY,
    line_no       INTEGER,
    sku           TEXT NOT NULL,
    tax_rate      TEXT NOT NULL,
    amount        TEXT NOT NULL,
    period        TEXT NOT NULL,
    supplier_code TEXT,
    received_at   TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipts_period ON receipts(period);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id        TEXT PRIMARY KEY,
    receipt_id        TEXT NOT NULL,
    sku               TEXT NOT NULL,
    tax_rate          TEXT NOT NULL,
    amount            TEXT NOT NULL,
    source_period     TEXT NOT NULL,
    adjustment_period TEXT NOT NULL,
    reversal_amount   TEXT NOT NULL DEFAULT '0.00',
    status            TEXT NOT NULL CHECK (status IN ('PENDING', 'POSTED')),
    created_at        TEXT NOT NULL,
    posted_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_invoices_adjustment ON invoices(adjustment_period);

CREATE TABLE IF NOT EXISTS accruals (
    receipt_id     TEXT PRIMARY KEY,
    source_period  TEXT NOT NULL,
    batch_id       TEXT,
    sku            TEXT NOT NULL,
    tax_rate       TEXT NOT NULL,
    accrued_amount TEXT NOT NULL,
    reversed_amount TEXT NOT NULL DEFAULT '0.00',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accruals_period ON accruals(source_period);

CREATE TABLE IF NOT EXISTS adjustments (
    adjustment_key    TEXT PRIMARY KEY,
    invoice_id        TEXT NOT NULL,
    receipt_id        TEXT NOT NULL,
    source_period     TEXT NOT NULL,
    adjustment_period TEXT NOT NULL,
    invoice_amount    TEXT NOT NULL,
    reversal_amount   TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('PENDING', 'POSTED')),
    created_at        TEXT NOT NULL,
    posted_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_adjustments_invoice ON adjustments(invoice_id);

CREATE TABLE IF NOT EXISTS postings (
    posting_id   TEXT PRIMARY KEY,
    entry_type   TEXT NOT NULL CHECK (entry_type IN ('ACCRUAL', 'ACCRUAL_REVERSAL', 'INVOICE_ACTUAL')),
    period       TEXT NOT NULL,
    source_period TEXT,
    batch_id     TEXT,
    receipt_id   TEXT,
    invoice_id   TEXT,
    sku          TEXT NOT NULL,
    tax_rate     TEXT NOT NULL,
    amount       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_postings_period ON postings(period);
CREATE INDEX IF NOT EXISTS idx_postings_receipt ON postings(receipt_id);

CREATE TABLE IF NOT EXISTS period_reports (
    period     TEXT NOT NULL,
    version    INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (period, version)
);
"""

_INIT_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()


def connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DATABASE_PATH, timeout=10)


def init_db() -> None:
    key = str(DATABASE_PATH)
    if key in _INITIALIZED:
        return
    with _INIT_LOCK:
        if key in _INITIALIZED:
            return
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(DATABASE_PATH, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.commit()
        finally:
            db.close()
        _INITIALIZED.add(key)


@contextmanager
def read():
    """只读连接；返回 sqlite3.Row。"""
    init_db()
    db = connect()
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


@contextmanager
def transaction():
    """写事务。BEGIN IMMEDIATE 让并发关账在读取状态前先取得写锁。"""
    init_db()
    db = connect()
    db.row_factory = sqlite3.Row
    db.isolation_level = None
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.execute("COMMIT")
    except BaseException:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        db.close()


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def record_run(manifest: dict) -> bool:
    init_db()
    with connect() as db:
        before = db.total_changes
        db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?, ?)", (manifest["run_id"], manifest["input_sha256"], manifest["accepted"], manifest["quarantined"]))
        return db.total_changes > before


def run_count() -> int:
    init_db()
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
