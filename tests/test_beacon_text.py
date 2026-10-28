"""Compact snippet diagnostics and readable display copies of full JSON reports."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP
from fastmcp.tools.base import ToolResult
from mcp_types import TextContent

from workflowy_mcp import server
from workflowy_mcp.beacon_snippet import append_snippet_findings, format_beacon_snippet_result
from workflowy_mcp.beacon_text import BeaconFindingsTextMiddleware


def _finding(severity="info", code="empty_comment"):
    return {"severity": severity, "code": code, "line": 12, "beacon_id": "example@read",
            "message": "A useful message.", "finding_id": "bf1:example",
            "fix": {"resolutions": [{"resolution": "keep", "params_schema": {"type": "object"}}]}}


def _read(findings=()):
    return {"resolution_strategy": "beacon", "findings": list(findings),
            "finding_scope": {"complete_file": {"family": "beacon", "verb": "check",
                              "params": {"file_path": "C:/repo/π.py", "min_severity": "info"}}}}


def test_info_only_is_notice_and_one_pointer_without_finding_rows_or_payload():
    read = _read([_finding(), _finding(code="legacy_field")])
    text = append_snippet_findings("source", read)
    lines = text.removeprefix("source\n\n").splitlines()
    assert lines == [
        "⚠ Snippet beacon: 2 finding(s) (empty_comment, legacy_field). Source was not changed.",
        'Full findings and fix resolutions: beacon(verb=\'check\', params={"file_path": "C:/repo/π.py", "min_severity": "info"}) or format=json.',
    ]
    assert "bf1:" not in text and "params_schema" not in text and "BEACON_FINDINGS_JSON" not in text


@pytest.mark.parametrize("severity", ["warning", "error"])
def test_warning_or_higher_has_one_actionable_line_but_no_info_row(severity):
    read = _read([_finding(), _finding(severity, "unknown_field_key")])
    text = append_snippet_findings("source", read)
    assert (f"- {severity} · unknown_field_key · line 12 · example@read · "
            "A useful message. · finding_id bf1:example") in text.splitlines()
    assert "- info" not in text and "params_schema" not in text
    assert len(text.removeprefix("source\n\n").splitlines()) == 3


def test_degraded_read_without_findings_is_still_unmistakable():
    read = {**_read(), "resolution_strategy": "degraded"}
    text = append_snippet_findings("useful source", read)
    assert text.startswith("useful source\n\n⚠ Snippet degraded: 0 finding(s). Source was not changed.")
    assert "format=json" in text


@pytest.mark.parametrize("key,value,expected", [
    ("analysis_error", {"exception": "RuntimeError", "message": "analysis failed\nnext line"},
     "- analysis_error · RuntimeError · analysis failed next line"),
    ("outline_errors", [{"record": "outline_error", "exception": "SyntaxError at line 4"}],
     "- outline_errors · SyntaxError at line 4"),
    ("anchor_degradation", "anchor_not_found", "- anchor_degradation · anchor_not_found"),
])
def test_each_degradation_diagnostic_is_visible_even_without_strategy_or_findings(key, value, expected):
    read = {key: value}
    text = append_snippet_findings("source", read)
    assert "⚠ Snippet read: 0 finding(s)" in text
    assert expected in text.splitlines()
    assert text.endswith("Full findings and fix resolutions: format=json.")
    assert ("; analysis_error" in text) == (key == "analysis_error")


@pytest.mark.parametrize("text", ["", "raw\r\nsource\r\n", "  π\nlast line  "])
def test_clean_read_is_byte_identical(text):
    assert append_snippet_findings(text, _read()) == text


def test_json_projection_keeps_every_original_field_and_no_text_notice():
    read = {**_read([_finding("warning")]), "location": {"anchor": None},
            "finding": _finding("warning"), "observed": {"file_sha256": "abc"},
            "analysis_error": {"exception": "RuntimeError", "message": "oops"},
            "outline_errors": [{"exception": "parse failed"}], "anchor_degradation": "missing"}
    original = copy.deepcopy(read)
    result = format_beacon_snippet_result(read, None, renderer=lambda *_: "raw source", output_format="json")
    assert result == {"text": "raw source", **read}
    assert read == original


def test_full_beacon_reports_are_pretty_only_in_fastmcp_text_copy():
    local = FastMCP("beacon-findings-display", middleware=[BeaconFindingsTextMiddleware()])
    payload = {"operation": "beacon.check", "rows": [_finding("warning")], "message": "π stays readable"}

    @local.tool(name="beacon")
    async def beacon():
        return payload

    async def call():
        async with Client(local) as client:
            return await client.call_tool("beacon", {})

    result = asyncio.run(call())
    assert result.structured_content == payload == asyncio.run(beacon())
    assert result.content[0].text == json.dumps(payload, indent=2, ensure_ascii=False)
    assert json.loads(result.content[0].text) == payload
    assert "π" in result.content[0].text and '"params_schema": {\n' in result.content[0].text
    assert any(isinstance(item, BeaconFindingsTextMiddleware) for item in server.mcp.middleware)


@pytest.mark.parametrize("operation", ["beacon.fix", "beacon.tags.ensure", "beacon.tags.remove", "beacon.sync_node", "beacon.snippet", None])
def test_all_full_findings_surfaces_preserve_structured_data_and_framing(operation):
    payload = ({"operation": operation, "detail": {"dry_run": True, "findings": [_finding()]}}
               if operation else {"text": "raw source", "findings": [_finding()]})
    if operation == "beacon.snippet":
        payload = {"operation": operation, "error": {"code": "source_extraction_failed",
                   "extra": {"findings": [_finding("error")], "observed": {"file_sha256": "abc"}}}}
    result = ToolResult(content=[TextContent(type="text", text=json.dumps(payload)),
                                 TextContent(type="text", text="separate advisory")],
                        structured_content=payload, meta={"witness": "kept"}, is_error=True)

    async def call_next(context):
        return result

    context = SimpleNamespace(message=SimpleNamespace(name="beacon"))
    formatted = asyncio.run(BeaconFindingsTextMiddleware().on_call_tool(context, call_next))
    assert formatted.content[0].text == json.dumps(payload, indent=2, ensure_ascii=False)
    assert formatted.content[1].text == "separate advisory"
    assert formatted.structured_content == result.structured_content == payload
    assert formatted.meta == result.meta and formatted.is_error == result.is_error
    assert result.content[0].text == json.dumps(payload)  # no in-place edits


@pytest.mark.parametrize("name,payload,text", [
    ("node", {"operation": "beacon.check"}, '{"operation": "beacon.check"}'),
    ("beacon", {"operation": "beacon.resolve"}, '{"operation": "beacon.resolve"}'),
    ("beacon", None, "source\n⚠ Snippet degraded: 0 finding(s)."),
    ("beacon", {"operation": "beacon.check"}, "custom text, not the display copy"),
])
def test_unrelated_and_custom_text_stays_byte_identical(name, payload, text):
    result = ToolResult(content=[TextContent(type="text", text=text)], structured_content=payload)

    async def call_next(context):
        return result

    context = SimpleNamespace(message=SimpleNamespace(name=name))
    formatted = asyncio.run(BeaconFindingsTextMiddleware().on_call_tool(context, call_next))
    assert formatted.content[0].text == text
    assert formatted.structured_content == payload
