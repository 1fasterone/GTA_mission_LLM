"""
E2E depth stress — every micro-generator, one kind at a time.

Generalizes test_hud_e2e_stress.py's harness (see its module docstring for
the full story, including what harden_against_template can and cannot do)
across the whole `micro.REGISTRY` via src/micro_stress.py, which also backs
the `micro-stress-test` CLI command (`python main.py micro-stress-test`).
One parametrized case per kind so a failure names exactly which generator's
sanitizer stack broke, rather than one monolithic pass/fail.

Offline and deterministic — no LLM, no Unity. The optional syntax-gate pass
inside each run no-ops without a .NET SDK installed.
"""

from __future__ import annotations

import pytest

from src.micro import REGISTRY
from src.micro_stress import run_kind

_KINDS = sorted(REGISTRY)


@pytest.mark.parametrize("kind", _KINDS)
def test_kind_survives_maximal_adversarial_load(kind):
    result = run_kind(kind)
    if result.skipped:
        pytest.skip(result.skipped)
    assert result.error is None, f"{kind}: {result.error}"
    assert result.variants_tested > 0, f"{kind}: nothing was actually tested"
    assert not result.findings, (
        f"{kind}: live poison survived — "
        + ", ".join(f"{f.poison_key}@{f.variant}/{f.backend}" for f in result.findings)
    )


def test_every_registry_kind_is_covered():
    """Guards against the parametrization silently going stale if REGISTRY
    grows — a kind added there and never added here would just not run."""
    from src.micro import REGISTRY as live_registry
    assert set(_KINDS) == set(live_registry)
