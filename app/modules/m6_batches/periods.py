"""账期：开账、关账与版本控制。

关账后旧月只读：任何新发生额都落在首个开放月份，绝不回写已关账月份。
并发关账依靠 SQLite 的 BEGIN IMMEDIATE 串行化，并校验调用方看到的版本。
"""

import re
from datetime import datetime, timedelta, timezone

from app.errors import DomainError
from app.store import now_iso

PERIOD_PATTERN = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def normalize_period(value) -> str:
    token = str(value or "").strip()
    if not PERIOD_PATTERN.match(token):
        raise DomainError("BAD_PERIOD", f"账期必须形如 YYYY-MM：{token or '空'}", 422)
    return token


def current_period() -> str:
    moment = datetime.now(timezone(timedelta(hours=8)))
    return f"{moment.year:04d}-{moment.month:02d}"


def _shift(period: str, months: int) -> str:
    year, month = (int(part) for part in normalize_period(period).split("-"))
    index = year * 12 + (month - 1) + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def next_period(period: str) -> str:
    return _shift(period, 1)


def previous_period(period: str) -> str:
    return _shift(period, -1)


def _period_row(db, period: str):
    return db.execute("SELECT * FROM periods WHERE period = ?", (period,)).fetchone()


def period_dict(row) -> dict:
    return {
        "period": row["period"],
        "status": row["status"],
        "version": row["version"],
        "batch_id": row["batch_id"],
        "closed_at": row["closed_at"],
        "created_at": row["created_at"],
    }


def list_periods(db) -> list[dict]:
    return [period_dict(row) for row in db.execute("SELECT * FROM periods ORDER BY period").fetchall()]


def get_period(db, period: str) -> dict | None:
    row = _period_row(db, normalize_period(period))
    return period_dict(row) if row else None


def ensure_period(db, period: str) -> dict:
    """开账：不存在则建立 OPEN 账期，存在则原样返回。"""
    period = normalize_period(period)
    row = _period_row(db, period)
    if row is None:
        db.execute(
            "INSERT INTO periods (period, status, version, created_at) VALUES (?, 'OPEN', 0, ?)",
            (period, now_iso()),
        )
        row = _period_row(db, period)
    return period_dict(row)


def assert_open(period_row: dict | None, period: str) -> None:
    if period_row is None or period_row["status"] != "OPEN":
        raise DomainError("PERIOD_CLOSED", f"{period} 已关账，不能回写旧月", 409)


def first_open_period_after(db, period: str) -> str | None:
    """首个开放月份：补票只能登记到这里，既不回写旧月也不跳到未来空档。"""
    period = normalize_period(period)
    row = db.execute(
        "SELECT period FROM periods WHERE period > ? AND status = 'OPEN' ORDER BY period LIMIT 1",
        (period,),
    ).fetchone()
    return row["period"] if row else None


def bump_version(db, period: str) -> int:
    db.execute("UPDATE periods SET version = version + 1 WHERE period = ?", (period,))
    return _period_row(db, period)["version"]
