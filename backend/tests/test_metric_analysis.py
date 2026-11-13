import pytest

from app.data import create_demo_db
from app.metric_analysis import analyze_metric


def test_company_ranking_and_identity(tmp_path):
    path = create_demo_db(tmp_path / 'demo.sqlite')
    result = analyze_metric('margin', 'company', 2025, 2025, 'desc', 12, path)
    assert result['kind'] == 'metric_analysis'
    assert result['audit']['status'] == 'passed'
    assert len(result['rows']) == 12
    assert result['rows'] == sorted(result['rows'], key=lambda r: (-r['value'], r['label']))
    assert all(r['months'] == 12 for r in result['rows'])
    assert result['chart']['kind'] == 'bar'


def test_industry_year_growth_is_weighted_and_complete(tmp_path):
    path = create_demo_db(tmp_path / 'demo.sqlite')
    trend = analyze_metric('margin', 'industry_year', 2023, 2025, path=path)
    assert trend['audit']['status'] == 'passed'
    assert len(trend['rows']) == 9
    assert len(trend['chart']['series']) == 3
    assert all(len(s['points']) == 3 for s in trend['chart']['series'])
    assert all(row['months'] == 48 for row in trend['rows'])
    assert trend['chart']['kind'] == 'line'
    for r in trend['rows']:
        assert abs(r['value'] - 100*r['net_profit']/r['revenue']) < .02


def test_year_and_industry_contract(tmp_path):
    path = create_demo_db(tmp_path / 'demo.sqlite')
    year = analyze_metric('revenue', 'year', 2023, 2025, path=path)
    assert len(year['rows']) == 3
    assert all(r['months'] == 144 for r in year['rows'])
    industries = analyze_metric('cost_rate', 'industry', 2024, 2025, top_n=2, path=path)
    assert len(industries['rows']) == 2
    assert industries['audit']['status'] == 'passed'


@pytest.mark.parametrize('kwargs', [
    {'metric': 'foo', 'group_by': 'company'},
    {'metric': 'margin', 'group_by': 'users'},
    {'metric': 'margin', 'group_by': 'company', 'start_year': 2026},
    {'metric': 'margin', 'group_by': 'company', 'start_year': 2025, 'end_year': 2024},
    {'metric': 'margin', 'group_by': 'company', 'top_n': 200},
    {'metric': 'margin', 'group_by': 'company', 'order': 'DROP TABLE'},
])
def test_invalid_queries_are_rejected(tmp_path, kwargs):
    path = create_demo_db(tmp_path / 'demo.sqlite')
    with pytest.raises(ValueError):
        analyze_metric(path=path, **kwargs)


def test_read_only_and_deterministic(tmp_path):
    path = create_demo_db(tmp_path / 'demo.sqlite')
    before = path.read_bytes()
    first = analyze_metric('net_profit', 'industry_year', 2023, 2025, path=path)
    assert first == analyze_metric('net_profit', 'industry_year', 2023, 2025, path=path)
    assert path.read_bytes() == before
