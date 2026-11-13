/** P2-A presentation mapping only: NEVER authorizes a business mutation. */
export type OpsStage = 'summary' | 'investigation' | 'decision' | 'execution' | 'review'

export const OPS_STAGES: ReadonlyArray<{ id: OpsStage; label: string }> = [
  { id: 'summary', label: '概览' },
  { id: 'investigation', label: '调查' },
  { id: 'decision', label: '方案' },
  { id: 'execution', label: '执行' },
  { id: 'review', label: '复盘' },
]

export type OpsIncidentFact = {
  state: string; order_status: string
} | null | undefined
export type OpsTaskFact = {
  status: string; fulfillment?: { status: string } | null; result?: { verified?: boolean } | null
} | null | undefined
export type OpsGateFact = {
  gate: string; next_action?: string | null; can_continue?: boolean
} | null | undefined
export type OpsEvidenceFact = { investigationId?: string; modelPlanId?: string } | null | undefined

export type ResolvedOpsStage = {
  stage: OpsStage
  nextLabel: string
  explanation: string
  canPlan: boolean
  /** This is UI guidance, not permission to execute. Check the server gate before every POST. */
  action: 'navigate' | 'attest' | 'approve_reservation' | 'approve_fulfillment' |
    'advance' | 'recover' | 'inspect' | 'none'
}

export function resolveOpsStage(
  incident: OpsIncidentFact,
  task?: OpsTaskFact,
  playbook?: OpsGateFact,
  evidence?: OpsEvidenceFact,
): ResolvedOpsStage {
  if (!incident) return { stage: 'summary', nextLabel: '读取事件事实', explanation: '尚未确认订单状态，暂不提供写入操作。', canPlan: false, action: 'none' }
  const canPlan = incident.order_status === 'pending' && ['open','investigating'].includes(incident.state)
  const gate = playbook?.gate
  const fulfillment = task?.fulfillment?.status
  const verifiedClosed = incident.state === 'resolved' && incident.order_status === 'delivered' && (
    gate === 'completed' || fulfillment === 'completed')
  if (verifiedClosed) return { stage: 'review', nextLabel: '查看已核验结果', explanation: '订单、履约和事件已进入闭环状态，可核查历史记录。', canPlan: false, action: 'inspect' }
  if (gate === 'explicit_recovery' || task?.status === 'executing' || fulfillment === 'executing') {
    return { stage: 'execution', nextLabel: '检查并恢复中断任务', explanation: '结果不确定，必须使用原任务的恢复接口，禁止重复发起新的写入。', canPlan, action: 'recover' }
  }
  if (gate === 'manual_handoff' || fulfillment === 'blocked' || fulfillment === 'handoff' || fulfillment === 'rejected') {
    return { stage: 'execution', nextLabel: '人工检查与接管', explanation: '原任务不能自动继续；保留原任务和审计记录。', canPlan, action: 'inspect' }
  }
  if (gate === 'delivery_attestation' || fulfillment === 'awaiting_delivery_confirmation') {
    return { stage: 'execution', nextLabel: '人工确认模拟签收', explanation: '必须由操作人核对并明确确认；不会自动替代签收。', canPlan: false, action: 'attest' }
  }
  if (incident.order_status === 'shipped' || incident.order_status === 'in_transit' || incident.order_status === 'delivered') {
    return { stage: 'execution', nextLabel: '核对当前物流与执行关口', explanation: '订单已不允许重新预留；仅展示原执行任务及已发生的决策。', canPlan: false, action: 'inspect' }
  }
  if (gate === 'fulfillment_approval') {
    return { stage: 'execution', nextLabel: '审阅并批准独立履约授权', explanation: '履约授权与库存预留审批相互独立。', canPlan, action: 'approve_fulfillment' }
  }
  if (gate === 'reservation_approval' || task?.status === 'pending_approval') {
    return { stage: 'execution', nextLabel: '审阅库存预留申请', explanation: '人工审批后仍由服务端检查库存与业务快照。', canPlan, action: 'approve_reservation' }
  }
  if (gate === 'ready' && playbook?.can_continue) {
    const actions: Record<string, string> = {
      reserve_inventory: '执行并核验库存预留', prepare_fulfillment: '准备独立履约授权',
      dispatch: '执行模拟发货', in_transit: '推进模拟运输', verify_incident: '核验并关闭事件',
    }
    return { stage: 'execution', nextLabel: actions[playbook.next_action || ''] || '推进已授权步骤',
      explanation: '只会执行服务端 preview 允许的步骤，遇到人工关口立即停下。', canPlan, action: 'advance' }
  }
  if (gate === 'completed' || fulfillment === 'completed') {
    return { stage: 'execution', nextLabel: '核对业务闭环状态', explanation: '任务记录已完成，但尚不能同时证实订单和事件均已关闭。', canPlan: false, action: 'inspect' }
  }
  if (task) {
    return { stage: 'execution', nextLabel: '核对既有任务状态', explanation: '当前任务没有可确认的自动步骤；请刷新服务端关口。', canPlan, action: 'inspect' }
  }
  if (incident.state === 'escalated' && !task) {
    return { stage: 'review', nextLabel: '已记录人工升级，等待后续人工处置',
      explanation: '升级事件已进入审计档案，但这不表示补货、调拨或交付已完成。', canPlan: false, action: 'inspect' }
  }
  if (!canPlan) {
    return { stage: incident.state === 'resolved' ? 'review' : 'execution', nextLabel: '核对状态与历史任务',
      explanation: '此订单不可重新规划；若存在已执行任务，请从运行记录恢复其上下文。', canPlan: false, action: 'inspect' }
  }
  if (evidence?.modelPlanId) return { stage: 'decision', nextLabel: '核对并人工采纳方案', explanation: '模型建议不能自动批准或执行。', canPlan, action: 'navigate' }
  if (evidence?.investigationId) return { stage: 'decision', nextLabel: '查看方案和证据', explanation: '调查已保存，规划前需重新检查当前业务事实。', canPlan, action: 'navigate' }
  return { stage: 'summary', nextLabel: '查看事实并开始调查', explanation: '从订单和库存事实开始；模型调查为可选的付费操作。', canPlan, action: 'navigate' }
}
