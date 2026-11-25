# 核算口径与计算公式

## 1. 分录符号与类别

`entries.signed_cents` 是带符号整数分：

- 增加品牌利益记正：顾客实付、平台补贴、退货成本冲回、暂估冲销；
- 减少品牌利益记负：品牌券、退款、商品成本、赠品、履约费、佣金、暂估费用。

类别（`ENTRY_KIND_CLASS`）决定它进哪一块报告：

| 类别 | 包含类型 | 是否进品牌净贡献 |
| --- | --- | --- |
| receipt 实收 | gross_receipt、brand_coupon、return_refund | 是 |
| cost 可归属成本 | cogs、cogs_reversal、gift_cost、fulfillment_fee、platform_commission、platform_fee、estimated_fee、estimate_reversal | 是 |
| settlement 平台结算 | platform_subsidy、platform_coupon | 否，单列「平台承担」 |
| pending 待确认 | unknown_discount、suspense | 否，单列「待确认/暂挂」 |

## 2. 渠道/商品/活动期损益公式

对每个分组（渠道、SKU、活动期）：

```
实收净额 net_receipt
  = gross_receipt + brand_coupon + return_refund

可归属成本 attributable_cost
  = cogs + cogs_reversal + gift_cost + fulfillment_fee
    + platform_commission + platform_fee

品牌净贡献 brand_net_contribution
  = 实收净额 + 可归属成本          # 成本类已带负号

暂估净额 estimated_fee
  = estimated_fee + estimate_reversal

可比净贡献 comparable_net_contribution
  = 品牌净贡献 + 暂估净额
```

- **品牌净贡献**只认已经实际发生、且由品牌承担的项目，是「已落地」口径；
- **可比净贡献**把尚未成熟结算的暂估费用视同必然发生，用于渠道继续投放决策——
  不会因为结算迟到而把该渠道当成零成本；
- 平台承担项（补贴、平台券）、未知承担方、暂挂金额各自单列，**不混入**品牌净贡献，
  避免把别人的钱或没查清的钱算进品牌损益。

## 3. 成熟口径与未结算

成熟与否按日期判断，不信任导入时的标记位：

```
matured  ⇔  COALESCE(received_date, event_date) <= as_of
```

- `pnl --matured` 只汇总成熟分录；未成熟部分用 `unsettled` 逐笔查看；
- 默认 `pnl` 含全部分录（含暂估），保证未到费用不会被静默当成 0；
- `unsettled` 报告逐笔列出截至 as_of 仍未到达的费用、退款、未确认优惠。

## 4. 分摊规则（都留存可核对依据）

| 场景 | 方法 | 依据 |
| --- | --- | --- |
| 凭证明确到订单行 | direct | 直接入账 |
| 订单级费用/优惠（line 留空） | line-weighted | 按各行实付权重，最大余数法分配尾差 |
| 一张退款凭证覆盖多订单 | voucher-split | 凭证总额、各行金额、订单实付权重全部入 basis |
| 未成熟结算 | estimate | 实付 × 费率（整数基点），实际到达后等额冲销 |
| 匹配不到订单 | suspense | 挂账，不进任何渠道，等补录或人工指派 |

最大余数法保证各份之和**严格等于**原始整数分，不产生小数分，也不允许
「为了对平而按销售额把无法匹配的成本摊进去」。

## 5. 退货与跨店退货

- 退款 `return_refund` 与成本冲回 `cogs_reversal` 都挂在**原始订单行的渠道**；
- `return_channel` 仅记录受理退货的门店/渠道，写入 basis 并置 `pending`，
  由运营核对订单身份与退货商品后转正，归属始终不变；
- 可二次销售（restocked=1）时，按 `退款/实付` 比例冲回商品成本；
  全额退款冲回该行全部成本，不可二次销售则不冲回；
- 旧月报把门店退款记到线下，本系统在 `bridge` 报告量化这一错误口径与
  正确口径之间的差异金额。

## 6. 暂估费用生命周期

1. 订单已发货未结算、且渠道有费率规则 → 生成 `estimated_fee`（负，pending）；
2. 实际费用分录到达 → 旧暂估置 `superseded`，生成等额 `estimate_reversal`（正），
   实际费用另行入账；暂估与冲销净额恒为 0，不重复计费；
3. 规则持久化在 `estimate_rules` 表，任何一次导入都全量重算，
   不依赖当次目录是否携带规则文件。

## 7. 期间定稿、迟到数据与重开

- `periods close` 把该期间全量分录冻结为快照（JSONL）；
- 定稿后新导入的数据照常进活账，`variance` 逐笔列出 added/changed/removed；
- 报告可用 `--snapshot` 看定稿原账、默认看活账，两个口径并存；
- 已复核结论保留原账，是否重开由财务决定；`reopen` 必须填写原因并留痕，
  重开后可再次定稿，旧快照保留不覆盖。

## 8. 恒等护栏

`reconcile` 报告强制：

```
分录总额 = 已归属到订单行的分录 + suspense 暂挂分录
```

任何一分钱要么带分摊依据落到具体订单行，要么显式挂在暂挂，
系统不存在「对不平就按销售额摊」的路径。
