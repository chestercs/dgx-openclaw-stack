#!/usr/bin/env python3
"""Archive retained JSONL sources only after verifying their imported history.

Run against a stopped, copied state directory after OpenClaw 2026.9.7 Doctor.
Existing .migrated files and the original deployment are never overwritten.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys


def main():
    state, archive = (Path(value).resolve() for value in sys.argv[1:])
    sources = sorted((state / "cron" / "runs").glob("*.jsonl"))
    if not sources:
        print("No retained legacy cron logs need archiving.")
        return
    if archive == state or state in archive.parents:
        raise SystemExit("The recovery archive must be outside the active state copy.")
    database = state / "state" / "openclaw.sqlite"
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    plan = []
    for source in sources:
        content = source.read_bytes()
        entries = [json.loads(line) for line in content.splitlines() if line.strip()]
        for entry in entries:
            job_id = entry.get("jobId", source.stem)
            if entry.get("action") != "finished" or job_id != source.stem:
                raise SystemExit("Unrecognized cron history input; source retained: " + source.name)
            rows = connection.execute(
                "SELECT terminal_summary, error, child_session_key, detail_json "
                "FROM task_runs WHERE runtime='cron' AND source_id=? AND ended_at=?",
                (job_id, entry["ts"]),
            ).fetchall()

            def matches(row):
                detail = json.loads(row[3])
                for key, value in entry.items():
                    if key in ("action", "jobId", "ts"):
                        continue
                    stored = row[2] if key == "sessionKey" else detail.get(key)
                    if stored != value:
                        return False
                return row[0] == entry.get("summary") and row[1] == entry.get("error")

            if not any(matches(row) for row in rows):
                raise SystemExit("Cron history is not fully mirrored in SQLite; source retained: " + source.name)
        destination = archive / source.name
        if destination.exists():
            raise SystemExit("Recovery archive already exists; refusing overwrite: " + source.name)
        plan.append({"source": str(source), "archive": str(destination), "sha256": hashlib.sha256(content).hexdigest(), "rows": len(entries)})
    connection.close()
    archive.mkdir(parents=True, mode=0o700, exist_ok=True)
    # All records must verify before the first source is moved.
    for item in plan:
        if hashlib.sha256(Path(item["source"]).read_bytes()).hexdigest() != item["sha256"]:
            raise SystemExit("Cron source changed during verification; refusing archive.")
        os.rename(item["source"], item["archive"])
    with (archive / "manifest.json").open("w") as output:
        json.dump({"verified_sources": plan}, output, indent=2)
    print("Archived", len(plan), "verified legacy cron files with", sum(item["rows"] for item in plan), "SQLite-mirrored records; all original bytes retained.")


if __name__ == "__main__":
    main()
