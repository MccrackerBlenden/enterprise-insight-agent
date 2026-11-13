import test from 'node:test'
import assert from 'node:assert/strict'
import { describeOpsError, safeDetail } from '../src/operations/opsErrors.ts'

test('write token failure is distinct from model provider and never requests the API key in frontend', () => {
  assert.match(describeOpsError(403,'unauthorized','write'), /EIA_OPS_WRITE_TOKEN/)
  assert.match(describeOpsError(403,'unauthorized','read'), /读取权限/)
})
test('stale and conflict warn before blind retry', () => {
  assert.match(describeOpsError(409,'snapshot mismatch','write'), /不要直接重试/)
  assert.match(describeOpsError(422,'plan stale','write'), /快照已变化/)
  assert.match(describeOpsError(500, null, 'write'), /不要立即重复提交/)
})
test('framework field validation arrays never render as objects', () => {
  assert.equal(safeDetail([{loc:['body','foo']}]), '请求数据未通过服务端校验')
})
