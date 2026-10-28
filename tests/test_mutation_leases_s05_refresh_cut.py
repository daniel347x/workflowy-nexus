"""Compat-patch generation preservation; cut serialization is retired."""

from __future__ import annotations

import pytest
from coherence_harness import s02_set_barrier, s02_set_external_dirt
from mutation_lease_harness import ACCOUNT_A, NOW, async_test, make_lease_env, node

from workflowy_mcp.coherence.refresh_authority import run_compat_patch
from workflowy_mcp.coherence.schema import (
    read_manifest, read_external_dirt,
)


async def _compat_preservation_witness(root):
    env = make_lease_env(root, ACCOUNT_A, nodes=[node("file")])
    s02_set_barrier(env.paths, 7)
    s02_set_external_dirt(env.paths, 5)
    outcome = await run_compat_patch(
        paths=env.paths, patches=[{"id": "file", "name": "renamed-file"}], snapshot_dir=env.snapshot_dir,
    )
    assert outcome.status == "published"
    hard, dirt = read_manifest(env.paths).dirty_status, read_external_dirt(env.paths)
    assert (hard.active, hard.generation) == (True, 7)
    assert (dirt.active, dirt.generation) == (True, 5)


@async_test
async def test_compat_patch_clears_none(tmp_path):
    await _compat_preservation_witness(tmp_path)


@async_test
async def test_compat_preservation_sensitivity(tmp_path, monkeypatch):
    from workflowy_mcp.coherence import refresh_authority

    original = refresh_authority.publish_epoch
    for signal, setter, generation in (("hard", s02_set_barrier, 7), ("external", s02_set_external_dirt, 5)):
        async def wrongly_clear(*args, **kwargs):
            result = await original(*args, **kwargs)
            setter(kwargs["paths"], generation, active=False)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(refresh_authority, "publish_epoch", wrongly_clear)
            with pytest.raises(AssertionError):
                await _compat_preservation_witness(tmp_path / (signal + "-neutralized"))
        await _compat_preservation_witness(tmp_path / (signal + "-restored"))
