import test from 'node:test'
import assert from 'node:assert/strict'
import { resolveOpsStage } from '../src/operations/stageResolver.ts'
const pending = { state: 'open', order_status: 'pending' }
const shipped = { state: 'investigating', order_status: 'shipped' }
const reserved = { state: 'investigating', order_status: 'reserved' }
const task = { status: 'succeeded', fulfillment: { status: 'approved' }, result: { verified: true } }
const gate = (name: string, next_action?: string) => ({ gate: name, can_continue: name === 'ready', next_action })

test('first incident is overview; plan/investigation is optional', () => {
  assert.equal(resolveOpsStage(pending).stage, 'summary')
  assert.equal(resolveOpsStage(pending, null, null, { investigationId: 'INV-1' }).stage, 'decision')
  assert.equal(resolveOpsStage(pending, null, null, { modelPlanId: 'PLAN-1' }).stage, 'decision')
})
test('shipped business fact overrides obsolete pending quotes and gate advice', () => {
  const actual = resolveOpsStage(shipped, { ...task, fulfillment: {status: 'awaiting_delivery_confirmation'} }, gate('delivery_attestation'))
  assert.equal(actual.stage, 'execution'); assert.equal(actual.action, 'attest')
  assert.equal(actual.canPlan, false); assert.match(actual.nextLabel, /模拟签收/)
  assert.equal(resolveOpsStage(shipped).canPlan, false)
  assert.equal(resolveOpsStage(shipped).stage, 'execution')
  assert.equal(resolveOpsStage(shipped, { status:'approved' }, gate('ready', 'reserve_inventory')).action, 'inspect')
})
test('approval gates remain distinct; readiness is service decided', () => {
  assert.equal(resolveOpsStage(pending, {status:'pending_approval'}, gate('reservation_approval')).action, 'approve_reservation')
  assert.equal(resolveOpsStage(reserved, task, gate('fulfillment_approval')).action, 'approve_fulfillment')
  const preview = resolveOpsStage(reserved, task, gate('ready', 'dispatch'))
  assert.equal(preview.action, 'advance'); assert.match(preview.nextLabel, /发货/)
  assert.equal(resolveOpsStage(reserved, task, {gate:'ready', can_continue: false}).action, 'inspect')
})
test('unknown outcome, handoff and rejected fulfillment never auto-advance', () => {
  assert.equal(resolveOpsStage(shipped, task, gate('explicit_recovery')).action, 'recover')
  assert.equal(resolveOpsStage(shipped, task, gate('manual_handoff')).action, 'inspect')
  assert.equal(resolveOpsStage(shipped, { ...task, fulfillment:{status:'blocked'} }, null).action, 'inspect')
})
test('only independently closed business facts reach review', () => {
  const completed = { status:'succeeded', fulfillment: {status:'completed'} }
  assert.equal(resolveOpsStage({state:'resolved',order_status:'delivered'}, completed, gate('completed')).stage,'review')
  assert.equal(resolveOpsStage({state:'investigating',order_status:'delivered'}, completed, gate('completed')).stage,'execution')
  assert.equal(resolveOpsStage({state:'resolved',order_status:'shipped'}, completed, gate('completed')).stage,'execution')
})
test('unknown and unexpected states never assert an authorized business action', () => {
  assert.equal(resolveOpsStage(null).action, 'none')
  assert.equal(resolveOpsStage({ state:'escalated', order_status:'reserved' }).action, 'inspect')
})

test('escalated incidents without a task have an audit path, not an execution CTA', () => {
  const result = resolveOpsStage({ state: 'escalated', order_status: 'pending' })
  assert.equal(result.stage, 'review')
  assert.equal(result.action, 'inspect')
  assert.equal(result.canPlan, false)
  assert.match(result.nextLabel, /人工升级/)
})

test('escalated pending order cannot be planned again', () => {
  assert.equal(resolveOpsStage({ state: 'escalated', order_status: 'pending' }).canPlan, false)
  assert.equal(resolveOpsStage({ state: 'escalated', order_status: 'pending' }).action, 'inspect')
})
