"""Reproducible offline and explicitly billable live evaluation.

OFFLINE never loads an LLM, sends a network request, or edits the project's DB.
LIVE calls the existing run_agent_stream exactly once per selected case.

Run from backend/: uv run python -m evals.run offline
                 uv run python -m evals.run live --limit 3 --confirm-billable
"""
from __future__ import annotations

import argparse
import json
import math
import hashlib
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.data import DB_PATH, create_demo_db, ensure_demo_db
from app.db import run_readonly_query
from app.metric_analysis import analyze_metric
from app.verified_analysis import build_verified_decline

CASES_PATH = Path(__file__).with_name('cases.jsonl')
RESULTS_DIR = Path(__file__).with_name('results')
METRICS = {'revenue', 'net_profit', 'margin', 'cost_rate', 'expense_rate'}
GROUPS = {'company', 'industry', 'year', 'industry_year'}


def cases_load(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    cases = []
    seen = set()
    for lineno, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        case = json.loads(line)
        if not isinstance(case, dict) or not isinstance(case.get('id'), str) or case['id'] in seen:
            raise ValueError(f'评测集第 {lineno} 行无效或 ID 重复')
        if case.get('kind') not in {'metric', 'decline', 'sql_allow', 'sql_deny', 'metric_deny'}:
            raise ValueError(f'未知用例类型：{case.get("kind")}')
        seen.add(case['id'])
        cases.append(case)
    if not cases:
        raise ValueError('评测集不能为空')
    return cases


def _clean_round(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError('非有限参考值')
    v = round(value, 2)
    return 0.0 if v == 0 else v


def raw_facts(path: Path) -> list[dict[str, Any]]:
    """Independent raw-row reader: no production metric engine or SQL aggregations."""
    with sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('''SELECT c.company_name, c.industry, f.year,
          f.revenue, f.cost, f.expenses, f.net_profit
          FROM monthly_financials AS f
          JOIN companies AS c ON c.company_id=f.company_id
          ORDER BY c.company_name, f.year, f.month''').fetchall()
    return [dict(r) for r in rows]


def expected_metric(case: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    """Independent Python reference derived from all unaggregated records."""
    metric, group_by = case['metric'], case['group_by']
    assert metric in METRICS and group_by in GROUPS
    start, end = int(case['start_year']), int(case['end_year'])
    sums: dict[tuple[str, str | None], dict[str, float]] = defaultdict(
        lambda: {'revenue': 0.0, 'cost': 0.0, 'expenses': 0.0, 'net_profit': 0.0, 'months': 0.0}
    )
    for rec in raw_facts(path):
        if not start <= rec['year'] <= end:
            continue
        if group_by == 'company': key = (rec['company_name'], None)
        elif group_by == 'industry': key = (rec['industry'], None)
        elif group_by == 'year': key = (str(rec['year']), None)
        else: key = (str(rec['year']), rec['industry'])
        item = sums[key]
        for col in ('revenue', 'cost', 'expenses', 'net_profit'):
            item[col] += float(rec[col])
        item['months'] += 1
    result = []
    for (label, series), item in sums.items():
        revenue = item['revenue']
        if revenue <= 0:
            raise AssertionError('参考数据营收非正')
        value = {
            'revenue': revenue,
            'net_profit': item['net_profit'],
            'margin': 100 * item['net_profit'] / revenue,
            'cost_rate': 100 * item['cost'] / revenue,
            'expense_rate': 100 * item['expenses'] / revenue,
        }[metric]
        result.append({'label': label, 'series': series, 'value': _clean_round(value),
                       'months': int(item['months']), 'revenue': _clean_round(revenue),
                       'net_profit': _clean_round(item['net_profit'])})
    if group_by in {'company', 'industry'}:
        rev = case.get('order', 'desc') == 'desc'
        result.sort(key=lambda r: (-r['value'] if rev else r['value'], r['label']))
        result = result[:int(case.get('top_n', 12))]
    else:
        result.sort(key=lambda r: (r['series'] or '', int(r['label'])))
    return result


def expected_decline(path: Path, limit: int) -> list[dict[str, Any]]:
    by_company: dict[str, dict[int, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(float))
    )
    for rec in raw_facts(path):
        if rec['year'] not in (2024, 2025):
            continue
        state = by_company[rec['company_name']][rec['year']]
        for col in ('revenue', 'cost', 'expenses', 'net_profit'):
            state[col] += float(rec[col])
        state['months'] += 1
    result = []
    for company, values in by_company.items():
        if 2024 not in values or 2025 not in values:
            continue
        a, b = values[2024], values[2025]
        if a['months'] != 12 or b['months'] != 12:
            continue
        old = 100*a['net_profit']/a['revenue']
        new = 100*b['net_profit']/b['revenue']
        delta = new-old
        if delta < 0:
            result.append({'company':company,'delta_pp':_clean_round(delta),
                           '_sort':delta,'margin_2024':_clean_round(old),'margin_2025':_clean_round(new)})
    result.sort(key=lambda r:(r['_sort'],r['company']))
    for row in result: row.pop('_sort')
    return result[:limit]


def verify_metric_artifact(artifact: dict[str, Any], case: dict[str, Any], path: Path) -> list[str]:
    problems = []
    if artifact.get('kind') != 'metric_analysis':
        return ['未获得 metric_analysis 成果']
    for name in ('metric', 'group_by'):
        if artifact.get(name) != case[name]:
            problems.append(f'{name} 不符合任务：{artifact.get(name)}')
    period = artifact.get('period') or {}
    if period.get('start_year') != case['start_year'] or period.get('end_year') != case['end_year']:
        problems.append('年份范围不符合任务')
    if (artifact.get('audit') or {}).get('status') != 'passed':
        problems.append('数据覆盖/财务勾稽校验未通过')
    rows = artifact.get('rows')
    if not isinstance(rows, list):
        return problems + ['缺少结构化结果行']
    expected = expected_metric(case, path)
    group_by = case['group_by']
    # Ranking tool is allowed to return MORE rows than the request, if top-N
    # appear in the correct order (e.g. asking top 3, tool returns top 12).
    expected_len = len(expected)
    if group_by in {'company','industry'} and len(rows) >= expected_len:
        pairs = zip(rows[:expected_len], expected)
    else:
        if len(rows) != expected_len:
            problems.append(f'行数不符：actual={len(rows)} reference={expected_len}')
        pairs = zip(rows, expected)
    for actual, oracle in pairs:
        if (actual.get('label'),actual.get('series')) != (oracle['label'],oracle['series']):
            problems.append(f'排名/分组不符：{actual.get("label")} != {oracle["label"]}')
            continue
        try:
            if abs(float(actual['value'])-oracle['value']) > .015:
                problems.append(f'{oracle["label"]} 数值不符')
            if int(actual.get('months',-1)) != oracle['months']:
                problems.append(f'{oracle["label"]} 覆盖月度数不符')
            for money in ('revenue', 'net_profit'):
                if abs(float(actual[money])-oracle[money]) > .015:
                    problems.append(f'{oracle["label"]} {money} 不符')
        except (ValueError,TypeError,KeyError):
            problems.append('结果行数值格式错误')
    chart=artifact.get('chart')
    if not isinstance(chart,dict) or not chart.get('series'):
        problems.append('缺少图表结构')
    else:
        if chart.get('kind') != ('line' if group_by in {'year','industry_year'} else 'bar'):
            problems.append('图表类型与分组不符')
        # The displayed chart must agree with its own evidence rows.
        expected_points: dict[str,list[tuple[str,float]]] = defaultdict(list)
        for row in rows:
            key=str(row.get('series') or '') if group_by=='industry_year' else str(artifact.get('metric_label',''))
            try: expected_points[key].append((str(row['label']),float(row['value'])))
            except (ValueError,TypeError,KeyError): problems.append('图表源数据格式无效')
        actual_points:dict[str,list[tuple[str,float]]] = defaultdict(list)
        for series in chart.get('series',[]):
            if not isinstance(series,dict):
                problems.append('图表系列无效'); continue
            try:
                actual_points[str(series['name'])].extend((str(p['label']),float(p['value'])) for p in series['points'])
            except (ValueError,TypeError,KeyError): problems.append('图表数据格式无效')
        if actual_points != expected_points:
            problems.append('图表数据与指标证据表不一致')
    if not artifact.get('source') or not artifact.get('method'):
        problems.append('缺少数据来源或计算口径')
    return problems


def verify_decline_artifact(artifact: dict[str, Any], case: dict[str, Any], path: Path) -> list[str]:
    problems = []
    if artifact.get('kind') != 'margin_decline_2024_2025':
        return ['未获得独立利润率下降核验']
    if (artifact.get('audit') or {}).get('status') != 'passed':
        problems.append('独立核验 audit 未通过')
    actual = artifact.get('companies') or []
    expected = expected_decline(path, int(case.get('limit',3)))
    if len(actual) < len(expected):
        problems.append('下降企业数量不足')
    for a, e in zip(actual,expected):
        if a.get('company') != e['company']:
            problems.append(f'下降排名不符：{a.get("company")} != {e["company"]}')
        for col in ('margin_2024','margin_2025','delta_pp'):
            try:
                if abs(float(a[col]) - e[col]) > .015:
                    problems.append(f'{e["company"]} 的 {col} 不符')
            except (ValueError,TypeError,KeyError):
                problems.append(f'{e["company"]} 缺少 {col}')
    return problems


def evaluate_offline_case(case: dict[str,Any], path: Path) -> dict[str,Any]:
    start = time.perf_counter()
    problems = []
    kind = case['kind']
    try:
        if kind == 'metric':
            artifact = analyze_metric(case['metric'],case['group_by'],case['start_year'],
                case['end_year'],case.get('order','desc'),case.get('top_n',12),path)
            problems.extend(verify_metric_artifact(artifact,case,path))
        elif kind == 'decline':
            artifact = build_verified_decline(path, limit=int(case.get('limit',3)))
            problems.extend(verify_decline_artifact(artifact,case,path))
        elif kind == 'sql_allow':
            result = run_readonly_query(case['sql'],path)
            if 'expect_rows' in case and result['rows'] != case['expect_rows']:
                problems.append('SQL 实际返回与预期不符')
            if 'expect_row_count' in case and result['row_count'] != case['expect_row_count']:
                problems.append('SQL 返回行数不符')
        elif kind == 'sql_deny':
            try: run_readonly_query(case['sql'],path)
            except ValueError: pass
            else: problems.append('不安全 SQL 未被拒绝')
        elif kind == 'metric_deny':
            try: analyze_metric(**case['params'],path=path)
            except ValueError: pass
            else: problems.append('无效指标参数未被拒绝')
    except Exception as exc:
        problems.append('发生未预期异常：'+type(exc).__name__)
    return {'id':case['id'],'kind':kind,'status':'passed' if not problems else 'failed',
            'problems':problems,'duration_ms':round((time.perf_counter()-start)*1000,2)}


def verify_sql_metric_evidence(sql: str, case: dict[str, Any], path: Path) -> dict[str, Any]:
    """Independently replay a *read-only* SQL query and compare grouped numbers.

    This verifier currently recognizes industry/year grouped financial questions.
    It never equates correct SQL output with a verified chart or final prose.
    SQL text and raw rows are intentionally not stored in evaluation reports.
    """
    out: dict[str, Any] = {"verified": False, "reason": "not_checked", "matched_rows": 0,
                           "expected_rows": 0, "sql_sha256_12": hashlib.sha256(sql.encode()).hexdigest()[:12]}
    if case.get("kind") != "metric" or case.get("group_by") != "industry_year":
        out["reason"] = "sql_audit_not_supported_for_this_grouping"
        return out
    try:
        actual = run_readonly_query(sql, path)
    except (ValueError, OSError, sqlite3.Error):
        out["reason"] = "sql_query_not_executable_by_readonly_engine"
        return out
    if actual.get("truncated") or not isinstance(actual.get("rows"), list):
        out["reason"] = "sql_result_truncated_or_missing"
        return out
    expected = expected_metric(case, path)
    out["expected_rows"] = len(expected)
    observed_rows = actual["rows"]
    if len(observed_rows) != len(expected):
        out["reason"] = "row_count_mismatch"
        return out

    def read_alias(row: dict[str, Any], *names: str) -> Any:
        normalized = {str(k).lower().replace("_", "").replace(" ", ""): v for k, v in row.items()}
        for name in names:
            key = name.lower().replace("_", "").replace(" ", "")
            if key in normalized:
                return normalized[key]
        return None

    reference = {(r["label"], r["series"]): r["value"] for r in expected}
    seen: set[tuple[str, str]] = set()
    for row in observed_rows:
        if not isinstance(row, dict):
            out["reason"] = "malformed_sql_result_row"
            return out
        year = read_alias(row, "year", "f.year", "年份", "年度")
        industry = read_alias(row, "industry", "industry_name", "行业")
        try:
            year = str(int(year))
            industry = str(industry)
        except (TypeError, ValueError):
            out["reason"] = "missing_year_or_industry_dimension"
            return out
        key = (year, industry)
        if key in seen or key not in reference:
            out["reason"] = "unexpected_or_duplicate_group"
            return out
        seen.add(key)

        revenue = read_alias(row, "revenue", "total_revenue", "sum_revenue", "revenue_sum", "营业收入", "总营业收入")
        profit = read_alias(row, "net_profit", "total_net_profit", "sum_net_profit", "netprofit_sum", "净利润")
        cost = read_alias(row, "cost", "total_cost", "sum_cost", "营业成本")
        expenses = read_alias(row, "expenses", "total_expenses", "sum_expenses", "期间费用")
        metric = case["metric"]
        try:
            if metric == "revenue":
                value = float(revenue)
            elif metric == "net_profit":
                value = float(profit)
            elif metric == "margin":
                value = float(profit) * 100 / float(revenue)
            elif metric == "cost_rate":
                value = float(cost) * 100 / float(revenue)
            elif metric == "expense_rate":
                value = float(expenses) * 100 / float(revenue)
            else:
                out["reason"] = "unsupported_metric"
                return out
        except (ValueError, TypeError, ZeroDivisionError):
            out["reason"] = "missing_or_invalid_measure_columns"
            return out
        if not math.isfinite(value) or abs(value - reference[key]) > max(0.035, abs(reference[key]) * 1e-7):
            out["reason"] = "measure_mismatch"
            return out
        out["matched_rows"] += 1
    out["verified"] = len(seen) == len(reference)
    out["reason"] = "numeric_sql_evidence_matches_reference" if out["verified"] else "missing_groups"
    return out


def evaluate_live_events(case: dict[str,Any], events: Iterable[tuple[str,dict[str,Any]]], path:Path,
                        duration_ms: float | None = None) -> dict[str,Any]:
    """Score what the existing SSE stream proves. Never claim full prose accuracy."""
    calls=0; tool_errors=0; completed=False; answer=''; artifact=None; observed_tools=[]; errors=[]
    observed_metric_artifacts=[]
    telemetry: dict[str, Any] | None = None
    pending_sql: list[str | None] = []
    sql_audits: list[dict[str,Any]] = []
    for kind,payload in events:
        if kind=='tool_call':
            calls+=1
            observed_tools.append(str(payload.get('name','unknown')))
            if payload.get('name') == 'query_database':
                args = payload.get('args')
                pending_sql.append(args.get('sql') if isinstance(args, dict) and isinstance(args.get('sql'), str) else None)
        elif kind=='tool_result':
            msg=str(payload.get('message',''))
            is_error = msg.lstrip().startswith('{"error"') or msg.lstrip().startswith("{'error'")
            if is_error:
                tool_errors+=1
            if payload.get('name') == 'query_database' and pending_sql:
                pending = pending_sql.pop(0)
                if pending is not None and not is_error and case.get('kind') == 'metric':
                    sql_audits.append(verify_sql_metric_evidence(pending, case, path))
        elif kind=='metric_artifact':
            period=payload.get('period') or {}
            observed_metric_artifacts.append({
                'metric':payload.get('metric'), 'group_by':payload.get('group_by'),
                'start_year':period.get('start_year'),'end_year':period.get('end_year'),
                'audit_status':(payload.get('audit') or {}).get('status'),
                'row_count':len(payload.get('rows')) if isinstance(payload.get('rows'),list) else None,
            })
            if case['kind']=='metric' and (payload.get('metric'),payload.get('group_by'))==(case['metric'],case['group_by']):
                artifact=payload
        elif kind=='verified_analysis' and case['kind']=='decline': artifact=payload
        elif kind=='telemetry' and isinstance(payload, dict) and payload.get('schema_version') == 1:
            telemetry = payload
        elif kind=='done':
            completed=True
            answer=str(payload.get('answer',''))
        elif kind=='error': errors.append(str(payload.get('message','执行失败')))
    problems=[]
    if not completed: problems.append('执行未正常完成')
    if calls == 0: problems.append('没有实际工具调用')
    if not answer.strip(): problems.append('缺少最终回答')
    if artifact is None: problems.append('未产生符合任务的程序核验成果')
    elif case['kind']=='metric': problems.extend(verify_metric_artifact(artifact,case,path))
    else: problems.extend(verify_decline_artifact(artifact,case,path))
    # A simple coverage signal, NOT a hallucination detector or full language grade.
    coverage=None
    if artifact is not None and isinstance(artifact,dict):
        rows=(artifact.get('rows') or artifact.get('companies') or [])
        if rows and case['kind']=='metric' and case['group_by'] in {'company','industry'}:
            coverage=any(str(r.get('label','')) in answer for r in rows[:min(3,len(rows))])
        elif rows and case['kind']=='decline':
            coverage=any(str(r.get('company','')) in answer for r in rows[:min(3,len(rows))])
    sql_evidence_verified = any(a['verified'] for a in sql_audits)
    if artifact is not None:
        evidence_diagnosis='matching_metric_artifact_observed'
    elif sql_evidence_verified:
        evidence_diagnosis='sql_numeric_evidence_verified_but_structured_artifact_missing'
    elif case['kind']=='decline':
        evidence_diagnosis='decline_artifact_missing'
    elif 'analyze_metrics' in observed_tools:
        evidence_diagnosis='analyze_metrics_called_without_matching_artifact'
    elif 'query_database' in observed_tools:
        evidence_diagnosis='sql_only_no_structured_metric_artifact'
    else:
        evidence_diagnosis='no_relevant_analysis_tool_observed'
    observed_usage = telemetry.get('token_usage') if telemetry else None
    if not isinstance(observed_usage, dict):
        observed_usage = None
    model_calls = telemetry.get('model_calls') if telemetry else None
    if not isinstance(model_calls, int) or model_calls < 0:
        model_calls = None
    estimated_cost = telemetry.get('cost_cny_estimate') if telemetry else None
    if not isinstance(estimated_cost, (int, float)) or not math.isfinite(estimated_cost) or estimated_cost < 0:
        estimated_cost = None
    return {'id':case['id'],'kind':case['kind'], 'status':'passed' if not problems else 'failed',
        'problems':problems,'observed_tools':observed_tools,
        'observed_metric_artifacts':observed_metric_artifacts,'evidence_diagnosis':evidence_diagnosis,
        'sql_evidence_verified':sql_evidence_verified, 'sql_audits':sql_audits,
        'structured_artifact_present':artifact is not None,
        'tool_calls':calls,
        'tool_errors':tool_errors,'completed':completed,'duration_ms':duration_ms,
        'answer_mentions_top_entity':coverage, 'prose_factual_accuracy':'not_automatically_verified',
        'model_calls':model_calls, 'token_usage':observed_usage,
        'model_cost':estimated_cost, 'model_cost_basis':(telemetry.get('cost_basis') if telemetry else None),
        'tool_telemetry':(telemetry.get('tools') if telemetry else None),
        'model_durations_ms':(telemetry.get('model_durations_ms') if telemetry else None),
        'model_failures':(telemetry.get('model_failures') if telemetry else None),
        'provider_error_types':[e.split('（')[0][:100] for e in errors]}


def _p95(numbers: list[float]) -> float | None:
    if not numbers: return None
    nums=sorted(numbers)
    return round(nums[math.ceil(.95*len(nums))-1],1)


def _aggregate(mode:str,results:list[dict[str,Any]],db_path:Path) -> dict[str,Any]:
    durations=[float(r['duration_ms']) for r in results if isinstance(r.get('duration_ms'),(int,float))]
    calls=[r['tool_calls'] for r in results if isinstance(r.get('tool_calls'),int)]
    passed=sum(r['status']=='passed' for r in results)
    model_counts=[r.get('model_calls') for r in results]
    complete_calls=bool(results) and all(isinstance(n,int) for n in model_counts)
    usages=[r.get('token_usage') for r in results]
    complete_tokens=bool(results) and all(
        isinstance(u,dict) and u.get('complete') is True for u in usages
    )
    total_tokens=({k:sum(int(u[k]) for u in usages)
                   for k in ('input_tokens','output_tokens','total_tokens')}
                  if complete_tokens else None)
    costs=[r.get('model_cost') for r in results]
    complete_costs=bool(results) and all(isinstance(c,(int,float)) for c in costs)
    return {
        'schema_version':1,'mode':mode,'timestamp_utc':datetime.now(timezone.utc).isoformat(),
        'dataset':'fixed-seed-synthetic-20261111',
        'run_count':len(results),'passed':passed,'failed':len(results)-passed,
        'pass_rate':round(passed/len(results),4) if results else None,
        'latency_ms_p50':round(statistics.median(durations),1) if durations else None,
        'latency_ms_p95':_p95(durations),
        'mean_tool_calls':round(statistics.mean(calls),2) if calls else None,
        'note':('Offline: deterministic components only; no model or network requests.'
                if mode=='offline' else 'Live: program evidence scored; prose accuracy not automatically verified; costs are estimates only if explicit rates configured.'),
        'model_calls':sum(model_counts) if complete_calls else None,
        'mean_model_calls':round(statistics.mean(model_counts),2) if complete_calls else None,
        'tokens':total_tokens, 'tokens_complete':complete_tokens,
        'cost':round(sum(costs),6) if complete_costs else None,
        'cost_type':'user_supplied_flat_rate_estimate' if complete_costs else 'unknown',
        'results':results,
        'by_case':summarize_live_repetitions(results) if mode == 'live' else None,
    }


def summarize_live_repetitions(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group independent live runs; never interpret missing usage as zero.

    These are descriptive sample statistics, not claims of population reliability.
    Tool paths are recorded as names only, without prompts, SQL, or results.
    """
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in results:
        groups[str(record['id'])].append(record)
    summary: dict[str, dict[str, Any]] = {}
    for case_id, records in groups.items():
        durations = [float(r['duration_ms']) for r in records
                     if isinstance(r.get('duration_ms'), (int, float))]
        model_counts = [r.get('model_calls') for r in records]
        usages = [r.get('token_usage') for r in records]
        paths: dict[str, int] = defaultdict(int)
        failure_reasons: dict[str, int] = defaultdict(int)
        for record in records:
            path = ' → '.join(str(t) for t in (record.get('observed_tools') or [])) or '(no tool)'
            paths[path] += 1
            for problem in set(str(x) for x in record.get('problems', [])):
                failure_reasons[problem[:180]] += 1
        calls_known = all(isinstance(v, int) for v in model_counts)
        usage_complete = all(isinstance(u, dict) and u.get('complete') is True for u in usages)
        totals = ({k: sum(int(u[k]) for u in usages)
                   for k in ('input_tokens', 'output_tokens', 'total_tokens')}
                  if usage_complete and records else None)
        passed = sum(r.get('status') == 'passed' for r in records)
        summary[case_id] = {
            'repetitions': len(records), 'passed': passed, 'failed': len(records) - passed,
            'observed_pass_fraction': round(passed / len(records), 4),
            'latency_ms_p50': round(statistics.median(durations), 1) if durations else None,
            'latency_ms_p95': _p95(durations),
            'model_calls_total': sum(model_counts) if calls_known else None,
            'model_calls_mean': round(statistics.mean(model_counts), 2) if calls_known else None,
            'tokens_complete': usage_complete, 'tokens_total': totals,
            'tool_path_counts': dict(sorted(paths.items())),
            'failure_reason_counts': dict(sorted(failure_reasons.items())),
        }
    return summary


def plan_live_runs(cases: list[dict[str, Any]], limit: int, case_id: str | None,
                   repeat: int, max_runs: int = 9) -> list[tuple[dict[str, Any], int]]:
    """Exact billable task schedule, bounded before importing any model code."""
    if not 1 <= repeat <= 3:
        raise ValueError('--repeat 必须是 1–3')
    if not 1 <= limit <= 5:
        raise ValueError('--limit 必须是 1–5')
    selected = select_live_cases(cases, limit, case_id)
    if len(selected) * repeat > max_runs:
        raise ValueError(f'安全上限：最多 {max_runs} 次独立任务，请缩小 --limit 或 --repeat')
    return [(case, rep) for case in selected for rep in range(1, repeat + 1)]


def _write_report(data:dict[str,Any],out:Path|None) -> Path:
    RESULTS_DIR.mkdir(parents=True,exist_ok=True)
    target=out or RESULTS_DIR / f"{data['mode']}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return target


def run_offline(cases:list[dict[str,Any]]) -> dict[str,Any]:
    # Always create a fresh DB in a temporary directory. The original DB remains untouched.
    with tempfile.TemporaryDirectory(prefix='eia-eval-') as tmp:
        path=create_demo_db(Path(tmp)/'benchmark.sqlite')
        results=[evaluate_offline_case(case,path) for case in cases]
        return _aggregate('offline',results,path)


def select_live_cases(cases:list[dict[str,Any]],limit:int,case_id:str|None=None) -> list[dict[str,Any]]:
    eligible=[c for c in cases if c.get('live') and c['kind'] in ('metric','decline')]
    if case_id is not None:
        matches=[c for c in eligible if c['id']==case_id]
        if not matches:
            raise ValueError('未找到可执行的真实评测用例：'+case_id)
        return matches
    return eligible[:limit]


def run_live(cases: list[dict[str, Any]], limit: int, case_id: str | None = None,
             repeat: int = 1) -> dict[str, Any]:
    # Validate size *before* checking API configuration or making any request.
    schedule = plan_live_runs(cases, limit, case_id, repeat)
    from app.config import is_ready
    if not is_ready():
        raise RuntimeError('未配置模型：先设置 backend/.env；不会模拟模型执行。')
    from app.agent import run_agent_stream
    path = ensure_demo_db(DB_PATH)
    results = []
    for index, (case, rep) in enumerate(schedule, 1):
        print(f'[{index}/{len(schedule)}] {case["id"]} [第 {rep}/{repeat} 次]: {case["question"]}', flush=True)
        start = time.perf_counter()
        try:
            # Every repetition is a fresh, independently billable Agent run.
            events = list(run_agent_stream(case['question']))
            result = evaluate_live_events(case, events, path,
                                          round((time.perf_counter() - start) * 1000, 1))
        except Exception as exc:
            result = {'id': case['id'], 'kind': case['kind'], 'status': 'failed',
                      'problems': ['执行发生异常：' + type(exc).__name__],
                      'duration_ms': round((time.perf_counter() - start) * 1000, 1),
                      'model_calls': None, 'token_usage': None, 'model_cost': None}
        result['repetition'] = rep
        results.append(result)
        print('  -> ' + result['status'] + ' | ' + ','.join(result['problems'][:2]), flush=True)
    report = _aggregate('live', results, path)
    report['repetitions_per_case'] = repeat
    return report


def main(argv:list[str]|None=None) -> int:
    parser=argparse.ArgumentParser(description='Agent eval: offline free / live billable opt-in')
    parser.add_argument('mode',choices=['offline','live','list'])
    parser.add_argument('--limit',type=int,default=3,help='Live 选择用例数 1–5；使用 --repeat 独立重复')
    parser.add_argument('--confirm-billable',action='store_true',help='明确同意真实模型可能产生费用')
    parser.add_argument('--repeat', type=int, default=1,
                        help='真实评测每个用例独立重复次数，1–3，默认 1')
    parser.add_argument('--plan', action='store_true',
                        help='仅预览真实评测任务列表，不访问 API，不产生费用')
    parser.add_argument('--case-id',type=str,default=None,help='仅运行一个指定的 LIVE 用例，如 M02')
    parser.add_argument('--output',type=Path,default=None,help='评测报告 JSON 路径（默认忽略的本地目录）')
    args=parser.parse_args(argv)
    try: cases=cases_load()
    except Exception as exc:
        print('评测集无效：'+str(exc),file=sys.stderr)
        return 2
    if args.mode=='list':
        for case in cases:print(case['id'],case['kind'],case.get('question',''))
        return 0
    if args.case_id is not None and args.mode!='live':
        print('--case-id 仅支持 live 模式',file=sys.stderr)
        return 2
    if args.plan and args.mode != 'live':
        print('--plan 仅支持 live 模式', file=sys.stderr)
        return 2
    if args.repeat != 1 and args.mode != 'live':
        print('--repeat 仅支持 live 模式', file=sys.stderr)
        return 2
    if args.mode == 'live':
        try:
            schedule = plan_live_runs(cases, args.limit, args.case_id, args.repeat)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.plan:
            print(f'仅预览，不调用模型。预计任务执行次数：{len(schedule)}')
            for index, (case, rep) in enumerate(schedule, 1):
                print(f'  [{index}/{len(schedule)}] {case["id"]} 第 {rep}/{args.repeat} 次')
            print('执行需要显式添加 --confirm-billable，可能产生 API 费用。')
            return 0
        if not args.confirm_billable:
            print('拒绝运行付费评测：必须显式加入 --confirm-billable', file=sys.stderr)
            return 2
        try:
            report = run_live(cases, args.limit, args.case_id, args.repeat)
        except (RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    else:
        report = run_offline(cases)
    output=_write_report(report,args.output)
    print(f"\n{args.mode.upper()}  {report['passed']}/{report['run_count']} PASS  "
          f"success_rate={report['pass_rate']}  p50={report['latency_ms_p50']}ms  "
          f"p95={report['latency_ms_p95']}ms")
    print('报告位置：'+str(output))
    if report['failed']:
        for r in report['results']:
            if r['status']=='failed': print(f"FAIL {r['id']}: {'; '.join(r['problems'][:3])}")
    if args.mode == 'live':
        print('模型调用次数：'+str(report['model_calls'])+'；Token：'+str(report['tokens']))
        print('费用：'+(str(report['cost'])+' CNY（用户填写价格的估算，非账单）'
                     if report['cost'] is not None else '未知（未配置价格或用量不完整）'))
    else:
        print('离线模式不调用模型；模型用量和费用不适用。')
    return 1 if report['failed'] else 0


if __name__=='__main__':
    raise SystemExit(main())
