"""Runtime-selected chat agent.

One ``ChatAgent`` Protocol, two implementations behind ``build_agent``:

- ``StubAgent`` — deterministic, honest, no API key; grounds every claim
  in a re-fetchable ``source_ref`` or returns none at all.
- ``ClaudeAgent`` — real Anthropic tool-use loop; refuses without a key.

The chat endpoints depend on ``ChatAgent`` / ``AgentAnswer`` /
``ConversationTurn`` — not on which implementation is wired.
"""

from copilot.agent.base import AgentAnswer, ChatAgent, ConversationTurn
from copilot.agent.claude import AgentError, ClaudeAgent
from copilot.agent.factory import build_agent
from copilot.agent.stub import StubAgent

__all__ = [
    "AgentAnswer",
    "AgentError",
    "ChatAgent",
    "ClaudeAgent",
    "ConversationTurn",
    "StubAgent",
    "build_agent",
]
