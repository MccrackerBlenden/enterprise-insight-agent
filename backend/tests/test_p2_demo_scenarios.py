"""P2-C.2 verification: the demo never modifies the normal operations DB."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from fastapi import HTTPException

from app.ops.api import demo_scenarios
from app.ops.action_tasks import TaskStore
from app.ops.fulfillment import FulfillmentCoordinator
from app.ops.store import OpsStore
from scripts.create_p2_demo import create_demo


def test_three_scenarios_are_real_durable_business_states(tmp_path):
    data = create_demo(tmp_path / "new-run")
    cases = data["scenarios"]
    ops = OpsStore(tmp_path / "new-run" / "operations.sqlite")
    tasks = TaskStore(tmp_path / "new-run" / "action_tasks.sqlite")

    assert data["synthetic"] and data["business_audit_rows"] == 7
    assert len(ops.list_incidents()) == 6  # 3 original + 3 demos
    assert cases["normal"]["incident_state"] == "resolved"
    assert cases["normal"]["order_status"] == "delivered"
    assert cases["normal"]["fulfillment_status"] == "completed"
    assert cases["rejected"]["task_status"] == "rejected"
    assert cases["rejected"]["order_status"] == "pending"
    assert cases["recovery"]["order_status"] == "shipped"
    assert cases["recovery"]["fulfillment_status"] == "executing"
    # Three competing orders require 13 units against the original 12.
    assert sum(ops.get_incident(x["incident_id"])["quantity"] for x in cases.values()) == 13
    assert ops.fulfillment_quote("ORD-DEMO-REJECT")["recommended_warehouse_id"] is None
    assert data["inventory_after"] == [
        {"warehouse_id": "WH-EAST", "on_hand": 0, "reserved": 0, "available": 0},
        {"warehouse_id": "WH-WEST", "on_hand": 5, "reserved": 0, "available": 5},
    ]
    # Rejection consumes no stock and cannot be approved retroactively.
    with pytest.raises(Exception):
        tasks.review(cases["rejected"]["task_id"], True)
    manifest = json.loads((tmp_path / "new-run" / "scenario_manifest.json").read_text())
    assert manifest["shared_sku"] == "SKU-P2C2"


def test_interrupted_dispatch_recovers_exactly_once(tmp_path):
    manifest = create_demo(tmp_path / "new-run")
    ops = OpsStore(tmp_path / "new-run" / "operations.sqlite")
    tasks = TaskStore(tmp_path / "new-run" / "action_tasks.sqlite")
    tid = manifest["scenarios"]["recovery"]["task_id"]
    with ops._connection() as con:
        before = con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        dispatch_before = con.execute("SELECT COUNT(*) FROM command_receipts WHERE operation='dispatch'").fetchone()[0]
    restored = FulfillmentCoordinator(ops, tasks).advance(tid, recover=True)
    assert restored["fulfillment"]["status"] == "approved"
    assert restored["fulfillment"]["next_step"] == 1
    assert restored["events"][-1]["detail"]["evidence"]["command_replayed"] is True
    with ops._connection() as con:
        assert con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before
        assert con.execute("SELECT COUNT(*) FROM command_receipts WHERE operation='dispatch'").fetchone()[0] == dispatch_before


def test_new_run_only_and_existing_data_unchanged(tmp_path):
    existing = tmp_path / "normal-system" / "operations.sqlite"
    ops = OpsStore(existing)
    ops.init()
    with ops._connection() as con:
        before = con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    output = tmp_path / "new-run"
    create_demo(output)
    with pytest.raises(FileExistsError):
        create_demo(output)
    assert len(OpsStore(existing).list_incidents()) == 3
    with ops._connection() as con:
        assert con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before
    assert (output / "scenario_manifest.json").exists()


def test_demo_endpoint_hidden_when_disabled_and_no_local_path_leak(tmp_path, monkeypatch):
    manifest = create_demo(tmp_path / "new-run")
    ops = OpsStore(tmp_path / "new-run" / "operations.sqlite")
    tasks = TaskStore(tmp_path / "new-run" / "action_tasks.sqlite")
    monkeypatch.delenv("EIA_P2_DEMO_MODE", raising=False)
    with pytest.raises(HTTPException) as exc:
        demo_scenarios(ops, tasks)
    assert exc.value.status_code == 404
    monkeypatch.setenv("EIA_P2_DEMO_MODE", "1")
    read = demo_scenarios(ops, tasks)
    assert read["synthetic"] and len(read["scenarios"]) == 3
    assert str(tmp_path) not in json.dumps(read)
    assert read["scenarios"][0]["incident_id"] == manifest["scenarios"]["normal"]["incident_id"]
    FulfillmentCoordinator(ops, tasks).advance(manifest["scenarios"]["recovery"]["task_id"], recover=True)
    updated = demo_scenarios(ops, tasks)
    assert updated["scenarios"][2]["fulfillment_status"] == "approved"
