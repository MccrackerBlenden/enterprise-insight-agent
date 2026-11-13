import pytest

from app.data import create_demo_db
from app.verified_analysis import applies_to, build_verified_decline


def test_scoped_task_detection():
    assert applies_to("找出2025年相较2024年利润率下降最明显的三家企业")
    assert applies_to("2024、2025 利润率下滑分析")
    assert not applies_to("2025 年企业经营排名")
    assert not applies_to("分析 2023 和 2025 年利润率下降")


def test_ranked_report_has_real_audited_figures(tmp_path):
    path = create_demo_db(tmp_path / "evidence.sqlite")
    report = build_verified_decline(path)
    assert report["kind"] == "margin_decline_2024_2025"
    assert report["compared_companies"] == 12
    assert report["audit"]["status"] == "passed"
    assert report["audit"]["max_identity_residual_pp"] < 1e-7
    assert len(report["companies"]) == 3
    assert all(row["delta_pp"] < 0 for row in report["companies"])
    assert all(row["months_2024"] == row["months_2025"] == 12 for row in report["companies"])
    assert [r["company"] for r in report["companies"]] == ["晨汐零售", "森禾制造", "云岚数据"]
    assert [r["delta_pp"] for r in report["companies"]] == sorted(r["delta_pp"] for r in report["companies"])
    for row in report["companies"]:
        # Rounding each output to 2dp creates at most a small display residual.
        assert abs(row["delta_pp"] + row["cost_rate_change_pp"] + row["expense_rate_change_pp"]) <= .02


def test_deterministic_and_bounded(tmp_path):
    path = create_demo_db(tmp_path / "evidence.sqlite")
    assert build_verified_decline(path) == build_verified_decline(path)
    assert len(build_verified_decline(path, limit=2)["companies"]) == 2
    with pytest.raises(ValueError):
        build_verified_decline(path, limit=20)


def test_does_not_modify_database(tmp_path):
    path = create_demo_db(tmp_path / "evidence.sqlite")
    before = path.read_bytes()
    build_verified_decline(path)
    assert before == path.read_bytes()
