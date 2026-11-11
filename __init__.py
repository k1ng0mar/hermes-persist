"""persist: stop the agent from quitting a coding task it had started.

The agent writes a fix, the check still fails, and it wants to stop with
"I can't get this green." That is giving up after one or two tries, not
proof the task is impossible. persist counts the *distinct* approaches the
agent took this turn (same failing command retried is one approach, not
five; the native tool guardrail already handles identical repeats), and when
the agent tries to end a coding turn below the floor with give-up language,
it forces one more turn and demands a genuinely different attempt or a
written-down blocker.

Scope, stated plainly: the only plugin hook that can keep a turn going is
``pre_verify``, and Hermes fires it only on turns where the agent changed
files. So persist catches the give-up on work the agent had started editing.
A pure research "I couldn't find it" with no edits is out of reach; that is
a core seam, not a plugin one. The footer is honest about the count either
way.

Deterministic, not a prompt: the counter comes from ``post_tool_call``
``status`` (a real ok/error signal), the floor is a setting, and the ledger
is printed on the final reply. Give-up detection is a conservative phrase
match, but it only fires when the turn also holds real tool failures, so a
clean success or a policy refusal (no failures) never trips it.

No credentials, no network, no disk. The ledger lives in memory and resets
every turn (``pre_llm_call``). ``transform_llm_output`` is registered
without ``hold_stream``: a bug here must never withhold the reply. Every
hook is fail-soft; a raise inside persist is logged and the turn proceeds.
"""

import hashlib
import json
import logging
import re
import threading

logger = logging.getLogger("persist")

__all__ = ["Persist", "register", "DEFAULT_MIN_ATTEMPTS", "MAX_FOOTER_LINES"]

DEFAULT_MIN_ATTEMPTS = 3
MAX_FOOTER_LINES = 8

# Give-up language at the END of a turn (pre_verify only fires as the agent
# finishes). Conservative: only clear "I could not complete this" phrasing.
# A policy refusal has no tool failures, so the failure gate below is what
# really keeps persist out of the safety lane; this regex just spots intent.
_GIVEUP = re.compile(
    r"\b(?:"
    r"i'?m (?:unable|not able) to|"
    r"i cannot|can'?t (?:complete|finish|make|get|do this|resolve)|"
    r"unable to (?:complete|finish|resolve|make|get)|"
    r"(?:this|it)? ?(?:isn'?t|is not) (?:possible|working|resolvable)|"
    r"no way to (?:make|get|fix|resolve)|"
    r"i'?ve (?:tried|exhausted)|"
    r"(?:could|can) ?not (?:get|make|fix|resolve|complete)|"
    r"stuck|blocked by|gives? up|throw[s]? in"
    r")\b",
    re.IGNORECASE,
)

# Strong success markers: if the final answer claims the work is done and
# green, do not nudge even if a failure phrase appears earlier in it.
# Deliberately verb-bearing: a bare "<tests> green" appears inside negations
# like "I cannot get these tests green", which is a give-up, not a success.
_SUCCESS = re.compile(
    r"\b(?:all tests? (?:pass|passed|are passing|are green)|"
    r"tests? (?:pass|passed|are passing|are green)|"
    r"fully (?:working|fixed|resolved)|it'?s (?:working|fixed)|"
    r"now (?:works|passing)|problem (?:solved|resolved)|"
    r"done[.!]? *$)\b",
    re.IGNORECASE,
)


def _session_id(kwargs):
    sid = kwargs.get("session_id")
    return sid if isinstance(sid, str) and sid else "_"


def _status_error(status, result):
    """True when this tool call failed. Mirrors slip's ok/error derivation."""
    if isinstance(status, str):
        state = status.strip().lower()
        if state == "error":
            return True
        if state == "ok":
            return False
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and parsed.get("error"):
            return True
    return False


def _canonical(args):
    """Stable JSON of the args dict for hashing; None-safe."""
    if not isinstance(args, dict):
        return ""
    try:
        return json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(args)


def _signature(tool_name, args):
    raw = "%s\x00%s" % (tool_name or "", _canonical(args))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _label(tool_name, args):
    """Human-readable one-liner for the footer. Truncated, never the body."""
    args = args if isinstance(args, dict) else {}
    if tool_name == "terminal":
        cmd = str(args.get("command") or "").strip().replace("\n", " ")
        cmd = cmd[:70] + ("…" if len(cmd) > 70 else "")
        return "ran: %s" % (cmd or "(empty command)")
    if tool_name in ("patch", "write_file", "read_file"):
        path = args.get("path") or "?"
        return "%s: %s" % (tool_name, path)
    if tool_name == "browser_exec":
        return "browser: %s" % (str(args.get("session") or "default"))
    return tool_name or "tool"


def _looks_like_giveup(text):
    if not isinstance(text, str) or not text.strip():
        return False
    if _SUCCESS.search(text):
        return False
    return bool(_GIVEUP.search(text))


