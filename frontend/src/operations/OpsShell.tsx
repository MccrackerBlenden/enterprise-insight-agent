import type { ReactNode } from 'react'

export type OpsArea = 'overview' | 'incident' | 'journal'

export function OpsShell({ area, onChange, children }: {
  area: OpsArea; onChange: (area: OpsArea) => void; children?: ReactNode
}) {
  const items: Array<{ id: OpsArea; label: string; hint: string }> = [
    { id: 'overview', label: '运营态势', hint: '优先级与待办' },
    { id: 'incident', label: '事件工作台', hint: '当前事件处置' },
    { id: 'journal', label: '运行记录与质量', hint: '任务和审计' },
  ]
  return <nav className="ops-p2-navigation" aria-label="运营处置工作空间">
    {items.map(item => <button key={item.id} type="button" aria-current={area === item.id ? 'page' : undefined}
      className={area === item.id ? 'active' : ''} onClick={() => onChange(item.id)}>
      <strong>{item.label}</strong><small>{item.hint}</small>
    </button>)}
    {children}
  </nav>
}
