/** Specific, actionable errors without treating a successful write plus failed refresh as a failed mutation. */
export type OpsErrorContext = 'read' | 'write' | 'model'

export function safeDetail(detail: unknown): string {
  if (typeof detail === 'string') return detail.slice(0, 200)
  if (Array.isArray(detail)) return '请求数据未通过服务端校验'
  if (detail && typeof detail === 'object') {
    const payload = detail as Record<string, unknown>
    return typeof payload.message === 'string' ? payload.message.slice(0, 200) : '请求未能完成'
  }
  return ''
}

export function describeOpsError(status: number, detail: unknown, context: OpsErrorContext): string {
  const original = safeDetail(detail)
  if (status === 403) return context === 'write' || context === 'model'
    ? '操作未获授权（403）。请检查本地演示写入令牌 EIA_OPS_WRITE_TOKEN；这不是模型 API Key。若调用已被受理但模型流报错，请查看提供商返回的信息。'
    : '当前数据无读取权限（403）。请检查服务端访问配置，不需要在网页填写模型密钥。'
  if (status === 404) return '记录不存在或已不可访问。请返回运营态势重新选择事件或刷新任务状态。'
  if (status === 409 || /stale|expired|过期|snapshot|conflict|order_not_pending/i.test(original))
    return `业务状态或方案快照已变化，旧操作不能继续。请先刷新并复核，不要直接重试写入。${original ? `（服务端：${original}）` : ''}`
  if (status === 422) return `操作未通过业务校验；请核对当前仓库、审批关口和填写内容。${original ? `（服务端：${original}）` : ''}`
  if (status === 429) return '调用频率受到限制，请稍后重新检查；不要连续重复提交相同业务写入。'
  if (status >= 500) return '服务端暂时无法确认结果。若刚提交了写入，请先刷新任务与审计记录，不要立即重复提交。'
  return `请求未能完成（HTTP ${status}）。${original ? `服务端：${original}` : '请刷新业务状态后再检查。'}`
}
