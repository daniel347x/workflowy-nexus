#!/usr/bin/env python3
"""Reversible mutation checks for the S1 ``include_rows`` tests.

A test that cannot fail proves nothing.  This script applies four deliberate
breakages to a THROWAWAY copy of the tree (never a worktree that someone is
editing), runs the relevant test ids, and restores the file.  Each mutation
must turn at least one of the named tests red.

USAGE (create the disposable tree first, e.g.)
    git worktree add --detach /mnt/c/tmp/s1-mut <commit-with-your-changes>
    python tests/fixtures/glimpse-include-rows/mutation_check.py \
        --tree /mnt/c/tmp/s1-mut

It refuses to touch the worktree it lives in unless ``--allow-worktree`` is
passed (do not pass it while an implementer is editing).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE_TREE = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SERVER_REL = os.path.join("src", "workflowy_mcp", "server.py")
TEST_REL = os.path.join("tests", "test_glimpse_include_rows.py")

# (name, exact old text, replacement, pytest -k expression)
MUTATIONS = [
    (
        "no_optin_threading",
        "            include_rows=params.get(\"include_rows\", False),\n",
        "",
        "test_markdown_envelope_carries_exact_legacy_string or "
        "test_anchor_dock_render_entry_returns_the_same_envelope",
    ),
    (
        "no_cache_by_id_timestamps",
        "client._build_modal_rows(_fr, cache_by_id=cache_by_id)",
        "client._build_modal_rows(_fr)",
        "test_rows_match_modal_authority or "
        "test_every_row_timestamp_comes_from_the_cache_by_id_join",
    ),
    (
        "no_resolved_spec_echo",
        "        payload[\"_resolved_render_spec\"] = spec.to_render_spec_blob()\n",
        "",
        "test_markdown_envelope_carries_exact_legacy_string or "
        "test_render_spec_echo_round_trips_through_the_authority_serializer",
    ),
    (
        "recompose_drops_header_or_advisory",
        "                \"markdown\": stub_empty_text_result(_md),",
        "                \"markdown\": stub_empty_text_result("
        "result.get(\"preview_markdown\") or \"\"),",
        "test_markdown_envelope_carries_exact_legacy_string and "
        "(raw_root_recent or raw_root_advisory)",
    ),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", required=True)
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--allow-worktree", action="store_true")
    ap.add_argument("--report-dir")
    args = ap.parse_args()
    tree = os.path.abspath(args.tree)
    if tree == os.path.abspath(LIVE_TREE) and not args.allow_worktree:
        raise SystemExit(
            "refusing to mutate the live worktree; make a throwaway copy/worktree"
        )
    server = os.path.join(tree, SERVER_REL)
    test = os.path.join(tree, TEST_REL)
    if not (os.path.isfile(server) and os.path.isfile(test)):
        raise SystemExit(f"{tree!r} does not look like the S1 tree")

    backup = server + ".mutation-backup"
    shutil.copy2(server, backup)
    failures = []
    reports = []
    report_dir = os.path.abspath(args.report_dir or os.path.join(tree, "tests", ".runs", "include_rows_mutations"))
    os.makedirs(report_dir, exist_ok=True)
    source_sha256 = hashlib.sha256(open(server, "rb").read()).hexdigest()
    test_sha256 = hashlib.sha256(open(test, "rb").read()).hexdigest()
    try:
        for name, old, new, expr in MUTATIONS:
            if args.only and name not in args.only:
                continue
            text = open(server, "r", encoding="utf-8").read()
            count = text.count(old)
            if count == 0:
                failures.append((name, "selector not found (implementation moved?)"))
                print(f"[{name}] SELECTOR MISSING")
                continue
            open(server, "w", encoding="utf-8").write(text.replace(old, new, 1))
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", test, "-q", "-k", expr],
                cwd=tree,
                capture_output=True,
                text=True,
            )
            # An import/collection failure is not mutation sensitivity.
            red = proc.returncode == 1 and "FAILED " in proc.stdout
            log = os.path.join(report_dir, name + ".log")
            with open(log, "w", encoding="utf-8") as fh:
                fh.write(proc.stdout + "\n" + proc.stderr)
            reports.append({"name": name, "exit": proc.returncode,
                            "killed": red, "selection": expr, "log": log})
            print(f"[{name}] {'RED as expected' if red else 'STILL GREEN -- NOT SENSITIVE'}")
            print((proc.stdout or "").splitlines()[-1] if proc.stdout else "")
            if not red:
                failures.append((name, "tests stayed green"))
            shutil.copy2(backup, server)
    finally:
        shutil.copy2(backup, server)
        os.remove(backup)
    with open(os.path.join(report_dir, "report.json"), "w", encoding="utf-8") as fh:
        json.dump({"source_sha256": source_sha256, "test_sha256": test_sha256,
                   "mutations": reports, "failures": failures}, fh, indent=2)
        fh.write("\n")
    if failures:
        print("\nMUTATION FAILURES:")
        for name, why in failures:
            print(f"  {name}: {why}")
        return 1
    print("\nall requested mutations turned the relevant tests red")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
