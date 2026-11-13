"""Deterministic, fictional fulfillment domain. Never reuses the analytics DB."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS ops_meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS warehouses (
  warehouse_id TEXT PRIMARY KEY, name TEXT NOT NULL, region TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);
CREATE TABLE IF NOT EXISTS products (
  sku_id TEXT PRIMARY KEY, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory (
  warehouse_id TEXT NOT NULL REFERENCES warehouses(warehouse_id),
  sku_id TEXT NOT NULL REFERENCES products(sku_id),
  on_hand INTEGER NOT NULL CHECK(on_hand >= 0),
  reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved >= 0 AND reserved <= on_hand),
  PRIMARY KEY(warehouse_id, sku_id)
);
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY,
  sku_id TEXT NOT NULL REFERENCES products(sku_id),
  quantity INTEGER NOT NULL CHECK(quantity > 0),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','reserved','shipped','delivered','cancelled')),
  warehouse_id TEXT REFERENCES warehouses(warehouse_id),
  promised_at TEXT NOT NULL,
  CHECK ((status = 'pending' AND warehouse_id IS NULL)
       OR (status IN ('reserved','shipped','delivered') AND warehouse_id IS NOT NULL)
       OR status = 'cancelled')
);
CREATE TABLE IF NOT EXISTS shipments (
  shipment_id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL UNIQUE REFERENCES orders(order_id),
  carrier TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'created'
    CHECK(status IN ('created','in_transit','delivered')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS incidents (
  incident_id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES orders(order_id),
  kind TEXT NOT NULL CHECK(kind IN ('fulfillment_risk')),
  severity TEXT NOT NULL CHECK(severity IN ('low','medium','high')),
  state TEXT NOT NULL DEFAULT 'open'
    CHECK(state IN ('open','investigating','resolved','escalated')),
  summary TEXT NOT NULL,
  resolution_note TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_incidents_state ON incidents(state,severity);
CREATE TABLE IF NOT EXISTS command_receipts (
  idempotency_key TEXT PRIMARY KEY,
  operation TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS audit_log (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  idempotency_key TEXT NOT NULL UNIQUE REFERENCES command_receipts(idempotency_key)
    DEFERRABLE INITIALLY DEFERRED,
  operation TEXT NOT NULL,
  actor TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  before_json TEXT NOT NULL,
  after_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# Deliberately small but *stateful* synthetic acceptance scenarios, not model outputs.
WAREHOUSES = [
    ('WH-EAST', '东区主仓', 'east', 1),
    ('WH-WEST', '西区备仓', 'west', 1),
]
PRODUCTS = [('SKU-A', '轻量设备'), ('SKU-B', '办公组件'), ('SKU-C', '紧缺部件')]
INVENTORY = [
    ('WH-EAST', 'SKU-A', 2, 0), ('WH-WEST', 'SKU-A', 10, 0),
    ('WH-EAST', 'SKU-B', 8, 0), ('WH-WEST', 'SKU-B', 3, 0),
    ('WH-EAST', 'SKU-C', 1, 0), ('WH-WEST', 'SKU-C', 2, 0),
]
ORDERS = [
    ('ORD-1001', 'SKU-A', 3, 'pending', None, '2026-11-13T18:00:00+08:00'),
    ('ORD-1002', 'SKU-B', 2, 'pending', None, '2026-11-14T18:00:00+08:00'),
    ('ORD-1003', 'SKU-C', 20, 'pending', None, '2026-11-13T18:00:00+08:00'),
]
INCIDENTS = [
    ('INC-1001', 'ORD-1001', 'fulfillment_risk', 'high', 'open', '东区库存不足，需要从其他仓履约'),
    ('INC-1002', 'ORD-1002', 'fulfillment_risk', 'medium', 'open', '待校验库存并完成履约'),
    ('INC-1003', 'ORD-1003', 'fulfillment_risk', 'high', 'open', '所有仓库均不足，需升级人工处置'),
]

# V0.6-A2: durable signals, independent of operator-controlled incidents.
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS detection_signals (
  signal_id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES orders(order_id),
  rule_code TEXT NOT NULL CHECK(rule_code IN ('inventory_shortage','promise_overdue','shipment_stalled')),
  incident_id TEXT REFERENCES incidents(incident_id),
  status TEXT NOT NULL CHECK(status IN ('active','cleared')),
  severity TEXT NOT NULL CHECK(severity IN ('low','medium','high')),
  evidence_json TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
  first_detected_at TEXT NOT NULL,
  last_transition_at TEXT NOT NULL,
  UNIQUE(order_id,rule_code)
);
CREATE INDEX IF NOT EXISTS idx_detection_signals_status
  ON detection_signals(status,severity,order_id);
CREATE TABLE IF NOT EXISTS detection_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id TEXT NOT NULL REFERENCES detection_signals(signal_id),
  transition TEXT NOT NULL CHECK(transition IN ('detected','updated','cleared','reopened')),
  revision INTEGER NOT NULL CHECK(revision > 0),
  severity TEXT NOT NULL CHECK(severity IN ('low','medium','high')),
  evidence_json TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  UNIQUE(signal_id,revision)
);
CREATE INDEX IF NOT EXISTS idx_detection_events_order ON detection_events(event_id);
"""
