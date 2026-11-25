# 角色工作流与 CLI Cookbook

全局参数：`--db`（默认 `data/pnl.sqlite3`）、`--as-of`（核算截止日，默认今天）。
建议用隔离模式：`python3 -I run_pnl.py ...`。

## 一、数据工程：导入与质量护栏

```bash
# 导入一个数据目录（六张新表，或旧版 orders+adjustments）
python3 -I run_pnl.py ingest data/synthetic-demo --as-of 2026-11-05 --note "首批"

# 同一目录重复导入是幂等的（自然键去重），修订就地更新
python3 -I run_pnl.py ingest data/synthetic-demo --as-of 2026-11-05

# 结构不合法的行进 rejections 表，命令以 stderr 提示数量；
# 匹配不到订单的合法事件进 suspense，绝不摊入渠道
python3 -I run_pnl.py suspense
```

## 二、财务：确认哪些结算项由品牌承担

```bash
# 看待核对清单（未知承担方、跨渠道退货、暂估、暂挂）
python3 -I run_pnl.py pending

# 确认某条 unknown 优惠/费用由品牌承担（entry_id 取自 pending 输出）
python3 -I run_pnl.py confirm confirm-bearer 38 \
    --actor finance-li --bearer brand --note "平台确认该券品牌承担"

# 若实为平台承担
python3 -I run_pnl.py confirm confirm-bearer 38 --actor finance-li --bearer platform

# 查某条分录的全部留痕
python3 -I run_pnl.py audit --entry-id 38
```

确认承担方后，`unknown_discount` 会重分类为 `brand_coupon` 或 `platform_coupon`，
渠道净贡献立即变化，历史值与操作人留在 `confirmations`。

## 三、运营：核对跨渠道订单身份与退货商品

```bash
# 跨店退货在 pending 中显示，basis 里有 original_channel/return_channel
python3 -I run_pnl.py pending

# 核对无误后转正（归属仍是原下单渠道，不会变）
python3 -I run_pnl.py confirm confirm-return 61 --actor ops-wang \
    --note "门店扫码核到 O2 订单与退货商品一致"
```

## 四、暂挂处理：补录后重新匹配或人工指派

```bash
# 迟到的订单补录后，让系统重新匹配暂挂事件
python3 -I run_pnl.py ingest data/synthetic-demo-drop2 --as-of 2026-11-25
python3 -I run_pnl.py recheck --actor ops-wang

# 无法自动匹配时，人工指派到真实订单行（仍需财务确认承担方）
python3 -I run_pnl.py confirm resolve-suspense 54 \
    --actor finance-li --order-id O1 --line-no 2 --note "凭证补单核对"
```

## 五、经营者：按渠道/商品/活动期看净贡献

```bash
# 渠道
python3 -I run_pnl.py pnl --dimension channel --as-of 2026-11-25
# 商品
python3 -I run_pnl.py pnl --dimension sku --as-of 2026-11-25
# 活动期
python3 -I run_pnl.py pnl --dimension campaign --as-of 2026-11-25

# 只看已成熟结算的口径
python3 -I run_pnl.py pnl --dimension channel --matured --as-of 2026-11-05

# 仍未结算/未成熟的逐笔清单
python3 -I run_pnl.py unsettled --as-of 2026-11-05

# 导出 CSV（含整数分列与元列）
python3 -I run_pnl.py pnl --dimension channel --as-of 2026-11-25 \
    --out data/reports/channel_pnl.csv
```

继续投放决策看 **comparable_net_contribution（可比净贡献，含暂估）**，
而不是把尚未成熟结算当成零成本的 brand_net_contribution。

## 六、识别「总利润稳定但渠道归属变化」

```bash
# 归因桥：旧月报口径（门店退款记线下）与正确口径（锚定原渠道）的差异
python3 -I run_pnl.py bridge --as-of 2026-11-25
```

输出按（正确渠道, 旧月报渠道）累计被错配的金额，并解释来源是跨店退货还是暂挂。

## 七、期间定稿与迟到数据

```bash
# 定稿 10 月（冻结全量分录快照）
python3 -I run_pnl.py periods close 2026-10 --actor finance-li --as-of 2026-11-05

# 定稿后迟到数据照常进活账
python3 -I run_pnl.py ingest data/synthetic-demo-drop2 --as-of 2026-11-25

# 逐笔差异：定稿后新增/金额变化/承担方变化/状态变化
python3 -I run_pnl.py variance 2026-10

# 对比定稿原账与当前活账
python3 -I run_pnl.py pnl --dimension channel --period 2026-10 --snapshot
python3 -I run_pnl.py pnl --dimension channel --period 2026-10

# 财务评估差异后决定是否重开（必须填原因）
python3 -I run_pnl.py periods reopen 2026-10 --actor finance-li \
    --reason "迟到履约费与跨店退货改变渠道结论"

# 恒等护栏：已归属 + 暂挂 = 分录总额
python3 -I run_pnl.py reconcile
```

## 八、机器可读输出

所有列表/报告子命令支持 `--json`（标准输出 JSON）与 `--out 文件.csv`，
便于接入下游系统或 BI。
