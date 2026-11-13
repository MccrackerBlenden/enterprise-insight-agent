"""A6: bounded, deterministic continuation of the existing synthetic Agent case.

This is NOT an autonomous model with write access. It bridges existing durable
A4/A5 coordinators and stops at every human authorization boundary. Each side
 effect is already guarded by its own claim, snapshot check and stable Ops
 idempotency key. A crash must be recovered explicitly with the existing API.
"""
from __future__ import annotations

from .action_tasks import ActionCoordinator, TaskStore
from .fulfillment import FulfillmentCoordinator
from .service import RuleViolation
from .store import OpsStore


class OperationsPlaybook:
    MAX_ACTIONS = 4

    def __init__(self, ops: OpsStore, tasks: TaskStore):
        self.ops, self.tasks = ops, tasks

    def preview(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        workflow = task['fulfillment']
        current = self.ops.get_incident(task['incident_id'])
        if current is None:
            return self._decision(task, 'manual_handoff', None,
                                  'Original incident is no longer available', current)
        if task['status'] == 'pending_approval':
            return self._decision(task, 'reservation_approval', None,
                                  'An operator must approve this exact inventory reservation', current)
        if task['status'] == 'approved':
            return self._decision(task, 'ready', 'reserve_inventory',
                                  'Inventory reservation was approved; fresh preconditions are checked on execution', current)
        if task['status'] == 'executing':
            return self._decision(task, 'explicit_recovery', None,
                                  'Reservation has an unknown outcome: use the existing recovery endpoint', current)
        if task['status'] != 'succeeded':
            return self._decision(task, 'manual_handoff', None,
                                  'Reservation did not finish successfully', current)
        if not task['result'] or not task['result'].get('verified'):
            return self._decision(task, 'manual_handoff', None,
                                  'Reservation has no independent success evidence', current)
        if workflow is None:
            if (current['order_status'] != 'reserved' or
                    current['warehouse_id'] != task['warehouse_id'] or
                    current['state'] != 'investigating'):
                return self._decision(task, 'manual_handoff', None,
                                      'The approved reservation is no longer the active business state', current)
            return self._decision(task, 'ready', 'prepare_fulfillment',
                                  'Prepare a second, separately approved fulfillment proposal', current)
        status = workflow['status']
        if status == 'pending_approval':
            return self._decision(task, 'fulfillment_approval', None,
                                  'Dispatch and simulated logistics require separate human approval', current)
        if status == 'executing':
            return self._decision(task, 'explicit_recovery', None,
                                  'A step has an uncertain outcome: use explicit recovery with its original key', current)
        if status == 'awaiting_delivery_confirmation':
            return self._decision(task, 'delivery_attestation', None,
                                  'The operator must explicitly attest a synthetic delivery event', current)
        if status == 'completed':
            return self._decision(task, 'completed', None,
                                  'Business order, simulated shipment and incident were verified', current)
        if status in ('blocked', 'rejected', 'handoff'):
            return self._decision(task, 'manual_handoff', None,
                                  'The fulfillment workflow cannot be advanced automatically', current)
        if status == 'approved':
            step = workflow['next_step']
            actions = {0: 'dispatch', 1: 'in_transit', 3: 'verify_incident'}
            action = actions.get(step)
            if action is not None:
                return self._decision(task, 'ready', action,
                                      'Run the approved synthetic step and verify the Ops receipt', current)
        return self._decision(task, 'manual_handoff', None,
                              'Unrecognized checkpoint; no write was attempted', current)

    @staticmethod
    def _decision(task: dict, gate: str, action: str | None, explanation: str,
                  incident: dict | None) -> dict:
        return {
            'synthetic': True,
            'task_id': task['task_id'],
            'incident_id': task['incident_id'],
            'gate': gate,
            'can_continue': gate == 'ready',
            'next_action': action,
            'reason': explanation,
            'reservation_status': task['status'],
            'fulfillment_status': task['fulfillment']['status'] if task['fulfillment'] else None,
            'order_status': incident['order_status'] if incident else None,
            'incident_state': incident['state'] if incident else None,
            'requires_live_model': False,
        }

    def advance_until_gate(self, task_id: str, *, max_actions: int = MAX_ACTIONS) -> dict:
        if not isinstance(max_actions, int) or isinstance(max_actions, bool) or not 1 <= max_actions <= self.MAX_ACTIONS:
            raise RuleViolation('Invalid playbook step budget')
        executed: list[str] = []
        for _ in range(max_actions):
            decision = self.preview(task_id)
            if not decision['can_continue']:
                break
            action = decision['next_action']
            if action == 'reserve_inventory':
                result = ActionCoordinator(self.ops, self.tasks).run(task_id)
            elif action == 'prepare_fulfillment':
                result = FulfillmentCoordinator(self.ops, self.tasks).prepare(task_id)
            elif action in ('dispatch', 'in_transit', 'verify_incident'):
                result = FulfillmentCoordinator(self.ops, self.tasks).advance(task_id)
            else:
                raise RuntimeError('Unexpected playbook action')
            executed.append(action)
            # Never auto-retry a blocked/ambiguous step. The next preview will
            # force human review, recovery, delivery attestation or completion.
            assert result['task_id'] == task_id
        result = self.tasks.get(task_id)
        result['playbook'] = {
            'synthetic': True,
            'steps_executed': executed,
            'next': self.preview(task_id),
            'bounded_at': max_actions,
        }
        return result
