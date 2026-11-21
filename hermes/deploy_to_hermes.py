#!/usr/bin/env python3
"""Safe deployer to update Reddit Monitor skill and runtime for Hermes Agent.

Features:
- Supports `--dry-run` preview without touching disk.
- Creates timestamped backups before replacing skill or runtime files.
- Strictly refuses to overwrite `REDDIT_MONITOR_DIR` or `state.json`.
- Verifies Python syntax and runs the offline test suite after deployment.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

HERMES_BUNDLE_ROOT = Path(__file__).resolve().parent
SRC_SKILL_DIR = HERMES_BUNDLE_ROOT / "skill"
SRC_RUNTIME_DIR = HERMES_BUNDLE_ROOT / "runtime"
RUNTIME_CORE_FILES = (
    "file_lock.py",
    "reddit_monitor_tick_noagent.py",
    "reddit_inbox_state.py",
)


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _check_not_state_dir(target: Path, label: str) -> None:
    env_state_dir = os.getenv("REDDIT_MONITOR_DIR", "").strip()
    if env_state_dir and target.resolve() == Path(env_state_dir).resolve():
        raise ValueError(f"{label} ({target}) points to REDDIT_MONITOR_DIR; refusing to overwrite production state")
    if (target / "state.json").exists() or (target / "review-cache").exists() or (target / "inbox-cache").exists():
        raise ValueError(
            f"{label} ({target}) contains live state files (state.json / cache); "
            "keep runtime code separate from REDDIT_MONITOR_DIR"
        )


def deploy(
    *,
    skill_dest: Path,
    runtime_dest: Path | None,
    backup_root: Path,
    dry_run: bool,
    run_tests: bool,
) -> int:
    if not (SRC_SKILL_DIR / "SKILL.md").is_file():
        raise FileNotFoundError(f"source skill missing SKILL.md: {SRC_SKILL_DIR}")
    for fname in RUNTIME_CORE_FILES:
        if not (SRC_RUNTIME_DIR / fname).is_file():
            raise FileNotFoundError(f"source runtime missing {fname}: {SRC_RUNTIME_DIR}")

    _check_not_state_dir(skill_dest, "skill_dest")
    if runtime_dest is not None:
        _check_not_state_dir(runtime_dest, "runtime_dest")
        if runtime_dest.resolve() == skill_dest.resolve():
            raise ValueError("runtime_dest and skill_dest must be distinct directories")

    stamp = _timestamp()
    plan_lines = [
        f"[PLAN] Timestamp       : {stamp}",
        f"[PLAN] Source skill    : {SRC_SKILL_DIR}",
        f"[PLAN] Target skill    : {skill_dest}",
    ]
    if runtime_dest is not None:
        plan_lines.append(f"[PLAN] Source runtime  : {SRC_RUNTIME_DIR}")
        plan_lines.append(f"[PLAN] Target runtime  : {runtime_dest} ({', '.join(RUNTIME_CORE_FILES)})")
    else:
        plan_lines.append("[PLAN] Target runtime  : (skipped; pass --runtime-dir to update cron scripts)")
    plan_lines.append(f"[PLAN] Backup root     : {backup_root / stamp}")
    print("\n".join(plan_lines))

    if dry_run:
        print("[DRY-RUN] No files were modified.")
        return 0

    session_backup = backup_root / stamp

    # 1. Backup & replace skill directory
    if skill_dest.exists():
        skill_backup = session_backup / "skill_reddit-monitor"
        skill_backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_dest, skill_backup, symlinks=True)
        print(f"[BACKUP] Saved existing skill -> {skill_backup}")
        shutil.rmtree(skill_dest)

    skill_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        SRC_SKILL_DIR,
        skill_dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".persist-fixture-*"),
    )
    print(f"[OK] Updated Hermes skill -> {skill_dest}")

    # 2. Backup & update runtime scripts (if requested)
    if runtime_dest is not None:
        runtime_dest.mkdir(parents=True, exist_ok=True)
        existing_runtime = [runtime_dest / f for f in RUNTIME_CORE_FILES if (runtime_dest / f).exists()]
        if existing_runtime:
            rt_backup = session_backup / "runtime"
            rt_backup.mkdir(parents=True, exist_ok=True)
            for path in existing_runtime:
                shutil.copy2(path, rt_backup / path.name)
            print(f"[BACKUP] Saved existing runtime scripts -> {rt_backup}")

        for fname in RUNTIME_CORE_FILES:
            dst_file = runtime_dest / fname
            shutil.copy2(SRC_RUNTIME_DIR / fname, dst_file)
            if os.name != "nt" and fname != "file_lock.py":
                dst_file.chmod(0o755)
            py_compile.compile(str(dst_file), doraise=True)
        print(f"[OK] Updated runtime scripts -> {runtime_dest}")

    # 3. Compile check template & run offline test suite
    template_py = skill_dest / "scripts" / "scheduled_tick_persist_template.py"
    py_compile.compile(str(template_py), doraise=True)

    if run_tests:
        print("[VERIFY] Running offline unit test suite...")
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(SRC_RUNTIME_DIR), "-p", "test_*.py"],
            cwd=str(HERMES_BUNDLE_ROOT.parent),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            sys.stderr.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            raise RuntimeError("offline test suite failed after deployment")
        summary_line = [line for line in proc.stderr.splitlines() if line.startswith("Ran ") or line.startswith("OK")]
        print(f"[VERIFY] {' | '.join(summary_line)}")

    print("[DONE] Hermes Reddit Monitor update completed successfully.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely update Reddit Monitor skill & runtime for Hermes Agent.")
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=Path.home() / ".hermes",
        help="Hermes home directory (default: ~/.hermes)",
    )
    parser.add_argument(
        "--skill-dir",
        type=Path,
        default=None,
        help="Explicit target skill path (overrides <hermes-home>/skills/reddit-monitor)",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=None,
        help="Directory where Hermes Cron executes reddit_monitor_tick_noagent.py / reddit_inbox_state.py",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help="Directory to store timestamped backups (default: <hermes-home>/backups/reddit-monitor)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned actions without modifying files")
    parser.add_argument("--skip-tests", action="store_true", help="Skip offline unittest verification")
    args = parser.parse_args(argv)

    hermes_home = args.hermes_home.expanduser()
    skill_dest = (args.skill_dir.expanduser() if args.skill_dir else hermes_home / "skills" / "reddit-monitor")
    runtime_dest = args.runtime_dir.expanduser() if args.runtime_dir else None
    backup_root = (args.backup_dir.expanduser() if args.backup_dir else hermes_home / "backups" / "reddit-monitor")

    return deploy(
        skill_dest=skill_dest,
        runtime_dest=runtime_dest,
        backup_root=backup_root,
        dry_run=args.dry_run,
        run_tests=not args.skip_tests,
    )


if __name__ == "__main__":
    sys.exit(main())
