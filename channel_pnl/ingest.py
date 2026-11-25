"""数据摄入、校验与分摊引擎。

设计原则（对应业务要求）：
  * 六类源文件加旧版双文件样例，统一规范为带分摊依据的会计分录；
  * 每条分录都带 source_ref / allocation_method / allocation_basis，可逐笔核对；
  * 结构合法但匹配不到订单的事件进 **suspense 暂挂**，绝不按销售额强摊；
  * 一张凭证覆盖多笔订单、订单级费用拆行、部分退款都留存权重与计算式；
  * 未成熟结算按费率规则生成暂估分录，实际费用到达后冲销，不当零成本；
  * 自然键去重，同一目录重复导入幂等，修订就地更新并保留核对留痕。
"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import domain as D
from .db import transaction
from .money import cents_to_yuan, split_cents, yuan_to_cents

# 新版六张表的表头定义（顺序即文件约定顺序）
ORDER_COLS = ["order_id", "line", "channel", "sku", "campaign",
              "order_date", "paid", "cogs", "state"]
SETTLE_COLS = ["ref", "order_id", "line", "kind", "amount", "bearer",
               "event_date", "received", "rate"]
DISCOUNT_COLS = ["ref", "order_id", "line", "kind", "amount", "bearer",
                 "event_date", "received"]
GIFT_COLS = ["ref", "order_id", "line", "sku", "amount", "bearer",
             "event_date", "received"]
FULFILL_COLS = ["ref", "order_id", "line", "amount", "bearer",
                "event_date", "received"]
RETURN_COLS = ["ref", "order_id", "line", "refund_amount", "restocked",
               "return_channel", "event_date", "received"]
ESTRULE_COLS = ["channel", "kind", "rate", "basis", "note"]

SETTLE_KINDS = {"platform_commission", "platform_fee", "platform_subsidy"}
DISCOUNT_KINDS = {"brand_coupon", "platform_coupon", "unknown_discount"}


# ---------------------------------------------------------------- 基础工具
def read_csv(path: Path) -> list[dict]:
    """读取 CSV，附加 ``_lineno``（数据行从 2 开始，对应电子表格行号）。"""
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, strict=True)
        if reader.fieldnames is None:
            raise ValueError(f"{path.name} 缺少表头")
        rows = []
        for i, row in enumerate(reader, start=2):
            if None in row or None in row.values():
                raise ValueError(f"{path.name} 第 {i} 行列数与表头不一致")
            row["_lineno"] = i
            rows.append(row)
    return rows


def check_header(path: Path, expected: list[str]) -> None:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh, strict=True)
        header = next(reader, [])
    if header != expected:
        raise ValueError(
            f"{path.name} 表头不符。\n  期望: {expected}\n  实际: {header}"
        )


def parse_date(raw: str, *, allow_blank: bool = False) -> str | None:
    """校验 ISO 日期；allow_blank 时空值返回 None（表示尚未到达/结算）。"""
    text = (raw or "").strip()
    if text == "":
        if allow_blank:
            return None
        raise ValueError("业务日期不能为空")
    date.fromisoformat(text)  # 非法日期直接抛错
    return text


def period_of(iso_date: str) -> str:
    return iso_date[:7]


class Ingester:
    """单次导入批次的执行器。"""

    def __init__(self, conn: sqlite3.Connection, as_of: str, note: str = ""):
        self.conn = conn
        self.as_of = as_of
        self.note = note
        self.run_id: int | None = None
        self.rejected = 0
        self.emitted = 0
        self.suspended = 0
        self.touched_files: list[str] = []
        # 订单行缓存：order_id -> {line_no: row(dict)}
        self._orders: dict[str, dict[int, sqlite3.Row]] = {}
        self._est_rules: list[dict] = []

    # ------------------------------------------------------------- 杂项
    def reject(self, file_name: str, line_no: int, reason: str, raw: dict) -> None:
        self.conn.execute(
            "INSERT INTO rejections(run_id,file_name,line_no,reason,raw_row) "
            "VALUES (?,?,?,?,?)",
            (self.run_id, file_name, line_no, reason,
             json.dumps(raw, ensure_ascii=False, sort_keys=True)),
        )
        self.rejected += 1

    def ensure_period(self, period_key: str) -> None:
        self.conn.execute(
            "INSERT INTO periods(period_key,state) VALUES(?,?) "
            "ON CONFLICT(period_key) DO NOTHING",
            (period_key, D.PERIOD_OPEN),
        )

    def order_line(self, order_id: str, line_no: int | None):
        """返回订单行；line_no 为空时返回该订单的全部行（用于订单级拆行）。"""
        if order_id not in self._orders:
            rows = self.conn.execute(
                "SELECT * FROM order_lines WHERE order_id=?", (order_id,)
            ).fetchall()
            self._orders[order_id] = {r["line_no"]: r for r in rows}
        lines = self._orders[order_id]
        if line_no is None:
            return list(lines.values())
        return lines.get(int(line_no))

    # ------------------------------------------------------------- 分录落库
    def emit(self, *, kind: str, bearer: str, amount_cents: int,
             order_id: str | None, line_no: int | None, channel: str | None,
             sku: str, campaign: str, period_key: str, source_ref: str,
             source_file: str, source_line: int, method: str,
             basis: dict, event_date: str, received_date: str | None,
             verify_status: str = D.STATUS_CONFIRMED,
             supersedes: int | None = None) -> int:
        """按自然键幂等写入一条分录，返回 entry_id。

        重复导入同一凭证时就地更新金额/承担方/日期；若承担方被改动，
        核对状态回退为 pending，要求财务重新确认（历史留痕仍在 confirmations）。
        """
        signed = D.ENTRY_KIND_SIGN[kind] * amount_cents
        natural = "|".join([
            source_file, source_ref, kind,
            order_id or "", str(line_no or ""),
        ])
        basis_json = json.dumps(basis, ensure_ascii=False, sort_keys=True)
        matured = 1 if (received_date or event_date) <= self.as_of else 0
        old = self.conn.execute(
            "SELECT * FROM entries WHERE natural_key=?", (natural,)
        ).fetchone()
        if old is not None:
            reset = D.STATUS_PENDING if old["bearer"] != bearer else old["verify_status"]
            self.conn.execute(
                "UPDATE entries SET signed_cents=?, bearer=?, period_key=?, "
                "channel=?, sku=?, campaign=?, allocation_basis=?, event_date=?, "
                "received_date=?, matured=?, verify_status=? WHERE entry_id=?",
                (signed, bearer, period_key, channel, sku, campaign, basis_json,
                 event_date, received_date, matured, reset, old["entry_id"]),
            )
            return old["entry_id"]

        cur = self.conn.execute(
            "INSERT INTO entries(run_id,period_key,entry_kind,bearer,signed_cents,"
            "order_id,line_no,channel,sku,campaign,source_ref,source_file,source_line,"
            "allocation_method,allocation_basis,event_date,received_date,matured,"
            "verify_status,supersedes_entry_id,natural_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, period_key, kind, bearer, signed, order_id, line_no,
             channel, sku, campaign, source_ref, source_file, source_line, method,
             basis_json, event_date, received_date, matured, verify_status,
             supersedes, natural),
        )
        self.emitted += 1
        return cur.lastrowid

    def emit_suspense(self, *, kind: str, amount_cents: int, source_ref: str,
                      source_file: str, source_line: int, reason: str,
                      event_date: str, received_date: str | None,
                      raw: dict) -> int:
        """结构合法但无法匹配订单：挂暂挂，不摊入任何渠道。"""
        entry_id = self.emit(
            kind="suspense",
            bearer=D.BEARER_UNKNOWN, amount_cents=abs(amount_cents),
            order_id=None, line_no=None, channel=None, sku="", campaign="",
            period_key=period_of(event_date), source_ref=source_ref,
            source_file=source_file, source_line=source_line,
            method=D.ALLOC_SUSPENSE,
            basis={"reason": reason, "original_kind": kind, "raw_event": raw},
            event_date=event_date, received_date=received_date,
            verify_status=D.STATUS_PENDING,
        )
        self.conn.execute(
            "INSERT INTO suspense_cases(entry_id,reason) VALUES (?,?) "
            "ON CONFLICT(entry_id) DO NOTHING", (entry_id, reason),
        )
        self.suspended += 1
        return entry_id

    # ------------------------------------------------------------- 订单级拆行
    def split_across_order(self, order_id: str, amount_cents: int,
                           method: str, basis_extra: dict
                           ) -> list[tuple[sqlite3.Row, int, dict]]:
        """把订单级金额按各行实付权重拆到订单行，返回(行, 分摊额, 依据)。"""
        lines = self.order_line(order_id, None)
        weights = [max(r["paid_cents"], 0) for r in lines]
        shares = split_cents(amount_cents, weights)
        out = []
        for row, share in zip(lines, shares):
            basis = {
                "allocation": method,
                "order_total_paid_cents": sum(r["paid_cents"] for r in lines),
                "line_weights_paid_cents": {
                    str(r["line_no"]): r["paid_cents"] for r in lines
                },
                "amount_cents": amount_cents,
                "share_cents": share,
                "formula": f"{cents_to_yuan(amount_cents)} × 本行实付 / 订单实付",
                **basis_extra,
            }
            out.append((row, share, basis))
        return out

    # ------------------------------------------------------------- 主流程
    def ingest_dir(self, source_dir: str | Path) -> dict:
        source = Path(source_dir)
        if not source.is_dir():
            raise FileNotFoundError(f"输入目录不存在: {source}")
        present = {p.name for p in source.glob("*.csv")}

        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO runs(imported_at,source_dir,file_names,note) "
                "VALUES (?,?,?,?)",
                (self.as_of, str(source), json.dumps(sorted(present)), self.note),
            )
            self.run_id = cur.lastrowid

            if "orders.csv" in present and "adjustments.csv" in present:
                # 旧版双文件样例：表头与新版六表不同，走兼容加载器
                self._load_legacy_orders(source / "orders.csv")
            elif "orders.csv" in present:
                check_header(source / "orders.csv", ORDER_COLS)
                self._load_orders(source / "orders.csv")
            for loader, fname in (
                (self._load_settlements, "settlements.csv"),
                (self._load_discounts, "discounts.csv"),
                (self._load_gifts, "gifts.csv"),
                (self._load_fulfillment, "fulfillment.csv"),
                (self._load_returns, "returns.csv"),
                (self._load_est_rules, "estimate_rules.csv"),
            ):
                if fname in present:
                    self.touched_files.append(fname)
                    loader(source / fname)

            # 旧版样例兼容：orders.csv + adjustments.csv
            if "adjustments.csv" in present and "settlements.csv" not in present:
                self.touched_files.append("adjustments.csv")
                self._load_legacy(source / "adjustments.csv")

            self._apply_estimates()

        return {
            "run_id": self.run_id, "emitted": self.emitted,
            "rejected": self.rejected, "suspended": self.suspended,
            "files": sorted(present),
        }

    # ------------------------------------------------------------- 订单
    def _load_orders(self, path: Path) -> None:
        for row in read_csv(path):
            ln = row["_lineno"]
            try:
                order_id = row["order_id"].strip()
                line_no = int(row["line"])
                channel = row["channel"].strip()
                if not order_id:
                    raise ValueError("order_id 为空")
                if channel not in D.CHANNELS:
                    raise ValueError(f"未知渠道 {channel}")
                order_date = parse_date(row["order_date"])
                paid = yuan_to_cents(row["paid"])
                cogs = yuan_to_cents(row["cogs"])
                if paid < 0 or cogs < 0:
                    raise ValueError("金额不能为负")
                state = row["state"].strip()
                if state not in D.ORDER_STATES:
                    raise ValueError(f"未知订单状态 {state}")
            except (ValueError, TypeError) as exc:
                self.reject(path.name, ln, f"订单行校验失败: {exc}", row)
                continue

            period_key = period_of(order_date)
            self.ensure_period(period_key)
            self.conn.execute(
                "INSERT INTO order_lines(order_id,line_no,channel,sku,campaign,"
                "order_date,period_key,paid_cents,cogs_cents,state,run_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(order_id,line_no) DO UPDATE SET channel=excluded.channel,"
                "sku=excluded.sku,campaign=excluded.campaign,order_date=excluded.order_date,"
                "period_key=excluded.period_key,paid_cents=excluded.paid_cents,"
                "cogs_cents=excluded.cogs_cents,state=excluded.state,run_id=excluded.run_id",
                (order_id, line_no, channel, row["sku"].strip(), row["campaign"].strip(),
                 order_date, period_key, paid, cogs, state, self.run_id),
            )
            self._orders.pop(order_id, None)
            received = order_date if state == "settled" else None
            common = dict(
                order_id=order_id, line_no=line_no, channel=channel,
                sku=row["sku"].strip(), campaign=row["campaign"].strip(),
                period_key=period_key, source_ref=f"{order_id}#L{line_no}",
                source_file=path.name, source_line=ln,
                method=D.ALLOC_DIRECT, event_date=order_date,
            )
            self.emit(kind="gross_receipt", bearer=D.BEARER_BRAND,
                      amount_cents=paid, received_date=received,
                      basis={"paid_cents": paid, "order_state": state}, **common)
            self.emit(kind="cogs", bearer=D.BEARER_BRAND,
                      amount_cents=cogs, received_date=order_date,
                      basis={"cogs_cents": cogs}, **common)

    # ------------------------------------------------------------- 通用事件
    def _load_money_event(self, path: Path, expected_header: list[str],
                          allowed_kinds: set[str], *, amount_col: str) -> None:
        """结算/优惠/赠品/履约的通用处理：校验、匹配、拆行或暂挂。"""
        check_header(path, expected_header)
        for row in read_csv(path):
            ln = row["_lineno"]
            try:
                kind = row["kind"].strip()
                if kind not in allowed_kinds:
                    raise ValueError(f"未知事件类型 {kind}")
                bearer = row["bearer"].strip()
                if bearer not in D.BEARERS:
                    raise ValueError(f"未知承担方 {bearer}")
                amount = yuan_to_cents(row[amount_col])
                if amount <= 0:
                    raise ValueError(f"{amount_col} 必须为正数")
                event_date = parse_date(row["event_date"])
                received = parse_date(row["received"], allow_blank=True)
                line_no = int(row["line"]) if row["line"].strip() else None
            except (ValueError, TypeError) as exc:
                self.reject(path.name, ln, f"{path.name} 行校验失败: {exc}", row)
                continue

            order_id = row["order_id"].strip()
            lines = self.order_line(order_id, None)
            if not lines:
                self.emit_suspense(
                    kind=kind, amount_cents=amount, source_ref=row["ref"].strip(),
                    source_file=path.name, source_line=ln,
                    reason=f"找不到订单 {order_id}", event_date=event_date,
                    received_date=received, raw=row)
                continue

            if line_no is not None:
                target = self.order_line(order_id, line_no)
                if target is None:
                    self.emit_suspense(
                        kind=kind, amount_cents=amount, source_ref=row["ref"].strip(),
                        source_file=path.name, source_line=ln,
                        reason=f"订单 {order_id} 不存在第 {line_no} 行",
                        event_date=event_date, received_date=received, raw=row)
                    continue
                targets = [(target, amount, {
                    "allocation": D.ALLOC_DIRECT, "amount_cents": amount,
                    "rate": row.get("rate", ""),
                })]
                method = D.ALLOC_DIRECT
            else:
                targets = self.split_across_order(
                    order_id, amount, D.ALLOC_WEIGHTED,
                    {"source_ref": row["ref"].strip(), "rate": row.get("rate", "")})
                method = D.ALLOC_WEIGHTED

            for oline, share, basis in targets:
                self.emit(
                    kind=kind, bearer=bearer, amount_cents=share,
                    order_id=order_id, line_no=oline["line_no"],
                    channel=oline["channel"], sku=oline["sku"],
                    campaign=oline["campaign"],
                    period_key=oline["period_key"],
                    source_ref=row["ref"].strip(), source_file=path.name,
                    source_line=ln, method=method, basis=basis,
                    event_date=event_date, received_date=received)

    def _load_settlements(self, path: Path) -> None:
        self._load_money_event(path, SETTLE_COLS, SETTLE_KINDS, amount_col="amount")

    def _load_discounts(self, path: Path) -> None:
        self._load_money_event(path, DISCOUNT_COLS, DISCOUNT_KINDS, amount_col="amount")

    def _load_gifts(self, path: Path) -> None:
        # 赠品表结构与通用事件略有差异（含 sku 列、无 kind），单独适配
        check_header(path, GIFT_COLS)
        for row in read_csv(path):
            ln = row["_lineno"]
            try:
                bearer = row["bearer"].strip()
                if bearer not in D.BEARERS:
                    raise ValueError(f"未知承担方 {bearer}")
                amount = yuan_to_cents(row["amount"])
                if amount <= 0:
                    raise ValueError("赠品成本必须为正数")
                event_date = parse_date(row["event_date"])
                received = parse_date(row["received"], allow_blank=True)
                line_no = int(row["line"]) if row["line"].strip() else None
            except (ValueError, TypeError) as exc:
                self.reject(path.name, ln, f"赠品行校验失败: {exc}", row)
                continue
            self._dispatch_cost_row(path.name, ln, row, "gift_cost", bearer,
                                    amount, event_date, received, line_no)

    def _load_fulfillment(self, path: Path) -> None:
        check_header(path, FULFILL_COLS)
        for row in read_csv(path):
            ln = row["_lineno"]
            try:
                bearer = row["bearer"].strip()
                if bearer not in D.BEARERS:
                    raise ValueError(f"未知承担方 {bearer}")
                amount = yuan_to_cents(row["amount"])
                if amount <= 0:
                    raise ValueError("履约费用必须为正数")
                event_date = parse_date(row["event_date"])
                received = parse_date(row["received"], allow_blank=True)
                line_no = int(row["line"]) if row["line"].strip() else None
            except (ValueError, TypeError) as exc:
                self.reject(path.name, ln, f"履约行校验失败: {exc}", row)
                continue
            self._dispatch_cost_row(path.name, ln, row, "fulfillment_fee",
                                    bearer, amount, event_date, received, line_no)

    def _dispatch_cost_row(self, file_name, ln, row, kind, bearer, amount,
                           event_date, received, line_no) -> None:
        order_id = row["order_id"].strip()
        if not self.order_line(order_id, None):
            self.emit_suspense(
                kind=kind, amount_cents=amount, source_ref=row["ref"].strip(),
                source_file=file_name, source_line=ln,
                reason=f"找不到订单 {order_id}", event_date=event_date,
                received_date=received, raw=row)
            return
        if line_no is not None:
            oline = self.order_line(order_id, line_no)
            if oline is None:
                self.emit_suspense(
                    kind=kind, amount_cents=amount, source_ref=row["ref"].strip(),
                    source_file=file_name, source_line=ln,
                    reason=f"订单 {order_id} 不存在第 {line_no} 行",
                    event_date=event_date, received_date=received, raw=row)
                return
            targets = [(oline, amount, {"allocation": D.ALLOC_DIRECT,
                                        "amount_cents": amount})]
            method = D.ALLOC_DIRECT
        else:
            targets = self.split_across_order(
                order_id, amount, D.ALLOC_WEIGHTED,
                {"source_ref": row["ref"].strip()})
            method = D.ALLOC_WEIGHTED
        for oline, share, basis in targets:
            self.emit(
                kind=kind, bearer=bearer, amount_cents=share,
                order_id=order_id, line_no=oline["line_no"],
                channel=oline["channel"],
                sku=row.get("sku", "").strip() or oline["sku"],
                campaign=oline["campaign"], period_key=oline["period_key"],
                source_ref=row["ref"].strip(), source_file=file_name,
                source_line=ln, method=method, basis=basis,
                event_date=event_date, received_date=received)

    # ------------------------------------------------------------- 退货
    def _load_returns(self, path: Path) -> None:
        check_header(path, RETURN_COLS)
        rows = read_csv(path)
        # 同一 ref 出现多行 = 一张退款凭证覆盖多笔订单（voucher-split）
        ref_counts: dict[str, int] = {}
        voucher_totals: dict[str, int] = {}
        for row in rows:
            ref = row["ref"].strip()
            ref_counts[ref] = ref_counts.get(ref, 0) + 1
            try:
                voucher_totals[ref] = voucher_totals.get(ref, 0) + yuan_to_cents(row["refund_amount"])
            except ValueError:
                # 非法金额行会在主循环里进 rejections，这里不阻断总额统计
                pass

        for row in rows:
            ln = row["_lineno"]
            ref = row["ref"].strip()
            try:
                refund = yuan_to_cents(row["refund_amount"])
                if refund <= 0:
                    raise ValueError("退款金额必须为正数")
                restocked = row["restocked"].strip() in ("1", "true", "TRUE", "yes")
                event_date = parse_date(row["event_date"])
                received = parse_date(row["received"], allow_blank=True)
                line_no = int(row["line"]) if row["line"].strip() else None
                return_channel = row["return_channel"].strip()
            except (ValueError, TypeError) as exc:
                self.reject(path.name, ln, f"退货行校验失败: {exc}", row)
                continue

            order_id = row["order_id"].strip()
            if not self.order_line(order_id, None):
                self.emit_suspense(
                    kind="return_refund", amount_cents=refund, source_ref=ref,
                    source_file=path.name, source_line=ln,
                    reason=f"退货凭证 {ref} 找不到订单 {order_id}",
                    event_date=event_date, received_date=received, raw=row)
                continue

            if line_no is None:
                # 凭证只给了订单级总额：按订单行实付拆，依据完整保留
                targets = self.split_across_order(
                    order_id, refund, D.ALLOC_WEIGHTED,
                    {"voucher_ref": ref, "restocked": restocked})
            else:
                oline = self.order_line(order_id, line_no)
                if oline is None:
                    self.emit_suspense(
                        kind="return_refund", amount_cents=refund, source_ref=ref,
                        source_file=path.name, source_line=ln,
                        reason=f"订单 {order_id} 不存在第 {line_no} 行",
                        event_date=event_date, received_date=received, raw=row)
                    continue
                targets = [(oline, refund, {
                    "allocation": D.ALLOC_DIRECT, "refund_cents": refund})]

            multi = ref_counts[ref] > 1 or line_no is None
            for oline, share, basis in targets:
                # 跨店/跨渠道退货：归属仍是原下单渠道，只标记待运营核对身份
                cross = bool(return_channel) and return_channel != oline["channel"]
                if multi:
                    method = D.ALLOC_VOUCHER
                    basis = {**basis, "allocation": D.ALLOC_VOUCHER,
                             "voucher_ref": ref,
                             "voucher_total_cents": voucher_totals.get(ref, share)}
                else:
                    method = basis.get("allocation", D.ALLOC_DIRECT)
                basis = {**basis, "original_channel": oline["channel"],
                         "return_channel": return_channel,
                         "cross_channel": cross, "restocked": restocked}
                self.emit(
                    kind="return_refund",
                    bearer=D.BEARER_BRAND, amount_cents=share,
                    order_id=order_id, line_no=oline["line_no"],
                    channel=oline["channel"], sku=oline["sku"],
                    campaign=oline["campaign"], period_key=oline["period_key"],
                    source_ref=ref, source_file=path.name, source_line=ln,
                    method=method, basis=basis, event_date=event_date,
                    received_date=received,
                    verify_status=D.STATUS_PENDING if cross else D.STATUS_CONFIRMED)

                # 可二次销售的退货：按退款占实付比例冲回商品成本
                # （全额退款冲回全部成本；部分退款按比例，整数分 + 最大余数法）
                if restocked and oline["paid_cents"] > 0:
                    if share >= oline["paid_cents"]:
                        reversal = oline["cogs_cents"]
                    else:
                        reversal = split_cents(
                            oline["cogs_cents"],
                            [share, oline["paid_cents"] - share])[0]
                    if reversal:
                        self.emit(
                            kind="cogs_reversal", bearer=D.BEARER_BRAND,
                            amount_cents=reversal, order_id=order_id,
                            line_no=oline["line_no"], channel=oline["channel"],
                            sku=oline["sku"], campaign=oline["campaign"],
                            period_key=oline["period_key"],
                            source_ref=f"{ref}#cogs-reversal",
                            source_file=path.name, source_line=ln,
                            method=method,
                            basis={"allocation": method, "cogs_cents": oline["cogs_cents"],
                                   "refund_cents": share,
                                   "paid_cents": oline["paid_cents"],
                                   "formula": "商品成本 × 退款 / 实付"},
                            event_date=event_date, received_date=received,
                            verify_status=D.STATUS_PENDING if cross else D.STATUS_CONFIRMED)

    # ------------------------------------------------------------- 暂估规则
    def _load_est_rules(self, path: Path) -> None:
        check_header(path, ESTRULE_COLS)
        for row in read_csv(path):
            ln = row["_lineno"]
            try:
                channel = row["channel"].strip()
                if channel not in D.CHANNELS:
                    raise ValueError(f"未知渠道 {channel}")
                kind = row["kind"].strip()
                if kind not in ("fulfillment_fee", "platform_commission", "platform_fee"):
                    raise ValueError(f"暂估仅支持费用类，收到 {kind}")
                rate_text = row["rate"].strip()
                # 费率转整数基点（1bp = 0.01%），全程整数运算
                basis_points = int(Decimal(rate_text) * 10000)
                if not 0 < basis_points < 10000:
                    raise ValueError("rate 应落在 (0,1)")
            except (ValueError, TypeError, InvalidOperation) as exc:
                self.reject(path.name, ln, f"暂估规则校验失败: {exc}", row)
                continue
            self._est_rules.append({
                "channel": channel, "kind": kind,
                "basis_points": basis_points, "rate": rate_text,
                "basis": row["basis"].strip() or "paid", "note": row["note"].strip(),
                "source_line": ln,
            })
            # 规则持久化：后续批次即使不带规则文件，也沿用同一套暂估口径
            self.conn.execute(
                "INSERT INTO estimate_rules(channel,entry_kind,basis_points,"
                "rate_text,basis,note,updated_run_id) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(channel,entry_kind) DO UPDATE SET "
                "basis_points=excluded.basis_points, rate_text=excluded.rate_text, "
                "basis=excluded.basis, note=excluded.note, updated_run_id=excluded.updated_run_id",
                (channel, kind, basis_points, rate_text,
                 row["basis"].strip() or "paid", row["note"].strip(), self.run_id))

    def _load_rule_table(self) -> list[dict]:
        return [{
            "channel": r["channel"],
            "kind": r["entry_kind"],
            "basis_points": r["basis_points"],
            "rate": r["rate_text"],
            "basis": r["basis"],
            "note": r["note"],
            "source_line": 0,
        } for r in self.conn.execute(
            "SELECT * FROM estimate_rules ORDER BY rule_id").fetchall()]

    def _apply_estimates(self) -> None:
        """对缺少实际费用分录的订单行按规则暂估；实际到达后冲销旧暂估。

        规则来自持久化的 estimate_rules 表，因此任何一次导入都会全量重算，
        不依赖当次目录是否携带规则文件。
        """
        rules = self._est_rules or self._load_rule_table()
        if not rules:
            return
        lines = self.conn.execute("SELECT * FROM order_lines").fetchall()
        for oline in lines:
            for rule in rules:
                channel = rule["channel"]
                kind = rule["kind"]
                bp = rule.get("basis_points")
                rate_text = rule.get("rate_text") or rule.get("rate")
                if channel != oline["channel"]:
                    continue
                actual = self.conn.execute(
                    "SELECT entry_id FROM entries WHERE order_id=? AND line_no=? "
                    "AND entry_kind=? AND bearer=? AND verify_status!=?",
                    (oline["order_id"], oline["line_no"], rule["kind"],
                     D.BEARER_BRAND, D.STATUS_SUPERSEDED)).fetchone()
                active_est = self.conn.execute(
                    "SELECT entry_id FROM entries WHERE order_id=? AND line_no=? "
                    "AND entry_kind='estimated_fee' AND verify_status!=?",
                    (oline["order_id"], oline["line_no"], D.STATUS_SUPERSEDED)).fetchone()
                if actual:
                    if active_est:
                        self._supersede_estimate(active_est["entry_id"], oline, rule)
                    continue
                if active_est:
                    continue
                amount = split_cents(
                    oline["paid_cents"], [bp, 10000 - bp])[0]
                if amount <= 0:
                    continue
                self.emit(
                    kind="estimated_fee", bearer=D.BEARER_BRAND,
                    amount_cents=amount, order_id=oline["order_id"],
                    line_no=oline["line_no"], channel=oline["channel"],
                    sku=oline["sku"], campaign=oline["campaign"],
                    period_key=oline["period_key"],
                    source_ref=f"EST:{kind}:{oline['order_id']}#L{oline['line_no']}",
                    source_file="estimate_rules.csv",
                    source_line=rule.get("source_line", 0), method=D.ALLOC_ESTIMATE,
                    basis={"estimated_for": kind, "rate": rate_text,
                           "basis_points": bp,
                           "basis": rule.get("basis", "paid"),
                           "paid_cents": oline["paid_cents"],
                           "formula": f"实付 × {rate_text}",
                           "note": rule.get("note", "")},
                    event_date=oline["order_date"], received_date=None,
                    verify_status=D.STATUS_PENDING)

    def _supersede_estimate(self, est_entry_id: int, oline: sqlite3.Row,
                            rule: dict) -> None:
        est = self.conn.execute(
            "SELECT * FROM entries WHERE entry_id=?", (est_entry_id,)).fetchone()
        self.conn.execute(
            "UPDATE entries SET verify_status=? WHERE entry_id=?",
            (D.STATUS_SUPERSEDED, est_entry_id))
        # 冲销分录与原暂估金额相反（estimated_fee 为负，reversal 为正），
        # 同期间、同订单行，净效果归零，实际费用另行入账。
        self.emit(
            kind="estimate_reversal", bearer=D.BEARER_BRAND,
            amount_cents=abs(est["signed_cents"]), order_id=oline["order_id"],
            line_no=oline["line_no"], channel=oline["channel"], sku=oline["sku"],
            campaign=oline["campaign"], period_key=oline["period_key"],
            source_ref=f"{est['source_ref']}#reversal",
            source_file="estimate_rules.csv",
            source_line=rule.get("source_line", 0),
            method=D.ALLOC_ESTIMATE,
            basis={"reverses_entry_id": est_entry_id,
                   "reason": f"实际 {rule['kind']} 已到达"},
            event_date=est["event_date"], received_date=self.as_of,
            supersedes=est_entry_id)

    # ------------------------------------------------------------- 旧版样例
    LEGACY_ORDER_COLS = ["order_id", "line", "channel", "paid", "cost", "state"]

    def _load_legacy_orders(self, path: Path) -> None:
        """旧版 orders.csv：无 sku/campaign/order_date，成本列名为 cost。

        缺失的业务日期用本次 as_of 兜底（期间归入 as_of 所在月），
        并在 basis 中标记 legacy_format，保持与新表同一套落库逻辑。
        """
        check_header(path, self.LEGACY_ORDER_COLS)
        for row in read_csv(path):
            ln = row["_lineno"]
            try:
                order_id = row["order_id"].strip()
                line_no = int(row["line"])
                channel = row["channel"].strip()
                if channel not in D.CHANNELS:
                    raise ValueError(f"未知渠道 {channel}")
                paid = yuan_to_cents(row["paid"])
                cogs = yuan_to_cents(row["cost"])
                state = row["state"].strip()
                if state not in D.ORDER_STATES:
                    raise ValueError(f"未知订单状态 {state}")
            except (ValueError, TypeError) as exc:
                self.reject(path.name, ln, f"旧版订单行校验失败: {exc}", row)
                continue
            period_key = period_of(self.as_of)
            self.ensure_period(period_key)
            self.conn.execute(
                "INSERT INTO order_lines(order_id,line_no,channel,sku,campaign,"
                "order_date,period_key,paid_cents,cogs_cents,state,run_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(order_id,line_no) DO UPDATE SET channel=excluded.channel,"
                "paid_cents=excluded.paid_cents,cogs_cents=excluded.cogs_cents,"
                "state=excluded.state,run_id=excluded.run_id",
                (order_id, line_no, channel, "", "", self.as_of, period_key,
                 paid, cogs, state, self.run_id))
            self._orders.pop(order_id, None)
            common = dict(
                order_id=order_id, line_no=line_no, channel=channel, sku="",
                campaign="", period_key=period_key,
                source_ref=f"{order_id}#L{line_no}", source_file=path.name,
                source_line=ln, method=D.ALLOC_DIRECT, event_date=self.as_of)
            self.emit(kind="gross_receipt", bearer=D.BEARER_BRAND,
                      amount_cents=paid, received_date=self.as_of,
                      basis={"paid_cents": paid, "order_state": state,
                             "legacy_format": True}, **common)
            self.emit(kind="cogs", bearer=D.BEARER_BRAND,
                      amount_cents=cogs, received_date=self.as_of,
                      basis={"cogs_cents": cogs, "legacy_format": True}, **common)

    def _load_legacy(self, path: Path) -> None:
        """读取 samples/ 风格的 adjustments.csv，走同一套事件管线。"""
        expected = ["ref", "order_id", "kind", "amount", "owner", "received"]
        check_header(path, expected)
        for row in read_csv(path):
            ln = row["_lineno"]
            try:
                kind = D.LEGACY_KIND_MAP[row["kind"].strip()]
                bearer = D.LEGACY_BEARER_MAP[row["owner"].strip()]
                amount = yuan_to_cents(row["amount"])
                received = parse_date(row["received"], allow_blank=True)
            except (ValueError, TypeError) as exc:
                self.reject(path.name, ln, f"旧版调整行校验失败: {exc}", row)
                continue
            fake = {**row, "line": "", "event_date": received or self.as_of,
                    "rate": "", "sku": ""}
            if kind == "return_refund":
                # 旧样例无 restock/return_channel 信息：不做成本冲回，
                # 也不假设跨渠道，仅按原渠道挂退款，待运营补核对。
                self._dispatch_legacy_return(row, ln, amount, received)
                continue
            self._dispatch_cost_row(path.name, ln, fake,
                                    "fulfillment_fee" if kind == "fulfillment_fee" else kind,
                                    bearer, amount, fake["event_date"], received, None)

    def _dispatch_legacy_return(self, row: dict, ln: int, amount: int,
                                received: str | None) -> None:
        order_id = row["order_id"].strip()
        if not self.order_line(order_id, None):
            self.emit_suspense(
                kind="return_refund", amount_cents=amount,
                source_ref=row["ref"].strip(), source_file="adjustments.csv",
                source_line=ln, reason=f"找不到订单 {order_id}",
                event_date=received or self.as_of, received_date=received, raw=row)
            return
        targets = self.split_across_order(
            order_id, amount, D.ALLOC_WEIGHTED,
            {"source_ref": row["ref"].strip(), "legacy_format": True})
        for oline, share, basis in targets:
            self.emit(
                kind="return_refund", bearer=D.BEARER_BRAND,
                amount_cents=share, order_id=order_id, line_no=oline["line_no"],
                channel=oline["channel"], sku=oline["sku"],
                campaign=oline["campaign"], period_key=oline["period_key"],
                source_ref=row["ref"].strip(), source_file="adjustments.csv",
                source_line=ln, method=D.ALLOC_WEIGHTED, basis=basis,
                event_date=received or self.as_of, received_date=received,
                verify_status=D.STATUS_PENDING)


def recheck_suspense(conn: sqlite3.Connection, actor: str, as_of: str) -> dict:
    """新订单导入后重新匹配暂挂事件；匹配成功则转正并留痕，仍失败的保留。"""
    resolved = 0
    cases = conn.execute(
        "SELECT s.entry_id, s.reason, e.* FROM suspense_cases s "
        "JOIN entries e ON e.entry_id=s.entry_id WHERE s.resolved=0").fetchall()
    for case in cases:
        basis = json.loads(case["allocation_basis"])
        raw = basis.get("raw_event", {})
        order_id = (raw.get("order_id") or "").strip()
        if not order_id:
            continue
        rows = conn.execute(
            "SELECT * FROM order_lines WHERE order_id=?", (order_id,)).fetchall()
        if not rows:
            continue
        # 暂挂事件转正：挂到订单的第一行（原始凭证未给行信息），
        # 恢复原始分录类型与承担方，依据中保留原暂挂原因，仍需人工确认。
        oline = rows[0]
        original_kind = basis.get("original_kind", "suspense")
        raw_bearer = (raw.get("bearer") or "").strip()
        bearer = raw_bearer if raw_bearer in D.BEARERS else D.BEARER_UNKNOWN
        # suspense 分录以负数列示，恢复后按目标类型的标准符号重算
        amount = abs(case["signed_cents"])
        signed = D.ENTRY_KIND_SIGN.get(original_kind, -1) * amount
        new_basis = {**basis, "reallocated_after_backfill": True,
                     "allocation": D.ALLOC_WEIGHTED}
        conn.execute(
            "UPDATE entries SET order_id=?, line_no=?, channel=?, sku=?, campaign=?, "
            "period_key=?, allocation_method=?, verify_status=?, matured=?, "
            "entry_kind=?, bearer=?, signed_cents=?, allocation_basis=? "
            "WHERE entry_id=?",
            (oline["order_id"], oline["line_no"], oline["channel"], oline["sku"],
             oline["campaign"], oline["period_key"], D.ALLOC_WEIGHTED,
             D.STATUS_PENDING,
             1 if (case["received_date"] or case["event_date"]) <= as_of else 0,
             original_kind, bearer, signed,
             json.dumps(new_basis, ensure_ascii=False, sort_keys=True),
             case["entry_id"]))
        conn.execute(
            "UPDATE suspense_cases SET resolved=1, resolution_note=? WHERE entry_id=?",
            (f"{actor} 重新匹配到订单行 {order_id}#L{oline['line_no']}，待人工确认",
             case["entry_id"]))
        conn.execute(
            "INSERT INTO confirmations(entry_id,action,actor,old_value,new_value,"
            "note,confirmed_at) VALUES (?,?,?,?,?,?,?)",
            (case["entry_id"], "resolve-suspense", actor, case["reason"],
             f"{order_id}#L{oline['line_no']}", "订单补录后系统重新匹配", as_of))
        resolved += 1
    conn.commit()
    return {"resolved": resolved, "remaining": len(cases) - resolved}
