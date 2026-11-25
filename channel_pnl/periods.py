"""期间管理：定稿快照、迟到数据差异与重开复核。

业务规则：
  * 未定版（open）期间内，迟到的费用/退货到达后直接更新活账；
  * 定稿（closed）时把全量分录冻结为快照，已复核的经营结论保留原账；
  * 定稿后到达的数据照常进入活账，报告默认展示「快照原账 + 新差异」两列，
    由财务决定是否重开复核；重开会留下原因与操作留痕。
"""
from __future__ import annotations

import json
import sqlite3

from . import domain as D
from .db import transaction

SNAPSHOT_FIELDS = [
    "entry_id", "natural_key", "period_key", "entry_kind", "bearer",
    "signed_cents", "order_id", "line_no", "channel", "sku", "campaign",
    "source_ref", "source_file", "allocation_method", "event_date",
    "received_date", "verify_status",
]


def list_periods(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM periods ORDER BY period_key").fetchall()


def close_period(conn: sqlite3.Connection, period_key: str, actor: str,
                 as_of: str) -> dict:
    """定稿：冻结当前全量分录。已定稿期间重复定稿会被拒绝。"""
    period = conn.execute(
        "SELECT * FROM periods WHERE period_key=?", (period_key,)).fetchone()
    if period is None:
        raise ValueError(f"期间不存在: {period_key}")
    if period["state"] == D.PERIOD_CLOSED:
        raise ValueError(f"期间 {period_key} 已定稿，请先查看差异或重开复核")

    rows = conn.execute(
        "SELECT * FROM entries WHERE period_key=? ORDER BY entry_id",
        (period_key,)).fetchall()
    payload = "\n".join(
        json.dumps({k: r[k] for k in SNAPSHOT_FIELDS}, ensure_ascii=False)
        for r in rows
    )
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO period_snapshots(period_key,taken_at,taken_by,payload_jsonl) "
            "VALUES (?,?,?,?)", (period_key, as_of, actor, payload))
        conn.execute(
            "UPDATE periods SET state=?, closed_at=?, closed_by=? WHERE period_key=?",
            (D.PERIOD_CLOSED, as_of, actor, period_key))
    return {"period": period_key, "snapshot_id": cur.lastrowid,
            "entries": len(rows)}


def reopen_period(conn: sqlite3.Connection, period_key: str, actor: str,
                  reason: str) -> dict:
    """重开复核：必须给出原因；重开后可再次定稿生成新快照（旧快照保留）。"""
    period = conn.execute(
        "SELECT * FROM periods WHERE period_key=?", (period_key,)).fetchone()
    if period is None:
        raise ValueError(f"期间不存在: {period_key}")
    if not reason.strip():
        raise ValueError("重开复核必须填写原因")
    with transaction(conn):
        conn.execute(
            "UPDATE periods SET state=?, reopen_reason=? WHERE period_key=?",
            (D.PERIOD_REOPENED, reason.strip(), period_key))
        conn.execute(
            "INSERT INTO confirmations(entry_id,action,actor,old_value,new_value,"
            "note,confirmed_at) VALUES(NULL,?,?,?,?,?,?)",
            ("reopen-period", actor, period["state"], D.PERIOD_REOPENED,
             reason.strip(), reason.strip()))
    return {"period": period_key, "state": D.PERIOD_REOPENED}


def _load_snapshot(conn: sqlite3.Connection, period_key: str) -> dict:
    snap = conn.execute(
        "SELECT * FROM period_snapshots WHERE period_key=? "
        "ORDER BY snapshot_id DESC LIMIT 1", (period_key,)).fetchone()
    if snap is None:
        return {}
    keyed = {}
    for line in snap["payload_jsonl"].splitlines():
        rec = json.loads(line)
        # natural_key 在重新匹配/修订中保持稳定，是快照与活账的对账键
        keyed[rec["natural_key"]] = rec
    return keyed


def period_variance(conn: sqlite3.Connection, period_key: str) -> list[dict]:
    """对比定稿快照与当前活账，逐笔列出新增/金额变化/承担方变化/状态变化。

    已经复核的经营结论不会被改写：快照原账保留，差异单独列示，
    供财务判断是否需要重开复核。
    """
    snapshot = _load_snapshot(conn, period_key)
    if not snapshot:
        return []
    current_rows = conn.execute(
        "SELECT * FROM entries WHERE period_key=? ORDER BY entry_id",
        (period_key,)).fetchall()
    current = {r["natural_key"]: r for r in current_rows}
    diffs: list[dict] = []
    for key, old in snapshot.items():
        new = current.get(key)
        if new is None:
            diffs.append({"change": "removed", "key": key,
                          "source_ref": old["source_ref"],
                          "entry_kind": old["entry_kind"],
                          "order_id": old["order_id"], "line_no": old["line_no"],
                          "snapshot_cents": old["signed_cents"], "live_cents": None,
                          "detail": "快照分录在活账中消失"})
            continue
        details = []
        if new["signed_cents"] != old["signed_cents"]:
            details.append(f"金额 {old['signed_cents']}→{new['signed_cents']} 分")
        if new["bearer"] != old["bearer"]:
            details.append(f"承担方 {old['bearer']}→{new['bearer']}")
        if new["entry_kind"] != old["entry_kind"]:
            details.append(f"类型 {old['entry_kind']}→{new['entry_kind']}")
        if new["verify_status"] != old["verify_status"]:
            details.append(f"核对状态 {old['verify_status']}→{new['verify_status']}")
        if new["channel"] != old["channel"] or new["order_id"] != old["order_id"]:
            details.append(
                f"归属 {old['order_id']}/{old['channel']}→"
                f"{new['order_id']}/{new['channel']}")
        if details:
            diffs.append({"change": "changed", "key": key,
                          "source_ref": old["source_ref"],
                          "entry_kind": old["entry_kind"],
                          "order_id": old["order_id"], "line_no": old["line_no"],
                          "snapshot_cents": old["signed_cents"],
                          "live_cents": new["signed_cents"],
                          "detail": "；".join(details)})
    for key, new in current.items():
        if key not in snapshot:
            diffs.append({"change": "added", "key": key,
                          "source_ref": new["source_ref"],
                          "entry_kind": new["entry_kind"],
                          "order_id": new["order_id"], "line_no": new["line_no"],
                          "snapshot_cents": None, "live_cents": new["signed_cents"],
                          "detail": "定稿后新到达的分录（迟到数据）"})
    return diffs
