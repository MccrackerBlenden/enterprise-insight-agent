import test from 'node:test'
import assert from 'node:assert/strict'
import { describeCaseVerdict, caseCoverageNote } from '../src/operations/caseVerdict.ts'
const make = (incident_state: string, order_status: string) => ({
  incident_state, order_status, shipment_status: null, has_more: false, linked_tasks: [],
})
test('closed verdict requires event and order both closed and snapshots agreeing', () => {
  assert.equal(describeCaseVerdict(make('resolved','delivered'), {state:'resolved',order_status:'delivered'}).kind, 'closed')
  assert.equal(describeCaseVerdict(make('resolved','delivered'), {state:'open',order_status:'shipped'}).kind, 'out_of_sync')
  assert.equal(describeCaseVerdict(make('resolved','shipped'), {state:'resolved',order_status:'shipped'}).kind, 'in_delivery')
})
test('escalated is never a completed task or fulfilled order', () => {
  const verdict = describeCaseVerdict(make('escalated','pending'), {state:'escalated',order_status:'pending'})
  assert.equal(verdict.kind, 'escalated')
  assert.match(verdict.explanation, /无法证明/)
})
test('shipped is pending verification rather than resolved', () => {
  const verdict = describeCaseVerdict(make('open','shipped'), {state:'open',order_status:'shipped'})
  assert.equal(verdict.kind, 'in_delivery')
})
test('read-only cross-source record may be truncated', () => {
  const facts = {...make('open','pending'), has_more:true, linked_tasks:[{task_id:'T1',status:'approved',updated_at:'2026-11-13'}]}
  assert.match(caseCoverageNote(facts), /最近部分/)
  assert.match(caseCoverageNote(facts), /1 项/)
})
