import test from 'node:test'
import assert from 'node:assert/strict'
import { getStageHelp, guideWasCompleted, rememberGuideDismissal,
  OPS_GUIDE_DONE_KEY } from '../src/operations/guideContent.ts'

const empty = { canPlan: false, quoteBlocked: false, hasTask: false,
  incidentState: 'open', viewingOtherStage: false }

test('onboarding is first-use only and can be dismissed without throwing on denied storage', () => {
  let saved = ''
  const storage = { getItem: (_key: string) => saved,
    setItem: (key: string, value: string) => { assert.equal(key, OPS_GUIDE_DONE_KEY); saved = value } }
  assert.equal(guideWasCompleted(storage), false)
  rememberGuideDismissal(storage)
  assert.equal(guideWasCompleted(storage), true)
  const denied = { getItem: () => { throw new Error('denied') }, setItem: () => { throw new Error('denied') } }
  assert.equal(guideWasCompleted(denied), false)
  assert.doesNotThrow(() => rememberGuideDismissal(denied))
  assert.equal(guideWasCompleted(null), false)
})

test('blocked stock never suggests forcing a warehouse; escalation is not completion', () => {
  const guide = getStageHelp('decision', { ...empty, canPlan: true, quoteBlocked: true })
  assert.match(guide.title, /没有可行/)
  assert.match(guide.instruction, /人工升级/)
  assert.match(guide.boundary, /不会解决库存不足/)
  const escalated = getStageHelp('decision', { ...empty, incidentState: 'escalated' })
  assert.match(escalated.boundary, /不代表/)
})

test('history views do not claim to be current business state', () => {
  const guide = getStageHelp('decision', { ...empty, viewingOtherStage: true })
  assert.match(guide.title, /其他阶段/)
  assert.match(guide.boundary, /服务端/)
})

test('read-only investigation remains possible without model calls', () => {
  const guide = getStageHelp('investigation', empty)
  assert.match(guide.instruction, /可选/)
  assert.match(guide.boundary, /可能产生 API 费用/)
})

test('shipped orders never encourage new reserve from decision screen', () => {
  const guide = getStageHelp('decision', empty)
  assert.match(guide.title, /不能重新规划/)
  assert.match(guide.instruction, /执行/)
})

test('execution without a task does not offer approval', () => {
  const guide = getStageHelp('execution', empty)
  assert.match(guide.title, /没有关联的执行任务/)
  assert.match(guide.boundary, /不应显示可执行/)
})
