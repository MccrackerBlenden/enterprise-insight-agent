import type { ReactNode } from 'react'

/** Safe minimal Markdown subset for LLM narrative: no raw HTML or unsafe links. */
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean).map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**')) return <strong key={i}>{part.slice(2, -2)}</strong>
    if (part.startsWith('`') && part.endsWith('`')) return <code key={i}>{part.slice(1, -1)}</code>
    return part
  })
}

type Block = { kind: 'paragraph'|'heading'|'list'|'code'; value: string[]; ordered?: boolean; level?: number }

function blocks(text: string): Block[] {
  const out: Block[] = []
  let inCode = false
  for (const original of text.replace(/\r\n?/g, '\n').split('\n')) {
    const line = original.trim()
    if (line.startsWith('```')) {
      if (!inCode) { out.push({ kind: 'code', value: [] }); inCode = true }
      else inCode = false
      continue
    }
    if (inCode) { out[out.length - 1].value.push(original); continue }
    if (!line) continue
    const heading = line.match(/^(#{1,4})\s+(.+)$/)
    if (heading) { out.push({ kind: 'heading', level: heading[1].length, value: [heading[2]] }); continue }
    const bullet = line.match(/^(?:[-*]\s+)(.+)$/)
    const numbered = line.match(/^\d+[.)]\s+(.+)$/)
    if (bullet || numbered) {
      const ordered = Boolean(numbered)
      const last = out[out.length - 1]
      if (last?.kind === 'list' && last.ordered === ordered) last.value.push((bullet || numbered)![1])
      else out.push({ kind: 'list', value: [(bullet || numbered)![1]], ordered })
      continue
    }
    // Keep each standalone line readable; no HTML is interpreted.
    out.push({ kind: 'paragraph', value: [line] })
  }
  return out
}

export default function MarkdownView({ text }: { text: string }) {
  return <div className="markdown-view">{blocks(text).map((block, i) => {
    if (block.kind === 'heading') return <h4 key={i}>{inline(block.value[0])}</h4>
    if (block.kind === 'code') return <pre key={i}><code>{block.value.join('\n')}</code></pre>
    if (block.kind === 'list') {
      const entries = block.value.map((v, n) => <li key={n}>{inline(v)}</li>)
      return block.ordered ? <ol key={i}>{entries}</ol> : <ul key={i}>{entries}</ul>
    }
    return <p key={i}>{inline(block.value.join(' '))}</p>
  })}</div>
}
