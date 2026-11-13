"""Read-only, incident-scoped provenance across synthetic Ops and task databases.

No SQL write and no AI call. Entries are *observations* from independent SQLite
files, not an atomic distributed transaction or a proof of human identity.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from .action_tasks import TaskStore
from .store import OpsStore


class CaseNotFound(ValueError):
    pass


def _timestamp(raw: str | None) -> float:
    if not raw:
        return 0.0
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)  # SQLite CURRENT_TIMESTAMP is UTC
        return parsed.timestamp()
    except ValueError:
        return 0.0


def build_case_history(ops: OpsStore, tasks: TaskStore, incident_id: str,
                       limit: int = 100) -> dict:
    """Merge existing audit trails for one incident without mutating either DB."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError('Case history limit must be between 1 and 200')
    if not incident_id or len(incident_id) > 80:
        raise CaseNotFound('Incident not found')
    incident = ops.get_incident(incident_id)
    if incident is None:
        raise CaseNotFound('Incident not found')
    order_id = incident['order_id']
    entries: list[dict] = []

    def append(source: str, row_id: int, at: str, kind: str, description: str,
               *, task_id: str | None = None) -> None:
        entries.append({
            'id': f'{source}:{row_id}', 'source': source,
            'created_at': at, 'kind': kind, 'description': description,
            'task_id': task_id,
        })

    # These two databases have separate transactions. Read independently and
    # label the projection as non-atomic rather than inventing a single snapshot.
    with ops._connection() as con:
        for row in con.execute('''
            SELECT audit_id,operation,actor,subject_id,created_at
            FROM audit_log WHERE subject_id IN (?,?)
            ORDER BY audit_id DESC LIMIT ?
        ''', (order_id, incident_id, limit + 1)):
            append('business_audit', row['audit_id'], row['created_at'], row['operation'],
                   f"业务命令：{row['operation']} · 对象 {row['subject_id']} · 记录角色 {row['actor']}")
        for row in con.execute('''
            SELECT e.event_id,e.transition,e.observed_at,s.rule_code,s.signal_id
            FROM detection_events e JOIN detection_signals s ON s.signal_id=e.signal_id
            WHERE s.order_id=? OR s.incident_id=?
            ORDER BY e.event_id DESC LIMIT ?
        ''', (order_id, incident_id, limit + 1)):
            append('detection', row['event_id'], row['observed_at'], row['transition'],
                   f"异常信号 {row['rule_code']} · {row['transition']} · {row['signal_id']}")

    linked_tasks: list[dict] = []
    if tasks.path.is_file():
        with closing(sqlite3.connect(f'file:{tasks.path}?mode=ro', uri=True)) as con:
            con.row_factory = sqlite3.Row
            linked_tasks = [dict(row) for row in con.execute('''
                SELECT task_id,status,updated_at FROM agent_tasks
                WHERE incident_id=? ORDER BY created_at DESC,task_id DESC LIMIT ?
            ''', (incident_id, limit))]
            for row in con.execute('''
                SELECT e.event_id,e.task_id,e.kind,e.created_at
                FROM agent_task_events e JOIN agent_tasks t ON t.task_id=e.task_id
                WHERE t.incident_id=? ORDER BY e.event_id DESC LIMIT ?
            ''', (incident_id, limit + 1)):
                append('task_event', row['event_id'], row['created_at'], row['kind'],
                       f"任务 {row['task_id']} · {row['kind']}", task_id=row['task_id'])

    # Timestamp normalized to UTC. Stable tie-breaks make repeated reads identical.
    entries.sort(key=lambda r: (_timestamp(r['created_at']), r['source'], r['id']), reverse=True)
    total_collected = len(entries)
    entries = entries[:limit]
    source_counts = {
        name: sum(1 for event in entries if event['source'] == name)
        for name in ('detection', 'business_audit', 'task_event')
    }
    return {
        'synthetic': True, 'incident_id': incident_id, 'order_id': order_id,
        'incident_state': incident['state'], 'order_status': incident['order_status'],
        'shipment_status': incident['shipment_status'],
        'linked_tasks': linked_tasks, 'items': entries,
        'source_counts': source_counts, 'has_more': total_collected > limit,
        'consistency': 'read_only_non_atomic_cross_database_projection',
        'limitations': 'Database role labels are not authenticated user identities; times are database records, not an SLA.',
    }
