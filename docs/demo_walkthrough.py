#!/usr/bin/env python3
"""端到端业务剧本：导入 → 暂挂/暂估 → 人工核对 → 定稿 → 迟到数据 → 差异 → 重开。

用独立临时数据库运行，结束后保留库路径供检查。
隔离模式运行：python3 -I docs/demo_walkthrough.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from channel_pnl.db import connect  # noqa: E402
from channel_pnl.ingest import Ingester, recheck_suspense  # noqa: E402
from channel_pnl import reports as R  # noqa: E402
from channel_pnl import verify as V  # noqa: E402
from channel_pnl.periods import close_period, period_variance, reopen_period  # noqa: E402
from channel_pnl.money import cents_to_yuan  # noqa: E402


def show_pnl(conn, as_of, title, period_key=None, **kw):
    print(f"\n--- {title} ---")
    for r in R.pnl_report(conn, "channel", period_key, as_of, **kw):
        print(f"  {r['group']:12s} 实收净额 {cents_to_yuan(r['net_receipt_cents']):>10s} "
              f"品牌净贡献 {cents_to_yuan(r['brand_net_contribution_cents']):>10s} "
              f"可比净贡献 {cents_to_yuan(r['comparable_net_contribution_cents']):>10s} "
              f"暂挂 {cents_to_yuan(r['suspense_cents']):>8s}")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="pnl-demo-"))
    db = tmp / "demo.sqlite3"
    conn = connect(db)

    print("① 首批导入（含订单级费用拆行、跨店退货、未知承担方、未到费用、暂挂事件）")
    s1 = Ingester(conn, "2026-11-05", "首批").ingest_dir(ROOT / "data/synthetic-demo")
    print(f"   生成分录 {s1['emitted']} 条，拒收 {s1['rejected']}，暂挂 {s1['suspended']}")

    print("\n② 财务确认一笔未知承担优惠由品牌承担")
    eid = conn.execute(
        "SELECT entry_id FROM entries WHERE source_ref='C4'").fetchone()["entry_id"]
    V.confirm_bearer(conn, eid, "finance-li", "brand", "2026-11-05", "平台确认品牌承担")

    print("③ 运营核对一笔跨渠道退货（归属保持原下单渠道）")
    ret = conn.execute(
        "SELECT entry_id, channel FROM entries "
        "WHERE source_ref='R2' AND entry_kind='return_refund'").fetchone()
    V.confirm_return(conn, ret["entry_id"], "ops-wang", "2026-11-05", "门店扫码核对一致")
    print(f"   退货分录 {ret['entry_id']} 是 market-a 订单在 offline 退回，"
          f"渠道仍锚定 {ret['channel']}")

    show_pnl(conn, "2026-11-05", "④ 截至 11-05 渠道损益（未成熟结算以暂估计入可比口径）")
    print("\n   未成熟逐笔清单（不被当成零成本）：")
    for u in R.unsettled_report(conn, None, "2026-11-05")[:5]:
        print(f"   - {u['source_ref']:6s} {u['entry_kind']:18s} "
              f"{cents_to_yuan(u['signed_cents']):>8s} 渠道={u['channel']}")

    print("\n⑤ 定稿 2026-10（冻结快照）")
    close = close_period(conn, "2026-10", "finance-li", "2026-11-05")
    print(f"   快照 {close['snapshot_id']} 冻结 {close['entries']} 条分录")

    print("\n⑥ 定稿后到达迟到费用/退货，并补录暂挂订单 D99")
    s2 = Ingester(conn, "2026-11-25", "迟到批次").ingest_dir(
        ROOT / "data/synthetic-demo-drop2")
    print(f"   新分录 {s2['emitted']} 条；重新匹配暂挂：",
          recheck_suspense(conn, "ops-wang", "2026-11-25"))

    print("\n⑦ 定稿原账 vs 当前活账（已复核结论保留，差异单列）")
    show_pnl(conn, "2026-11-25", "定稿原账 snapshot", period_key="2026-10",
             use_snapshot=True)
    show_pnl(conn, "2026-11-25", "当前活账 live", period_key="2026-10")

    print("\n⑧ 迟到数据导致的逐笔差异（供财务决定是否重开）")
    for d in period_variance(conn, "2026-10"):
        old = cents_to_yuan(d["snapshot_cents"]) if d["snapshot_cents"] is not None else "-"
        new = cents_to_yuan(d["live_cents"]) if d["live_cents"] is not None else "-"
        print(f"   [{d['change']:7s}] {d['source_ref']:16s} {d['entry_kind']:18s} "
              f"原账 {old:>9s} → 活账 {new:>9s}")

    print("\n⑨ 渠道归属变化桥（旧月报错配口径 → 正确口径）")
    for b in R.attribution_bridge(conn, None, "2026-11-25"):
        print(f"   正确渠道 {b['correct_channel']:12s} 旧月报记 "
              f"{b['monthly_report_channel']:10s} 错配 {cents_to_yuan(b['misattributed_cents'])}")

    print("\n⑩ 财务重开复核（强制原因留痕）")
    reopen_period(conn, "2026-10", "finance-li", "迟到费用与跨店退货改变渠道结论")
    print("   期间状态 → reopened，旧快照保留")

    print("\n⑪ 恒等护栏")
    for k, v in R.reconcile_totals(conn, "2026-11-25").items():
        print(f"   {k:32s} {v}")

    conn.close()
    print(f"\n演示数据库保留在：{db}")


if __name__ == "__main__":
    main()
