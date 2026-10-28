"""Best-effort immutable source identity in build artifacts, never the checkout."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


def _valid_stamp(values: dict[str, Any]) -> bool:
    commit, stamp_time = values.get("SOURCE_COMMIT"), values.get("SOURCE_COMMIT_TIME")
    return (
        type(values.get("SOURCE_IDENTITY_SCHEMA")) is int and values["SOURCE_IDENTITY_SCHEMA"] == 1
        and values.get("SOURCE_IDENTITY_KIND") in {"git", "direct_url", "editable", "dirty", "unknown"}
        and (commit is None or isinstance(commit, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) is not None)
        and (stamp_time is None or type(stamp_time) is int and stamp_time >= 0)
        and (commit is not None or stamp_time is None)
    )


def _preserved_stamp(root: Path) -> dict[str, Any] | None:
    source = root / "src" / "workflowy_mcp" / "_build_identity.py"
    if not source.is_file():
        return None
    values: dict[str, Any] = {}
    for statement in ast.parse(source.read_text(encoding="utf-8")).body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
            values[statement.targets[0].id] = ast.literal_eval(statement.value)
        elif not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str)):
            return None
    return values if _valid_stamp(values) else None


def _warn() -> None:
    # Do not echo git stderr, repository URLs, environment or exception payloads.
    # stderr instead of warnings.warn: -Werror must not make metadata fatal.
    try:
        print("WARNING: N06 build source identity unavailable; build continues with honest unknown identity.", file=sys.stderr)
    except Exception:
        pass


def _source_stamp(root: Path) -> dict[str, Any]:
    unknown = {"SOURCE_COMMIT": None, "SOURCE_COMMIT_TIME": None, "SOURCE_IDENTITY_KIND": "unknown", "SOURCE_IDENTITY_SCHEMA": 1}

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True,
            text=True, timeout=90,
        ).stdout.strip()

    try:
        # A source-less fixture inside some other git tree is not that tree's code.
        if Path(git("rev-parse", "--show-toplevel")).resolve() != root.resolve():
            raise ValueError("not the source tree")
        commit = git("rev-parse", "HEAD")
        stamp_time = int(git("show", "-s", "--format=%ct", "HEAD"))
        if git("status", "--porcelain", "--untracked-files=no"):
            _warn()
            return {**unknown, "SOURCE_IDENTITY_KIND": "dirty"}
        values = {**unknown, "SOURCE_COMMIT": commit, "SOURCE_COMMIT_TIME": stamp_time, "SOURCE_IDENTITY_KIND": "git"}
        if not _valid_stamp(values):
            raise ValueError("invalid source metadata")
        return values
    except Exception:
        try:
            preserved = _preserved_stamp(root)
            if preserved is not None:
                return preserved
        except Exception:
            pass
        _warn()
        return unknown


# @beacon[
#   id=daemon-build@custom-build-hook,
#   role=CustomBuildHook: best-effort immutable wheel and sdist source stamp,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
#   comment=Generates source commit and committer time only in temporary build staging; dirty trees are unpinned and no-git sdists retain valid packaged stamps; all hook failures warn without failing the build.,
# ]
class CustomBuildHook(BuildHookInterface):
    """Package source identity without a runtime git dependency or tracked writes."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        self._identity_temp = None
        if self.target_name not in {"wheel", "sdist"} or version == "editable":
            return
        try:
            values = _source_stamp(Path(self.root))
            self._identity_temp = tempfile.TemporaryDirectory(prefix="nexus-build-identity-")
            generated = Path(self._identity_temp.name) / "_build_identity.py"
            generated.write_text(
                '"""Generated immutable source identity; not a runtime control file."""\n'
                + "".join(f"{key} = {value!r}\n" for key, value in values.items()),
                encoding="utf-8",
            )
            destination = "workflowy_mcp/_build_identity.py" if self.target_name == "wheel" else "src/workflowy_mcp/_build_identity.py"
            build_data.setdefault("force_include", {})[str(generated)] = destination
        except Exception:
            _warn()

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        try:
            temporary = getattr(self, "_identity_temp", None)
            if temporary is not None:
                temporary.cleanup()
        except Exception:
            _warn()
