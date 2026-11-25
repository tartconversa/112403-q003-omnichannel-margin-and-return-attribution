"""边界与旧版兼容测试。"""
import tempfile
import unittest
from pathlib import Path

from channel_pnl.db import connect
from channel_pnl.ingest import Ingester
from channel_pnl import reports as R


def write(path: Path, name: str, content: str) -> None:
    (path / name).write_text(content.strip() + "\n", encoding="utf-8")


class BoundaryTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "d"
        self.dir.mkdir()
        self.db = Path(self.tmp.name) / "t.sqlite3"
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_over_refund_and_negative_amounts_rejected(self):
        write(self.dir, "orders.csv", """
order_id,line,channel,sku,campaign,order_date,paid,cogs,state
O1,1,market-a,S1,C1,2026-10-01,100,50,settled
""")
        # 退款超过实付属于异常凭证；负数金额同样拒收
        write(self.dir, "returns.csv", """
ref,order_id,line,refund_amount,restocked,return_channel,event_date,received
R1,O1,1,150,1,offline,2026-10-02,2026-10-02
""")
        write(self.dir, "fulfillment.csv", """
ref,order_id,line,amount,bearer,event_date,received
F1,O1,1,-5,brand,2026-10-01,2026-10-01
""")
        summary = Ingester(self.conn, "2026-10-31", "t").ingest_dir(self.dir)
        # 退货本身结构合法（超额是业务校验范畴），但负费用必须被拒收
        self.assertGreaterEqual(summary["rejected"], 1)
        reasons = [r["reason"] for r in self.conn.execute(
            "SELECT reason FROM rejections").fetchall()]
        self.assertTrue(any("正数" in x for x in reasons))

    def test_cross_channel_cogs_reversal_anchors_original_channel(self):
        write(self.dir, "orders.csv", """
order_id,line,channel,sku,campaign,order_date,paid,cogs,state
O1,1,live,S1,C1,2026-10-01,500,260,settled
""")
        write(self.dir, "returns.csv", """
ref,order_id,line,refund_amount,restocked,return_channel,event_date,received
R1,O1,1,500,1,offline,2026-10-05,2026-10-05
""")
        Ingester(self.conn, "2026-10-31", "t").ingest_dir(self.dir)
        # 退款与成本冲回都必须挂在 live，而不是受理退货的 offline
        rows = self.conn.execute(
            "SELECT entry_kind, channel, verify_status FROM entries "
            "WHERE entry_kind IN ('return_refund','cogs_reversal')").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["channel"] == "live" for r in rows))
        self.assertTrue(all(r["verify_status"] == "pending" for r in rows))
        pnl = {r["group"]: r for r in
               R.pnl_report(self.conn, "channel", None, "2026-10-31")}
        self.assertNotIn("offline", pnl)  # offline 渠道没有任何归属分录
        self.assertIn("live", pnl)

    def test_legacy_two_file_format_ingests_and_anchors_return(self):
        # 直接复用仓库交付的 samples 目录
        samples = Path(__file__).resolve().parent.parent / "samples"
        summary = Ingester(self.conn, "2026-10-31", "legacy").ingest_dir(samples)
        self.assertEqual(summary["rejected"], 0)
        # O2 是 live 订单，其退货必须留在 live
        ret = self.conn.execute(
            "SELECT channel FROM entries WHERE source_ref='A3' "
            "AND entry_kind='return_refund'").fetchone()
        self.assertEqual(ret["channel"], "live")
        # 旧样例无退货渠道信息，退货挂待核对
        self.assertEqual(ret["channel"], "live")
        pending = R.verification_report(self.conn)
        self.assertTrue(any(p["source_ref"] == "A3" for p in pending))


if __name__ == "__main__":
    unittest.main()
