import test from 'node:test'
import assert from 'node:assert/strict'
import { stockBrief, planView, mutationReceipt, auditHeadline } from '../src/operations/workflowBrief.ts'

test('missing inventory never implies a shortage, availability or ability to execute', () => {
  assert.equal(stockBrief(20).kind, 'unknown')
  assert.equal(stockBrief(20, []).kind, 'unknown')
  assert.equal(stockBrief(0, [{warehouse_id:'EAST',available:2}]).kind, 'unknown')
})
test('INC-1003 stock preview reports real single-warehouse shortage, not executable quote', () => {
  const brief = stockBrief(20, [{warehouse_id:'EAST',available:1},{warehouse_id:'WEST',available:2}])
  assert.equal(brief.kind, 'shortage')
  assert.equal(brief.maxAvailable, 2)
  assert.equal(brief.potentialWarehouses, 0)
  assert.match(brief.detail, /服务端/)
})
test('stock preview is cautious even when some warehouses cover quantity', () => {
  const brief = stockBrief(3, [{warehouse_id:'EAST',available:2},{warehouse_id:'WEST',available:3}])
  assert.equal(brief.kind, 'possible')
  assert.equal(brief.potentialWarehouses, 1)
  assert.match(brief.detail, /不代表/)
})
test('existing task or historical order never invites creation of duplicate task', () => {
  const possible = {options:[{eligible:true}]}
  assert.equal(planView(false, true, possible), 'history')
  assert.equal(planView(true, true, possible), 'existing_task')
  assert.equal(planView(true, false, null), 'not_loaded')
})
test('quote availability only comes from eligible server options', () => {
  assert.equal(planView(true, false, {options:[]}), 'blocked')
  assert.equal(planView(true, false, {options:[{eligible:false}]}), 'blocked')
  assert.equal(planView(true, false, {options:[{eligible:false},{eligible:true}]}), 'ready')
})
test('write receipt never claims delivery or case closure from reservation approval', () => {
  const receipt = mutationReceipt({status:'approved'})
  assert.equal(receipt.nextStage,'execution')
  assert.match(receipt.detail, /不等于/)
})
test('completed fulfillment goes to independent review instead of declaring event resolved', () => {
  const receipt = mutationReceipt({status:'succeeded',fulfillment:{status:'completed'}})
  assert.equal(receipt.nextStage,'review')
  assert.match(receipt.detail, /不.*等同于/)
})
test('stale and handoff receipts direct to check rather than automatic retry', () => {
  assert.match(mutationReceipt({status:'stale'}).detail, /不要重复/)
  assert.match(mutationReceipt({status:'succeeded',fulfillment:{status:'handoff'}}).detail, /暂不重复/)
})
test('audit narrative has readable category while raw original record remains untouched', () => {
  assert.equal(auditHeadline('detection','reopened'),'异常再次出现')
  assert.equal(auditHeadline('business_audit','dispatch'),'模拟发货')
  assert.equal(auditHeadline('task_event','reviewed'),'审批与执行任务记录')
})
