"""Durable, approval-gated fulfillment tasks on a separate SQLite file.

The LLM is never given write access. Task state and the Ops business command are
in separate DBs; a stable command key reconciles a crash between the two commits.
This is a local synthetic demo, not a public multi-tenant authorization system.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .service import OpsError, OpsService, RuleViolation, StateConflict, NotFound
from .store import OpsStore
from .evidence import capture_evidence, only_quote_window_changed
from .decision import decision_packet, validate_stored_decision
from .model_plan import validate_stored_model_plan, revalidate_prior_suggestion


class TaskStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=8, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA busy_timeout=8000')
        con.execute('PRAGMA foreign_keys=ON')
        try:
            con.execute('BEGIN IMMEDIATE')
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    warehouse_id TEXT,
                    snapshot_fingerprint TEXT,
                    status TEXT NOT NULL CHECK(status IN
                        ('blocked','pending_approval','approved','rejected',
                         'executing','succeeded','stale','failed')),
                    proposal_json TEXT NOT NULL,
                    result_json TEXT,
                    failure_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES agent_tasks(task_id),
                    kind TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_tasks_created
                  ON agent_tasks(created_at DESC);
                CREATE TABLE IF NOT EXISTS agent_model_plans (
                    plan_id TEXT PRIMARY KEY,
                    investigation_id TEXT NOT NULL REFERENCES agent_investigations(investigation_id),
                    incident_id TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    adopted_task_id TEXT REFERENCES agent_tasks(task_id),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_model_plans_investigation
                  ON agent_model_plans(investigation_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_task_events
                  ON agent_task_events(task_id,event_id);
                -- Additive investigation dossier, separate from Ops schema v2.
                CREATE TABLE IF NOT EXISTS agent_investigations (
                    investigation_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    model_status TEXT NOT NULL CHECK(model_status IN
                        ('not_run','running','completed','failed')),
                    model_summary TEXT,
                    model_tool_calls INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                -- Additive task-store table. Existing A4 tasks and Ops schema v2
                -- remain unchanged. No operations.sqlite migration is needed.
                CREATE TABLE IF NOT EXISTS fulfillment_workflows (
                    task_id TEXT PRIMARY KEY REFERENCES agent_tasks(task_id),
                    carrier TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'pending_approval','approved','executing',
                        'awaiting_delivery_confirmation','completed',
                        'rejected','blocked','handoff')),
                    next_step INTEGER NOT NULL DEFAULT 0 CHECK(next_step BETWEEN 0 AND 4),
                    inflight_step TEXT,
                    failure_code TEXT,
                    handoff_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            ''')

    @staticmethod
    def _event(con, task_id: str, kind: str, detail: dict):
        con.execute('''INSERT INTO agent_task_events(task_id,kind,detail_json,created_at)
                       VALUES (?,?,?,?)''',
                    (task_id, kind, json.dumps(detail, ensure_ascii=False, sort_keys=True),
                     datetime.now(timezone.utc).isoformat()))

    def create_investigation(self, incident_id: str, captured: dict, *,
                             model_status: str = 'not_run') -> dict:
        if model_status not in ('not_run', 'running'):
            raise RuleViolation('Invalid investigation status')
        investigation_id = 'INV-' + uuid.uuid4().hex[:20].upper()
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as con:
            con.execute('''INSERT INTO agent_investigations
                (investigation_id,incident_id,evidence_sha256,evidence_json,
                 model_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)''',
                (investigation_id, incident_id, captured['sha256'],
                 json.dumps(captured['facts'], ensure_ascii=False, sort_keys=True),
                 model_status, now, now))
        return self.get_investigation(investigation_id)

    def get_investigation(self, investigation_id: str) -> dict:
        if not re.fullmatch(r'INV-[0-9A-F]{20}', investigation_id):
            raise NotFound('Investigation not found')
        with self.transaction() as con:
            row = con.execute('SELECT * FROM agent_investigations WHERE investigation_id=?',
                              (investigation_id,)).fetchone()
            if row is None:
                raise NotFound('Investigation not found')
            data = dict(row)
            data['evidence'] = json.loads(data.pop('evidence_json'))
            data['model_text_independently_verified'] = False
            return data

    def finish_investigation(self, investigation_id: str, *,
                             model_status: str, summary: str = '',
                             tool_calls: int = 0) -> dict:
        if model_status not in ('completed', 'failed'):
            raise RuleViolation('Invalid investigation final status')
        if not isinstance(tool_calls, int) or isinstance(tool_calls, bool) or not 0 <= tool_calls <= 20:
            raise RuleViolation('Invalid tool call count')
        with self.transaction() as con:
            row = con.execute('SELECT model_status FROM agent_investigations WHERE investigation_id=?',
                              (investigation_id,)).fetchone()
            if row is None:
                raise NotFound('Investigation not found')
            if row['model_status'] != 'running':
                raise StateConflict('Investigation is no longer running')
            con.execute('''UPDATE agent_investigations SET
                model_status=?,model_summary=?,model_tool_calls=?,updated_at=?
                WHERE investigation_id=?''',
                (model_status, summary[:4000] if model_status == 'completed' else None,
                 tool_calls, datetime.now(timezone.utc).isoformat(), investigation_id))
        return self.get_investigation(investigation_id)

    def create_model_plan(self, investigation_id: str, proposal: dict) -> dict:
        validate_stored_model_plan({'plan': proposal})
        plan_id = 'PLAN-' + uuid.uuid4().hex[:20].upper()
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as con:
            inv = con.execute('''SELECT incident_id,evidence_sha256,model_status
                FROM agent_investigations WHERE investigation_id=?''', (investigation_id,)).fetchone()
            if inv is None or inv['model_status'] != 'completed':
                raise StateConflict('Model plan requires a completed investigation')
            if inv['evidence_sha256'] != proposal['evidence_sha256']:
                raise StateConflict('Plan evidence does not match investigation')
            con.execute('''INSERT INTO agent_model_plans
                (plan_id,investigation_id,incident_id,evidence_sha256,plan_json,created_at)
                VALUES(?,?,?,?,?,?)''',
                (plan_id, investigation_id, inv['incident_id'], proposal['evidence_sha256'],
                 json.dumps(proposal, ensure_ascii=False, sort_keys=True), now))
        return self.get_model_plan(plan_id)

    def get_model_plan(self, plan_id: str) -> dict:
        if not re.fullmatch(r'PLAN-[0-9A-F]{20}', plan_id):
            raise NotFound('Model plan not found')
        with self.transaction() as con:
            row = con.execute('SELECT * FROM agent_model_plans WHERE plan_id=?', (plan_id,)).fetchone()
            if row is None:
                raise NotFound('Model plan not found')
            result = dict(row)
            result['plan'] = json.loads(result.pop('plan_json'))
            validate_stored_model_plan(result)
            return result

    def latest_unadopted_model_plan(self, incident_id: str) -> dict | None:
        """Read-only recovery of a prior model plan after browser refresh."""
        if not re.fullmatch(r'[A-Za-z0-9-]{3,40}', incident_id):
            raise NotFound('Incident not found')
        with self.transaction() as con:
            row = con.execute('''SELECT plan_id FROM agent_model_plans
                WHERE incident_id=? AND adopted_task_id IS NULL
                ORDER BY created_at DESC, plan_id DESC LIMIT 1''',
                (incident_id,)).fetchone()
        return self.get_model_plan(row['plan_id']) if row else None

    def create_revalidated_model_plan(self, source_plan_id: str, captured: dict,
                                      refreshed: dict) -> dict:
        # Append immutable origin and new evidence; do not claim a fresh model run.
        validate_stored_model_plan({'plan': refreshed})
        if (refreshed.get('revalidated_from_plan_id') != source_plan_id or
                refreshed.get('model_reused_without_new_call') is not True or
                refreshed.get('evidence_sha256') != captured['sha256']):
            raise StateConflict('Invalid free revalidation provenance')
        now = datetime.now(timezone.utc).isoformat()
        new_investigation_id = 'INV-' + uuid.uuid4().hex[:20].upper()
        new_plan_id = 'PLAN-' + uuid.uuid4().hex[:20].upper()
        with self.transaction() as con:
            prior = con.execute('SELECT * FROM agent_model_plans WHERE plan_id=?',
                                (source_plan_id,)).fetchone()
            if prior is None or prior['adopted_task_id'] is not None:
                raise StateConflict('Original model plan missing or already adopted')
            old_plan = json.loads(prior['plan_json'])
            validate_stored_model_plan({'plan': old_plan})
            inv = con.execute('SELECT * FROM agent_investigations WHERE investigation_id=?',
                              (prior['investigation_id'],)).fetchone()
            if inv is None or inv['incident_id'] != prior['incident_id']:
                raise StateConflict('Original investigation provenance missing')
            if (old_plan['evidence_sha256'] != inv['evidence_sha256'] or
                    not only_quote_window_changed(json.loads(inv['evidence_json']), captured['facts'])):
                raise StateConflict('Business facts changed; new investigation required')
            if (refreshed['incident_id'] != old_plan['incident_id'] or
                    refreshed['order_id'] != old_plan['order_id'] or
                    refreshed['warehouse_id'] != old_plan['warehouse_id'] or
                    refreshed['policy_recommended_warehouse_id'] != old_plan['policy_recommended_warehouse_id'] or
                    refreshed['policy_version'] != old_plan['policy_version']):
                raise StateConflict('Revalidated target or policy changed')
            # Same source and evidence always return the same immutable refresh.
            existing_id = None
            for row in con.execute('''SELECT plan_id,plan_json FROM agent_model_plans
                WHERE incident_id=? AND evidence_sha256=?''',
                (prior['incident_id'], captured['sha256'])):
                candidate = json.loads(row['plan_json'])
                if candidate.get('revalidated_from_plan_id') == source_plan_id:
                    validate_stored_model_plan({'plan': candidate})
                    existing_id = row['plan_id']
                    break
            if existing_id is None:
                con.execute('''INSERT INTO agent_investigations
                    (investigation_id,incident_id,evidence_sha256,evidence_json,model_status,
                     created_at,updated_at) VALUES(?,?,?,?,?,?,?)''',
                    (new_investigation_id, prior['incident_id'], captured['sha256'],
                     json.dumps(captured['facts'], ensure_ascii=False, sort_keys=True),
                     'not_run', now, now))
                con.execute('''INSERT INTO agent_model_plans
                    (plan_id,investigation_id,incident_id,evidence_sha256,plan_json,created_at)
                    VALUES(?,?,?,?,?,?)''',
                    (new_plan_id, new_investigation_id, prior['incident_id'], captured['sha256'],
                     json.dumps(refreshed, ensure_ascii=False, sort_keys=True), now))
        return self.get_model_plan(existing_id or new_plan_id)

    def create(self, incident_id: str, order_id: str, quote: dict, evidence: dict,
               *, investigation_id: str, evidence_sha256: str, decision: dict,
               model_plan_id: str | None = None) -> dict:
        task_id = 'TASK-' + uuid.uuid4().hex[:20].upper()
        warehouse = decision['selected_warehouse_id']
        status = 'pending_approval' if warehouse else 'blocked'
        proposal = {
            'synthetic': True, 'incident_id': incident_id, 'order_id': order_id,
            'action': 'reserve_inventory' if warehouse else 'requires_human_resolution',
            'warehouse_id': warehouse, 'quote': quote,
            'evidence': {'incident_state': evidence['state'],
                         'order_status': evidence['order_status'],
                         'sku_id': evidence['sku_id'],
                         'quantity': evidence['quantity'],
                         'inventory': evidence['inventory']},
            'scope': 'inventory reservation only; NOT dispatch, delivery, or incident resolution',
            'investigation_id': investigation_id,
            'evidence_sha256': evidence_sha256,
            'decision': decision,
            'model_narrative_independently_verified': False,
            **({'model_plan_id': model_plan_id} if model_plan_id else {}),
        }
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as con:
            source = con.execute('''SELECT incident_id,evidence_sha256,model_status FROM agent_investigations
                WHERE investigation_id=?''', (investigation_id,)).fetchone()
            if source is None or source['incident_id'] != incident_id:
                raise StateConflict('Investigation does not belong to the incident')
            if source['evidence_sha256'] != evidence_sha256 or source['model_status'] not in ('not_run','completed'):
                raise StateConflict('Investigation is incomplete or evidence has changed')
            if model_plan_id is not None:
                linked = con.execute('''SELECT investigation_id,incident_id,evidence_sha256,
                    plan_json,adopted_task_id FROM agent_model_plans WHERE plan_id=?''',
                    (model_plan_id,)).fetchone()
                if linked is None or linked['adopted_task_id'] is not None:
                    raise StateConflict('Model plan missing or already adopted')
                model = json.loads(linked['plan_json'])
                validate_stored_model_plan({'plan': model})
                if (linked['investigation_id'] != investigation_id or
                        linked['incident_id'] != incident_id or
                        linked['evidence_sha256'] != evidence_sha256 or
                        model['quote_fingerprint'] != quote['snapshot_fingerprint'] or
                        model['warehouse_id'] != warehouse):
                    raise StateConflict('Model plan is not valid for this selected task')
            con.execute('''INSERT INTO agent_tasks
                (task_id,incident_id,order_id,warehouse_id,snapshot_fingerprint,
                 status,proposal_json,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?)''',
                (task_id, incident_id, order_id, warehouse,
                 quote['snapshot_fingerprint'], status,
                 json.dumps(proposal, ensure_ascii=False, sort_keys=True), now, now))
            if model_plan_id is not None:
                con.execute('UPDATE agent_model_plans SET adopted_task_id=? WHERE plan_id=?',
                            (task_id, model_plan_id))
                self._event(con, task_id, 'model_plan_adopted', {
                    'plan_id': model_plan_id,
                    'model_text_independently_verified': False,
                    'operator_confirmation_required': True,
                })
            self._event(con, task_id, 'proposal_created', {
                'status': status, 'warehouse_id': warehouse,
                'decision_sha256': decision['decision_sha256'],
                'evidence_sha256': evidence_sha256,
                'selection_source': decision['selection_source'],
            })
        return self.get(task_id)

    @staticmethod
    def _decode(row) -> dict:
        data = dict(row)
        data['proposal'] = json.loads(data.pop('proposal_json'))
        data['result'] = json.loads(data.pop('result_json')) if data['result_json'] else None
        return data

    @staticmethod
    def _attach_fulfillment(con, data: dict) -> dict:
        row = con.execute('SELECT * FROM fulfillment_workflows WHERE task_id=?',
                          (data['task_id'],)).fetchone()
        data['fulfillment'] = dict(row) if row else None
        return data

    @staticmethod
    def _attach_events(con, data: dict) -> dict:
        data['events'] = [
            {**dict(e), 'detail': json.loads(e['detail_json'])}
            for e in con.execute('''SELECT event_id,kind,detail_json,created_at
                FROM agent_task_events WHERE task_id=? ORDER BY event_id''',
                (data['task_id'],))
        ]
        for entry in data['events']:
            entry.pop('detail_json', None)
        return data

    def get(self, task_id: str) -> dict:
        if not re.fullmatch(r'TASK-[0-9A-F]{20}', task_id):
            raise NotFound('Task not found')
        with self.transaction() as con:
            row = con.execute('SELECT * FROM agent_tasks WHERE task_id=?', (task_id,)).fetchone()
            if row is None:
                raise NotFound('Task not found')
            return self._attach_events(con, self._attach_fulfillment(con, self._decode(row)))

    def list(self, limit: int = 30) -> list[dict]:
        if not 1 <= limit <= 100:
            raise RuleViolation('Invalid task limit')
        with self.transaction() as con:
            return [self._attach_fulfillment(con, self._decode(r)) for r in con.execute(
                'SELECT * FROM agent_tasks ORDER BY created_at DESC,task_id DESC LIMIT ?', (limit,))]

    def review(self, task_id: str, approved: bool) -> dict:
        with self.transaction() as con:
            row = con.execute('SELECT status FROM agent_tasks WHERE task_id=?', (task_id,)).fetchone()
            if not row:
                raise NotFound('Task not found')
            desired = 'approved' if approved else 'rejected'
            if row['status'] == desired:
                return self.get_after_tx(con, task_id)  # safe replay; no duplicate journal event
            if row['status'] != 'pending_approval':
                raise StateConflict('Task is not awaiting approval')
            con.execute('UPDATE agent_tasks SET status=?,updated_at=? WHERE task_id=?',
                        (desired, datetime.now(timezone.utc).isoformat(), task_id))
            self._event(con, task_id, 'approved' if approved else 'rejected', {})
            return self.get_after_tx(con, task_id)

    def get_after_tx(self, con, task_id: str) -> dict:
        return self._attach_events(con, self._attach_fulfillment(con, self._decode(con.execute(
            'SELECT * FROM agent_tasks WHERE task_id=?', (task_id,)).fetchone())))

    def claim(self, task_id: str) -> dict:
        """An 'executing' task is not reclaimed automatically during a concurrent call.

        After a crash use recover=True, which can safely retry the stable command key.
        """
        with self.transaction() as con:
            row = con.execute('SELECT * FROM agent_tasks WHERE task_id=?', (task_id,)).fetchone()
            if not row:
                raise NotFound('Task not found')
            if row['status'] != 'approved':
                raise StateConflict('Task is not approved for execution')
            con.execute('UPDATE agent_tasks SET status=?,updated_at=? WHERE task_id=?',
                        ('executing', datetime.now(timezone.utc).isoformat(), task_id))
            self._event(con, task_id, 'execution_claimed', {})
            return self.get_after_tx(con, task_id)

    def reclaim(self, task_id: str) -> dict:
        with self.transaction() as con:
            row = con.execute('SELECT * FROM agent_tasks WHERE task_id=?', (task_id,)).fetchone()
            if not row:
                raise NotFound('Task not found')
            if row['status'] != 'executing':
                raise StateConflict('Only interrupted executing tasks may be recovered')
            self._event(con, task_id, 'execution_retried', {})
            return self.get_after_tx(con, task_id)

    def finish(self, task_id: str, status: str, result: dict | None = None,
               failure_code: str | None = None) -> dict:
        if status not in ('succeeded', 'stale', 'failed'):
            raise ValueError('Invalid task end state')
        with self.transaction() as con:
            old = con.execute('SELECT status FROM agent_tasks WHERE task_id=?', (task_id,)).fetchone()
            if not old or old['status'] != 'executing':
                raise StateConflict('Task is no longer executing')
            con.execute('''UPDATE agent_tasks SET status=?,result_json=?,failure_code=?,
                updated_at=? WHERE task_id=?''',
                (status, json.dumps(result, sort_keys=True) if result else None,
                 failure_code, datetime.now(timezone.utc).isoformat(), task_id))
            self._event(con, task_id, status, {'failure_code': failure_code,
                                               'verified': bool(result and result.get('verified'))})
            return self.get_after_tx(con, task_id)


class ActionCoordinator:
    def __init__(self, ops: OpsStore, tasks: TaskStore):
        self.ops, self.tasks = ops, tasks

    def propose(self, incident_id: str, *, investigation_id: str | None = None,
                warehouse_id: str | None = None, selection_reason: str | None = None) -> dict:
        incident = self.ops.get_incident(incident_id)
        if incident is None:
            raise NotFound('Incident not found')
        quote = self.ops.fulfillment_quote(incident['order_id'])
        captured = capture_evidence(self.ops, incident_id, incident=incident, quote=quote)
        decision = decision_packet(quote, captured['sha256'],
                                   warehouse_id=warehouse_id, selection_reason=selection_reason)
        if investigation_id is None:
            investigation_id = self.tasks.create_investigation(incident_id, captured)['investigation_id']
        return self.tasks.create(incident_id, incident['order_id'], quote, incident,
                                 investigation_id=investigation_id,
                                 evidence_sha256=captured['sha256'], decision=decision)

    def refresh_model_plan(self, plan_id: str) -> dict:
        # Explicit, no-model-cost operation: refuse any actual business change.
        prior = self.tasks.get_model_plan(plan_id)
        if prior['adopted_task_id'] is not None:
            raise StateConflict('Model plan already adopted')
        if prior['plan']['next_action'] != 'reserve_inventory':
            raise RuleViolation('Escalation plans require manual handling')
        incident = self.ops.get_incident(prior['incident_id'])
        if incident is None:
            raise NotFound('Incident not found')
        quote = self.ops.fulfillment_quote(incident['order_id'])
        captured = capture_evidence(self.ops, prior['incident_id'], incident=incident, quote=quote)
        if (captured['sha256'] == prior['evidence_sha256'] and
                quote['snapshot_fingerprint'] == prior['plan']['quote_fingerprint']):
            return prior
        source = self.tasks.get_investigation(prior['investigation_id'])
        if not only_quote_window_changed(source['evidence'], captured['facts']):
            raise StateConflict('Business facts changed; run a new investigation')
        refreshed = revalidate_prior_suggestion(prior, quote, captured['sha256'], plan_id)
        return self.tasks.create_revalidated_model_plan(plan_id, captured, refreshed)

    def propose_from_model_plan(self, plan_id: str, *,
                                confirmed_warehouse_id: str,
                                selection_reason: str | None = None) -> dict:
        """An explicit operator confirmation creates only a pending-approval task."""
        persisted = self.tasks.get_model_plan(plan_id)
        model = persisted['plan']
        if model['next_action'] != 'reserve_inventory' or model['warehouse_id'] is None:
            raise RuleViolation('This plan requires human escalation, not inventory reservation')
        if confirmed_warehouse_id != model['warehouse_id']:
            raise RuleViolation('Operator choice must match the reviewed model plan')
        incident_id = persisted['incident_id']
        incident = self.ops.get_incident(incident_id)
        if incident is None:
            raise NotFound('Incident not found')
        quote = self.ops.fulfillment_quote(incident['order_id'])
        captured = capture_evidence(self.ops, incident_id, incident=incident, quote=quote)
        if (captured['sha256'] != persisted['evidence_sha256'] or
                quote['snapshot_fingerprint'] != model['quote_fingerprint']):
            raise StateConflict('Model plan is stale; investigate the incident again')
        decision = decision_packet(quote, captured['sha256'],
                                   warehouse_id=confirmed_warehouse_id,
                                   selection_reason=selection_reason)
        return self.tasks.create(incident_id, incident['order_id'], quote, incident,
                                 investigation_id=persisted['investigation_id'],
                                 evidence_sha256=captured['sha256'], decision=decision,
                                 model_plan_id=plan_id)

    def run(self, task_id: str, *, recover: bool = False) -> dict:
        # Verify the persisted decision before ANY claim or business mutation.
        task_before = self.tasks.get(task_id)
        validate_stored_decision(task_before)
        plan_id = task_before['proposal'].get('model_plan_id')
        if plan_id:
            record = self.tasks.get_model_plan(plan_id)
            if (record['adopted_task_id'] != task_id or
                    record['plan']['warehouse_id'] != task_before['warehouse_id'] or
                    record['plan']['evidence_sha256'] != task_before['proposal']['evidence_sha256']):
                raise StateConflict('Linked model plan does not match the task')
        task = self.tasks.reclaim(task_id) if recover else self.tasks.claim(task_id)
        # The task ID never changes. Thus a crashed write can only replay the same
        # preconditioned command; no new order, warehouse or proposal is substituted.
        key = 'agent-task:' + task_id
        try:
            outcome = OpsService(self.ops).reserve(
                task['order_id'], task['warehouse_id'], key,
                expected_snapshot=task['snapshot_fingerprint'])
            current = self.ops.get_incident(task['incident_id'])
            if not current or current['order_status'] != 'reserved' or \
                    current['warehouse_id'] != task['warehouse_id']:
                raise RuntimeError('Independent business read-back did not match executed reservation')
            with self.ops._connection() as con:
                receipt = con.execute(
                    'SELECT operation FROM command_receipts WHERE idempotency_key=?',
                    (key,)).fetchone()
            if receipt is None or receipt['operation'] != 'reserve':
                raise RuntimeError('Durable business command receipt missing')
            self.tasks.finish(task_id, 'succeeded', {
                'verified': True, 'actual_order_status': current['order_status'],
                'warehouse_id': current['warehouse_id'],
                'command_replayed': outcome['replayed'],
                # A4-A7 tasks created before the A8 ledger must remain executable.
                # Do not invent provenance for those historical tasks.
                **({'decision_sha256': task['proposal']['decision']['decision_sha256'],
                    'evidence_sha256': task['proposal'].get('evidence_sha256'),
                    'selection_source': task['proposal']['decision']['selection_source']}
                   if task['proposal'].get('decision') else {}),
                'scope': 'inventory reserved only; incident is NOT resolved',
            })
        except OpsError as exc:
            self.tasks.finish(task_id, 'stale' if isinstance(exc, StateConflict) else 'failed',
                              failure_code=type(exc).__name__)
        except Exception:
            # Fail closed: on uncertain failure, leave 'executing' and allow an
            # explicit retry with the same idempotency key; never claim failure
            # or success until a durable business read-back establishes it.
            raise
        return self.tasks.get(task_id)
