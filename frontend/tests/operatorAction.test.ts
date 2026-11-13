import test from 'node:test'
import assert from 'node:assert/strict'
import { resolveOperatorAction } from '../src/operations/operatorAction.ts'

const task = (status: string, fulfillment?: string) => ({ status, fulfillment: fulfillment ? {status: fulfillment} : null })
const gate = (code: string, next_action: string | null = null, can_continue = false) => ({gate: code, next_action, can_continue})

test('reservation and fulfillment approvals are distinct, each with a rejection path', () => {
  assert.equal(resolveOperatorAction(task('pending_approval'), gate('reservation_approval'), 'approve_reservation')?.kind, 'reserve_review')
  assert.equal(resolveOperatorAction(task('succeeded', 'pending_approval'), gate('fulfillment_approval'), 'approve_fulfillment')?.kind, 'fulfillment_review')
  assert.equal(resolveOperatorAction(task('succeeded', 'pending_approval'), gate('reservation_approval'), 'approve_reservation'), null)
})
test('shipped attest requires matching gate, status, and human confirmation', () => {
  const action = resolveOperatorAction(task('succeeded', 'awaiting_delivery_confirmation'), gate('delivery_attestation'), 'attest')
  assert.equal(action?.kind, 'delivery_attestation')
  assert.equal(action?.requiresAttestation, true)
  assert.equal(resolveOperatorAction(task('succeeded', 'blocked'), gate('delivery_attestation'), 'attest'), null)
})
test('only confirmed ready gates with known steps can advance', () => {
  assert.equal(resolveOperatorAction(task('approved'), gate('ready', 'reserve_inventory', true), 'advance')?.kind, 'advance')
  assert.equal(resolveOperatorAction(task('approved'), gate('ready', 'reserve_inventory'), 'advance'), null)
  assert.equal(resolveOperatorAction(task('approved'), gate('ready', 'mystery', true), 'advance'), null)
  assert.equal(resolveOperatorAction(task('approved'), gate('ready', 'reserve_inventory', true), 'inspect'), null)
})
test('recovery only uses original interrupted task; denied, handoff and absent gate cannot act', () => {
  assert.equal(resolveOperatorAction(task('executing'), gate('explicit_recovery'), 'recover')?.kind, 'reservation_recovery')
  assert.equal(resolveOperatorAction(task('succeeded', 'executing'), gate('explicit_recovery'), 'recover')?.kind, 'fulfillment_recovery')
  assert.equal(resolveOperatorAction(task('succeeded', 'handoff'), gate('manual_handoff'), 'inspect'), null)
  assert.equal(resolveOperatorAction(task('approved'), null, 'advance'), null)
})
