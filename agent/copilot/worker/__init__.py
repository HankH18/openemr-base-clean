"""Background poller + memory-file synthesizer.

Ownership per ARCHITECTURE §"Components":
- 5–15 min detection tick (`Poller`).
- Change-gating (count query, then hash-confirm).
- Memory-file synthesis via `LlmSynthesizer` (Claude wrapper).
- Persistence via `MemoryRepository`.

Preempt/ranking is deferred (Redis-dependent; not MVP).
"""

from copilot.worker.hashing import content_hash_for_resources
from copilot.worker.poller import Poller, PollerResult, PollerTickOutcome
from copilot.worker.synthesizer import (
    ClaudeSynthesizer,
    LlmSynthesizer,
    StubSynthesizer,
    SynthesisError,
    SynthesisInput,
)

__all__ = [
    "ClaudeSynthesizer",
    "content_hash_for_resources",
    "LlmSynthesizer",
    "Poller",
    "PollerResult",
    "PollerTickOutcome",
    "StubSynthesizer",
    "SynthesisError",
    "SynthesisInput",
]
