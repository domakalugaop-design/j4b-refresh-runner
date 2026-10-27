"""Aggregate collected October controller data into controller_report_snapshot format."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

CHECKPOINT_DIR = Path.home() / ".j4b-october-collection"
CHECKPOINT_FILE = CHECKPOINT_DIR / "october_collection_checkpoint.json"
RAW_RESPONSES_DIR = CHECKPOINT_DIR / "raw_responses"

OUTPUT_FILE = CHECKPOINT_DIR / "october_controller_report_snapshot.json"


def _load_existing_fio_mapping() -> dict[str, str]:
    """Load FIO to corrector_id mapping from existing TEST data."""
    # Known mappings from controller_report_snapshot
    return {
        "Мартин Юлия Викторовна": "2345",
        "Злодеева Алена Геннадьевна": "149804",
        "Суханова Дарья Игоревна": "149856",
        "Клинова Дарья Александровна": "136323",
        "Сосипаторова Юлия Викторовна": "136322",
        "Кулагина Любовь Михайловна": "127735",
        "Васильева Елена Михайловна": "146056",
        "Гринцевич Екатерина Евгеньевна": "149030",
        "Литвинова Елена Александровна": "151512",
        "Прокудина Валентина Ивановна": "139301",
        "Королёва Наталья": "99030",  # from visit_registry_history
        "Королева Наталья": "99030",  # alternative spelling
    }


def main() -> int:
    # Load checkpoint
    with open(CHECKPOINT_FILE, encoding="utf-8") as f:
        checkpoint = json.load(f)

    all_facts = []
    project_names = {}
    
    for pid, info in checkpoint["projects"].items():
        if info.get("status") != "COLLECTED":
            continue
        
        facts_file = Path(info["facts_file"])
        if not facts_file.exists():
            print(f"Warning: facts file not found for {pid}: {facts_file}")
            continue
        
        with open(facts_file, encoding="utf-8") as f:
            facts_data = json.load(f)
        
        # Extract project name from first fact
        for fact in facts_data["facts"]:
            if fact.get("project_name_short"):
                project_names[pid] = fact["project_name_short"]
                break
        
        all_facts.extend(facts_data["facts"])

    print(f"Total facts aggregated: {len(all_facts)}")
    print(f"Projects with names: {len(project_names)}")

    # Load existing FIO to ID mapping
    fio_to_id = _load_existing_fio_mapping()
    print(f"Known FIO to ID mappings: {len(fio_to_id)}")

    # Build controller_report_snapshot rows
    generation_id = "oct2026_collection_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    source_snapshot_at = datetime.now(timezone.utc).isoformat()
    
    snapshot_rows = []
    seen_controllers = set()
    
    for fact in all_facts:
        controller_fio = fact.get("controller_fio", "")
        corrector_id = fio_to_id.get(controller_fio, "")
        
        if controller_fio:
            seen_controllers.add(controller_fio)
        
        # Determine workflow status flags
        workflow_codes = fact.get("workflow_state_codes", "")
        if isinstance(workflow_codes, str):
            codes = [c for c in workflow_codes.split("|") if c]
        else:
            codes = list(workflow_codes) if workflow_codes else []
        
        status_flags = {}
        for code in [35, 38, 39, 40, 45, 50]:
            code_str = str(code)
            if code_str in codes:
                status_flags[f"status_{code}"] = "OBSERVED_PRESENT"
            else:
                status_flags[f"status_{code}"] = "OBSERVED_ABSENT"
        
        # Determine source_status
        source_status = "CURRENT" if controller_fio else "NO_CONTROLLER"
        
        row = {
            "publication_key": "october_2026_collection",
            "generation_id": generation_id,
            "report_id": fact.get("action_id", ""),
            "action_id": fact.get("action_id", ""),
            "visit_id": fact.get("visit_id", ""),
            "project_id": fact.get("project_id", ""),
            "project_name": project_names.get(fact.get("project_id", ""), ""),
            "corrector_id": corrector_id,
            "corrector_fio": controller_fio,
            "report_date": fact.get("report_datetime", ""),
            "workflow_state_codes": workflow_codes if isinstance(workflow_codes, str) else "|".join(codes),
            **status_flags,
            "report_url": f"https://lk.j4b.ru/action/{fact.get('action_id', '')}",
            "source_status": source_status,
            "source_snapshot_at": source_snapshot_at,
            "data_coverage_flag": "OBSERVED_CORRECTOR_SNAPSHOT_ONLY",
        }
        snapshot_rows.append(row)

    # Save aggregated data
    output_data = {
        "generation_id": generation_id,
        "source_snapshot_at": source_snapshot_at,
        "total_facts": len(snapshot_rows),
        "projects_covered": len(set(r["project_id"] for r in snapshot_rows)),
        "controllers_observed": len(seen_controllers),
        "controllers_with_id": len([c for c in seen_controllers if c in fio_to_id]),
        "rows": snapshot_rows,
    }
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=1)
    
    print(f"Output saved to: {OUTPUT_FILE}")
    print(f"Generation ID: {generation_id}")
    print(f"Projects covered: {output_data['projects_covered']}")
    print(f"Unique controllers (by FIO): {output_data['controllers_observed']}")
    print(f"Controllers with known ID: {output_data['controllers_with_id']}")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