class Persist:
    """Per-turn distinct-failure ledger + a pre_verify give-up gate."""

    def __init__(self, min_attempts=DEFAULT_MIN_ATTEMPTS):
        self.min_attempts = max(1, int(min_attempts))
        self._lock = threading.Lock()
        # session_id -> {"failed": OrderedDict[sig -> label], "nudges": int}
        self._turn = {}

    def _bucket(self, sid):
        b = self._turn.get(sid)
        if b is None:
            b = {"failed": {}, "nudges": 0}
            self._turn[sid] = b
        return b

    # --- observer: count distinct failed approaches this turn ---
    def on_post_tool_call(self, tool_name=None, args=None, result=None, **kwargs):
        try:
            if not _status_error(kwargs.get("status"), result):
                return None
            sid = _session_id(kwargs)
            sig = _signature(tool_name, args)
            with self._lock:
                self._bucket(sid)["failed"].setdefault(sig, _label(tool_name, args))
        except Exception as exc:  # fail-soft: never break the tool loop
            logger.warning("persist post_tool_call error (ignored): %s", exc)
        return None

    # --- boundary: reset the ledger at the start of every user turn ---
    def on_pre_llm_call(self, **kwargs):
        try:
            sid = _session_id(kwargs)
            with self._lock:
                self._turn.pop(sid, None)
        except Exception as exc:
            logger.warning("persist pre_llm_call error (ignored): %s", exc)
        return None

    # --- the gate: keep a giving-up coding turn alive, once per turn ---
    def on_pre_verify(self, coding=False, attempt=0, final_response="", **kwargs):
        try:
            if attempt:  # one nudge per turn; the core caps at max_verify_nudges
                return None
            if not coding:  # scope to the coding posture (the seam's domain)
                return None
            sid = _session_id(kwargs)
            with self._lock:
                bucket = self._turn.get(sid)
                labels = list(bucket["failed"].values()) if bucket else []
                already = bucket["nudges"] if bucket else 0
            n = len(labels)
            if n == 0:  # no real failures: a clean stop or a policy refusal
                return None
            if n >= self.min_attempts:  # earned the right to give up
                return None
            if already:  # once per turn, don't lean on the caller's attempt counter
                return None
            if not _looks_like_giveup(final_response):
                return None
            with self._lock:
                self._bucket(sid)["nudges"] += 1
            tried = ", ".join(labels[:MAX_FOOTER_LINES])
            return {
                "action": "continue",
                "message": (
                    "[persist] You are stopping after %d distinct attempt(s): %s. "
                    "The floor is %d, and one or two failures are not proof this is "
                    "impossible. Try a genuinely different approach (do not re-run a "
                    "command that already failed). If you are certain it cannot be "
                    "done, list every approach you tried with the exact failure for "
                    "each, then name the specific blocker. Then stop."
                    % (n, tried, self.min_attempts)
                ),
            }
        except Exception as exc:
            logger.warning("persist pre_verify error (ignored): %s", exc)
            return None

    # --- the visible report: print the ledger on the final reply ---
    def on_transform_llm_output(self, response_text=None, **kwargs):
        try:
            sid = _session_id(kwargs)
            with self._lock:
                bucket = self._turn.get(sid)
                if not bucket or not bucket["failed"]:
                    return None
                labels = list(bucket["failed"].values())
                nudges = bucket["nudges"]
            if not isinstance(response_text, str) or not response_text.strip():
                return None
            shown = labels[:MAX_FOOTER_LINES]
            extra = len(labels) - len(shown)
            body = "\n".join("- %s" % ln for ln in shown)
            if extra > 0:
                body += "\n- +%d more" % extra
            head = "persist: %d distinct approach(es) failed this turn" % len(labels)
            if nudges:
                head += " | forced %d extra attempt(s)" % nudges
            return "%s\n\n%s\n%s" % (response_text.rstrip(), head, body)
        except Exception as exc:
            logger.warning("persist transform error (ignored): %s", exc)
            return None

    # --- cleanup: drop the session entry at boundaries ---
    def _drop(self, **kwargs):
        try:
            for key in ("session_id", "old_session_id"):
                sid = kwargs.get(key)
                if isinstance(sid, str) and sid:
                    with self._lock:
                        self._turn.pop(sid, None)
        except Exception as exc:
            logger.warning("persist cleanup error (ignored): %s", exc)
        return None


def _read_min_attempts(ctx):
    try:
        value = ctx.get_config("min_attempts", DEFAULT_MIN_ATTEMPTS)
    except Exception:
        return DEFAULT_MIN_ATTEMPTS
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return DEFAULT_MIN_ATTEMPTS
    return value


def register(ctx):
    persist = Persist(min_attempts=_read_min_attempts(ctx))
    ctx.register_hook("post_tool_call", persist.on_post_tool_call)
    ctx.register_hook("pre_llm_call", persist.on_pre_llm_call)
    ctx.register_hook("pre_verify", persist.on_pre_verify)
    ctx.register_hook("transform_llm_output", persist.on_transform_llm_output)
    ctx.register_hook("on_session_finalize", persist._drop)
    ctx.register_hook("on_session_reset", persist._drop)
    ctx.register_hook("post_compaction", persist._drop)
    return persist
