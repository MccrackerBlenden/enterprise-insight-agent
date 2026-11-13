"""A8: reviewable, evidence-bound choices for a synthetic fulfillment plan.

The policy ranks *fictional* hours and cost points. It cannot establish a
real-world delivery promise. An LLM narrative is never treated as an approval.
"""
from __future__ import annotations

import hashlib
import json

from .service import RuleViolation


def decision_packet(quote: dict, evidence_sha256: str, *,
                    warehouse_id: str | None = None,
                    selection_reason: str | None = None) -> dict:
    """Produce a stable decision ledger from an already-verified business quote.

    An alternative requires an explicit operator reason; no hallucinated
    warehouse, unavailable stock or implicit model suggestion can be selected.
    Execution later re-checks the *original* quote fingerprint under a write
    transaction, including stock, order and incident constraints.
    """
    options = quote['options']
    recommended = quote['recommended_warehouse_id']
    alternative = warehouse_id is not None and warehouse_id != recommended
    reason = (selection_reason or '').strip()
    if alternative and not 10 <= len(reason) <= 300:
        raise RuleViolation('Alternative warehouse requires an operator reason (10-300 characters)')
    if not alternative and reason:
        raise RuleViolation('Operator reason is only allowed for an alternative warehouse')

    selected = warehouse_id if warehouse_id is not None else recommended
    choice = next((o for o in options if o['warehouse_id'] == selected), None)
    if selected is not None and (choice is None or not choice['eligible']):
        raise RuleViolation('Selected warehouse is not eligible under the current quote')
    # No override can turn a blocked quote into executable authorization.
    if recommended is None and selected is not None:
        raise RuleViolation('No eligible warehouse exists')

    candidates = [
        {
            'warehouse_id': option['warehouse_id'],
            'eligible': bool(option['eligible']),
            'blockers': list(option['blockers']),
            'available': option['available'],
            'required': option['required'],
            'simulated_late_minutes': option['simulated_late_minutes'],
            'simulated_cost_points': option['simulated_cost_points'],
            'simulated_handling_hours': option['simulated_handling_hours'],
        }
        for option in options
    ]
    source = 'operator_override' if alternative else 'deterministic_policy'
    packet = {
        'synthetic': True,
        'order_id': quote['order_id'],
        'evidence_sha256': evidence_sha256,
        'quote_fingerprint': quote['snapshot_fingerprint'],
        'policy_version': quote['policy_version'],
        'ranking': list(quote['ranking']),
        'candidates': candidates,
        'recommended_warehouse_id': recommended,
        'selected_warehouse_id': selected,
        'selection_source': source,
        'operator_reason': reason if alternative else None,
        'rationale': (
            'No feasible warehouse: requires human resolution'
            if selected is None else
            'Operator chose an eligible alternative; deterministic business constraints still apply'
            if alternative else
            'Highest-ranked eligible option by simulated lateness, cost points and warehouse ID'
        ),
        'limitations': 'Synthetic costs and handling times, not live logistics quotes or SLA predictions',
        'requires_approval': selected is not None,
    }
    serialized = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    packet['decision_sha256'] = hashlib.sha256(serialized.encode('utf-8')).hexdigest()
    return packet


def validate_stored_decision(task: dict) -> None:
    """Fail closed on a modified A8 ledger; tolerate genuine pre-A8 tasks.

    This only detects internal record drift/corruption. A SHA-256 stored beside
    its payload is NOT a cryptographic signature or user authentication.
    """
    from .service import StateConflict

    proposal = task['proposal']
    stored = proposal.get('decision')
    if stored is None:  # Existing A4-A7 tasks have no decision ledger.
        return
    try:
        selected = stored['selected_warehouse_id']
        recommended = stored['recommended_warehouse_id']
        expected = decision_packet(
            proposal['quote'], proposal['evidence_sha256'],
            warehouse_id=selected if selected != recommended else None,
            selection_reason=stored['operator_reason'],
        )
        if (stored != expected or selected != task['warehouse_id']
                or stored['quote_fingerprint'] != task['snapshot_fingerprint']):
            raise StateConflict('Decision ledger does not match the approved task')
    except (KeyError, TypeError, ValueError, RuleViolation) as exc:
        raise StateConflict('Decision ledger is invalid') from exc
