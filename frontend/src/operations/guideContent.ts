import type { OpsStage } from './stageResolver'

/** UI-only preferences. Never store demo tokens or business outcomes here. */
export const OPS_GUIDE_DONE_KEY = 'eia.ops.p2.first-use.v1'

type StorageLike = Pick<Storage, 'getItem' | 'setItem'>

export function guideWasCompleted(storage: StorageLike | null): boolean {
  try { return storage?.getItem(OPS_GUIDE_DONE_KEY) === 'done' } catch { return false }
}

export function rememberGuideDismissal(storage: StorageLike | null): void {
  try { storage?.setItem(OPS_GUIDE_DONE_KEY, 'done') } catch { /* private mode may deny storage */ }
}

export type StageHelpFacts = {
  canPlan: boolean
  quoteBlocked: boolean
  hasTask: boolean
  incidentState: string
  viewingOtherStage: boolean
}

/** Contextual copy only. NEVER determine write permission from these hints. */
export function getStageHelp(stage: OpsStage, facts: StageHelpFacts): {
  title: string
  instruction: string
  boundary: string
} {
  if (facts.viewingOtherStage) return {
    title: '你在查看其他阶段的资料',
    instruction: '这不代表业务已经走到这一步。可先阅读历史内容，再点“返回当前阶段”处理真正的待办。',
    boundary: '实际审批与可执行操作只由服务端业务关口决定。',
  }
  if (stage === 'summary') return {
    title: '先判断发生了什么',
    instruction: '核对订单、业务对象和事件状态。下方会显示系统建议的当前业务关口。',
    boundary: '这一步只查看事实，不会更改订单。',
  }
  if (stage === 'investigation') return {
    title: '先确认订单与仓库库存',
    instruction: '看到库存信息后，点击“查看仓库方案”继续。模型辅助调查是可选项，不是必经步骤。',
    boundary: '真实模型调用可能产生 API 费用；查看库存不会。',
  }
  if (stage === 'decision' && facts.incidentState === 'escalated') return {
    title: '事件已经交给人工协调',
    instruction: '无需再创建普通库存预留方案。请转到“复盘”核对升级理由和审计记录。',
    boundary: '人工升级不代表已经补货、有人接单或订单完成。',
  }
  if (stage === 'decision' && !facts.canPlan) return {
    title: '当前订单不能重新规划仓库',
    instruction: '订单或事件状态已经发生变化，请前往“执行”查看当前流程，或在“复盘”查看旧方案。',
    boundary: '历史方案不能当作现时可执行报价。',
  }
  if (stage === 'decision' && facts.quoteBlocked) return {
    title: '没有可行的自动履约方案',
    instruction: '先看库存缺口；确需协调时，填写本地演示令牌并使用“记录人工升级”。',
    boundary: '不要凭空选择仓库；人工升级会留下业务记录但不会解决库存不足。',
  }
  if (stage === 'decision') return {
    title: '核对仓库候选，再决定是否提交审批',
    instruction: '对比可行仓库与模拟成本，然后选择方案，创建待审批任务。',
    boundary: '创建任务不等于已经批准、预留或发货。',
  }
  if (stage === 'execution' && !facts.hasTask) return {
    title: '当前事件没有关联的执行任务',
    instruction: '可以返回“方案”查看是否需要建立任务；已发货事件只核查已有状态与历史，不重新预留。',
    boundary: '没有服务端任务时，不应显示可执行的审批按钮。',
  }
  if (stage === 'execution') return {
    title: '按照当前服务端关口执行',
    instruction: '先看任务状态和待确认事项，再按服务端允许的按钮审批、推进、恢复或签收。',
    boundary: '审批、模拟签收和故障恢复各需独立核验；引导不会替你自动执行。',
  }
  return {
    title: '确认真正发生了什么',
    instruction: '查看事件、订单、物流状态及审计时间线，确认哪些动作已经写入业务数据库。',
    boundary: '人工升级、任务完成和事件核验解决是不同状态。',
  }
}
