#!/usr/bin/env python3
"""Create an isolated, auditable P2-C.2 demo using real ops state machines.

Never touches the running application's DB, Docker volume, or cloud services.
The output directory MUST NOT already exist. Each run gets a fresh dataset.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from shutil import rmtree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.ops.action_tasks import ActionCoordinator, TaskStore  # noqa: E402
from app.ops.fulfillment import FulfillmentCoordinator  # noqa: E402
from app.ops.store import OpsStore  # noqa: E402

SKU = "SKU-P2C2"
SCENARIOS = (
    ("normal", "ORD-DEMO-NORMAL", "INC-DEMO-NORMAL", 4, "high", "正常履约：可核验签收与事件关闭"),
    ("rejected", "ORD-DEMO-REJECT", "INC-DEMO-REJECT", 6, "medium", "审批拒绝：订单不能被擅自预留"),
    ("recovery", "ORD-DEMO-RECOVER", "INC-DEMO-RECOVER", 3, "high", "中断恢复：发运已入账但工作流尚未确认"),
)


def create_demo(directory: Path) -> dict:
    """Provision NEW output directory; raise if occupied, never overwrite data."""
    directory = Path(directory).expanduser().resolve()
    # Guard against common mistakes even if a user passes a different filename.
    if directory.exists():
        raise FileExistsError(f"目录已存在，拒绝覆盖：{directory}。请使用新的演示目录。")
    if directory == ROOT or directory == ROOT / "backend":
        raise ValueError("演示目录不能是项目源码目录")
    directory.mkdir(parents=True, mode=0o700)
    try:
        ops = OpsStore(directory / "operations.sqlite")
        tasks = TaskStore(directory / "action_tasks.sqlite")
        ops.init()
        tasks.init()
        # Append to the standard synthetic seed exclusively in this new DB.
        promised_at = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        with ops.transaction() as con:
            con.execute("INSERT INTO products(sku_id,name) VALUES (?,?)", (SKU, "合成设备套件（P2-C.2）"))
            con.executemany(
                "INSERT INTO inventory(warehouse_id,sku_id,on_hand,reserved) VALUES (?,?,?,0)",
                (("WH-WEST", SKU, 8), ("WH-EAST", SKU, 4)),
            )
            for _, order_id, incident_id, quantity, severity, summary in SCENARIOS:
                con.execute(
                    "INSERT INTO orders(order_id,sku_id,quantity,status,warehouse_id,promised_at) "
                    "VALUES (?,?,?,'pending',NULL,?)",
                    (order_id, SKU, quantity, promised_at),
                )
                con.execute(
                    "INSERT INTO incidents(incident_id,order_id,kind,severity,state,summary) "
                    "VALUES (?,?,'fulfillment_risk',?,'open',?)",
                    (incident_id, order_id, severity, summary),
                )

        actor = ActionCoordinator(ops, tasks)
        fulfill = FulfillmentCoordinator(ops, tasks)
        result = {}

        def propose_preferred(incident_id: str, desired_warehouse: str) -> dict:
            incident = ops.get_incident(incident_id)
            quote = ops.fulfillment_quote(incident["order_id"])
            if quote["recommended_warehouse_id"] == desired_warehouse:
                return actor.propose(incident_id)
            return actor.propose(
                incident_id, warehouse_id=desired_warehouse,
                selection_reason="合成演示情境：人工选择同样满足库存条件的仓库",
            )

        # Normal: independent inventory and fulfillment approvals, explicit demo
        # delivery attestation, then independently verified incident closure.
        normal = propose_preferred("INC-DEMO-NORMAL", "WH-EAST")
        n_id = normal["task_id"]
        tasks.review(n_id, True)
        if actor.run(n_id)["status"] != "succeeded":
            raise RuntimeError("Normal reservation was not verified")
        fulfill.prepare(n_id)
        fulfill.review(n_id, True)
        fulfill.advance(n_id)  # dispatch
        fulfill.advance(n_id)  # in_transit
        fulfill.advance(n_id, confirm_delivery=True)  # synthetic attestation
        final = fulfill.advance(n_id)  # verify
        if final["fulfillment"]["status"] != "completed":
            raise RuntimeError("Normal fulfillment did not complete")
        result["normal"] = _snapshot(ops, tasks, n_id, "INC-DEMO-NORMAL",
                                      "独立核验完成；可在复盘查看审计证据")

        # Rejection: proposed task must not alter order or reserve stock.
        rejected = propose_preferred("INC-DEMO-REJECT", "WH-WEST")
        r_id = rejected["task_id"]
        if tasks.review(r_id, False)["status"] != "rejected":
            raise RuntimeError("Rejection was not persisted")
        result["rejected"] = _snapshot(ops, tasks, r_id, "INC-DEMO-REJECT",
                                        "审批已拒绝；订单保持待处理，没有写入库存")

        # Recovery: reproduce a failure AFTER durable dispatch. The workflow is
        # left 'executing' on purpose, while the real business receipt exists.
        recovery = propose_preferred("INC-DEMO-RECOVER", "WH-WEST")
        c_id = recovery["task_id"]
        tasks.review(c_id, True)
        if actor.run(c_id)["status"] != "succeeded":
            raise RuntimeError("Recovery reservation was not verified")
        fulfill.prepare(c_id)
        fulfill.review(c_id, True)
        original_finish = fulfill._finish

        def simulate_crash(*args, **kwargs):
            raise RuntimeError("P2 demo: simulated crash AFTER durable dispatch")

        fulfill._finish = simulate_crash
        try:
            fulfill.advance(c_id)
        except RuntimeError as exc:
            if "simulated crash AFTER durable dispatch" not in str(exc):
                raise
        else:
            raise RuntimeError("Expected simulated interruption did not happen")
        finally:
            fulfill._finish = original_finish
        incident = ops.get_incident("INC-DEMO-RECOVER")
        current = tasks.get(c_id)
        if (incident["order_status"] != "shipped" or
                current["fulfillment"]["status"] != "executing" or
                current["fulfillment"]["inflight_step"] != "dispatch"):
            raise RuntimeError("Recovery checkpoint differs from persisted business state")
        result["recovery"] = _snapshot(ops, tasks, c_id, "INC-DEMO-RECOVER",
                                        "服务端发运已入账；待通过原任务恢复并核对回执，禁止重新发货")

        with ops._connection() as con:
            inventory = [dict(r) for r in con.execute(
                "SELECT warehouse_id,on_hand,reserved,on_hand-reserved AS available "
                "FROM inventory WHERE sku_id=? ORDER BY warehouse_id", (SKU,))]
            count = con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        manifest = {
            "version": "P2-C.2", "synthetic": True,
            "warning": "仅本地合成数据，配送、签收均为模拟，不代表真实物流或客户数据。",
            "scenario_directory": str(directory),
            "shared_sku": SKU, "initial_stock": {"WH-EAST": 4, "WH-WEST": 8},
            "inventory_after": inventory, "business_audit_rows": count,
            "scenarios": result,
            "ui": "http://localhost:8081",
        }
        (directory / "scenario_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return manifest
    except BaseException:
        # Only delete the directory which this invocation just created.
        rmtree(directory)
        raise


def _snapshot(ops: OpsStore, tasks: TaskStore, task_id: str, incident_id: str,
              next_action: str) -> dict:
    incident = ops.get_incident(incident_id)
    task = tasks.get(task_id)
    return {
        "incident_id": incident_id, "order_id": incident["order_id"], "task_id": task_id,
        "incident_state": incident["state"], "order_status": incident["order_status"],
        "shipment_status": incident["shipment_status"], "task_status": task["status"],
        "fulfillment_status": (task.get("fulfillment") or {}).get("status"),
        "next_action": next_action,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a new isolated P2-C.2 demo dataset")
    parser.add_argument("--output", type=Path, required=True,
                        help="NEW empty-target path (existing paths are rejected)")
    args = parser.parse_args()
    try:
        manifest = create_demo(args.output)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"创建失败：{exc}\n")
    print(f"演示数据已生成：{manifest['scenario_directory']}")
    for label, item in manifest["scenarios"].items():
        print(f"  {label:<9} {item['incident_id']:<19} {item['order_status']:<10} {item['task_id']}")
    print("该脚本没有启动容器，也没有访问现有运营数据库。")


if __name__ == "__main__":
    main()
