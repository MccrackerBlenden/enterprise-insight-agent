/** UI action presentation, not authorization. All mutations must still pass the server Playbook and write-token gates. */
export type OperatorActionKind =
  | 'reserve_review' | 'advance' | 'fulfillment_review' | 'delivery_attestation'
  | 'reservation_recovery' | 'fulfillment_recovery'

export type OperatorAction = {
  kind: OperatorActionKind
  title: string
  explanation: string
  buttonLabel: string
  allowReject: boolean
  requiresAttestation: boolean
}

type TaskState = { status: string; fulfillment?: { status: string } | null } | null | undefined
type GateState = { gate: string; can_continue: boolean; next_action: string | null } | null | undefined

const advanceLabels: Record<string, string> = {
  reserve_inventory: '执行并核验库存预留',
  prepare_fulfillment: '准备独立履约申请',
  dispatch: '执行模拟发货',
  in_transit: '推进模拟运输',
  verify_incident: '核验事件是否完成',
}

/** A rejected / missing / contradictory server gate must NEVER synthesize an executable action. */
export function resolveOperatorAction(
  task: TaskState, gate: GateState, businessAction: string,
): OperatorAction | null {
  if (!task || !gate) return null
  if (gate.gate === 'reservation_approval' && task.status === 'pending_approval' && businessAction === 'approve_reservation') {
    return { kind: 'reserve_review', title: '审阅库存预留申请',
      explanation: '仅决定是否批准本任务的库存预留；不包含发货和签收授权。',
      buttonLabel: '批准本次库存预留', allowReject: true, requiresAttestation: false }
  }
  if (gate.gate === 'fulfillment_approval' && task.status === 'succeeded' &&
      task.fulfillment?.status === 'pending_approval' && businessAction === 'approve_fulfillment') {
    return { kind: 'fulfillment_review', title: '审阅独立履约授权',
      explanation: '库存预留已核验，但发货与运输必须另行审批。',
      buttonLabel: '批准模拟发货与运输', allowReject: true, requiresAttestation: false }
  }
  if (gate.gate === 'delivery_attestation' && task.status === 'succeeded' &&
      task.fulfillment?.status === 'awaiting_delivery_confirmation' && businessAction === 'attest') {
    return { kind: 'delivery_attestation', title: '核对并确认模拟签收',
      explanation: '这是合成业务的人工签收确认，不代表真实承运商已完成交付。',
      buttonLabel: '记录模拟签收并核验', allowReject: false, requiresAttestation: true }
  }
  if (gate.gate === 'explicit_recovery' && businessAction === 'recover') {
    if (task.fulfillment?.status === 'executing' && task.status === 'succeeded') {
      return { kind: 'fulfillment_recovery', title: '恢复受中断的履约步骤',
        explanation: '仅复用原任务回执和幂等键核对结果，不新建重复发货。',
        buttonLabel: '核查并恢复原履约步骤', allowReject: false, requiresAttestation: false }
    }
    if (task.status === 'executing') {
      return { kind: 'reservation_recovery', title: '恢复受中断的库存预留',
        explanation: '结果尚未确认；只能通过原任务恢复接口处理，不重复创建库存预留。',
        buttonLabel: '核查并恢复原预留', allowReject: false, requiresAttestation: false }
    }
  }
  if (gate.gate === 'ready' && gate.can_continue && businessAction === 'advance') {
    // Never call the server's generic "advance" from an unknown next_action.
    const actionLabel = gate.next_action ? advanceLabels[gate.next_action] : null
    if (!actionLabel) return null
    return { kind: 'advance', title: actionLabel,
      explanation: '仅推进服务端已经允许的步骤；遇到审批、恢复或模拟签收关口即停止。',
      buttonLabel: `${actionLabel}，直到下一人工关口`, allowReject: false, requiresAttestation: false }
  }
  return null
}
