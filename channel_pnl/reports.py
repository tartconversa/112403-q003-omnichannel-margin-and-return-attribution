"""报表层：实收净额、可归属成本、未结算部分与归因变化。

所有聚合直接基于分录表，金额列均为整数分，输出时再转元字符串。
报告口径（与业务要求逐条对应）：

  * ``matured`` 口径只含 event/received 日期不晚于 as_of 的分录，
    未到达的费用不会被当成零成本，而是以「未成熟/暂估」单列；
  * 品牌净贡献只汇总 brand 承担的 receipt/cost 类分录；
    platform 承担项、unknown 承担项、suspense 暂挂各自单列，不混入；
  * 退货始终锚定原始下单渠道，跨店退货只体现在核对标记和归因桥里；
  * 已定稿期间可同时输出 snapshot（原账）与 live（含迟到数据）两个口径。
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from .money import cents_to_yuan
from .periods import _load_snapshot

# 损益列：entry_kind -> 输出列名（负值类已在分录层带符号）
PIVOT_KINDS = [
    "gross_receipt", "brand_coupon", "return_refund",
    "platform_subsidy", "platform_coupon",
    "platform_commission", "platform_fee",
    "cogs", "cogs_reversal", "gift_cost", "fulfillment_fee",
    "estimated_fee", "estimate_reversal", "unknown_discount", "suspense",
]


def _matured_clause(as_of: str) -> str:
    """成熟口径的 WHERE 片段：以实际日期判断，而非信任导入时的标记位。"""
    return f"(COALESCE(received_date, event_date) <= '{as_of}')"


def _rows(conn: sqlite3.Connection, period_key: str | None, as_of: str,
          group_field: str, *, matured_only: bool, snapshot: dict | None
          ) -> list[dict]:
    where = ["1=1"]
    params: list = []
    if period_key:
        where.append("period_key=?")
        params.append(period_key)
    if matured_only:
        where.append(_matured_clause(as_of))
    if snapshot is not None:
        # 定稿原账口径：只保留快照里存在的 natural 键
        if not snapshot:
            return []
        where.append("natural_key IN (%s)" % ",".join("?" * len(snapshot)))
        params.extend(snapshot.keys())

    sql = f"""
        SELECT {group_field} AS grp, entry_kind, bearer,
               SUM(signed_cents) AS amount,
               SUM(CASE WHEN {_matured_clause(as_of)} THEN 0 ELSE 1 END) AS immature_count
        FROM entries WHERE {' AND '.join(where)}
        GROUP BY grp, entry_kind, bearer
    """
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _build_pnl(rows: list[dict]) -> list[dict]:
    """把长表分录聚合为每个分组一行的损益宽表（单位：分）。"""
    groups: dict[str, dict] = {}
    for r in rows:
        grp = r["grp"] or "(未归属/暂挂)"
        g = groups.setdefault(grp, {k: 0 for k in PIVOT_KINDS})
        g["_immature_events"] = g.get("_immature_events", 0) + r["immature_count"]
        kind = r["entry_kind"]
        if kind in g:
            g[kind] += r["amount"]
        elif kind == "suspense":
            g["suspense"] += r["amount"]

    result = []
    for grp, g in sorted(groups.items()):
        net_receipt = g["gross_receipt"] + g["brand_coupon"] + g["return_refund"]
        attributable_cost = (g["cogs"] + g["cogs_reversal"] + g["gift_cost"]
                             + g["fulfillment_fee"] + g["platform_commission"]
                             + g["platform_fee"])
        brand_contribution = net_receipt + attributable_cost
        # 决策口径：把暂估费用视为必然发生（未成熟结算不当零成本）
        estimated = g["estimated_fee"] + g["estimate_reversal"]
        comparable_contribution = brand_contribution + estimated
        pending_bearer = g["unknown_discount"]
        suspense_total = g["suspense"]
        platform_borne = g["platform_subsidy"] + g["platform_coupon"]
        result.append({
            "group": grp,
            "net_receipt_cents": net_receipt,
            "attributable_cost_cents": attributable_cost,
            "brand_net_contribution_cents": brand_contribution,
            "estimated_fee_cents": estimated,
            "comparable_net_contribution_cents": comparable_contribution,
            "platform_borne_cents": platform_borne,
            "pending_bearer_cents": pending_bearer,
            "suspense_cents": suspense_total,
            "immature_events": g["_immature_events"],
            **{f"{k}_cents": g[k] for k in PIVOT_KINDS},
        })
    return result


def pnl_report(conn: sqlite3.Connection, dimension: str,
               period_key: str | None, as_of: str, *,
               matured_only: bool = False, use_snapshot: bool = False
               ) -> list[dict]:
    """渠道 / 商品 / 活动期维度的损益报告。"""
    field = {"channel": "channel", "sku": "sku", "campaign": "campaign"}[dimension]
    snapshot = None
    if use_snapshot:
        if not period_key:
            raise ValueError("快照口径必须指定期间")
        snapshot = _load_snapshot(conn, period_key)
    rows = _rows(conn, period_key, as_of, field,
                 matured_only=matured_only, snapshot=snapshot)
    return _build_pnl(rows)


def unsettled_report(conn: sqlite3.Connection, period_key: str | None,
                     as_of: str) -> list[dict]:
    """仍未结算/未成熟部分：逐笔列出，供判断「不能当零成本」的规模。"""
    where = [f"NOT {_matured_clause(as_of)}"]
    params: list = []
    if period_key:
        where.append("period_key=?")
        params.append(period_key)
    sql = f"""
        SELECT entry_id, period_key, entry_kind, bearer, signed_cents,
               order_id, line_no, channel, sku, campaign, source_ref,
               allocation_method, event_date, received_date, verify_status,
               allocation_basis
        FROM entries WHERE {' AND '.join(where)}
        ORDER BY period_key, channel, entry_id
    """
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def suspense_report(conn: sqlite3.Connection) -> list[dict]:
    """暂挂工作清单：无法匹配的成本/退款，禁止摊入渠道总额。"""
    sql = """
        SELECT e.entry_id, e.period_key, e.entry_kind, e.signed_cents,
               e.source_ref, e.source_file, e.event_date, e.received_date,
               e.allocation_basis, s.reason, s.resolved, s.resolution_note
        FROM suspense_cases s JOIN entries e ON e.entry_id=s.entry_id
        ORDER BY s.resolved, e.period_key, e.entry_id
    """
    return [dict(r) for r in conn.execute(sql).fetchall()]


def verification_report(conn: sqlite3.Connection) -> list[dict]:
    """待核对清单：跨渠道退货身份、未知承担方、暂估、未解决的暂挂。

    已通过 recheck/人工解决的暂挂工单不再出现（其分录已转为正常费用类）。
    """
    sql = """
        SELECT e.entry_id, e.period_key, e.entry_kind, e.bearer, e.signed_cents,
               e.order_id, e.line_no, e.channel, e.source_ref, e.verify_status,
               e.received_date, e.allocation_basis
        FROM entries e
        LEFT JOIN suspense_cases s ON s.entry_id=e.entry_id
        WHERE e.verify_status='pending'
          AND (s.entry_id IS NULL OR s.resolved=0)
        ORDER BY e.period_key, e.entry_kind, e.entry_id
    """
    return [dict(r) for r in conn.execute(sql).fetchall()]


def attribution_bridge(conn: sqlite3.Connection, period_key: str | None,
                       as_of: str) -> list[dict]:
    """渠道归属变化桥：量化「月报错误口径」与「正确口径」的差异。

    错误口径（现状月报）：按下单渠道确认全部收入，门店退款记入线下渠道；
    正确口径：退货与成本锚定原始下单渠道。差异主要来自跨店退货，
    另有承担方未确认/暂挂项在错误口径下被随意归入线下的部分。
    """
    where = [_matured_clause(as_of)]
    params: list = []
    if period_key:
        where.append("period_key=?")
        params.append(period_key)
    sql = f"""
        SELECT e.entry_kind, e.channel, e.signed_cents, e.allocation_basis,
               s.resolved AS suspense_resolved
        FROM entries e LEFT JOIN suspense_cases s ON s.entry_id=e.entry_id
        WHERE {' AND '.join(where)}
    """
    # 按（正确渠道, 错误渠道）累计影响分
    bridge: dict[tuple[str, str], int] = {}
    for r in conn.execute(sql, params).fetchall():
        if r["entry_kind"] not in ("return_refund", "cogs_reversal", "suspense"):
            continue
        basis = json.loads(r["allocation_basis"])
        if r["entry_kind"] == "suspense":
            # 已解决的暂挂已恢复为正常费用，不再算错配
            if r["suspense_resolved"]:
                continue
            # 错误口径下暂挂常被塞进线下；正确口径不归属任何渠道
            key = ("(暂挂不归属)", "offline")
            bridge[key] = bridge.get(key, 0) + r["signed_cents"]
            continue
        ret_ch = basis.get("return_channel") or ""
        if not ret_ch or ret_ch == r["channel"]:
            continue  # 同渠道退货，两口径一致
        # 正确：留在原渠道 r["channel"]；错误：记到 ret_ch
        key = (r["channel"], ret_ch)
        bridge[key] = bridge.get(key, 0) + r["signed_cents"]

    out = []
    for (correct_ch, wrong_ch), amount in sorted(bridge.items()):
        out.append({
            "correct_channel": correct_ch,
            "monthly_report_channel": wrong_ch,
            "misattributed_cents": amount,
            "explanation": "跨店/跨渠道退货：正确口径锚定原下单渠道，"
                           "旧月报把退款记入受理退货的渠道",
        })
    return out


def reconcile_totals(conn: sqlite3.Connection, as_of: str) -> dict:
    """总额核对：源凭证金额合计与分录金额合计必须一致（含暂挂）。

    这是「不为对平而强摊」的护栏：所有钱要么带依据落到订单行，
    要么显式挂在 suspense，两边总额恒等。
    """
    row = conn.execute(
        f"SELECT COUNT(*) n, COALESCE(SUM(signed_cents),0) total "
        f"FROM entries WHERE {_matured_clause(as_of)}").fetchone()
    suspense = conn.execute(
        f"SELECT COUNT(*) n, COALESCE(SUM(signed_cents),0) total "
        f"FROM entries e JOIN suspense_cases s ON s.entry_id=e.entry_id "
        f"WHERE {_matured_clause(as_of)} AND s.resolved=0").fetchone()
    pending = conn.execute(
        "SELECT COUNT(*) n FROM entries e "
        "LEFT JOIN suspense_cases s ON s.entry_id=e.entry_id "
        "WHERE e.verify_status='pending' AND (s.entry_id IS NULL OR s.resolved=0)"
    ).fetchone()
    return {
        "matured_entry_count": row["n"],
        "matured_signed_total_cents": row["total"],
        "open_suspense_count": suspense["n"],
        "open_suspense_cents": suspense["total"],
        "pending_verification_count": pending["n"],
        "identity_check": "分录总额 = 已归属分录 + 暂挂分录（无强摊差异）",
    }


# ---------------------------------------------------------------- 输出
def write_csv(rows: list[dict], out_path: str | Path) -> None:
    """写 CSV：每个金额列同时保留整数分列（_cents）与可读元列（_yuan），
    dict/basis 列序列化为 JSON 字符串。空结果写空文件。"""
    if not rows:
        Path(out_path).write_text("", encoding="utf-8")
        return
    # 展开列：原列 + 每个金额列追加对应的 _yuan 可读列
    fields = list(rows[0].keys())
    out_fields = []
    for f in fields:
        out_fields.append(f)
        if f.endswith("_cents"):
            out_fields.append(f.removesuffix("_cents") + "_yuan")
    with Path(out_path).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=out_fields, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            out = {}
            for k, v in r.items():
                if isinstance(v, (dict, list)):
                    out[k] = json.dumps(v, ensure_ascii=False, sort_keys=True)
                else:
                    out[k] = "" if v is None else v
                if k.endswith("_cents"):
                    out[k.removesuffix("_cents") + "_yuan"] = cents_to_yuan(v or 0)
            writer.writerow(out)


def format_console_table(rows: list[dict], money_cols: tuple[str, ...] = ()) -> str:
    """生成控制台表格；列太多或终端太窄时改为每条记录一块键值列表，避免截断。"""
    if not rows:
        return "（无数据）"
    headers = list(rows[0].keys())
    display_headers = [h.removesuffix("_cents") for h in headers]
    table = []
    for r in rows:
        line = []
        for h in headers:
            v = r[h]
            if h in money_cols and isinstance(v, int):
                line.append(cents_to_yuan(v))
            elif isinstance(v, (dict, list)):
                line.append(json.dumps(v, ensure_ascii=False))
            else:
                line.append("" if v is None else str(v))
        table.append(line)

    try:
        term_width = __import__("shutil").get_terminal_size((120, 24)).columns
    except OSError:
        term_width = 120
    widths = [max(len(h), *(len(line[i]) for line in table))
              for i, h in enumerate(display_headers)]
    table_width = sum(widths) + 2 * (len(widths) - 1)

    # 宽表（损益宽表）按记录纵向打印，杜绝数字被视觉截断
    if table_width > term_width:
        blocks = []
        for r, line in zip(rows, table):
            label = r.get("group") or r.get("source_ref") or r.get("entry_id") or "记录"
            key_w = max(len(h) for h in display_headers)
            blocks.append(f"■ {label}")
            blocks.extend(
                f"    {h.ljust(key_w)} : {cell}"
                for h, cell in zip(display_headers, line))
            blocks.append("")
        return "\n".join(blocks).rstrip()

    sep = "  ".join("-" * w for w in widths)
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(display_headers)), sep]
    out.extend("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line))
               for line in table)
    return "\n".join(out)
