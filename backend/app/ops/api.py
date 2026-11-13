"""Narrow, local-demo operations API. Mutations are disabled without an explicit token."""
from __future__ import annotations

import hmac
import os
import json

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .service import OpsError, OpsService
from .detection import DetectionEngine, DetectionError
from .store import OpsStore, DEFAULT_PATH
from .action_tasks import TaskStore, ActionCoordinator
from .fulfillment import FulfillmentCoordinator
from .playbook import OperationsPlaybook
from .evidence import capture_evidence
from .decision import decision_packet
from .model_plan import generate_model_plan
from .triage import build_triage
from .case_history import build_case_history, CaseNotFound

router = APIRouter(prefix='/api/ops', tags=['synthetic-operations'])
store = OpsStore(DEFAULT_PATH)
task_store = TaskStore(store.path.with_name("action_tasks.sqlite"))


def get_store() -> OpsStore:
    return store


def get_task_store() -> TaskStore:
    return task_store


def require_write_token(x_ops_demo_token: str | None = Header(default=None)) -> None:
    configured = os.environ.get('EIA_OPS_WRITE_TOKEN', '')
    # The current Compose binds only loopback. The token is still mandatory:
    # local websites must not be able to mutate operations via a blind request.
    if (len(configured) < 24 or not configured.isascii() or not x_ops_demo_token
            or not x_ops_demo_token.isascii()
            or not hmac.compare_digest(configured, x_ops_demo_token)):
        raise HTTPException(status_code=403, detail='Demo operations writes are disabled or unauthorized')


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ReserveBody(StrictModel):
    order_id: str = Field(pattern=r'^[A-Za-z0-9-]{3,40}$')
    warehouse_id: str = Field(pattern=r'^[A-Za-z0-9-]{3,40}$')


class PlanReserveBody(ReserveBody):
    snapshot_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')


class OrderBody(StrictModel):
    order_id: str = Field(pattern=r'^[A-Za-z0-9-]{3,40}$')


class DispatchBody(OrderBody):
    carrier: str = Field(pattern=r'^[A-Za-z0-9_-]{2,40}$')


class AdvanceBody(OrderBody):
    to_status: str = Field(pattern=r'^(in_transit|delivered)$')


class IncidentBody(StrictModel):
    incident_id: str = Field(pattern=r'^[A-Za-z0-9-]{3,40}$')


class EscalateBody(IncidentBody):
    reason: str = Field(min_length=5, max_length=300)


def execute(fn):
    try:
        return fn()
    except OpsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get('/demo/scenarios')
def demo_scenarios(db: OpsStore = Depends(get_store),
                   tasks: TaskStore = Depends(get_task_store)):
    """Read-only tour metadata available only in the separately launched demo app.

    Business states always come from the current DB; the manifest only supplies
    which generated entities belong to the three reproducible scenarios.
    """
    if os.environ.get('EIA_P2_DEMO_MODE') != '1':
        raise HTTPException(status_code=404, detail='Demo scenarios are not enabled')
    manifest_file = db.path.parent / 'scenario_manifest.json'
    try:
        manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
        if manifest.get('version') != 'P2-C.2' or not manifest.get('synthetic'):
            raise ValueError('Invalid demo scenario manifest')
        scenarios = []
        labels = {'normal': '正常解决', 'rejected': '审批拒绝', 'recovery': '中断恢复'}
        for kind, title in labels.items():
            data = manifest['scenarios'][kind]
            incident = db.get_incident(data['incident_id'])
            task = tasks.get(data['task_id'])
            if not incident or task['incident_id'] != data['incident_id']:
                raise ValueError('Scenario source no longer matches the active business records')
            scenarios.append({
                'kind': kind, 'title': title, 'incident_id': incident['incident_id'],
                'order_id': incident['order_id'], 'task_id': task['task_id'],
                'incident_state': incident['state'],
                'order_status': incident['order_status'],
                'task_status': task['status'],
                'fulfillment_status': (task.get('fulfillment') or {}).get('status'),
            })
    except (OSError, KeyError, TypeError, ValueError, OpsError) as exc:
        raise HTTPException(status_code=503, detail='Demo scenario data is unavailable or inconsistent') from exc
    return {'synthetic': True, 'version': 'P2-C.2', 'scenarios': scenarios}


@router.get('/summary')
def summary(db: OpsStore = Depends(get_store)):
    return db.summary()


@router.get('/incidents')
def incidents(db: OpsStore = Depends(get_store)):
    return {'synthetic': True, 'items': db.list_incidents()}


