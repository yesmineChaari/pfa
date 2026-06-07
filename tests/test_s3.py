"""Run Phase 3 LLM on all S3 buckets, save output + persist to Neon DB. No PR created."""
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
from shared.persistence import load_phase1_s3_outputs, save_phase3_outputs
from agent2.phase3.llm_phase3 import run_phase3_llm

EC2_RUN_ID = int(os.environ.get("AGENT2_RUN_ID", "17"))
S3_RUN_ID = int(os.environ.get("AGENT2_S3_RUN_ID", "41"))
OUTPUT_FILE = PROJECT_ROOT / "output" / "phase3_s3.json"


async def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    conn = await connect_database(database_url)

    s3_results = await load_phase1_s3_outputs(conn, S3_RUN_ID)
    print(f"[info] Found {len(s3_results)} S3 buckets in run_id={S3_RUN_ID}: {[r.get('bucket_name') for r in s3_results]}")

    # Disable PR creation for this verification run
    os.environ["PHASE3_CREATE_PR"] = "0"

    result = await asyncio.to_thread(
        run_phase3_llm,
        [],           # no EC2
        [],           # no EC2 phase2
        s3_results,
    )

    # Persist to Neon DB
    await conn.close()
    conn = await connect_database(database_url)
    await save_phase3_outputs(conn, S3_RUN_ID, result, phase2_results=[], s3_results=s3_results)
    await conn.close()
    print("[info] Saved to Neon DB")

    OUTPUT_FILE.parent.mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"[done] Output saved → {OUTPUT_FILE}")

    print("\n--- Per-bucket results ---")
    for run in result.get("runs", []):
        llm = run.get("llm", {})
        parsed = llm.get("parsed") or {}
        print(
            f"  {run.get('bucket_name', '?'):35s}  "
            f"verdict={parsed.get('verdict', '?'):10s}  "
            f"action={parsed.get('decision_summary', {}).get('action', '?'):15s}  "
            f"tf={parsed.get('terraform_action', '?')}  "
            f"model={run['model_used']}"
        )


if __name__ == "__main__":
    asyncio.run(main())
