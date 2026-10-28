"""Human-readable JSON copies of Beacon reports; structured results stay intact."""
from __future__ import annotations

import json

from fastmcp.server.middleware import Middleware


# @beacon[
#   id=beacon-text@BeaconFindingsTextMiddleware,
#   slice_labels=nexus--mcp-api,ra-read-text-snippet,beacon-writeback,
#   kind=ast,
#   comment=Pretty-prints only FastMCP's text copy of full Beacon findings/fix reports including check previews tags sync snippet JSON and extraction-error evidence; handlers structured payloads inputs and all non-Beacon content stay unchanged.,
# ]
class BeaconFindingsTextMiddleware(Middleware):
    """Format full reports/errors for display, never their structured JSON payload."""

    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        payload = result.structured_content
        if context.message.name != "beacon" or not isinstance(payload, dict):
            return result
        if payload.get("operation") not in {
            "beacon.check", "beacon.fix", "beacon.tags.ensure", "beacon.tags.remove", "beacon.sync_node",
            "beacon.snippet",
        } and "findings" not in payload:
            return result
        content = []
        for block in result.content:
            if block.type == "text":
                # Only replace the framework's JSON display copy, not a custom
                # text block, advisory, source window or other framing.
                try:
                    is_payload = json.loads(block.text) == payload
                except ValueError:
                    is_payload = False
                if is_payload:
                    block = block.model_copy(update={"text": json.dumps(payload, indent=2, ensure_ascii=False)})
            content.append(block)
        return result.model_copy(update={"content": content})
