# 渠道净贡献核算与跨店退货成本分摊

合成平台订单结算、优惠分担、跨店退款及履约费用样例。

All example identifiers and values are synthetic. CSV files use UTF-8. The first row contains column names, and an empty cell means the source supplied no value.

## Sample files

- `orders.csv` contains 订单渠道、销售金额和商品成本。 Columns are `order_id`, `line`, `channel`, `paid`, `cost`, `state`.
- `adjustments.csv` contains 平台补贴、品牌优惠、跨店退货和履约费用。 Columns are `ref`, `order_id`, `kind`, `amount`, `owner`, `received`.

Python 3 standard library is sufficient to inspect these inputs. Run `python3 -m unittest discover -s tests -v` to check the delivered file structures and example references.
