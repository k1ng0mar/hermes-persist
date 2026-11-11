# persist

Stops the agent from quitting a coding task too early.

The agent writes a fix, the check still fails, and it wants to end the turn with
"I can't get this green." One or two failed attempts is not proof the task is
impossible. persist counts the distinct approaches taken in the turn, and when
the agent tries to stop below the floor with give-up language, it forces one
more turn and demands a different attempt or a written-down blocker.

## What it does

- Counts distinct failed tool calls per turn, keyed on tool name plus a hash of
  the arguments. Retrying the same failing command is one approach, not five.
- Keeps a coding turn alive when the agent stops below the floor with give-up
  phrasing, once per turn.
- Prints the attempt ledger on the final reply, including how many extra
  attempts were forced.

## What it does not do

It is not a universal anti-give-up gate. The only plugin hook that can force a
continuation is `pre_verify`, and Hermes fires it only on turns where the agent
changed files. So persist catches giving up on work the agent had started
editing. A research turn that ends in "I couldn't find it" with no file edits is
out of reach, and that is a core seam, not a plugin one. The footer still shows
the count either way.

It also does not touch the safety lane. The gate only fires when the turn holds
real tool failures, so a policy refusal (no failures) never trips it, and
success language suppresses the nudge.

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
hermes config set plugins.entries.persist.min_attempts 4
```

## Example

Turn where the agent edits a file, runs `pytest` twice (same command, so one
approach), then stops with "I can't get this test green":

```
persist: 1 distinct approach(es) failed this turn | forced 1 extra attempt(s)
- ran: pytest -q
```

The forced turn carries:

> [persist] You are stopping after 1 distinct attempt(s): ran: pytest -q. The
> floor is 3, and one or two failures are not proof this is impossible. Try a
> genuinely different approach (do not re-run a command that already failed). If
> you are certain it cannot be done, list every approach you tried with the exact
> failure for each, then name the specific blocker. Then stop.

## Tests

```bash
python3 -m unittest discover -s test -v
```

29 tests, stdlib only, no Hermes install needed.

## Design notes

- All state is in memory, keyed on `session_id`, cleared every turn by
  `pre_llm_call` and dropped at session boundaries and compaction.
- Every hook is fail-soft. An exception inside persist is logged and the turn
  proceeds.
- `transform_llm_output` is registered without `hold_stream`. A bug here must
  never withhold the reply.
- No credentials, no network, no disk.

MIT
