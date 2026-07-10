"""feat_ready — /ready must verify dependencies are REACHABLE, not just configured.

FROZEN GOALS. probe_llm and probe_langfuse must attempt a real, short-timeout call
to their backend: credentials present but the backend down => NOT ok. Baseline: the
probes only check credential presence, so a set key with a dead backend reports ok
=True — these fail until a real reachability check is added.
"""

from __future__ import annotations

import asyncio

# Refuses instantly (port 1) — a reachability probe must report NOT ok, fast.
_DEAD = "http://127.0.0.1:1"


def _run(coro):
    return asyncio.run(coro)


def test_llm_probe_reports_unreachable_backend():
    from copilot.api import readiness
    from copilot.config import Settings

    dep = _run(
        readiness.probe_llm(
            Settings(anthropic_api_key="sk-ant-test", anthropic_base_url=_DEAD)
        )
    )
    assert dep.name == "llm"
    assert dep.ok is False, (
        "probe_llm must ping the provider; a set key pointed at a dead backend is NOT ready"
    )


def test_langfuse_probe_reports_unreachable_backend():
    from copilot.api import readiness
    from copilot.config import Settings

    dep = _run(
        readiness.probe_langfuse(
            Settings(langfuse_host=_DEAD, langfuse_public_key="pk", langfuse_secret_key="sk")
        )
    )
    assert dep.name == "langfuse"
    assert dep.ok is False, (
        "probe_langfuse must ping the host; creds set + host down is NOT reachable"
    )
