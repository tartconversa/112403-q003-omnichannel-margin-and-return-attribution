"""领域常量与分录语义定义。

一张订单行（order_line）是所有收入与成本的归集锚点：无论补贴、券、赠品、
履约费还是退货，都尽量落到具体订单行；落不到的进暂挂（suspense），
不做任何「为了对平而按销售额摊派」的处理。

金额符号约定（``entries.signed_cents``）：
  - 增加品牌利益的记正（实收、平台补贴、退货冲回的成本等）；
  - 减少品牌利益的记负（商品成本、退款、品牌承担的费用等）。
报告层按 :data:`ENTRY_KIND_CLASS` 归类，不依赖调用方再次判断正负。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 渠道
# 渠道是订单的下单渠道，也是全部归因的唯一归属维度。门店退线上货时，
# 退货分录仍挂在原始订单的渠道上，仅标记 return_channel 供运营核对。
CHANNELS = ("market-a", "market-b", "live", "offline")

# ---------------------------------------------------------------- 承担方
# 平台结算与优惠行可能由平台或品牌承担。owner=unknown 表示财务尚未确认，
# 系统只挂账、不默认并入品牌成本，报告中以「待确认承担方」单独列示。
BEARER_BRAND = "brand"
BEARER_PLATFORM = "platform"
BEARER_UNKNOWN = "unknown"
BEARERS = (BEARER_BRAND, BEARER_PLATFORM, BEARER_UNKNOWN)

# ---------------------------------------------------------------- 分录类别
# 报告的三大块：实收净额、可归属成本、未结算/待确认。
CLASS_RECEIPT = "receipt"          # 实收净额组成
CLASS_COST = "cost"                # 可归属成本组成
CLASS_SETTLEMENT = "settlement"    # 平台结算相关（补贴/佣金/结算费）
CLASS_PENDING = "pending"          # 待确认承担方，不并入品牌损益

# ---------------------------------------------------------------- 分录类型
# 每一种业务事件对应一个 entry_kind，语义和符号在此集中声明。
# 值为 (类别, 符号, 中文说明)。符号 +1 表示 signed_cents 与业务金额同向，
# -1 表示业务金额以负数列示。
ENTRY_KIND_SPEC: dict[str, tuple[str, int, str]] = {
    # 订单实付（顾客实付金额，渠道订单金额的基础）
    "gross_receipt": (CLASS_RECEIPT, +1, "顾客实付金额"),
    # 平台/品牌优惠对实收的抵减
    "platform_subsidy": (CLASS_SETTLEMENT, +1, "平台补贴（平台承担，品牌实收增加）"),
    "brand_coupon": (CLASS_RECEIPT, -1, "品牌券（品牌承担，抵减实收）"),
    "platform_coupon": (CLASS_SETTLEMENT, +1, "平台券（平台承担）"),
    "unknown_discount": (CLASS_PENDING, -1, "承担方未确认的优惠"),
    # 平台结算扣项
    "platform_commission": (CLASS_COST, -1, "平台佣金"),
    "platform_fee": (CLASS_COST, -1, "平台结算费"),
    # 商品与赠品成本
    "cogs": (CLASS_COST, -1, "商品成本"),
    "gift_cost": (CLASS_COST, -1, "赠品成本"),
    # 履约费用
    "fulfillment_fee": (CLASS_COST, -1, "履约费用"),
    # 退货（冲减原订单行收入与成本；归属仍为原始下单渠道）
    "return_refund": (CLASS_RECEIPT, -1, "退货退款（冲减原渠道实收）"),
    "cogs_reversal": (CLASS_COST, +1, "退货商品成本冲回（可二次销售时）"),
    # 暂估与暂挂
    "estimated_fee": (CLASS_COST, -1, "未成熟结算的暂估费用（实际到达后冲销）"),
    "estimate_reversal": (CLASS_COST, +1, "暂估费用冲销"),
    "suspense": (CLASS_PENDING, -1, "无法匹配订单的暂挂金额"),
}

ENTRY_KINDS = tuple(ENTRY_KIND_SPEC)
ENTRY_KIND_CLASS = {k: v[0] for k, v in ENTRY_KIND_SPEC.items()}
ENTRY_KIND_SIGN = {k: v[1] for k, v in ENTRY_KIND_SPEC.items()}
ENTRY_KIND_LABEL = {k: v[2] for k, v in ENTRY_KIND_SPEC.items()}

# 品牌净贡献只汇总「品牌实际承担/实收」的类别；
# settlement 类（平台承担项）与 pending 类（未确认/暂挂）单列。
BRAND_PNL_CLASSES = (CLASS_RECEIPT, CLASS_COST)

# ---------------------------------------------------------------- 分摊方式
ALLOC_DIRECT = "direct"            # 明确指向单一订单行
ALLOC_WEIGHTED = "line-weighted"   # 按订单行权重（如实付金额）分摊
ALLOC_VOUCHER = "voucher-split"    # 一张退款凭证覆盖多笔订单，按凭证明细
ALLOC_ESTIMATE = "estimate"        # 系统按费率规则暂估
ALLOC_SUSPENSE = "suspense"        # 无法匹配，挂账不摊

ALLOCATION_METHODS = (
    ALLOC_DIRECT, ALLOC_WEIGHTED, ALLOC_VOUCHER, ALLOC_ESTIMATE, ALLOC_SUSPENSE,
)

# ---------------------------------------------------------------- 核对状态
STATUS_PENDING = "pending"         # 待财务/运营核对
STATUS_CONFIRMED = "confirmed"     # 已核对
STATUS_SUPERSEDED = "superseded"   # 被实际到达的分录冲销（暂估）
VERIFY_STATUSES = (STATUS_PENDING, STATUS_CONFIRMED, STATUS_SUPERSEDED)

# ---------------------------------------------------------------- 订单状态
ORDER_STATES = ("settled", "shipped", "pending", "returned", "cancelled")

# ---------------------------------------------------------------- 期间状态
PERIOD_OPEN = "open"
PERIOD_CLOSED = "closed"
PERIOD_REOPENED = "reopened"

# ---------------------------------------------------------------- 旧版样例映射
# samples/ 目录下的 adjustments.csv 把四类事件合并在一张表里，
# 这里保留读取兼容，使交付的样例结构测试与新应用共存。
LEGACY_KIND_MAP = {
    "platform-subsidy": "platform_subsidy",
    "brand-coupon": "brand_coupon",
    "return": "return_refund",
    "fulfillment": "fulfillment_fee",
}
LEGACY_BEARER_MAP = {
    "platform": BEARER_PLATFORM,
    "brand": BEARER_BRAND,
    "unknown": BEARER_UNKNOWN,
}
