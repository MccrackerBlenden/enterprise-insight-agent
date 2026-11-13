/** Summary from persisted, read-only Ops facts. This is NOT a cross-DB atomic verification. */
export type CaseFacts = {
  incident_state: string
  order_status: string
  shipment_status: string | null
  has_more: boolean
  linked_tasks: ReadonlyArray<{ task_id: string; status: string; updated_at: string }>
}
export type CurrentIncidentFacts = { state: string; order_status: string }
export type CaseVerdict = {
  kind: 'closed' | 'escalated' | 'in_delivery' | 'ongoing' | 'out_of_sync'
  title: string
  explanation: string
  nextStep: string
}

export function describeCaseVerdict(caseFacts: CaseFacts, current?: CurrentIncidentFacts | null): CaseVerdict {
  if (!current || caseFacts.incident_state !== current.state || caseFacts.order_status !== current.order_status) {
    return {
      kind: 'out_of_sync', title: '两处业务快照尚未同步',
      explanation: '事件概览与只读档案返回了不同状态，不能据此宣布已解决或继续写入。',
      nextStep: '重新同步业务事实和档案，再核对订单与事件状态。',
    }
  }
  if (caseFacts.incident_state === 'resolved' && caseFacts.order_status === 'delivered') {
    return {
      kind: 'closed', title: '业务系统记录：事件已关闭、订单已签收',
      explanation: '这是合成业务中的关闭状态，仍可展开检查操作和任务记录；不等于真实物流交付证明。',
      nextStep: '核查时间线与历史决策，然后返回待办队列。',
    }
  }
  if (caseFacts.incident_state === 'escalated') {
    return {
      kind: 'escalated', title: '已登记人工升级，事件尚未解决',
      explanation: '系统保留升级记录，但无法证明已经有人接单、补货或完成履约。',
      nextStep: '由人工协调并记录后续业务变化；待状态更新后再评估。',
    }
  }
  if (['shipped', 'in_transit', 'delivered'].includes(caseFacts.order_status)) {
    return {
      kind: 'in_delivery', title: '物流已有进展，事件仍待核验',
      explanation: '已发货、运输或订单签收均不自动等于事件关闭。',
      nextStep: '返回执行阶段核对签收、核验或人工恢复关口。',
    }
  }
  return {
    kind: 'ongoing', title: '事件仍在处理中',
    explanation: '档案展示已记录的事实和操作，不代表仓库方案已审批或执行成功。',
    nextStep: '返回当前业务阶段，先处理尚未完成的授权或人工动作。',
  }
}

export function caseCoverageNote(caseFacts: CaseFacts): string {
  const taskCount = caseFacts.linked_tasks.length
  return `${taskCount} 项关联任务${caseFacts.has_more ? ' · 时间线仅展示最近部分记录' : ' · 当前时间线按记录时间排列'}`
}
