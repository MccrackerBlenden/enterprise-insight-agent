/* No third-party test framework. `typescript` is already a development dependency. */
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const Module = require('node:module')
const ts = require('typescript')

function loadSource(name) {
  const filename = path.join(__dirname, '..', 'src', name)
  const source = fs.readFileSync(filename, 'utf8')
  const result = ts.transpileModule(source, {
    fileName: filename, compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, reportDiagnostics: true,
  })
  assert.equal((result.diagnostics || []).filter(d => d.category === ts.DiagnosticCategory.Error).length, 0)
  const mod = new Module(filename, module)
  mod.filename = filename
  mod.paths = Module._nodeModulePaths(path.dirname(filename))
  mod._compile(result.outputText, filename)
  return mod.exports
}

let data = new Map()
global.window = { localStorage: {
  getItem(k) { return data.has(k) ? data.get(k) : null },
  setItem(k, v) { data.set(k, v) },
  removeItem(k) { data.delete(k) },
} }
const { saveRunHistory, loadRunHistory, clearRunHistory, summarizeRun } = loadSource('history.ts')
const { buildMarkdownReport } = loadSource('report.ts')
const events = [{ event: 'tool_call', id: 1, data: { name: 'analyze_metrics' }, at: 1000 },
  { event: 'tool_result', id: 2, data: { name: 'analyze_metrics', message: 'ok' }, at: 1220 },
  { event: 'done', id: 3, data: { answer: 'ok' }, at: 1350 }]
const snap = { id: 'one', createdAt: '2026-11-11T10:00:00Z', question: '按年度分析净利润率', answer: '真实模型返回的文本', verified: null, metricArtifacts: [], events }
assert.equal(summarizeRun(events).toolCalls, 1)
assert.equal(summarizeRun(events).durationMs, 350)
assert.equal(saveRunHistory([snap]), true)
assert.deepEqual(loadRunHistory(), [snap])
assert.equal(saveRunHistory(Array.from({ length: 12 }, (_, i) => ({...snap, id: String(i) }))), true)
assert.equal(loadRunHistory().length, 8)
const report = buildMarkdownReport(snap, '2026-11-11')
assert.ok(report.includes('经营洞察 Agent · 分析报告'))
assert.ok(report.includes('真实模型返回的文本'))
assert.ok(report.includes('本次没有程序化核验产物'))
assert.ok(report.includes('analyze_metrics'))
const audited = {
  ...snap,
  metricArtifacts: [{
    title: '利润率趋势', source: 'synthetic', method: 'SUM(net_profit)/SUM(revenue)',
    unit: '%', rows: [{ series: '制造业', label: '2025', value: 17.23, months: 48 }],
    audit: { status: 'passed', records_covered: 48 }, query_sql: 'SELECT 1',
  }],
}
const reportWithEvidence = buildMarkdownReport(audited)
assert.ok(reportWithEvidence.includes('17.23 %'))
assert.ok(reportWithEvidence.includes('SELECT 1'))
assert.ok(reportWithEvidence.includes('程序核验：passed'))
clearRunHistory()
assert.deepEqual(loadRunHistory(), [])
console.log('PASS: history save/load/clear, event summary, evidence and Markdown report')
