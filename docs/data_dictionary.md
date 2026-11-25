# 数据字典

所有 CSV 为 UTF-8、首行表头、空单元格表示源系统未提供值。金额以**元**为单位，
摄入时统一转为整数**分**。日期为 ISO 格式 `YYYY-MM-DD`。

## 一、输入文件（新版六表 + 可选暂估规则）

### orders.csv — 订单行（收入与商品成本锚点）

| 列 | 说明 |
| --- | --- |
| order_id | 订单号 |
| line | 订单内行号；同一订单多行是拆行分摊的基础 |
| channel | 下单渠道：`market-a` / `market-b` / `live` / `offline` |
| sku | 商品编码（可为空） |
| campaign | 活动期编码（可为空） |
| order_date | 下单日期，决定期间归属（`YYYY-MM`） |
| paid | 顾客实付金额（元） |
| cogs | 商品成本（元） |
| state | `settled`/`shipped`/`pending`/`returned`/`cancelled` |

每个订单行生成两条分录：`gross_receipt`（实收，+）与 `cogs`（成本，−）。

### settlements.csv — 平台结算

| 列 | 说明 |
| --- | --- |
| ref | 结算凭证号 |
| order_id / line | 指向订单；**line 留空表示订单级费用，按各行实付权重拆行** |
| kind | `platform_commission` 佣金 / `platform_fee` 结算费 / `platform_subsidy` 平台补贴 |
| amount | 金额（元，正数） |
| bearer | `brand` / `platform` / `unknown` |
| event_date | 费用/补贴归属日期 |
| received | 实际结算到达日；**留空=尚未结算** |
| rate | 费率（可空，仅作凭证信息留存） |

### discounts.csv — 优惠分摊

`kind` 为 `brand_coupon`（品牌券，抵减品牌实收）、`platform_coupon`（平台券）、
`unknown_discount`（承担方未确认，进待确认列，不进品牌损益）。其余列同结算表。

### gifts.csv — 赠品成本

含 `sku`（赠品编码）列，无 `kind` 列；`bearer=unknown` 的赠品成本同样挂待确认。

### fulfillment.csv — 履约费用

列：`ref,order_id,line,amount,bearer,event_date,received`。
`received` 留空表示费用尚未到账，报告会列入未结算，并由暂估规则兜底。

### returns.csv — 退货记录

| 列 | 说明 |
| --- | --- |
| ref | 退款凭证号；**同一 ref 多行 = 一张凭证覆盖多笔订单**（`voucher-split`） |
| order_id / line | 退货对应的原始订单行；line 留空按订单行实付权重拆 |
| refund_amount | 退款金额（元，正数） |
| restocked | `1/true/yes` 表示商品可二次销售，按比例冲回商品成本 |
| return_channel | 实际受理退货的渠道；与原订单渠道不同即**跨店/跨渠道退货** |
| event_date / received | 退货事件日 / 实际退款日 |

退货分录 `return_refund` 始终锚定**原始下单渠道**；跨渠道时 `verify_status=pending`，
等运营核对订单身份与商品。

### estimate_rules.csv — 暂估费率（可选）

`channel,kind,rate,basis,note`。`rate` 为费率（如 0.02 = 2%），按整数基点存储；
`kind` 仅支持费用类。规则一旦导入即持久化，后续批次即使不带此文件也沿用同一口径。

## 二、旧版样例（samples/）

`orders.csv`（列 `order_id,line,channel,paid,cost,state`）与
`adjustments.csv`（列 `ref,order_id,kind,amount,owner,received`）。
`kind` 映射：`platform-subsidy→platform_subsidy`、`brand-coupon→brand_coupon`、
`return→return_refund`、`fulfillment→fulfillment_fee`。
旧样例无退货渠道/再销售信息，退货不做成本冲回并标记待运营补核对。

## 三、数据库表（data/pnl.sqlite3）

| 表 | 作用 |
| --- | --- |
| runs | 每次导入批次（时间、目录、文件清单、备注），构成版本链 |
| rejections | 结构不合法被拒收的行与原因（区别于 suspense） |
| order_lines | 订单行锚点（唯一键 order_id+line_no） |
| entries | 会计分录核心表，见下 |
| suspense_cases | 暂挂工单，关联一条 `suspense` 分录 |
| confirmations | 财务/运营的每次人工操作留痕 |
| estimate_rules | 持久化的暂估费率 |
| periods / period_snapshots | 期间状态与定稿全量分录快照（JSONL） |

### entries 关键字段

- `entry_kind`：分录类型（见 `domain.py` 的 `ENTRY_KIND_SPEC`）
- `bearer`：`brand`/`platform`/`unknown`
- `signed_cents`：带符号整数分；增加品牌利益为正、抵减为负
- `order_id/line_no/channel/sku/campaign`：归因锚点（暂挂分录允许为空）
- `source_ref/source_file/source_line`：原始凭证溯源
- `allocation_method`：`direct` / `line-weighted` / `voucher-split` / `estimate` / `suspense`
- `allocation_basis`：JSON，含权重、金额、计算公式、跨渠道标记等
- `event_date/received_date/matured`：业务归属日、实际到达日、截至 as_of 是否成熟
- `verify_status`：`pending` / `confirmed` / `superseded`
- `natural_key`：去重自然键（源文件+凭证号+类型+订单行），重复导入幂等
- `supersedes_entry_id`：暂估冲销指向被冲销的暂估分录