@router.get('/incidents/{incident_id}/case-history')
def case_history(incident_id: str, limit: int = 100,
                 db: OpsStore = Depends(get_store),
                 tasks: TaskStore = Depends(get_task_store)):
    try:
        return build_case_history(db, tasks, incident_id, limit)
    except CaseNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get('/incidents/{incident_id}')
def incident_detail(incident_id: str, db: OpsStore = Depends(get_store)):
    result = db.get_incident(incident_id)
    if result is None:
        raise HTTPException(status_code=404, detail='Incident not found')
    return {'synthetic': True, **result}


@router.post('/commands/reserve', dependencies=[Depends(require_write_token)])
def reserve(body: ReserveBody, x_idempotency_key: str = Header(),
            db: OpsStore = Depends(get_store)):
    return execute(lambda: OpsService(db).reserve(body.order_id, body.warehouse_id, x_idempotency_key))


@router.get('/orders/{order_id}/fulfillment-options')
def fulfillment_options(order_id: str, db: OpsStore = Depends(get_store)):
    return execute(lambda: db.fulfillment_quote(order_id))


@router.post('/commands/reserve-from-plan', dependencies=[Depends(require_write_token)])
def reserve_from_plan(body: PlanReserveBody, x_idempotency_key: str = Header(),
                      db: OpsStore = Depends(get_store)):
    return execute(lambda: OpsService(db).reserve(
        body.order_id, body.warehouse_id, x_idempotency_key,
        expected_snapshot=body.snapshot_fingerprint))


@router.post('/commands/release', dependencies=[Depends(require_write_token)])
def release(body: OrderBody, x_idempotency_key: str = Header(),
            db: OpsStore = Depends(get_store)):
    return execute(lambda: OpsService(db).release(body.order_id, x_idempotency_key))


@router.post('/commands/dispatch', dependencies=[Depends(require_write_token)])
def dispatch(body: DispatchBody, x_idempotency_key: str = Header(),
             db: OpsStore = Depends(get_store)):
    return execute(lambda: OpsService(db).dispatch(body.order_id, body.carrier, x_idempotency_key))


@router.post('/commands/advance-shipment', dependencies=[Depends(require_write_token)])
def advance(body: AdvanceBody, x_idempotency_key: str = Header(),
            db: OpsStore = Depends(get_store)):
    return execute(lambda: OpsService(db).advance_shipment(body.order_id, body.to_status,
                                                          x_idempotency_key))


@router.post('/commands/verify-incident', dependencies=[Depends(require_write_token)])
def verify(body: IncidentBody, x_idempotency_key: str = Header(),
           db: OpsStore = Depends(get_store)):
    return execute(lambda: OpsService(db).verify_incident(body.incident_id, x_idempotency_key))


@router.post('/commands/escalate', dependencies=[Depends(require_write_token)])
def escalate(body: EscalateBody, x_idempotency_key: str = Header(),
             db: OpsStore = Depends(get_store)):
    return execute(lambda: OpsService(db).escalate(body.incident_id, body.reason,
                                                  x_idempotency_key))


@router.get('/detector/summary')
def detection_summary(db: OpsStore = Depends(get_store)):
    return db.detector_summary()


