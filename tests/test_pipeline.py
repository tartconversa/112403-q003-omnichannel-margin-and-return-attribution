"""端到端核算管线测试：六表摄入、分摊依据、跨渠道退货、暂挂、暂估与期间差异。"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from channel_pnl import domain as D
from channel_pnl.db import connect
from channel_pnl.ingest import Ingester, recheck_suspense
from channel_pnl import reports as R
from channel_pnl import verify as V
from channel_pnl.periods import close_period, period_variance, reopen_period


def write(path: Path, name: str, content: str) -> None:
    (path / name).write_text(content.strip() + "\n", encoding="utf-8")


ORDERS = """
order_id,line,channel,sku,campaign,order_date,paid,cogs,state
O1,1,market-a,SKU1,C1,2026-10-03,800,400,settled
O1,2,market-a,SKU2,C1,2026-10-03,200,100,settled
O2,1,live,SKU1,C2,2026-10-05,500,260,settled
O3,1,offline,SKU3,C3,2026-10-12,400,200,settled
O4,1,market-b,SKU2,C1,2026-10-20,600,300,shipped
"""

SETTLE = """
ref,order_id,line,kind,amount,bearer,event_date,received,rate
S1,O1,,platform_fee,10,brand,2026-10-03,2026-10-16,
S2,O2,1,platform_commission,30,brand,2026-10-05,2026-10-19,0.06
S3,O2,1,platform_subsidy,50,platform,2026-10-05,2026-10-19,
"""

DISCOUNT = """
ref,order_id,line,kind,amount,bearer,event_date,received
C1,O1,1,brand_coupon,80,brand,2026-10-03,2026-10-03
C2,O4,1,unknown_discount,60,unknown,2026-11-08,
"""

GIFT = """
ref,order_id,line,sku,amount,bearer,event_date,received
G1,O2,1,GIFT,18,brand,2026-10-05,2026-10-05
"""

FULFILL = """
ref,order_id,line,amount,bearer,event_date,received
F1,O1,,20,brand,2026-10-03,2026-10-20
F2,O9,1,15,brand,2026-10-21,2026-10-21
"""

RETURN = """
ref,order_id,line,refund_amount,restocked,return_channel,event_date,received
R1,O2,1,250,1,offline,2026-10-25,2026-10-25
R2,O3,1,100,0,offline,2026-10-26,2026-10-26
R3,O1,1,80,1,offline,2026-10-27,2026-10-27
R3,O2,1,120,0,offline,2026-10-27,2026-10-27
"""

RULES = """
channel,kind,rate,basis,note
market-b,fulfillment_fee,0.02,paid,测试暂估
"""


class PipelineTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.data = self.dir / "drop1"
        self.data.mkdir()
        for name, content in [
            ("orders.csv", ORDERS), ("settlements.csv", SETTLE),
            ("discounts.csv", DISCOUNT), ("gifts.csv", GIFT),
            ("fulfillment.csv", FULFILL), ("returns.csv", RETURN),
            ("estimate_rules.csv", RULES),
        ]:
            write(self.data, name, content)
        self.db = self.dir / "t.sqlite3"
        self.conn = connect(self.db)
        self.summary = Ingester(self.conn, "2026-10-31", "test").ingest_dir(self.data)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_ingest_basic_counts_and_suspense(self):
        self.assertEqual(self.summary["rejected"], 0)
        self.assertEqual(self.summary["suspended"], 1)  # F2 → O9 不存在
        suspense = R.suspense_report(self.conn)
        self.assertEqual(len(suspense), 1)
        self.assertEqual(suspense[0]["source_ref"], "F2")

    def test_suspense_not_allocated_to_any_channel(self):
        # 护栏：暂挂金额不得出现在任何渠道的损益里
        pnl = {r["group"]: r for r in
               R.pnl_report(self.conn, "channel", None, "2026-10-31")}
        for ch in D.CHANNELS:
            self.assertEqual(pnl[ch]["suspense_cents"], 0)
        self.assertIn("(未归属/暂挂)", pnl)
        self.assertEqual(pnl["(未归属/暂挂)"]["suspense_cents"], -1500)

    def test_order_level_fee_split_keeps_basis_and_sums(self):
        # S1=10元订单级平台费按 800:200 拆到 O1 两行；F1=20 同理
        rows = self.conn.execute(
            "SELECT line_no, signed_cents, allocation_method, allocation_basis "
            "FROM entries WHERE source_ref='S1' ORDER BY line_no").fetchall()
        self.assertEqual([r["signed_cents"] for r in rows], [-800, -200])
        self.assertTrue(all(r["allocation_method"] == D.ALLOC_WEIGHTED for r in rows))
        basis = json.loads(rows[0]["allocation_basis"])
        self.assertIn("line_weights_paid_cents", basis)
        self.assertEqual(basis["amount_cents"], 1000)
        f1 = self.conn.execute(
            "SELECT SUM(signed_cents) t FROM entries WHERE source_ref='F1'").fetchone()
        self.assertEqual(f1["t"], -2000)

    def test_cross_channel_return_stays_on_original_channel(self):
        # O2 是 live 渠道，在 offline 门店退货：退款仍归属 live，且标记待运营核对
        rows = self.conn.execute(
            "SELECT * FROM entries WHERE source_ref='R1' AND entry_kind='return_refund'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["channel"], "live")
        self.assertEqual(rows[0]["verify_status"], D.STATUS_PENDING)
        basis = json.loads(rows[0]["allocation_basis"])
        self.assertTrue(basis["cross_channel"])
        self.assertEqual(basis["return_channel"], "offline")
        # 运营核对后转正，渠道不变
        V.confirm_return(self.conn, rows[0]["entry_id"], "ops", "2026-10-31")
        again = self.conn.execute(
            "SELECT channel, verify_status FROM entries WHERE entry_id=?",
            (rows[0]["entry_id"],)).fetchone()
        self.assertEqual(again["channel"], "live")
        self.assertEqual(again["verify_status"], D.STATUS_CONFIRMED)

    def test_one_voucher_multi_orders_marked_voucher_split(self):
        rows = self.conn.execute(
            "SELECT order_id, signed_cents, allocation_method FROM entries "
            "WHERE source_ref='R3' AND entry_kind='return_refund' ORDER BY order_id").fetchall()
        self.assertEqual([r["order_id"] for r in rows], ["O1", "O2"])
        self.assertTrue(all(r["allocation_method"] == D.ALLOC_VOUCHER for r in rows))
        self.assertEqual(sum(r["signed_cents"] for r in rows), -20000)

    def test_restocked_return_reverses_cogs(self):
        # R1 部分退款 250/实付500，可二次销售 → 冲回成本 260×(250/500)=130
        rev = self.conn.execute(
            "SELECT signed_cents FROM entries WHERE source_ref LIKE 'R1%' "
            "AND entry_kind='cogs_reversal'").fetchone()
        self.assertEqual(rev["signed_cents"], 13000)
        # R3 的 O1 行退款 80/实付800 → 冲回 400×(80/800)=40
        rev_partial = self.conn.execute(
            "SELECT signed_cents FROM entries WHERE source_ref LIKE 'R3%' "
            "AND entry_kind='cogs_reversal' AND order_id='O1'").fetchone()
        self.assertEqual(rev_partial["signed_cents"], 4000)
        # R2 不可二次销售 → 无成本冲回
        none = self.conn.execute(
            "SELECT COUNT(*) c FROM entries WHERE source_ref LIKE 'R2%' "
            "AND entry_kind='cogs_reversal'").fetchone()
        self.assertEqual(none["c"], 0)

    def test_full_refund_reverses_full_cogs(self):
        # 全额退款且可二次销售：冲回该行全部商品成本
        late = self.dir / "drop-full"
        late.mkdir()
        write(late, "returns.csv", """
