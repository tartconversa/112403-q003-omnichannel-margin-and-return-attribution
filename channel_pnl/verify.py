"""人工核对工作流：财务确认承担方、运营核对跨渠道退货、处理暂挂。

所有操作都写 confirmations 留痕；承担方被改动会重新归类分录，
核对状态回退 pending 时要求再次确认，保证「谁在什么时候基于什么改了账」可追。
"""
from __future__ import annotations

import json
import sqlite3

from . import domain as D
from .db import transaction

VALID_BEARER_ACTIONS = {
    "confirm-bearer": "财务确认结算/优惠项的品牌或平台承担归属",
    "confirm-return": "运营核对跨渠道退货的订单身份与商品",
    "resolve-suspense": "暂挂事件匹配到真实订单后转正",
}


def _get_entry(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM entries WHERE entry_id=?",
                       (entry_id,)).fetchone()
    if row is None:
        raise ValueError(f"分录不存在: {entry_id}")
    return row


def _log(conn: sqlite3.Connection, entry_id: int | None, action: str,
         actor: str, old: str, new: str, note: str, as_of: str) -> None:
    conn.execute(
        "INSERT INTO confirmations(entry_id,action,actor,old_value,new_value,"
        "note,confirmed_at) VALUES (?,?,?,?,?,?,?)",
        (entry_id, action, actor, old, new, note, as_of))


def confirm_bearer(conn: sqlite3.Connection, entry_id: int, actor: str,
                   bearer: str, as_of: str, note: str = "") -> dict:
    """财务确认某条结算/优惠分录由谁承担。

    unknown → brand/platform 时分录类别随之切换，渠道净贡献立即变化，
    但历史值与操作人留痕在 confirmations 中可查。
    """
    if bearer not in (D.BEARER_BRAND, D.BEARER_PLATFORM):
        raise ValueError("确认承担方只能是 brand 或 platform")
    with transaction(conn):
        entry = _get_entry(conn, entry_id)
        old_bearer = entry["bearer"]
        new_kind = entry["entry_kind"]
        # 承担方未确认的优惠在确认后重分类：品牌券抵减实收，平台券归平台结算项
        if entry["entry_kind"] == "unknown_discount":
            new_kind = ("brand_coupon" if bearer == D.BEARER_BRAND
                        else "platform_coupon")
        conn.execute(
            "UPDATE entries SET bearer=?, entry_kind=?, verify_status=? WHERE entry_id=?",
            (bearer, new_kind, D.STATUS_CONFIRMED, entry_id))
        _log(conn, entry_id, "confirm-bearer", actor,
             f"{old_bearer}/{entry['entry_kind']}", f"{bearer}/{new_kind}",
             note, as_of)
    return {"entry_id": entry_id, "bearer": bearer, "entry_kind": new_kind}


def confirm_return(conn: sqlite3.Connection, entry_id: int, actor: str,
                   as_of: str, note: str = "") -> dict:
    """运营核对跨渠道退货：确认订单身份与退货商品无误后转正。"""
    with transaction(conn):
        entry = _get_entry(conn, entry_id)
        if entry["entry_kind"] not in ("return_refund", "cogs_reversal"):
            raise ValueError("该分录不是退货分录")
        conn.execute(
            "UPDATE entries SET verify_status=? WHERE entry_id=?",
            (D.STATUS_CONFIRMED, entry_id))
        _log(conn, entry_id, "confirm-return", actor,
             D.STATUS_PENDING, D.STATUS_CONFIRMED, note, as_of)
    return {"entry_id": entry_id, "status": D.STATUS_CONFIRMED}


def resolve_suspense(conn: sqlite3.Connection, entry_id: int, actor: str,
                     order_id: str, line_no: int, as_of: str,
                     note: str = "") -> dict:
    """把暂挂分录人工指派到真实订单行；按直接匹配入账并关闭暂挂工单。"""
    with transaction(conn):
        entry = _get_entry(conn, entry_id)
        case = conn.execute(
            "SELECT * FROM suspense_cases WHERE entry_id=?",
            (entry_id,)).fetchone()
        if case is None:
            raise ValueError("该分录没有暂挂工单")
        oline = conn.execute(
            "SELECT * FROM order_lines WHERE order_id=? AND line_no=?",
            (order_id, line_no)).fetchone()
        if oline is None:
            raise ValueError(f"目标订单行不存在: {order_id}#L{line_no}")
        basis = json.loads(entry["allocation_basis"])
        original_kind = basis.get("original_kind", "suspense")
        # 原始事件若带承担方则恢复，否则保持 unknown 等财务确认
        raw_bearer = (basis.get("raw_event", {}).get("bearer") or "").strip()
        bearer = raw_bearer if raw_bearer in D.BEARERS else D.BEARER_UNKNOWN
        amount = abs(entry["signed_cents"])
        signed = D.ENTRY_KIND_SIGN.get(original_kind, -1) * amount
        basis.update({
            "manual_resolution": True,
            "resolved_by": actor,
            "previous_suspense_reason": case["reason"],
            "allocation": D.ALLOC_DIRECT,
        })
        conn.execute(
            "UPDATE entries SET order_id=?, line_no=?, channel=?, sku=?, "
            "campaign=?, period_key=?, entry_kind=?, allocation_method=?, "
            "allocation_basis=?, bearer=?, signed_cents=?, verify_status=? "
            "WHERE entry_id=?",
            (order_id, line_no, oline["channel"], oline["sku"], oline["campaign"],
             oline["period_key"], original_kind, D.ALLOC_DIRECT,
             json.dumps(basis, ensure_ascii=False, sort_keys=True),
             bearer, signed, D.STATUS_PENDING, entry_id))
        conn.execute(
            "UPDATE suspense_cases SET resolved=1, resolution_note=? WHERE entry_id=?",
            (note or f"人工指派至 {order_id}#L{line_no}，承担方仍待财务确认",
             entry_id))
        _log(conn, entry_id, "resolve-suspense", actor, case["reason"],
             f"{order_id}#L{line_no}", note, as_of)
    return {"entry_id": entry_id, "order_id": order_id, "line_no": line_no}


def audit_trail(conn: sqlite3.Connection, entry_id: int | None = None
                ) -> list[dict]:
    """查看核对留痕（可按分录过滤）。"""
    if entry_id is None:
        sql = ("SELECT c.*, e.source_ref FROM confirmations c "
               "LEFT JOIN entries e ON e.entry_id=c.entry_id "
               "ORDER BY c.confirmation_id")
        return [dict(r) for r in conn.execute(sql).fetchall()]
    return [dict(r) for r in conn.execute(
        "SELECT c.*, e.source_ref FROM confirmations c "
        "LEFT JOIN entries e ON e.entry_id=c.entry_id WHERE c.entry_id=? "
        "ORDER BY c.confirmation_id", (entry_id,)).fetchall()]
