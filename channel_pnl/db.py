"""SQLite 持久层：建表、连接与事务辅助。

只使用标准库 ``sqlite3``。外键约束显式开启；所有金额列为整数分。
表分四组：
  1. 导入批次 runs / rejections（可追溯每次导入了什么、拒了什么）；
  2. 业务锚点 order_lines；
  3. 会计分录 entries + 暂挂 suspense_cases + 人工核对 confirmations；
  4. 期间 periods 与定稿快照 period_snapshots。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 每次 ingest 是一个批次，天然形成未定版期间内的版本链
CREATE TABLE IF NOT EXISTS runs (
    run_id          INTEGER PRIMARY KEY,
    imported_at     TEXT NOT NULL,
    source_dir      TEXT NOT NULL,
    file_names      TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT ''
);

-- 结构不合法、无法进入核算流程的行（与 suspense 区分：
-- suspense 是结构合法但找不到对应订单，rejections 是文件本身就不合规）
CREATE TABLE IF NOT EXISTS rejections (
    rejection_id    INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES runs(run_id),
    file_name       TEXT NOT NULL,
    line_no         INTEGER NOT NULL,
    reason          TEXT NOT NULL,
    raw_row         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_lines (
    order_id        TEXT NOT NULL,
    line_no         INTEGER NOT NULL,
    channel         TEXT NOT NULL,
    sku             TEXT NOT NULL DEFAULT '',
    campaign        TEXT NOT NULL DEFAULT '',
    order_date      TEXT NOT NULL,
    period_key      TEXT NOT NULL,
    paid_cents      INTEGER NOT NULL,
    cogs_cents      INTEGER NOT NULL,
    state           TEXT NOT NULL,
    run_id          INTEGER NOT NULL REFERENCES runs(run_id),
    PRIMARY KEY (order_id, line_no)
);

CREATE TABLE IF NOT EXISTS entries (
    entry_id            INTEGER PRIMARY KEY,
    run_id              INTEGER NOT NULL REFERENCES runs(run_id),
    period_key          TEXT NOT NULL,
    entry_kind          TEXT NOT NULL,
    bearer              TEXT NOT NULL,              -- brand/platform/unknown
    signed_cents        INTEGER NOT NULL,
    order_id            TEXT,                       -- 暂挂分录允许为空
    line_no             INTEGER,
    channel             TEXT,                       -- 冗余自订单，便于聚合
    sku                 TEXT NOT NULL DEFAULT '',
    campaign            TEXT NOT NULL DEFAULT '',
    source_ref          TEXT NOT NULL,              -- 原始凭证号
    source_file         TEXT NOT NULL,
    source_line         INTEGER NOT NULL,
    allocation_method   TEXT NOT NULL,
    allocation_basis    TEXT NOT NULL,              -- JSON：分摊权重与计算依据
    event_date          TEXT NOT NULL,              -- 业务归属日期
    received_date       TEXT,                       -- 实际到达/结算日期
    matured             INTEGER NOT NULL DEFAULT 1, -- 截至 as_of 是否已成熟
    verify_status       TEXT NOT NULL DEFAULT 'pending',
    supersedes_entry_id INTEGER REFERENCES entries(entry_id),
    natural_key         TEXT NOT NULL,
    UNIQUE(natural_key),
    CHECK (allocation_method IN ('direct','line-weighted','voucher-split',
                                 'estimate','suspense'))
);

-- 结构合法但无法匹配到订单/订单行的事件，一一对应一条 suspense 分录
CREATE TABLE IF NOT EXISTS suspense_cases (
    entry_id        INTEGER PRIMARY KEY REFERENCES entries(entry_id),
    reason         TEXT NOT NULL,
    resolved       INTEGER NOT NULL DEFAULT 0,
    resolution_note TEXT NOT NULL DEFAULT ''
);

-- 财务确认承担方、运营核对跨渠道身份的留痕
CREATE TABLE IF NOT EXISTS confirmations (
    confirmation_id INTEGER PRIMARY KEY,
    entry_id        INTEGER REFERENCES entries(entry_id),
    action          TEXT NOT NULL,                  -- confirm-bearer / confirm-return / resolve-suspense / reopen-period
    actor           TEXT NOT NULL,
    old_value       TEXT NOT NULL,
    new_value       TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    confirmed_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS periods (
    period_key      TEXT PRIMARY KEY,
    state           TEXT NOT NULL DEFAULT 'open',
    closed_at       TEXT,
    closed_by       TEXT NOT NULL DEFAULT '',
    reopen_reason   TEXT NOT NULL DEFAULT ''
);

-- 暂估费率规则持久化：任何一次导入提供后，后续批次都按同一套规则重算
CREATE TABLE IF NOT EXISTS estimate_rules (
    rule_id         INTEGER PRIMARY KEY,
    channel         TEXT NOT NULL,
    entry_kind      TEXT NOT NULL,
    basis_points    INTEGER NOT NULL,
    rate_text       TEXT NOT NULL,
    basis           TEXT NOT NULL DEFAULT 'paid',
    note            TEXT NOT NULL DEFAULT '',
    updated_run_id  INTEGER NOT NULL REFERENCES runs(run_id),
    UNIQUE(channel, entry_kind)
);

-- 定稿时的全量分录冻结副本（JSON 行），迟到数据到达后与之比对出差异
CREATE TABLE IF NOT EXISTS period_snapshots (
    snapshot_id     INTEGER PRIMARY KEY,
    period_key      TEXT NOT NULL REFERENCES periods(period_key),
    taken_at        TEXT NOT NULL,
    taken_by        TEXT NOT NULL,
    payload_jsonl   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_period ON entries(period_key);
CREATE INDEX IF NOT EXISTS idx_entries_order ON entries(order_id, line_no);
CREATE INDEX IF NOT EXISTS idx_entries_verify ON entries(verify_status);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开/初始化数据库连接，开启外键与按列名访问。"""
    path = Path(db_path)
    if path.parent != Path(".") and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """简单事务上下文：异常回滚，正常提交。"""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
