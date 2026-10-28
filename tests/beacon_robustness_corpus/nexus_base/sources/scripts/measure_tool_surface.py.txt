#!/usr/bin/env python3
"""Measure legacy versus consolidated FastMCP prompt surface."""

from __future__ import annotations

import json
import sys

from workflowy_mcp.tool_surface_measurement import measure_profiles


def main() -> int:
    report = measure_profiles(python_executable=sys.executable)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
