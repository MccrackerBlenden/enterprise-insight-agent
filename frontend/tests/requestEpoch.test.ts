import test from 'node:test'
import assert from 'node:assert/strict'
import { createRequestEpoch, canRestoreRefreshSelection } from '../src/operations/requestEpoch.ts'

test('slow task read cannot overwrite newer task read', async () => {
  const gate = createRequestEpoch()
  const prior = gate.begin()
  const latest = gate.begin()
  assert.equal(gate.isCurrent(prior), false)
  assert.equal(gate.isCurrent(latest), true)
})

test('mutation invalidates previously pending reads even if no subsequent GET has started', () => {
  const gate = createRequestEpoch()
  const ticket = gate.begin()
  gate.invalidate()
  assert.equal(gate.isCurrent(ticket), false)
})

test('slow refresh cannot navigate back to an incident selected before the request', () => {
  assert.equal(canRestoreRefreshSelection(2, 3, 'INC-1001'), false)
  assert.equal(canRestoreRefreshSelection(2, 2, 'INC-1001'), true)
  assert.equal(canRestoreRefreshSelection(2, 2), false)
})
