# persist

Stops the agent from quitting a coding task too early.

The agent edits a fix, the check still fails, and it wants to end the turn with
"I can't get this green." One or two failed attempts is not proof the task is
impossible. persist counts the distinct approaches taken in the turn, and when
the agent tries to stop below the floor, it forces one more turn and demands a
genuinely different attempt or a written-down blocker.

## What it does

- Counts distinct failed tool calls per turn, keyed on tool name plus a hash of
  the arguments. Retrying the same failing command is one approach, not five.
- Keeps a coding turn alive when the agent stops below the floor, once per turn.
- Prints the attempt ledger on the final reply, including how many extra
  attempts were forced.

Two independent signals decide "the agent is stopping while still broken":

1. **Ended red.** The last tool call of the turn returned an error. This is
   deterministic and needs no phrasing guesswork.
2. **Give-up phrasing.** A conservative phrase match ("not fixable", "no way to",
   "impossible to satisfy", "mutually exclusive", "stuck", ...).

An explicit success claim ("all tests pass", "now works") vetoes both. persist is
an anti-give-up gate, not a lie detector: a false victory claim is a different
bug and not this plugin's job.

## What it does not do

It is not a universal anti-give-up gate. The only plugin hook that can force a
continuation is `pre_verify`, and Hermes fires it only on turns where the agent
changed files (`if _edited and has_hook("pre_verify")` in
`agent/turn_stop_gates.py`). So persist catches giving up on work the agent had
started editing. A research turn that ends in "I couldn't find it" with no file
edits is out of reach, and that is a core seam, not a plugin one. The footer
still shows the count either way.

It also does not touch the safety lane. The gate needs at least one recorded
tool failure, so a policy refusal (no failures) never trips it.

Identical repeated failing calls are already handled by Hermes' native tool
guardrail. persist handles the different-approach quit, which the native guard
lets through.

## Install

```bash
hermes plugins install k1ng0mar/hermes-persist
hermes plugins enable persist
```

## Settings

`min_attempts` (default 3): distinct failed approaches required before the agent
may give up. Set it to 1 to disable the gate.

```bash
hermes config set plugins.entries.persist.settings.min_attempts 4
```

## Demo

A repo with a real bug in `calc.py` and a contradictory, frozen test the agent
may not touch (`add(1, 1) == 2` in one test, `== 3` in another). The agent fixes
the real bug, hits the contradiction, and moves to stop. persist forces one more
attempt, and the agent comes back with the ledger and a named blocker.

```
persist: 1 distinct approach(es) failed this turn | forced 1 extra attempt(s)
- ran: cd /home/ubuntu/projects/persist-demo && python -m unittest -v
```

The forced turn carries:

> [persist] You are stopping after 1 distinct attempt(s): ran: python -m unittest
> -v. The floor is 3, and one or two failures are not proof this is impossible.
> Try a genuinely different approach (do not re-run a command that already
> failed). If you are certain it cannot be done, list every approach you tried
> with the exact failure for each, then name the specific blocker. Then stop.

Full transcript: [`examples/demo-transcript.txt`](examples/demo-transcript.txt).

## Tests

```bash
python3 -m unittest discover -s test -v
```

47 tests, stdlib only, no Hermes install needed.

To prove the gate through Hermes' real dispatch (not the class directly):

```bash
HERMES_DISABLE_LAZY_INSTALLS=1 HERMES_HOME=$HOME/.hermes \
  /path/to/hermes-agent/venv/bin/python scripts/integration_proof.py
```

The env var matters: without it, `hermes_bootstrap` re-execs the process into a
different interpreter.

## Design notes

- All state is in memory, keyed on `session_id`, cleared every turn by
  `pre_llm_call` and dropped at session boundaries and compaction.
- The gate self-throttles on its own nudge counter instead of trusting the
  caller's `attempt` argument, so it fires once per turn regardless.
- Every hook is fail-soft. An exception inside persist is logged and the turn
  proceeds.
- `transform_llm_output` is registered without `hold_stream`. A bug here must
  never withhold the reply.
- No credentials, no network, no disk.

MIT
