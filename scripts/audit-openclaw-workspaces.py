#!/usr/bin/env python3
"""Verify workspace bytes and the specific upstream prompt migrations."""
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tarfile


def digest(filename):
    result = hashlib.sha256()
    with open(filename, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def main():
    directory = Path(sys.argv[1]).resolve()
    mode = sys.argv[2]
    root = (directory / (sys.argv[3] if len(sys.argv) > 3 else "state")).resolve()
    if directory not in root.parents or mode not in ("snapshot", "verify"):
        raise SystemExit("Expected snapshot/verify and a state copy inside the upgrade directory.")
    manifest = directory / "workspace-files.before.json"
    files = {}
    for workspace in root.iterdir():
        if not workspace.name.startswith("workspace") or not workspace.is_dir():
            continue
        for parent, dirs, names in os.walk(workspace):
            for name in names:
                filename = Path(parent) / name
                files[str(filename.relative_to(root))] = {"link": os.readlink(filename)} if filename.is_symlink() else {"sha256": digest(filename)}
    if mode == "snapshot":
        manifest.write_text(json.dumps(files, indent=2))
        print("Workspace preservation baseline:", len(files), "files")
        return
    before = json.loads(manifest.read_text())
    changed = [name for name, value in before.items() if files.get(name) != value]
    relocated, migrated, failures = {}, {}, []
    archived_markdown = {}
    for filename in glob.glob(str(root / "backups" / "**" / "*.md"), recursive=True):
        if os.path.isfile(filename):
            archived_markdown.setdefault(digest(filename), []).append(filename)
    config = json.loads((root / "openclaw.json").read_text())
    defaults = config.get("agents", {}).get("defaults", {})
    agents = config.get("agents", {}).get("entries", {})
    database = None
    ignored_template_lines = {
        "- When you learn a lesson → update AGENTS.md, TOOLS.md, or the relevant skill",
        "Skills provide your tools. When you need one, check its `SKILL.md`. Keep local notes (camera names, SSH details, voice preferences) in `TOOLS.md`.",
    }

    def heartbeat_preserved(workspace, text):
        nonlocal database
        ids = [agent_id for agent_id, entry in agents.items() if (entry.get("workspace") or defaults.get("workspace")) == "/home/node/.openclaw/" + workspace]
        if not ids:
            return False
        if database is None:
            database = sqlite3.connect((root / "state" / "openclaw.sqlite").as_uri() + "?mode=ro", uri=True)
        rows = database.execute(
            "SELECT j.agent_id,j.name,j.job_json,s.content FROM cron_jobs j "
            "LEFT JOIN cron_job_scratch s ON s.store_key=j.store_key AND s.job_id=j.job_id"
        ).fetchall()
        owned = [(name, json.loads(job), scratch) for agent, name, job, scratch in rows if agent in ids]
        scratches = [scratch for name, job, scratch in owned if job.get("payload", {}).get("kind") == "heartbeat" and scratch is not None]
        if not scratches:
            return False
        if "tasks:" not in text:
            return any(value.strip() == text.strip() for value in scratches)
        # Refuse unsupported legacy schedule syntax instead of guessing.
        parts = re.split(r"(?m)^\s*-\s+name:\s*", text)[1:]
        for part in parts:
            name = part.splitlines()[0].strip().strip("\"'")
            interval = re.search(r"(?m)^\s+interval:\s*(\d+)(ms|s|m|h|d)\s*$", part)
            if not interval:
                return False
            milliseconds = int(interval[1]) * {"ms": 1, "s": 1000, "m": 60000, "h": 3600000, "d": 86400000}[interval[2]]
            if not any(job_name == name and job.get("schedule", {}).get("everyMs") == milliseconds for job_name, job, scratch in owned):
                return False
        if not parts:
            return False
        effective = "\n".join(scratches + [str(job.get("payload", {}).get(key, "")) for name, job, scratch in owned for key in ("text", "message")])
        return all(line.strip() in effective for line in text.splitlines() if line.strip() and not re.match(r"^\s*(tasks:|-\s*name:|interval:|lastRun:|prompt:)", line))

    with tarfile.open(directory / "state.tar") as archive:
        for name in changed:
            value = before[name]
            matches = [path for path, current in files.items() if current == value and path.split("/")[0] == name.split("/")[0]]
            if name not in files and matches:
                relocated[name] = matches
                continue
            original = archive.extractfile("./" + name).read()
            if hashlib.sha256(original).hexdigest() != value.get("sha256"):
                failures.append(name)
                continue
            workspace, basename = name.split("/", 1)
            reason = None
            if basename == "AGENTS.md" and (root / name).is_file():
                old_lines = [line.strip() for line in original.decode().splitlines() if line.strip() and line.strip() not in ignored_template_lines]
                new_lines = iter(line.strip() for line in (root / name).read_text().splitlines())
                if all(any(current == line for current in new_lines) for line in old_lines) and "## Tools" in (root / name).read_text():
                    reason = "original instruction lines retained; Tools appended and standard retired TOOLS.md references updated"
            elif basename == "TOOLS.md" and name not in files:
                target = root / workspace / "AGENTS.md"
                if value["sha256"] in archived_markdown and original.decode().strip() in target.read_text():
                    reason = "full tool notes retained in AGENTS.md and byte-identical upstream archive"
            elif basename == "HEARTBEAT.md" and name not in files:
                if value["sha256"] in archived_markdown and heartbeat_preserved(workspace, original.decode()):
                    reason = "instructions and legacy task cadences verified in monitor scratch/jobs; byte-identical upstream archive retained"
            elif workspace == "workspace-attestations" and basename.endswith(".attested") and name not in files:
                reason = "upstream workspace setup metadata migrated; original bytes retained"
            if reason is None:
                failures.append(name)
                continue
            destination = directory / "workspace-originals" / name
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            if destination.exists():
                if destination.read_bytes() != original:
                    raise SystemExit("Refusing to overwrite original workspace archive: " + name)
            else:
                destination.write_bytes(original)
                destination.chmod(0o600)
            migrated[name] = {"reason": reason, "original": str(destination), "sha256": value["sha256"]}
    if database is not None:
        database.close()
    report = {"original_files": len(before), "current_files": len(files), "relocated": relocated, "verified_migrations": migrated, "changed_or_missing": failures}
    (directory / "workspace-preservation.json").write_text(json.dumps(report, indent=2))
    if failures:
        raise SystemExit("Workspace content changed; refusing activation. Review workspace-preservation.json: " + ", ".join(failures[:20]))
    print("Workspace content preserved:", len(before), "original files;", len(relocated), "byte-identical moves;", len(migrated), "verified upstream migrations")


if __name__ == "__main__":
    main()