ref,order_id,line,refund_amount,restocked,return_channel,event_date,received
RF,O3,1,400,1,offline,2026-10-28,2026-10-28
""")
        Ingester(self.conn, "2026-10-31", "full-refund").ingest_dir(late)
        rev = self.conn.execute(
            "SELECT signed_cents FROM entries WHERE source_ref LIKE 'RF%' "
            "AND entry_kind='cogs_reversal'").fetchone()
        self.assertEqual(rev["signed_cents"], 20000)  # O3 成本 200 全额冲回

    def test_unknown_bearer_excluded_from_brand_contribution(self):
        pnl = {r["group"]: r for r in
               R.pnl_report(self.conn, "channel", None, "2026-10-31")}
        mb = pnl["market-b"]
        self.assertEqual(mb["pending_bearer_cents"], -6000)
        # 待确认优惠不进品牌净贡献
        self.assertNotIn(-6000, [mb["brand_net_contribution_cents"]])
        # 财务确认 brand 后进入品牌损益
        eid = self.conn.execute(
            "SELECT entry_id FROM entries WHERE source_ref='C2'").fetchone()["entry_id"]
        V.confirm_bearer(self.conn, eid, "fin", D.BEARER_BRAND, "2026-10-31")
        pnl2 = {r["group"]: r for r in
                R.pnl_report(self.conn, "channel", None, "2026-10-31")}
        self.assertEqual(pnl2["market-b"]["pending_bearer_cents"], 0)

    def test_estimate_then_actual_supersession(self):
        # O4 是 shipped 未结算，market-b 有 2% 履约暂估 → 600*2%=12
        est = self.conn.execute(
            "SELECT * FROM entries WHERE order_id='O4' AND entry_kind='estimated_fee'").fetchall()
        self.assertEqual(len(est), 1)
        self.assertEqual(est[0]["signed_cents"], -1200)
        # 实际费用到达
        late = self.dir / "drop2"
        late.mkdir()
        write(late, "fulfillment.csv", """
