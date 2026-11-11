"""Integration proof: drives Hermes' real plugin dispatch, not the class directly.

Imports hermes_cli.plugins, lets it discover the *installed* plugin from
~/.hermes/plugins/persist, then calls:
  - invoke_hook("post_tool_call", ...)   with status=error
  - get_pre_verify_continue_message(...) which is the exact function the agent's
    turn-stop gate calls at agent/turn_stop_gates.py
and asserts a continuation comes back.

Run it like this (the env var stops Hermes' self-update relaunch wrapper from
re-execing the process into a different interpreter):

  HERMES_DISABLE_LAZY_INSTALLS=1 HERMES_HOME=$HOME/.hermes \
    /path/to/hermes-agent/venv/bin/python scripts/integration_proof.py

A plain `python scripts/integration_proof.py` can trip hermes_bootstrap, which
re-execs and lands on an interpreter without Hermes' deps.
"""

import json
import os
import sys

sys.path.insert(0, "/home/ubuntu/.hermes/hermes-agent")

from hermes_cli import plugins as P  # noqa: E402


def main() -> int:
    P.discover_plugins(force=True)
    print("has_hook pre_verify:", P.has_hook("pre_verify"))
    print("has_hook post_tool_call:", P.has_hook("post_tool_call"))
    print("has_hook transform_llm_output:", P.has_hook("transform_llm_output"))
    if not P.has_hook("pre_verify"):
        print("FAIL: persist not loaded")
        return 1

    sid = "integration-proof-1"

    # Turn starts: pre_llm_call clears the ledger
    P.invoke_hook("pre_llm_call", session_id=sid, platform="cli", model="test")

    # One failing approach (same command twice -> 1 distinct)
    for _ in range(2):
        P.invoke_hook(
            "post_tool_call",
            session_id=sid,
            tool_name="terminal",
            args={"command": "pytest -q"},
            result=json.dumps({"error": "3 failed"}),
            status="error",
            error_message="3 failed",
        )

    def ask():
        return P.get_pre_verify_continue_message(
            session_id=sid,
            platform="cli",
            model="test",
            coding=True,
            attempt=0,
            final_response="I can't get these tests green.",
            changed_paths=["app.py"],
        )

    msg = ask()
    print("\n--- pre_verify @ 1 distinct failure (floor 3) ---")
    print("returned:", (msg[:200] + "...") if msg else None)
    assert msg, "FAIL: gate did not fire"

    msg2 = ask()
    print("\n--- pre_verify again, same turn (attempt 0) ---")
    print("returned:", None if not msg2 else "SECOND NUDGE (bad)")

    # A different approach now
    P.invoke_hook(
        "post_tool_call", session_id=sid, tool_name="patch",
        args={"path": "app.py", "old": "x", "new": "y"},
        result=json.dumps({"error": "no match"}), status="error",
        error_message="no match",
    )
    P.invoke_hook(
        "post_tool_call", session_id=sid, tool_name="terminal",
        args={"command": "python -m pytest app.py::test_a -x"},
        result=json.dumps({"error": "1 failed"}), status="error",
        error_message="1 failed",
    )

    msg3 = P.get_pre_verify_continue_message(
        session_id=sid, platform="cli", model="test", coding=True, attempt=1,
        final_response="I can't get these tests green.", changed_paths=["app.py"],
    )
    print("\n--- pre_verify @ 3 distinct failures vs floor 3 ---")
    print("returned:", msg3, "(None = earned the right to give up)")
    assert msg3 is None, "FAIL: gate fired past the floor"

    # Footer via transform_llm_output
    res = P.invoke_hook(
        "transform_llm_output", response_text="I can't get these tests green.",
        session_id=sid, platform="cli", model="test",
    )
    texts = [r for r in res if isinstance(r, str) and r.strip()]
    print("\n--- transform_llm_output ---")
    print(texts[0] if texts else "NOTHING RETURNED")
    assert texts, "FAIL: no footer"
    assert "persist:" in texts[0], "FAIL: footer missing counter"

    print("\n--- success language must not fire ---")
    sid2 = "integration-proof-2"
    P.invoke_hook("pre_llm_call", session_id=sid2, platform="cli", model="test")
    P.invoke_hook("post_tool_call", session_id=sid2, tool_name="terminal",
                  args={"command": "pytest"}, result="{}", status="error",
                  error_message="x")
    ok = P.get_pre_verify_continue_message(
        session_id=sid2, platform="cli", model="test", coding=True, attempt=0,
        final_response="All tests pass now.", changed_paths=["app.py"],
    )
    print("returned:", ok)
    assert ok is None, "FAIL: fired on success language"

    print("\nPASS: real dispatch confirms the gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())