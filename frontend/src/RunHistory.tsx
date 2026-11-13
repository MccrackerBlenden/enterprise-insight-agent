import { Clock3, History, Play, Trash2 } from 'lucide-react'
import type { RunSnapshot } from './history'
import { summarizeRun } from './history'

export default function RunHistory({ items, busy, onOpen, onRerun, onClear }: {
  items: RunSnapshot[]
  busy: boolean
  onOpen: (run: RunSnapshot) => void
  onRerun: (question: string) => void
  onClear: () => void
}) {
  return <section className="panel history-panel" aria-label="本地任务历史">
    <div className="panel-heading"><div><span className="eyebrow">本地任务历史</span><h3>最近完成的分析</h3></div>
      {items.length > 0 && <button className="text-action" type="button" disabled={busy} onClick={onClear}><Trash2 size={14}/> 清空</button>}
    </div>
    <p className="history-disclaimer">仅保存本浏览器最近 8 次成功任务的快照；不上传历史，不重新调用模型。清空后无法恢复。</p>
    {items.length ? <div className="history-items">{items.map(run => {
      const summary = summarizeRun(run.events)
      return <article className="history-item" key={run.id}>
        <div className="history-item-top"><span><Clock3 size={13}/>{new Date(run.createdAt).toLocaleString('zh-CN')}</span><span>{summary.toolCalls} 次工具调用</span></div>
        <p>{run.question}</p>
        <div className="history-actions"><button type="button" disabled={busy} onClick={() => onOpen(run)}><History size={14}/> 查看快照</button>
          <button type="button" disabled={busy} onClick={() => onRerun(run.question)}><Play size={13}/> 重新执行</button></div>
      </article>
    })}</div> : <div className="history-empty">暂无已完成任务。先运行一次 Agent，结果才会保存在此浏览器中。</div>}
  </section>
}
