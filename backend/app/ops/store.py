"""Isolated SQLite store with explicit transactions and repeatable demo seeding."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .schema import SCHEMA, SCHEMA_V2, WAREHOUSES, PRODUCTS, INVENTORY, ORDERS, INCIDENTS

DEFAULT_PATH = Path(os.environ.get(
    'EIA_OPS_DB_PATH',
    str(Path(__file__).resolve().parents[3] / 'data' / 'operations.sqlite'),
))


class OpsStore:
    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys = ON')
        con.execute('PRAGMA busy_timeout = 5000')
        return con

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        con = self._connect()
        try:
            yield con
        finally:
            con.close()

    def init(self) -> None:
        """Initialize once. Never delete or overwrite an existing operations DB."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as con:
            con.execute('PRAGMA journal_mode = WAL')
            ver = con.execute('PRAGMA user_version').fetchone()[0]
            if ver not in (0, 1, 2):
                raise RuntimeError(f'Unsupported operations schema version: {ver}')
            con.executescript(SCHEMA)
            # The v2 DDL can be replayed safely after an interrupted migration.
            con.executescript(SCHEMA_V2)
            con.execute('BEGIN IMMEDIATE')
            try:
                if not con.execute("SELECT 1 FROM ops_meta WHERE key='seed_v1'").fetchone():
                    con.executemany('INSERT OR IGNORE INTO warehouses VALUES (?,?,?,?)', WAREHOUSES)
                    con.executemany('INSERT OR IGNORE INTO products VALUES (?,?)', PRODUCTS)
                    con.executemany('INSERT OR IGNORE INTO inventory VALUES (?,?,?,?)', INVENTORY)
                    con.executemany('INSERT OR IGNORE INTO orders VALUES (?,?,?,?,?,?)', ORDERS)
                    con.executemany(
                        'INSERT OR IGNORE INTO incidents '
                        '(incident_id,order_id,kind,severity,state,summary) VALUES (?,?,?,?,?,?)',
                        INCIDENTS,
                    )
                    con.execute("INSERT INTO ops_meta VALUES ('seed_v1','20261111')")
                con.execute('PRAGMA user_version = 2')
                con.commit()
            except Exception:
                con.rollback()
                raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                yield con
                con.commit()
            except Exception:
                con.rollback()
                raise

    def list_incidents(self) -> list[dict]:
        with self._connection() as con:
            return [dict(row) for row in con.execute('''
                SELECT i.incident_id,i.kind,i.severity,i.state,i.summary,
                       o.order_id,o.sku_id,o.quantity,o.status AS order_status,
                       o.warehouse_id
                FROM incidents i JOIN orders o ON i.order_id=o.order_id
                ORDER BY i.incident_id
            ''')]

    def get_incident(self, incident_id: str) -> dict | None:
        with self._connection() as con:
            row = con.execute('''
                SELECT i.*,o.sku_id,o.quantity,o.status AS order_status,
                       o.warehouse_id,o.promised_at,
                       s.shipment_id,s.carrier,s.status AS shipment_status
                FROM incidents i JOIN orders o ON i.order_id=o.order_id
                LEFT JOIN shipments s ON s.order_id=o.order_id
                WHERE i.incident_id=?
            ''', (incident_id,)).fetchone()
            if row is None:
                return None
            data = dict(row)
            data['inventory'] = [dict(x) for x in con.execute('''
                SELECT v.warehouse_id,v.on_hand,v.reserved,
                       (v.on_hand-v.reserved) AS available
                FROM inventory v WHERE v.sku_id=? ORDER BY v.warehouse_id
            ''', (data['sku_id'],))]
            data['audit'] = [dict(x) for x in con.execute('''
                SELECT audit_id,operation,actor,subject_id,created_at
                FROM audit_log WHERE subject_id IN (?,?) ORDER BY audit_id
            ''', (data['order_id'], incident_id))]
            return data

    def summary(self) -> dict:
        with self._connection() as con:
            counts = {r['state']: r['n'] for r in con.execute(
                'SELECT state,COUNT(*) n FROM incidents GROUP BY state')}
            return {
                'synthetic': True,
                'orders': con.execute('SELECT COUNT(*) FROM orders').fetchone()[0],
                'incidents': counts,
                'shipments': con.execute('SELECT COUNT(*) FROM shipments').fetchone()[0],
                'operations_schema': 2,
            }

    @staticmethod
    def _signal_dict(row: sqlite3.Row) -> dict:
        import json
        record = dict(row)
        record['evidence'] = json.loads(record.pop('evidence_json'))
        return record

    def list_signals(self, status: str | None = None, limit: int = 100) -> list[dict]:
        if status not in (None, 'active', 'cleared') or not 1 <= limit <= 500:
            raise ValueError('Invalid signal filter or limit')
        with self._connection() as con:
            query = ('SELECT * FROM detection_signals')
            params: tuple = ()
            if status is not None:
                query += ' WHERE status=?'
                params = (status,)
            query += ' ORDER BY last_transition_at DESC, signal_id LIMIT ?'
            rows = con.execute(query, (*params, limit)).fetchall()
            return [self._signal_dict(r) for r in rows]

    def get_signal(self, signal_id: str) -> dict | None:
        with self._connection() as con:
            row = con.execute('SELECT * FROM detection_signals WHERE signal_id=?',
                              (signal_id,)).fetchone()
            return self._signal_dict(row) if row else None

    def list_signal_events(self, after_id: int = 0, limit: int = 100) -> list[dict]:
        if after_id < 0 or not 1 <= limit <= 500:
            raise ValueError('Invalid event cursor or limit')
        with self._connection() as con:
            rows = con.execute('''
                SELECT event_id,signal_id,transition,revision,severity,evidence_json,observed_at
                FROM detection_events WHERE event_id>? ORDER BY event_id LIMIT ?
            ''', (after_id, limit)).fetchall()
            return [self._signal_dict(row) for row in rows]

    def fulfillment_quote(self, order_id: str) -> dict:
        """Read-only evaluation; it never reserves inventory."""
        from .planning import quote_order
        with self._connection() as con:
            return quote_order(con, order_id)

    def detector_summary(self) -> dict:
        with self._connection() as con:
            active = {row['severity']: row['n'] for row in con.execute('''
                SELECT severity, COUNT(*) AS n FROM detection_signals
                WHERE status='active' GROUP BY severity
            ''')}
            return {
                'synthetic': True, 'schema': 2, 'active_by_severity': active,
                'active_total': sum(active.values()),
                'signals_total': con.execute('SELECT COUNT(*) FROM detection_signals').fetchone()[0],
                'events_total': con.execute('SELECT COUNT(*) FROM detection_events').fetchone()[0],
                'last_event_id': con.execute('SELECT COALESCE(MAX(event_id),0) FROM detection_events').fetchone()[0],
            }
