"""A5: explicit, durable synthetic fulfillment after A4 inventory reservation.

The first A4 approval grants *only* inventory reservation. A separate approval
and an operator's explicit synthetic delivery attestation gate fulfillment.
Every side-effect uses a stable, per-step Ops command key, so a crash after the
Ops commit can be reconciled without repeating the physical inventory write.
No real customer/carrier integration is implied by this sandbox workflow.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .action_tasks import TaskStore
from .service import NotFound, OpsError, OpsService, RuleViolation, StateConflict
from .store import OpsStore

STEPS = ('dispatch', 'in_transit', 'delivered', 'verify_incident')
CARRIER = 'DEMO_CARRIER'


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FulfillmentCoordinator:
    def __init__(self, ops: OpsStore, tasks: TaskStore):
        self.ops, self.tasks = ops, tasks

    def _workflow(self, con, task_id: str):
        row = con.execute('SELECT * FROM fulfillment_workflows WHERE task_id=?',
                          (task_id,)).fetchone()
        if row is None:
            raise NotFound('Fulfillment workflow not found; prepare it after reservation')
        return dict(row)

    def _task(self, con, task_id: str):
        row = con.execute('SELECT * FROM agent_tasks WHERE task_id=?',
                          (task_id,)).fetchone()
        if row is None:
            raise NotFound('Task not found')
        return self.tasks._decode(row)

    def prepare(self, task_id: str) -> dict:
        # Safe replay: preparing never mutates the Ops business database.
        with self.tasks.transaction() as con:
            task = self._task(con, task_id)
            previous = con.execute('SELECT 1 FROM fulfillment_workflows WHERE task_id=?',
                                   (task_id,)).fetchone()
            if previous:
                return self.tasks.get_after_tx(con, task_id)
            if (task['status'] != 'succeeded' or not task['result']
                    or not task['result'].get('verified')):
                raise StateConflict('Verified A4 inventory reservation is required')
            current = self.ops.get_incident(task['incident_id'])
            if not current or (current['order_status'] != 'reserved'
                               or current['warehouse_id'] != task['warehouse_id']
                               or current['state'] != 'investigating'):
                raise StateConflict('Inventory reservation no longer matches approved task')
            con.execute('''INSERT INTO fulfillment_workflows
                (task_id,carrier,status,next_step,created_at,updated_at)
                VALUES (?,?,?,0,?,?)''',
                (task_id, CARRIER, 'pending_approval', now(), now()))
            self.tasks._event(con, task_id, 'fulfillment_proposed', {
                'carrier': CARRIER, 'scope': 'synthetic dispatch/transport; delivery requires explicit operator attestation',
            })
            return self.tasks.get_after_tx(con, task_id)

    def review(self, task_id: str, approved: bool) -> dict:
        with self.tasks.transaction() as con:
            wf = self._workflow(con, task_id)
            desired = 'approved' if approved else 'rejected'
            if wf['status'] == desired and wf['next_step'] == 0:
                return self.tasks.get_after_tx(con, task_id)
            if wf['status'] != 'pending_approval':
                raise StateConflict('Fulfillment is no longer awaiting approval')
            con.execute('UPDATE fulfillment_workflows SET status=?,updated_at=? WHERE task_id=?',
                        (desired, now(), task_id))
            self.tasks._event(con, task_id,
                              'fulfillment_approved' if approved else 'fulfillment_rejected', {})
            return self.tasks.get_after_tx(con, task_id)

    def _claim(self, task_id: str, *, recover: bool, confirm_delivery: bool) -> tuple[dict, dict, str]:
        with self.tasks.transaction() as con:
            task = self._task(con, task_id)
            wf = self._workflow(con, task_id)
            if recover:
                if wf['status'] != 'executing' or not wf['inflight_step']:
                    raise StateConflict('No interrupted fulfillment step to recover')
                step = wf['inflight_step']
                self.tasks._event(con, task_id, 'fulfillment_recovery_requested', {'step': step})
            else:
                if wf['status'] not in ('approved', 'awaiting_delivery_confirmation'):
                    raise StateConflict('Fulfillment step is not approved or awaiting confirmation')
                if wf['next_step'] >= len(STEPS):
                    raise StateConflict('Fulfillment is already complete')
                step = STEPS[wf['next_step']]
                if step == 'delivered':
                    if wf['status'] != 'awaiting_delivery_confirmation' or not confirm_delivery:
                        raise RuleViolation('Explicit synthetic delivery confirmation is required')
                    self.tasks._event(con, task_id, 'synthetic_delivery_attested', {
                        'source': 'local_demo_operator', 'not_real_carrier_proof': True,
                    })
                elif confirm_delivery:
                    raise RuleViolation('Delivery confirmation is only valid at the delivery step')
                con.execute('''UPDATE fulfillment_workflows
                    SET status='executing',inflight_step=?,failure_code=NULL,updated_at=?
                    WHERE task_id=?''', (step, now(), task_id))
                self.tasks._event(con, task_id, 'fulfillment_step_claimed', {'step': step})
            return task, wf, step

    def _finish(self, task_id: str, step: str, *, verified: bool, failure_code: str | None = None,
                evidence: dict | None = None) -> dict:
        with self.tasks.transaction() as con:
            wf = self._workflow(con, task_id)
            if wf['status'] != 'executing' or wf['inflight_step'] != step:
                raise StateConflict('Fulfillment step was claimed elsewhere')
            if failure_code:
                status, index = 'blocked', wf['next_step']
            else:
                if not verified or step != STEPS[wf['next_step']]:
                    raise StateConflict('Verified step does not match durable checkpoint')
                index = wf['next_step'] + 1
                status = ('completed' if index == len(STEPS)
                          else 'awaiting_delivery_confirmation' if index == 2
                          else 'approved')
            con.execute('''UPDATE fulfillment_workflows
                SET status=?,next_step=?,inflight_step=NULL,failure_code=?,updated_at=?
                WHERE task_id=?''', (status, index, failure_code, now(), task_id))
            self.tasks._event(con, task_id, 'fulfillment_step_blocked' if failure_code
                              else 'fulfillment_step_verified', {
                                  'step': step, 'failure_code': failure_code,
                                  'verified': verified, 'evidence': evidence or {},
                              })
            return self.tasks.get_after_tx(con, task_id)

    def _business_step(self, task: dict, wf: dict, step: str) -> dict:
        order_id = task['order_id']
        incident_id = task['incident_id']
        key = f'agent-fulfill:{task["task_id"]}:{step}'
        ops = OpsService(self.ops)
        if step == 'dispatch':
            outcome = ops.dispatch(order_id, wf['carrier'], key)
            expected_operation = 'dispatch'
        elif step in ('in_transit', 'delivered'):
            outcome = ops.advance_shipment(order_id, step, key)
            expected_operation = 'advance_shipment'
        elif step == 'verify_incident':
            outcome = ops.verify_incident(incident_id, key)
            expected_operation = 'verify_incident'
        elif step == 'release':
            outcome = ops.release(order_id, key)
            expected_operation = 'release'
        else:
            raise RuntimeError('Unsupported durable step')
        # Read-back is independent of the model and the command return text.
        current = self.ops.get_incident(incident_id)
        if not current:
            raise RuntimeError('Business incident disappeared during verification')
        expected = {
            'dispatch': ('shipped', 'created'),
            'in_transit': ('shipped', 'in_transit'),
            'delivered': ('delivered', 'delivered'),
            'verify_incident': ('delivered', 'delivered'),
            'release': ('pending', None),
        }[step]
        if (current['order_status'], current['shipment_status']) != expected:
            raise RuntimeError('Business read-back did not match step expectations')
        if step == 'verify_incident' and current['state'] != 'resolved':
            raise RuntimeError('Incident was not independently resolved')
        if step == 'release' and current['state'] != 'open':
            raise RuntimeError('Released reservation did not reopen incident')
        with self.ops._connection() as con:
            receipt = con.execute('''SELECT operation FROM command_receipts
                                     WHERE idempotency_key=?''', (key,)).fetchone()
            audit = con.execute('''SELECT audit_id FROM audit_log
                                  WHERE idempotency_key=?''', (key,)).fetchone()
        if receipt is None or receipt['operation'] != expected_operation or audit is None:
            raise RuntimeError('Durable business receipt/audit missing')
        return {'order_status': current['order_status'],
                'shipment_status': current['shipment_status'],
                'incident_state': current['state'],
                'command_replayed': outcome['replayed'],
                'business_audit_id': audit['audit_id']}

    def advance(self, task_id: str, *, recover: bool = False,
                confirm_delivery: bool = False) -> dict:
        task, wf, step = self._claim(task_id, recover=recover,
                                     confirm_delivery=confirm_delivery)
        if step == 'release':
            return self._release_claimed(task_id, task, wf)
        try:
            evidence = self._business_step(task, wf, step)
            return self._finish(task_id, step, verified=True, evidence=evidence)
        except OpsError as exc:
            return self._finish(task_id, step, verified=False,
                                failure_code=type(exc).__name__)
        except Exception:
            # Unknown outcome: stay executing. Manual recovery safely replays
            # the same idempotency key, even if the Ops commit already occurred.
            raise

    def handoff(self, task_id: str, *, reason: str, release_reservation: bool = False) -> dict:
        reason = reason.strip()
        if not 5 <= len(reason) <= 300:
            raise RuleViolation('Handoff reason must be between 5 and 300 characters')
        with self.tasks.transaction() as con:
            task = self._task(con, task_id)
            wf = self._workflow(con, task_id)
            if wf['status'] in ('completed', 'rejected', 'handoff', 'executing'):
                raise StateConflict('Workflow cannot be handed off in its current state')
            if release_reservation:
                if wf['next_step'] != 0:
                    raise StateConflict('Shipped orders cannot be compensated by releasing inventory')
                current = self.ops.get_incident(task['incident_id'])
                if not current or current['order_status'] != 'reserved':
                    raise StateConflict('Only a live unshipped reservation can be released')
                con.execute('''UPDATE fulfillment_workflows
                    SET status='executing',inflight_step='release',handoff_reason=?,updated_at=?
                    WHERE task_id=?''', (reason, now(), task_id))
                self.tasks._event(con, task_id, 'handoff_release_requested', {
                    'reason': reason, 'requires_undo': True})
            else:
                con.execute('''UPDATE fulfillment_workflows
                    SET status='handoff',handoff_reason=?,updated_at=? WHERE task_id=?''',
                    (reason, now(), task_id))
                self.tasks._event(con, task_id, 'manual_handoff', {
                    'reason': reason, 'reservation_released': False})
                return self.tasks.get_after_tx(con, task_id)
        # Release occurs outside task DB transaction; crash recovery uses stable key.
        return self.advance(task_id, recover=True)

    def _release_claimed(self, task_id: str, task: dict, wf: dict) -> dict:
        try:
            evidence = self._business_step(task, wf, 'release')
        except OpsError as exc:
            return self._finish(task_id, 'release', verified=False,
                                failure_code=type(exc).__name__)
        # Unlike normal stages, release is a compensation/handoff terminal path.
        with self.tasks.transaction() as con:
            active = self._workflow(con, task_id)
            if active['status'] != 'executing' or active['inflight_step'] != 'release':
                raise StateConflict('Release checkpoint changed concurrently')
            con.execute('''UPDATE fulfillment_workflows
                SET status='handoff',inflight_step=NULL,failure_code=NULL,updated_at=?
                WHERE task_id=?''', (now(), task_id))
            self.tasks._event(con, task_id, 'reservation_released_for_handoff', {
                'verified': True, 'evidence': evidence,
                'reason': active['handoff_reason']})
            return self.tasks.get_after_tx(con, task_id)
