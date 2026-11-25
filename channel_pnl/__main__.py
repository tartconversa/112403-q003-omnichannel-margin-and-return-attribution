"""命令行入口。

子命令一览：
  ingest      导入一个数据目录（六张新表或旧版 orders+adjustments）
  recheck     订单补录后重新匹配暂挂事件
  pending     待核对清单（跨渠道退货/未知承担方/暂估/暂挂）
  confirm     财务确认承担方 / 运营确认退货 / 人工解决暂挂
  audit       核对留痕
  pnl         渠道/商品/活动期损益（可选 --snapshot 定稿原账口径）
  unsettled   未结算与未成熟逐笔清单
  suspense    暂挂工作清单
  bridge      渠道归属变化桥（旧月报口径 vs 正确口径）
  variance    定稿后迟到数据导致的逐笔差异
  reconcile   总额恒等核对
  periods     期间列表 / close 定稿 / reopen 重开复核

建议用隔离模式运行：python3 -I run_ingest.py ... （见 run_ingest.py）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from . import domain as D
from . import reports as R
from . import verify as V
from .db import connect
from .ingest import Ingester, recheck_suspense
from .periods import close_period, list_periods, period_variance, reopen_period

DEFAULT_DB = "data/pnl.sqlite3"
TODAY = date.today().isoformat()


def add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite 数据库路径")
    p.add_argument("--as-of", default=TODAY, help="核算截止日（ISO 日期）")


def emit_rows(args: argparse.Namespace, rows: list[dict],
              money_cols: tuple[str, ...] = ()) -> None:
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return
    if args.out:
        R.write_csv(rows, args.out)
        print(f"已写出 {len(rows)} 行 → {args.out}")
        return
    print(R.format_console_table(rows, money_cols))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="channel-pnl",
        description="渠道净贡献核算与跨店退货成本分摊（Python 标准库）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="导入数据目录")
    add_common(p)
    p.add_argument("source_dir", help="含 CSV 的数据目录")
    p.add_argument("--note", default="", help="本批次说明")

    p = sub.add_parser("recheck", help="重新匹配暂挂事件")
    add_common(p)
    p.add_argument("--actor", required=True)

    p = sub.add_parser("pending", help="待核对清单")
    add_common(p)
    p.add_argument("--out"), p.add_argument("--json", action="store_true")

    p = sub.add_parser("suspense", help="暂挂工作清单")
    add_common(p)
    p.add_argument("--out"), p.add_argument("--json", action="store_true")

    p = sub.add_parser("unsettled", help="未结算/未成熟逐笔清单")
    add_common(p)
    p.add_argument("--period", default=None)
    p.add_argument("--out"), p.add_argument("--json", action="store_true")

    p = sub.add_parser("pnl", help="渠道/商品/活动期损益")
    add_common(p)
    p.add_argument("--dimension", choices=["channel", "sku", "campaign"],
                   default="channel")
    p.add_argument("--period", default=None)
    p.add_argument("--matured", action="store_true",
                   help="只含已成熟分录（未成熟部分在 unsettled 报告查看）")
    p.add_argument("--snapshot", action="store_true",
                   help="使用定稿快照原账口径（需配合 --period）")
    p.add_argument("--out"), p.add_argument("--json", action="store_true")

    p = sub.add_parser("bridge", help="渠道归属变化桥")
    add_common(p)
    p.add_argument("--period", default=None)
    p.add_argument("--out"), p.add_argument("--json", action="store_true")

    p = sub.add_parser("variance", help="定稿后差异")
    add_common(p)
    p.add_argument("period")
    p.add_argument("--out"), p.add_argument("--json", action="store_true")

    p = sub.add_parser("reconcile", help="总额恒等核对")
    add_common(p)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("periods", help="期间管理")
    add_common(p)
    p.add_argument("action", choices=["list", "close", "reopen"])
    p.add_argument("period", nargs="?", default=None)
    p.add_argument("--actor", default="")
    p.add_argument("--reason", default="")

    p = sub.add_parser("confirm", help="人工核对")
    add_common(p)
    p.add_argument("action", choices=list(V.VALID_BEARER_ACTIONS))
    p.add_argument("entry_id", type=int)
    p.add_argument("--actor", required=True)
    p.add_argument("--bearer", choices=[D.BEARER_BRAND, D.BEARER_PLATFORM])
    p.add_argument("--order-id", default=None)
    p.add_argument("--line-no", type=int, default=None)
    p.add_argument("--note", default="")

    p = sub.add_parser("audit", help="核对留痕")
    add_common(p)
    p.add_argument("--entry-id", type=int, default=None)
    p.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = connect(args.db)
    try:
        return dispatch(args, conn)
    except (ValueError, FileNotFoundError, KeyError) as exc:
        # 业务/输入类错误：简洁信息 + 非零退出码，不向用户暴露堆栈
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


def dispatch(args: argparse.Namespace, conn) -> int:
    if args.cmd == "ingest":
        result = Ingester(conn, args.as_of, args.note).ingest_dir(args.source_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["rejected"]:
            print(f"⚠ 有 {result['rejected']} 行被拒收，见 rejections 表",
                  file=sys.stderr)
        if result["suspended"]:
            print(f"⚠ 有 {result['suspended']} 条事件进暂挂，"
                  f"运行 suspense 查看，禁止摊入渠道", file=sys.stderr)
        return 0

    if args.cmd == "recheck":
        print(json.dumps(recheck_suspense(conn, args.actor, args.as_of),
                         ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "pending":
        emit_rows(args, R.verification_report(conn))
        return 0

    if args.cmd == "suspense":
        emit_rows(args, R.suspense_report(conn))
        return 0

    if args.cmd == "unsettled":
        emit_rows(args, R.unsettled_report(conn, args.period, args.as_of))
        return 0

    if args.cmd == "pnl":
        rows = R.pnl_report(conn, args.dimension, args.period, args.as_of,
                            matured_only=args.matured, use_snapshot=args.snapshot)
        money = tuple(k for k in rows[0] if k.endswith("_cents")) if rows else ()
        emit_rows(args, rows, money)
        return 0

    if args.cmd == "bridge":
        emit_rows(args, R.attribution_bridge(conn, args.period, args.as_of),
                  ("misattributed_cents",))
        return 0

    if args.cmd == "variance":
        emit_rows(args, period_variance(conn, args.period),
                  ("snapshot_cents", "live_cents"))
        return 0

    if args.cmd == "reconcile":
        result = R.reconcile_totals(conn, args.as_of)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for k, v in result.items():
                print(f"{k:32s} {v}")
        return 0

    if args.cmd == "periods":
        if args.action == "list":
            emit_rows(args, [dict(r) for r in list_periods(conn)])
        elif args.action == "close":
            if not args.period or not args.actor:
                raise SystemExit("close 需要 period 和 --actor")
            print(json.dumps(close_period(conn, args.period, args.actor,
                                          args.as_of), ensure_ascii=False, indent=2))
        else:
            if not args.period or not args.actor:
                raise SystemExit("reopen 需要 period、--actor 和 --reason")
            print(json.dumps(reopen_period(conn, args.period, args.actor,
                                           args.reason), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "confirm":
        if args.action == "confirm-bearer":
            if not args.bearer:
                raise SystemExit("confirm-bearer 需要 --bearer")
            out = V.confirm_bearer(conn, args.entry_id, args.actor,
                                   args.bearer, args.as_of, args.note)
        elif args.action == "confirm-return":
            out = V.confirm_return(conn, args.entry_id, args.actor,
                                   args.as_of, args.note)
        else:
            if not args.order_id or args.line_no is None:
                raise SystemExit("resolve-suspense 需要 --order-id 和 --line-no")
            out = V.resolve_suspense(conn, args.entry_id, args.actor,
                                     args.order_id, args.line_no,
                                     args.as_of, args.note)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "audit":
        rows = V.audit_trail(conn, args.entry_id)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        else:
            emit_rows(argparse.Namespace(json=False, out=None), rows)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
