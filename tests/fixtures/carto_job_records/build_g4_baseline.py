"""Freeze the independent JR2-base definitions for the G4 differential.

Only git's 157041c source is read; neither current writer nor fixture builder is
imported. The test runner's ext4 mirror intentionally has no .git directory.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import textwrap

BASE = "157041c56d51a063018c22e00efa93c48d2a6c57"
ROOT = Path(__file__).resolve().parents[3]
OUTPUT = Path(__file__).with_name("g4_baseline.json")


def build():
    source = subprocess.check_output(["git", "show", f"{BASE}:src/workflowy_mcp/client/api_client_nexus.py"], cwd=ROOT, text=True)
    owner = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == "WorkFlowyClientNexus")
    result = {"commit": BASE, "definitions": {}}
    for name in ("refresh_folder_cartographer_sync", "refresh_file_node_beacons"):
        node = next(node for node in owner.body if getattr(node, "name", None) == name)
        text = textwrap.dedent("".join(source.splitlines(keepends=True)[node.lineno - 1:node.end_lineno]))
        result["definitions"][name] = {"source": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    server = subprocess.check_output(["git", "show", f"{BASE}:src/workflowy_mcp/server.py"], cwd=ROOT, text=True)
    result["server_definitions"] = {}
    for name in ("_carto_task_terminal_payload", "_carto_terminal_payload_data"):
        node = next(node for node in ast.parse(server).body if getattr(node, "name", None) == name)
        text = "".join(server.splitlines(keepends=True)[node.lineno - 1:node.end_lineno])
        result["server_definitions"][name] = {"source": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    return json.dumps(result, indent=2, ensure_ascii=True) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = build()
    if args.check:
        assert OUTPUT.read_text(encoding="utf-8") == content, "G4 baseline fixture drifted from 157041c"
        print("G4 baseline matches 157041c")
    else:
        OUTPUT.write_text(content, encoding="utf-8")
        print(OUTPUT)