ref,order_id,line,amount,bearer,event_date,received
F3,O4,1,13,brand,2026-10-20,2026-11-10
""")
        Ingester(self.conn, "2026-11-15", "late").ingest_dir(late)
        kinds = {r["entry_kind"]: r["signed_cents"] for r in self.conn.execute(
            "SELECT entry_kind, signed_cents FROM entries WHERE order_id='O4' "
            "AND entry_kind IN ('estimated_fee','estimate_reversal','fulfillment_fee')")}
        self.assertEqual(kinds["estimated_fee"], -1200)
        self.assertEqual(kinds["estimate_reversal"], 1200)  # 暂估冲销
        self.assertEqual(kinds["fulfillment_fee"], -1300)  # 实际入账
        # 净效果：只承担实际 13，不重复计暂估
        self.assertEqual(kinds["estimated_fee"] + kinds["estimate_reversal"], 0)

    def test_unsettled_not_treated_as_zero_cost(self):
        unsettled = R.unsettled_report(self.conn, None, "2026-10-31")
        refs = {r["source_ref"] for r in unsettled}
        self.assertIn("C2", refs)   # 未知承担优惠尚未到达
        # 可比净贡献含暂估，与不含暂估的净贡献不同
        pnl = {r["group"]: r for r in
               R.pnl_report(self.conn, "channel", None, "2026-10-31")}
        self.assertNotEqual(pnl["market-b"]["brand_net_contribution_cents"],
                            pnl["market-b"]["comparable_net_contribution_cents"])

    def test_snapshot_vs_live_variance_after_late_data(self):
        close_period(self.conn, "2026-10", "fin", "2026-10-31")
        snap_pnl = {r["group"]: r for r in R.pnl_report(
            self.conn, "channel", "2026-10", "2026-11-15", use_snapshot=True)}
        before = snap_pnl["live"]["brand_net_contribution_cents"]
        # 定稿后到达一笔 live 渠道的迟到退货
        late = self.dir / "drop2"
        late.mkdir()
        write(late, "returns.csv", """
ref,order_id,line,refund_amount,restocked,return_channel,event_date,received
R9,O2,1,50,0,offline,2026-11-08,2026-11-08
""")
        Ingester(self.conn, "2026-11-15", "late-return").ingest_dir(late)
        diffs = period_variance(self.conn, "2026-10")
        self.assertTrue(any(d["change"] == "added" and d["source_ref"] == "R9"
                            for d in diffs))
        # 快照原账不变
        snap_pnl2 = {r["group"]: r for r in R.pnl_report(
            self.conn, "channel", "2026-10", "2026-11-15", use_snapshot=True)}
        self.assertEqual(snap_pnl2["live"]["brand_net_contribution_cents"], before)
        # 重开需要原因
        with self.assertRaises(ValueError):
            reopen_period(self.conn, "2026-10", "fin", "  ")
        reopen_period(self.conn, "2026-10", "fin", "迟到退货影响结论")

    def test_recheck_matches_suspense_after_order_backfill(self):
        late = self.dir / "drop2"
        late.mkdir()
        write(late, "orders.csv", """
order_id,line,channel,sku,campaign,order_date,paid,cogs,state
O9,1,market-a,SKU9,C1,2026-10-18,300,150,settled
""")
        Ingester(self.conn, "2026-11-15", "backfill").ingest_dir(late)
        result = recheck_suspense(self.conn, "ops", "2026-11-15")
        self.assertEqual(result["resolved"], 1)
        self.assertEqual(result["remaining"], 0)
        row = self.conn.execute(
            "SELECT order_id, channel FROM entries WHERE source_ref='F2'").fetchone()
        self.assertEqual(row["order_id"], "O9")
        self.assertEqual(row["channel"], "market-a")

    def test_reconcile_identity_and_manual_suspense_resolution(self):
        rec = R.reconcile_totals(self.conn, "2026-10-31")
        self.assertGreaterEqual(rec["open_suspense_count"], 1)
        # 人工把 F2 指派到 O1 第 2 行
        eid = self.conn.execute(
            "SELECT entry_id FROM entries WHERE source_ref='F2'").fetchone()["entry_id"]
        V.resolve_suspense(self.conn, eid, "fin", "O1", 2, "2026-10-31", "补单")
        rec2 = R.reconcile_totals(self.conn, "2026-10-31")
        self.assertEqual(rec2["open_suspense_count"], 0)
        # 留痕存在
        trail = V.audit_trail(self.conn, eid)
        self.assertTrue(any(t["action"] == "resolve-suspense" for t in trail))

    def test_idempotent_reingest_no_duplicates(self):
        Ingester(self.conn, "2026-10-31", "repeat").ingest_dir(self.data)
        dups = self.conn.execute(
            "SELECT natural_key, COUNT(*) c FROM entries GROUP BY natural_key "
            "HAVING c>1").fetchall()
        self.assertEqual(dups, [])


if __name__ == "__main__":
    unittest.main()
