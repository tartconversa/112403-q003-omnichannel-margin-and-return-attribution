"""金额与整数分摊的单元测试。"""
import unittest

from channel_pnl.money import cents_to_yuan, split_cents, yuan_to_cents


class MoneyTest(unittest.TestCase):

    def test_yuan_to_cents_half_up(self):
        self.assertEqual(yuan_to_cents("1.23"), 123)
        self.assertEqual(yuan_to_cents("0.1"), 10)
        self.assertEqual(yuan_to_cents("1.005"), 101)   # 四舍五入，非银行家舍入
        self.assertEqual(yuan_to_cents(""), 0)
        self.assertEqual(yuan_to_cents(None), 0)
        self.assertEqual(yuan_to_cents(2), 200)
        with self.assertRaises(ValueError):
            yuan_to_cents("not-a-number")

    def test_cents_to_yuan(self):
        self.assertEqual(cents_to_yuan(123), "1.23")
        self.assertEqual(cents_to_yuan(-5), "-0.05")
        self.assertEqual(cents_to_yuan(0), "0.00")

    def test_split_exact_sum_no_fractional_cents(self):
        # 任意权重下，各份之和必须严格等于原值且都是整数分
        for total, weights in [
            (10000, [80000, 30000]),
            (1100, [80000, 30000]),
            (100, [1, 1, 1]),
            (1, [3, 5, 7]),
            (99999, [13, 27, 41, 19]),
        ]:
            shares = split_cents(total, weights)
            self.assertEqual(sum(shares), total)
            self.assertTrue(all(isinstance(s, int) for s in shares))
            self.assertEqual(len(shares), len(weights))

    def test_split_negative_total(self):
        shares = split_cents(-1000, [3, 7])
        self.assertEqual(sum(shares), -1000)

    def test_split_zero_weights_uses_equal_parts(self):
        shares = split_cents(100, [0, 0])
        self.assertEqual(sum(shares), 100)

    def test_split_single_weight(self):
        self.assertEqual(split_cents(500, [10]), [500])
        self.assertEqual(split_cents(500, []), [])


if __name__ == "__main__":
    unittest.main()