@router.get('/signals')
def signals(status: str | None = None, limit: int = 100,
            db: OpsStore = Depends(get_store)):
    try:
        return {'synthetic': True, 'items': db.list_signals(status, limit)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get('/signals/events')
def signal_events(after_id: int = 0, limit: int = 100,
                  db: OpsStore = Depends(get_store)):
    try:
        return {'synthetic': True, 'items': db.list_signal_events(after_id, limit)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get('/signals/{signal_id}')
def signal_detail(signal_id: str, db: OpsStore = Depends(get_store)):
    signal = db.get_signal(signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail='Signal not found')
    return {'synthetic': True, **signal}


@router.post('/detector/scan', dependencies=[Depends(require_write_token)])
def scan(db: OpsStore = Depends(get_store)):
    # Deliberately no user-supplied clock or rule code. Local fake-clock tests
    # use DetectionEngine directly with temporary databases.
    try:
        return DetectionEngine(db).scan()
    except DetectionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


# V0.6-A4: optional read-only model investigation and durable human-gated actions.
class AgentInvestigationBody(StrictModel):
    incident_id: str = Field(pattern=r'^[A-Za-z0-9-]{3,40}$')


class TaskProposalBody(AgentInvestigationBody):
    investigation_id: str | None = Field(default=None, pattern=r'^INV-[0-9A-F]{20}$')
    warehouse_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9-]{3,40}$')
    selection_reason: str | None = Field(default=None, max_length=300)


class TaskReviewBody(StrictModel):
    approved: bool


@router.post('/agent/investigate/stream', dependencies=[Depends(require_write_token)])
def investigate_agent(body: AgentInvestigationBody, db: OpsStore = Depends(get_store),
                      task_store: TaskStore = Depends(get_task_store)):
    # A model call may incur costs, hence the same explicit token gate.
    from fastapi.responses import StreamingResponse
    import json
    def events():
        from .investigation_agent import investigate_stream
        report = capture_evidence(db, body.incident_id)
        entry = task_store.create_investigation(body.incident_id, report, model_status='running')
        investigation_id = entry['investigation_id']
        yield f"event: evidence\ndata: {json.dumps({'investigation_id': investigation_id, 'evidence_sha256': report['sha256']})}\n\n"
        finished = False
        try:
            for kind, data in investigate_stream(db, body.incident_id):
                if kind == 'done':
                    task_store.finish_investigation(investigation_id, model_status='completed',
                        summary=str(data.get('answer', '')), tool_calls=int(data.get('tool_calls', 0)))
                    finished = True
                elif kind == 'error':
                    task_store.finish_investigation(investigation_id, model_status='failed')
                    finished = True
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        finally:
            if not finished:
                try:
                    task_store.finish_investigation(investigation_id, model_status='failed')
                except (OpsError, ValueError):
                    pass
    return StreamingResponse(events(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@router.get('/agent/investigations/{investigation_id}')
def investigation_detail(investigation_id: str, tasks: TaskStore = Depends(get_task_store)):
    return execute(lambda: tasks.get_investigation(investigation_id))


@router.get('/agent/incidents/{incident_id}/decision-preview')
def decision_preview(incident_id: str, db: OpsStore = Depends(get_store)):
    def generate():
        incident = db.get_incident(incident_id)
        if incident is None:
            from .service import NotFound
            raise NotFound('Incident not found')
        quote = db.fulfillment_quote(incident['order_id'])
        captured = capture_evidence(db, incident_id, incident=incident, quote=quote)
        return decision_packet(quote, captured['sha256'])
    return execute(generate)


# A9: model recommendations are untrusted suggestions, persisted for operator review.
# No model endpoint can approve or execute an Ops command.
class ConfirmModelPlanBody(StrictModel):
    confirmed_warehouse_id: str = Field(pattern=r'^[A-Za-z0-9-]{3,40}$')
    selection_reason: str | None = Field(default=None, max_length=300)


@router.post('/agent/investigations/{investigation_id}/model-plan',
             dependencies=[Depends(require_write_token)])
def create_model_plan(investigation_id: str, db: OpsStore = Depends(get_store),
                      tasks: TaskStore = Depends(get_task_store)):
    # One explicit user-triggered paid model call; no tools or business writes.
    return execute(lambda: generate_model_plan(db, tasks, investigation_id))


@router.get('/agent/model-plans/{plan_id}')
def model_plan_detail(plan_id: str, tasks: TaskStore = Depends(get_task_store)):
    return execute(lambda: tasks.get_model_plan(plan_id))


@router.get('/agent/incidents/{incident_id}/latest-model-plan')
def latest_model_plan(incident_id: str, tasks: TaskStore = Depends(get_task_store)):
    return {'item': execute(lambda: tasks.latest_unadopted_model_plan(incident_id))}


@router.post('/agent/model-plans/{plan_id}/refresh',
             dependencies=[Depends(require_write_token)])
def refresh_model_plan(plan_id: str, db: OpsStore = Depends(get_store),
                       tasks: TaskStore = Depends(get_task_store)):
    # No model API call, no Ops business mutation; only refreshed evidence ledger.
    return execute(lambda: ActionCoordinator(db, tasks).refresh_model_plan(plan_id))


@router.post('/agent/model-plans/{plan_id}/create-task',
             dependencies=[Depends(require_write_token)])
def confirm_model_plan(plan_id: str, body: ConfirmModelPlanBody,
                       db: OpsStore = Depends(get_store),
                       tasks: TaskStore = Depends(get_task_store)):
    return execute(lambda: ActionCoordinator(db, tasks).propose_from_model_plan(
        plan_id, confirmed_warehouse_id=body.confirmed_warehouse_id,
        selection_reason=body.selection_reason))


@router.post('/agent/tasks', dependencies=[Depends(require_write_token)])
def propose_action(body: TaskProposalBody, db: OpsStore = Depends(get_store),
                   tasks: TaskStore = Depends(get_task_store)):
    return execute(lambda: ActionCoordinator(db, tasks).propose(
        body.incident_id, investigation_id=body.investigation_id,
        warehouse_id=body.warehouse_id, selection_reason=body.selection_reason))


@router.get('/agent/tasks')
def list_action_tasks(limit: int = 30, tasks: TaskStore = Depends(get_task_store)):
    return execute(lambda: {'synthetic': True, 'items': tasks.list(limit)})


@router.get('/agent/triage')
def agent_triage(limit: int = 100, db: OpsStore = Depends(get_store),
                 tasks: TaskStore = Depends(get_task_store)):
    try:
        return build_triage(db, tasks, limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get('/agent/tasks/{task_id}')
def action_task_detail(task_id: str, tasks: TaskStore = Depends(get_task_store)):
    return execute(lambda: tasks.get(task_id))


@router.post('/agent/tasks/{task_id}/review', dependencies=[Depends(require_write_token)])
def review_action(task_id: str, body: TaskReviewBody,
                  tasks: TaskStore = Depends(get_task_store)):
    return execute(lambda: tasks.review(task_id, body.approved))


@router.post('/agent/tasks/{task_id}/execute', dependencies=[Depends(require_write_token)])
def execute_action(task_id: str, db: OpsStore = Depends(get_store),
                   tasks: TaskStore = Depends(get_task_store)):
    return execute(lambda: ActionCoordinator(db, tasks).run(task_id))


@router.post('/agent/tasks/{task_id}/recover', dependencies=[Depends(require_write_token)])
def recover_action(task_id: str, db: OpsStore = Depends(get_store),
                   tasks: TaskStore = Depends(get_task_store)):
    return execute(lambda: ActionCoordinator(db, tasks).run(task_id, recover=True))


# V0.6-A5: fulfillment approval is separate from the earlier reservation approval.
# All actions are confined to the synthetic local sandbox and require the demo token.
class FulfillmentReviewBody(StrictModel):
    approved: bool


class FulfillmentAdvanceBody(StrictModel):
    confirm_synthetic_delivery: bool = False


class FulfillmentHandoffBody(StrictModel):
    reason: str = Field(min_length=5, max_length=300)
    release_reservation: bool = False


@router.post('/agent/tasks/{task_id}/fulfillment/prepare', dependencies=[Depends(require_write_token)])
def prepare_fulfillment(task_id: str, db: OpsStore = Depends(get_store),
                        tasks: TaskStore = Depends(get_task_store)):
    return execute(lambda: FulfillmentCoordinator(db, tasks).prepare(task_id))


@router.post('/agent/tasks/{task_id}/fulfillment/review', dependencies=[Depends(require_write_token)])
def review_fulfillment(task_id: str, body: FulfillmentReviewBody,
                       db: OpsStore = Depends(get_store),
                       tasks: TaskStore = Depends(get_task_store)):
    return execute(lambda: FulfillmentCoordinator(db, tasks).review(task_id, body.approved))


@router.post('/agent/tasks/{task_id}/fulfillment/advance', dependencies=[Depends(require_write_token)])
def advance_fulfillment(task_id: str, body: FulfillmentAdvanceBody,
                        db: OpsStore = Depends(get_store),
                        tasks: TaskStore = Depends(get_task_store)):
    return execute(lambda: FulfillmentCoordinator(db, tasks).advance(
        task_id, confirm_delivery=body.confirm_synthetic_delivery))


@router.post('/agent/tasks/{task_id}/fulfillment/recover', dependencies=[Depends(require_write_token)])
def recover_fulfillment(task_id: str, db: OpsStore = Depends(get_store),
                        tasks: TaskStore = Depends(get_task_store)):
    return execute(lambda: FulfillmentCoordinator(db, tasks).advance(task_id, recover=True))


@router.post('/agent/tasks/{task_id}/fulfillment/handoff', dependencies=[Depends(require_write_token)])
def handoff_fulfillment(task_id: str, body: FulfillmentHandoffBody,
                        db: OpsStore = Depends(get_store),
                        tasks: TaskStore = Depends(get_task_store)):
    return execute(lambda: FulfillmentCoordinator(db, tasks).handoff(
        task_id, reason=body.reason, release_reservation=body.release_reservation))


# V0.6-A6: deterministic, bounded continuation; this does NOT grant the model write tools.
@router.get('/agent/tasks/{task_id}/playbook')
def preview_playbook(task_id: str, db: OpsStore = Depends(get_store),
                     tasks: TaskStore = Depends(get_task_store)):
    return execute(lambda: OperationsPlaybook(db, tasks).preview(task_id))


@router.post('/agent/tasks/{task_id}/playbook/advance', dependencies=[Depends(require_write_token)])
def advance_playbook(task_id: str, db: OpsStore = Depends(get_store),
                     tasks: TaskStore = Depends(get_task_store)):
    # The server decides which authorized step to perform; no arbitrary action,
    # operator approval, delivery attestation or recovery flags are accepted.
    return execute(lambda: OperationsPlaybook(db, tasks).advance_until_gate(task_id))
