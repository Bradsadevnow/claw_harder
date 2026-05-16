from __future__ import annotations

import json
import uuid
from pathlib import Path
from time import time

from .seed_runtime import SeedRuntime, SeedCandidate
from .simulation import SimulationProcessor
from .agent_profile import load_agent_profile


def migrate_identity(
    profile_path: Path,
    log_path: Path,
) -> None:
    print(f"Starting identity migration from {profile_path}...")
    
    # 1. Load the structured profile
    profile_data = json.loads(profile_path.read_text(encoding="utf-8"))
    
    # 2. Map profile fields to identity traits
    traits_to_propose = []
    
    if "name" in profile_data:
        traits_to_propose.append(("name", profile_data["name"]))
    if "purpose" in profile_data:
        traits_to_propose.append(("purpose", profile_data["purpose"]))
    if "voice" in profile_data:
        v = profile_data["voice"]
        if "style" in v:
            traits_to_propose.append(("voice_style", v["style"]))
        if "avoid" in v:
            traits_to_propose.append(("voice_avoid", v["avoid"]))
    if "values" in profile_data:
        traits_to_propose.append(("values", profile_data["values"]))
    if "constraints" in profile_data:
        traits_to_propose.append(("constraints", profile_data["constraints"]))
        
    run_id = f"migration_{uuid.uuid4().hex[:8]}"
    
    # 3. Write proposal events directly to log
    with open(log_path, "a", encoding="utf-8") as f:
        seq = 0
        for trait_type, content in traits_to_propose:
            seq += 1
            item = {
                "id": f"{trait_type}_{uuid.uuid4().hex[:10]}",
                "type": trait_type,
                "content": content,
                "confidence": 1.0,
                "status": "proposed",
                "confirmed_at": time(),
                "updated_at": time(),
                "source_refs": ["cli_migration_script"],
            }
            event = {
                "run_id": run_id,
                "cycle": 0,
                "seq": seq,
                "kind": "seed.identity_proposed",
                "module": "seed",
                "level": "info",
                "msg": f"Identity memory proposed: {content}",
                "details": {"type": trait_type, "item": item},
                "timestamp": time(),
                "event_id": str(uuid.uuid4()),
            }
            f.write(json.dumps(event) + "\n")
            print(f"Proposed trait: {trait_type} = {content}")

    # 4. Run SimulationProcessor to confirm the traits
    print("Running SimulationProcessor to consolidate traits...")
    processor = SimulationProcessor(log_path)
    results = processor.run()
    
    for res in results:
        print(f"Confirmed trait: {res.trait}")
        
    print("Migration complete. Identity is now event-sourced.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="HSP/runtime_profile.json")
    parser.add_argument("--log", default=".runtime/events.jsonl")
    parser.add_argument("--state", default=".runtime/state.json")
    parser.add_argument("--mcp", default="mcp_servers.example.json")
    args = parser.parse_args()
    
    migrate_identity(
        Path(args.profile),
        Path(args.log),
    )
