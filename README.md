# 渠道净贡献核算与跨店退货成本分摊

一套**仅依赖 Python 3 标准库**的数据应用，用于回答品牌财务的核心问题：
线上平台、直播销售、线下门店各自的**真实**渠道贡献是多少。

它针对的是旧月报的三类失真：

1. 渠道订单金额含平台补贴、品牌券、赠品，履约费用又在结算数周后才到，
   按下单渠道确认全部收入会高估线上实收；
2. 顾客在门店退回线上购买的商品，旧月报把退款记到「线下」，
   让两个渠道的毛利此消彼长，争论的不是同一笔交易；
3. 为了让渠道总额对平，无法匹配的成本被按销售额任意摊入，破坏可核对性。

## 核心设计原则

| 原则 | 实现方式 |
| --- | --- |
| 归因锚定原始下单渠道 | 退货/费用全部挂在订单行的 `channel` 上；跨店退货只标记 `return_channel` 与待核对状态，**不改归属** |
| 不为对平而强摊 | 结构合法但匹配不到订单的事件进 **suspense 暂挂**，单列且不进任何渠道损益 |
| 每笔分摊可核对 | 每条分录带 `allocation_method` 与 `allocation_basis`（权重、公式、凭证号、原始行） |
| 金额零浮点误差 | 全系统整数「分」存储，分摊用整数最大余数法，输出才转两位元 |
| 未成熟结算不当零成本 | 按费率规则生成暂估费用，实际到达后冲销；报告区分「净贡献」与「可比净贡献（含暂估）」 |
| 承担方未确认不默认品牌 | `bearer=unknown` 单列「待确认」，财务确认后才重分类进品牌/平台损益 |
| 定稿可追溯、迟到不覆盖结论 | 定稿冻结快照；迟到数据进活账并出逐笔差异，财务决定是否重开复核 |

## 目录结构

```
channel_pnl/
  money.py       元↔分转换、整数最大余数法分摊
  domain.py      渠道/承担方/分录类型/分摊方式等领域常量与符号约定
  db.py          SQLite 建表与事务（标准库 sqlite3）
  ingest.py      六类源文件 + 旧版双文件的摄入、校验、拆行、暂估、暂挂
  verify.py      财务确认承担方、运营核对退货、人工解决暂挂（全部留痕）
  periods.py     期间定稿快照、迟到差异、重开复核
  reports.py     渠道/商品/活动期损益、未结算、暂挂、归因桥、恒等核对、CSV/控制台输出
  __main__.py    argparse CLI
run_pnl.py       隔离模式（python3 -I）启动器
data/
  synthetic-demo/        首批合成数据（覆盖全部业务场景）
  synthetic-demo-drop2/  定稿后到达的迟到费用/退货 + 暂挂订单补录
  reports/               导出示例
docs/
  data_dictionary.md     输入文件与数据库表结构
  accounting.md          核算口径与计算公式
  workflow.md            角色工作流与 CLI  cookbook
tests/
  test_money.py          金额与分摊不变量
  test_pipeline.py       端到端管线（22 个用例）
  test_sample_inputs.py  原有样例结构测试（保持不变并通过）
samples/                 最初提供的 orders.csv + adjustments.csv（兼容读取）
```

## 快速开始

```bash
# 隔离模式运行（推荐，避免当前目录被植入模块劫持）
python3 -I run_pnl.py ingest data/synthetic-demo --as-of 2026-11-05 \
    --db data/pnl.sqlite3 --note "首批导入"

# 渠道损益（控制台）
python3 -I run_pnl.py pnl --dimension channel --as-of 2026-11-05

# 导出 CSV（同时含整数分列与元列）
python3 -I run_pnl.py pnl --dimension sku --as-of 2026-11-25 \
    --out data/reports/sku_pnl.csv

# 测试
python3 -I tests/run_tests.py
```

一条命令跑通「导入 → 核对 → 定稿 → 迟到数据 → 差异 → 重开」的完整剧本：

```bash
python3 -I docs/demo_walkthrough.py
```

## 兼容旧样例

`samples/` 下的 `orders.csv` + `adjustments.csv`（平台补贴/品牌券/跨店退货/履约
合并在一张调整表）可直接导入，应用自动走兼容加载器，退货仍锚定原渠道、
未知承担方挂待核对：

```bash
python3 -I run_pnl.py ingest samples --db /tmp/legacy.sqlite3 --as-of 2026-10-31
```
