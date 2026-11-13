/** P2-B.2 presentation helpers; do not grant approvals or infer successful fulfillment. */
import type { OpsStage } from './stageResolver'

export type StockRow = { warehouse_id: string; available: number }
export type StockBrief = { kind: 'unknown' | 'shortage' | 'possible'; title: string; detail: string;
  maxAvailable: number | null; potentialWarehouses: number }

/** Inventory alone is only a preliminary check, not a verified executable quote. */
export function stockBrief(quantity: number, inventory?: StockRow[]): StockBrief {
  if (!inventory?.length || !Number.isFinite(quantity) || quantity <= 0) return {
    kind: 'unknown', title: '库存事实尚未完整返回', detail: '请同步业务状态；在完成核对前不判断是否能够履约。',
    maxAvailable: null, potentialWarehouses: 0,
  }
  const maxAvailable = Math.max(0, ...inventory.map(item => Math.max(0, item.available)))
  const potentialWarehouses = inventory.filter(item => item.available >= quantity).length
  return potentialWarehouses ? {
    kind: 'possible', title: `${potentialWarehouses} 个仓库的可用库存达到订单数量`,
    detail: '这是库存初筛，不代表符合全部履约约束；继续查看服务端方案和审批条件。',
    maxAvailable, potentialWarehouses,
  } : {
    kind: 'shortage', title: '当前没有单仓库存达到订单数量',
    detail: '先查看服务端是否仍有可行方案；若无可行方案，再选择人工协调。不能以模型建议绕过库存校验。',
    maxAvailable, potentialWarehouses: 0,
  }
}

export type PlanView = 'history' | 'existing_task' | 'not_loaded' | 'blocked' | 'ready'
export function planView(canPlan: boolean, hasTask: boolean,
  quote: { options: Array<{ eligible: boolean }> } | null): PlanView {
  if (!canPlan) return 'history'
  if (hasTask) return 'existing_task'
  if (!quote) return 'not_loaded'
  return quote.options.some(item => item.eligible) ? 'ready' : 'blocked'
}

export type Receipt = { title: string; detail: string; nextStage: OpsStage; button: string }
export function mutationReceipt(task: {
  status: string; fulfillment?: { status: string } | null;
}): Receipt {
  if (task.fulfillment?.status === 'completed') return {
    title: '履约任务已返回完成状态',
    detail: '还需要核对订单是否已签收、事件是否已核验关闭。请在复盘中查看来源记录；不要把任务完成等同于事件关闭。',
    nextStage: 'review', button: '查看复盘与业务核验',
  }
  if (['handoff', 'blocked', 'rejected'].includes(task.fulfillment?.status || '')) return {
    title: '服务端已记录履约停止或受阻状态',
    detail: '暂不重复发货或预留。请核对当前任务关口和人工接管记录。',
    nextStage: 'execution', button: '核对任务与人工处理',
  }
  if (task.status === 'rejected' || task.status === 'failed' || task.status === 'stale') return {
    title: '原任务已返回不可继续状态',
    detail: '请先核对拒绝、失败或过期原因；不要重复提交相同业务命令。',
    nextStage: 'execution', button: '查看原因与下一步',
  }
  return {
    title: '服务端已接受操作，需核对最新关口',
    detail: '请查看新的任务状态。审批通过不等于已预留，预留成功也不等于已发货或签收。',
    nextStage: 'execution', button: '查看最新任务关口',
  }
}

/** Technical codes are preserved in the expanded audit details, not lost. */
const auditLabels: Record<string, string> = {
  reserve: '库存预留', release: '释放预留', dispatch: '模拟发货',
  advance_shipment: '物流状态推进', verify_incident: '事件核验', escalate: '人工升级',
  detected: '异常首次发现', updated: '异常信息更新', cleared: '异常信号消除', reopened: '异常再次出现',
}
export function auditHeadline(source: string, kind: string): string {
  if (source === 'detection') return auditLabels[kind] || '异常检测记录'
  if (source === 'business_audit') return auditLabels[kind] || '业务命令记录'
  if (source === 'task_event') return '审批与执行任务记录'
  return '其他来源记录'
}
