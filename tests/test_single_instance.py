"""Run Phase 3 LLM for a single EC2 instance and save output to a file."""
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
from agent2.phase3.converter import build_ec2_scenario
from agent2.phase3.llm_phase3 import _iac_eval_repo_path, _import_iac_eval, _filter_tf_bundle_for_ec2, _run_with_fallback, _FALLBACK_MODEL
from agent2.phase3.github_terraform import TerraformSource, resolve_terraform_bundle

INSTANCE_NAME = os.environ.get("TEST_INSTANCE", "app1-worker-oversized-risky")
RUN_ID = int(os.environ.get("AGENT2_RUN_ID", "17"))
OUTPUT_FILE = PROJECT_ROOT / "output" / f"phase3_single_{INSTANCE_NAME}.json"


async def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    conn = await connect_database(database_url)

    p1_all = await load_phase1_ec2_outputs(conn, RUN_ID)
    p2_all = await load_phase2_ec2_outputs(conn, RUN_ID)

    p2 = next((r for r in p2_all if r.get("instance_name") == INSTANCE_NAME), None)
    if p2 is None:
        print(f"Instance '{INSTANCE_NAME}' not found in run_id={RUN_ID}")
        print("Available:", [r.get("instance_name") for r in p2_all])
        return

    rid = p2.get("resource_id")
    p1 = next((r for r in p1_all if r.get("resource_id") == rid), None)

    repo_path = _iac_eval_repo_path()
    config, prompt_builder, runners = _import_iac_eval(repo_path)

    models = config.MODELS
    model_key = os.environ.get("PHASE3_MODEL") or next(iter(models))
    model_cfg = models[model_key]
    api_keys = {
        "groq": os.environ.get("GROQ_API_KEY", ""),
        "google": os.environ.get("GOOGLE_API_KEY", ""),
        "mistral": os.environ.get("MISTRAL_API_KEY", ""),
    }
    runner = runners.get_runner(model_cfg, api_keys)

    fallback_cfg = models.get(_FALLBACK_MODEL)
    fallback_runner = runners.get_runner(fallback_cfg, api_keys) if fallback_cfg and model_key != _FALLBACK_MODEL else None

    repo_url = os.environ.get("PHASE3_TERRAFORM_REPO_URL", "")
    repo_ref = os.environ.get("PHASE3_TERRAFORM_REF", "main")
    repo_subdir = os.environ.get("PHASE3_TERRAFORM_SUBDIR", "")
    tf_prompt_bundle = ""
    if repo_url:
        try:
            tf_bundle = resolve_terraform_bundle(TerraformSource(repo_url=repo_url, ref=repo_ref, subdir=repo_subdir))
            tf_prompt_bundle = tf_bundle.prompt_bundle
        except Exception as exc:
            print(f"[warn] Could not fetch Terraform: {exc}")

    app_group = INSTANCE_NAME.split("-")[0]
    ec2_tf = _filter_tf_bundle_for_ec2(tf_prompt_bundle, app_group)

    scenario = build_ec2_scenario(
        [p1] if p1 else [],
        [p2],
        scenario_id=f"A_{INSTANCE_NAME}",
        app_group=app_group.upper(),
        current_terraform=ec2_tf,
    )

    system_prompt, user_prompt = prompt_builder.build_prompt(scenario)

    print(f"\n--- SYSTEM PROMPT ---\n{system_prompt}\n")
    print(f"--- USER PROMPT ---\n{user_prompt}\n")

    llm_result, model_used = _run_with_fallback(
        runner, system_prompt, user_prompt,
        fallback_runner=fallback_runner,
        primary_key=model_key,
        ContextTooLargeError=runners.ContextTooLargeError,
    )

    result = {
        "run_id": RUN_ID,
        "instance_name": INSTANCE_NAME,
        "model_used": model_used,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "llm": llm_result,
    }

    OUTPUT_FILE.parent.mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\n[done] Output saved → {OUTPUT_FILE}")
    print(f"[done] model_used={model_used}  latency={llm_result.get('latency_ms')}ms")
    print(f"\n--- LLM RAW RESPONSE ---\n{llm_result.get('raw_response', '')}")


if __name__ == "__main__":
    asyncio.run(main())
