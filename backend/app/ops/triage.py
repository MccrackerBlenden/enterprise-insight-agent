"""Read-only, explainable operator queue assembled from persisted Ops state.

No model calls, automatic approvals, or business mutations occur here. The
playbook remains the source of truth for each task's next authorization gate.
"""
from __future__ import annotations

from .action_tasks import TaskStore
from .playbook import OperationsPlaybook
from .store import OpsStore

SEVERITY_ORDER = {'high': 0, 'medium': 1, 'low': 2}
BUCKET_ORDER = {'recovery': 0, 'approval': 1, 'attention': 2,
                'ready': 3, 'new': 4, 'archive': 5}
GATE_BUCKET = {
    'explicit_recovery': 'recovery',
    'reservation_approval': 'approval',
    'fulfillment_approval': 'approval',
    'delivery_attestation': 'approval',
    'manual_handoff': 'attention',
    'ready': 'ready',
    'completed': 'archive',
}
GATE_LABEL = {
    'explicit_recovery': '需人工恢复',
    'reservation_approval': '待预留审批',
    'fulfillment_approval': '待履约审批',
    'delivery_attestation': '待人工确认演示签收',
    'manual_handoff': '需人工检查',
    'ready': '可继续推进',
    'completed': '履约已核验',
}


def build_triage(ops: OpsStore, tasks: TaskStore, limit: int = 100) -> dict:
    """Prioritize cases with source-of-truth gates and avoid fake SLA scores."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError('Queue limit must be between 1 and 100')
    incidents = ops.list_incidents()
    recent_tasks = tasks.list(limit)
    incident_by_id = {item['incident_id']: item for item in incidents}
    playbook = OperationsPlaybook(ops, tasks)
    entries: list[dict] = []
    live_task_incidents: set[str] = set()

    for task in recent_tasks:
        info = incident_by_id.get(task['incident_id'])
        gate = playbook.preview(task['task_id'])
        bucket = GATE_BUCKET.get(gate['gate'], 'attention')
        # Rejected/handoff cases remain searchable history, not phantom approvals.
        terminal = task['status'] in ('rejected', 'blocked', 'stale', 'failed') or (
            task['fulfillment'] is not None and
            task['fulfillment']['status'] in ('rejected', 'handoff', 'completed'))
        if terminal:
            bucket = 'archive'
        if bucket != 'archive':
            live_task_incidents.add(task['incident_id'])
        entries.append({
            'kind': 'task', 'bucket': bucket,
            'task_id': task['task_id'], 'incident_id': task['incident_id'],
            'order_id': task['order_id'],
            'severity': info['severity'] if info else 'unknown',
            'incident_state': info['state'] if info else 'missing',
            'task_status': task['status'],
            'fulfillment_status': task['fulfillment']['status'] if task['fulfillment'] else None,
            'gate': gate['gate'], 'label': GATE_LABEL.get(gate['gate'], '需人工检查'),
            'reason': gate['reason'], 'can_continue': gate['can_continue'],
            'updated_at': task['updated_at'],
        })

    for incident in incidents:
        if incident['incident_id'] in live_task_incidents:
            continue
        resolved = incident['state'] == 'resolved'
        bucket = 'archive' if resolved else ('attention' if incident['state'] == 'escalated' else 'new')
        label = '已核验关闭' if resolved else ('已升级人工' if bucket == 'attention' else '待建立处置任务')
        entries.append({
            'kind': 'incident', 'bucket': bucket, 'task_id': None,
            'incident_id': incident['incident_id'], 'order_id': incident['order_id'],
            'severity': incident['severity'], 'incident_state': incident['state'],
            'task_status': None, 'fulfillment_status': None, 'gate': None,
            'label': label, 'reason': incident['summary'], 'can_continue': False,
            'updated_at': incident.get('updated_at'),
        })

    # Stable order within same bucket and severity: recent tasks first.
    entries.sort(key=lambda item: (BUCKET_ORDER[item['bucket']],
                                   SEVERITY_ORDER.get(item['severity'], 3)))
    summary = {bucket: sum(row['bucket'] == bucket for row in entries)
               for bucket in BUCKET_ORDER}
    return {
        'synthetic': True, 'source': 'ops_sqlite_and_task_checkpoints',
        'queue_rule_version': 'triage-v1', 'total_incidents': len(incidents),
        'recent_task_count': len(recent_tasks), 'task_limit': limit,
        'summary': summary, 'items': entries,
        'incidents': incidents, 'tasks': recent_tasks,
    }
