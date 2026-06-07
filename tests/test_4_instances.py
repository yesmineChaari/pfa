"""Run Phase 3 full pipeline (including PR creation) for the 4 test instances."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.db import connect_database
from shared.persistence import load_phase1_ec2_outputs, load_phase2_ec2_outputs
from agent2.phase3.llm_phase3 import run_phase3_llm

RUN_ID = int(os.environ.get("AGENT2_RUN_ID", "17"))
OUTPUT_FILE = PROJECT_ROOT / "output" / "phase3_4instances.json"

TARGET_INSTANCES = [
    "app1-worker-oversized-risky",
    "app1-zombie-isolated",
    "app2-stopped-zombie",
    "app2-bursty-oversized",
]


async def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    conn = await connect_database(database_url)

    p1_all = await load_phase1_ec2_outputs(conn, RUN_ID)
    p2_all = await load_phase2_ec2_outputs(conn, RUN_ID)

    p2_filtered = [r for r in p2_all if r.get("instance_name") in TARGET_INSTANCES]
    found = [r["instance_name"] for r in p2_filtered]
    missing = [n for n in TARGET_INSTANCES if n not in found]
    if missing:
        print(f"[warn] Not found in run_id={RUN_ID}: {missing}")
    print(f"[info] Running {len(p2_filtered)} instances: {found}")

    await conn.close()

    result = run_phase3_llm(
        ec2_phase1_results=p1_all,
        ec2_phase2_results=p2_filtered,
        s3_phase1_results=[],
    )

    OUTPUT_FILE.parent.mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\n[done] Output saved → {OUTPUT_FILE}")

    pr = result.get("pull_request", {})
    if pr.get("created"):
        print(f"[PR]   {pr.get('pr_url')}")
        print(f"       branch: {pr.get('branch_name')}")
        print(f"       files:  {pr.get('changed_files')}")
    else:
        print(f"[PR]   not created — {pr.get('reason') or pr.get('errors')}")

    print("\n--- Per-instance results ---")
    for run in result.get("runs", []):
        llm = run.get("llm", {})
        parsed = llm.get("parsed") or {}
        print(
            f"  {run['instance_name']:35s}  "
            f"verdict={parsed.get('verdict', '?'):10s}  "
            f"action={parsed.get('decision_summary', {}).get('action', '?'):12s}  "
            f"tf={parsed.get('terraform_action', '?')}  "
            f"model={run['model_used']}"
        )


if __name__ == "__main__":
    asyncio.run(main())
