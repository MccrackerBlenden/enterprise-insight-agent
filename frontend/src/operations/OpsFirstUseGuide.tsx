import { ArrowRight, BookOpen, CheckCircle2, ClipboardList, ShieldCheck, X } from 'lucide-react'

export type GuideStep = 0 | 1 | 2

export function OpsFirstUseGuide({ step, openCount, approvalCount, hasExample, selectedSummary,
  selectedOrderId, currentStageLabel, currentAction, onStart, onOpenExample, onShowCurrent,
  onDismiss }: {
  step: GuideStep
  openCount: number
  approvalCount: number
  hasExample: boolean
  selectedSummary: string | null
  selectedOrderId: string | null
  currentStageLabel: string
  currentAction: string
  onStart: () => void
  onOpenExample: () => void
  onShowCurrent: () => void
  onDismiss: () => void
}) {
  return <section className="ops-p2-guide" aria-label="首次使用引导" aria-live="polite">
    <div className="ops-p2-guide-header">
      <span><BookOpen size={16}/> 首次使用引导 <small>{step + 1} / 3</small></span>
      <button type="button" className="ops-p2-guide-skip" onClick={onDismiss} aria-label="跳过并关闭新手引导">
        跳过引导 <X size={15}/>
      </button>
    </div>
    <div className="ops-p2-guide-progress" role="progressbar" aria-label="引导进度" aria-valuemin={1} aria-valuemax={3} aria-valuenow={step+1}>
      <span style={{ width: `${(step + 1) / 3 * 100}%` }}/>
    </div>
    {step === 0 && <>
      <div className="ops-p2-guide-body">
        <div className="ops-p2-guide-icon"><BookOpen size={22}/></div>
        <div><span className="ops-p2-guide-eyebrow">3 步 · 不改动业务数据</span>
          <h2>第一次使用，从处理一条异常开始</h2>
          <p>这里不是聊天机器人：系统先发现合成订单异常，再让你核对库存、审阅方案，必要时人工审批并查看审计结果。</p>
          <div className="ops-p2-guide-map">
            <span><b>决策总览</b> 看经营指标</span>
            <span><b>Agent 工作台</b> 提问做分析（可能计费）</span>
            <span><b>运营处置中心</b> 处理订单异常</span>
          </div>
        </div>
      </div>
      <div className="ops-p2-guide-footer"><small>只带你查看演示业务数据，不会自动调用模型、修改订单或执行审批。</small>
        <button type="button" onClick={onStart}>开始 · 认识待办 <ArrowRight size={15}/></button></div>
    </>}
    {step === 1 && <>
      <div className="ops-p2-guide-body">
        <div className="ops-p2-guide-icon"><ClipboardList size={22}/></div>
        <div><span className="ops-p2-guide-eyebrow">第一件事 · 看待办</span><h2>先从“优先处理”找到需要关注的事件</h2>
          <p>事件是“发生了什么”，任务是“准备执行什么”。不要急着点审批；先打开一条事件，了解订单和库存。</p>
          <div className="ops-p2-guide-stats"><span>当前非归档待办 <strong>{openCount}</strong></span><span>需要人工确认 <strong>{approvalCount}</strong></span></div>
        </div>
      </div>
      <div className="ops-p2-guide-footer"><small>{hasExample ? '下面的按钮只打开当前队列中的一条真实事件，不会提交任何业务命令。' : '当前没有可演示待办。可以刷新业务状态或先结束引导。'}</small>
        <button type="button" disabled={!hasExample} onClick={onOpenExample}>打开一条待办（只读） <ArrowRight size={15}/></button></div>
    </>}
    {step === 2 && <>
      <div className="ops-p2-guide-body">
        <div className="ops-p2-guide-icon"><ShieldCheck size={22}/></div>
        <div><span className="ops-p2-guide-eyebrow">最后一步 · 看当前业务关口</span><h2>让系统告诉你下一步，而不是按按钮猜</h2>
          <p>{selectedSummary ? `你正在查看 ${selectedOrderId || '一条订单'}：${selectedSummary}` : '系统正在读取事件详情。请核对订单状态后再继续。'}</p>
          <div className="ops-p2-guide-next"><small>服务端当前建议</small><strong>{currentStageLabel}</strong><span>{currentAction}</span></div>
          <div className="ops-p2-guide-safety">
            <span><CheckCircle2 size={14}/> 查看事实、方案与历史：无需演示令牌</span>
            <span><ShieldCheck size={14}/> 写入、审批和签收：需要本地演示令牌与服务端校验</span>
            <span><BookOpen size={14}/> 模型调查：可选，可能产生 API 费用</span>
          </div>
        </div>
      </div>
      <div className="ops-p2-guide-footer"><small>完成后，随时点击页面右上方的“新手指导”重新查看。</small>
        <button type="button" disabled={!selectedSummary} onClick={onShowCurrent}>完成引导 · 查看当前阶段 <ArrowRight size={15}/></button></div>
    </>}
  </section>
}
