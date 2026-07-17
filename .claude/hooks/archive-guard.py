#!/usr/bin/env python3
"""Archive quarantine guard — Claude Code PreToolUse hook.

Denies Read/Grep/Glob whose target path is under `archive/**` UNLESS the archive
has been explicitly unlocked (sentinel file `.claude/.archive-unlocked` exists).

This is context hygiene, not access control: an explicit unlock (via the
`/doc-archive` skill) opens it deliberately. Fails OPEN on any parse error so a
broken guard is never worse than no guard.

Contract:
  - exit 0, no output      -> allow (non-archive path)
  - exit 0, JSON on stdout -> allow, but inject an "this is archived" note into context
  - exit 2, msg on stderr  -> BLOCK the tool call, msg shown to the model
"""
import json
import os
import sys


def deny(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(2)


def allow_with_note(note: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": note,
        }
    }))
    sys.exit(0)


def is_archive(path: str) -> bool:
    q = path.replace("\\", "/")
    return q == "archive" or q.startswith("archive/") or "/archive/" in q or q.endswith("/archive")


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)  # unparseable -> fail open

    tool_input = data.get("tool_input") or {}
    paths = [
        v for k in ("file_path", "path", "notebook_path", "pattern", "glob")
        for v in [tool_input.get(k)]
        if isinstance(v, str) and v
    ]
    hits = [p for p in paths if is_archive(p)]
    if not hits:
        sys.exit(0)

    project = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    sentinel = os.path.join(project, ".claude", ".archive-unlocked")

    if not os.path.exists(sentinel):
        deny(
            "BLOCKED: " + ", ".join(hits) + " is under archive/ (historical / superseded — NOT active "
            "project context). Archived files must not be read by default. If the user explicitly asked "
            "to access or compare past-week work, run the /doc-archive skill's `unlock` first, then retry; "
            "run `lock` when done."
        )

    # unlocked; the sentinel may hold an optional scope substring (e.g. "week-1")
    try:
        with open(sentinel) as fh:
            scope = fh.read().strip()
    except Exception:
        scope = ""
    if scope and not all(scope in p.replace("\\", "/") for p in hits):
        deny(
            "BLOCKED: archive is unlocked only for scope '" + scope + "', which does not cover "
            + ", ".join(hits) + ". Re-run /doc-archive unlock with the needed scope."
        )

    allow_with_note(
        "NOTE: reading ARCHIVED / historical file(s): " + ", ".join(hits) + ". These are NOT active "
        "project context — use only for the explicit past-week comparison that was requested."
    )


if __name__ == "__main__":
    main()
