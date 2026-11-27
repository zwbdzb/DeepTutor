"""Derive a turn's latency structure from data the runtime already persists.

Pure functions over already-loaded rows: no I/O, no clock, no model call. The
same code backs the ``deeptutor trace`` CLI and ``GET /api/traces/*``, so the
two can never disagree.

Pairing keys (all confirmed against the emitters):

* model round — ``progress`` event with ``trace_kind=call_status``; ``running``
  opens, ``complete``/``failed`` closes, paired by ``metadata.call_id``. A retried
  call emits ``running`` again after a ``failed``, so one model call can yield
  several attempts; they are kept as separate spans and merged by interval
  union, which counts every second the provider was actually working and
  excludes the gap between attempts.
* tool — ``tool_call`` opens, ``tool_result`` closes, paired by
  ``metadata.call_id`` (assigned per call in ``runtime/agentic/tool_dispatch``).
  Pairing by key rather than by order is what keeps parallel batches correct.
* user wait — ``wait_for_input`` opens; the next tool result or model round
  closes it.
"""

from __future__ import annotations

from typing import Any

_CALL_MATCH_WINDOW_SECONDS = 120.0

# 列表页要一次显示几十轮，问题文本必须截断；完整文本由详情接口按需展示。
USER_MESSAGE_CAP = 240


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # reject NaN


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _seconds_to_ms(value: Any) -> float | None:
    seconds = _num(value)
    return None if seconds is None else round(seconds * 1000, 1)


def _merge(intervals: list[tuple[float, float]]) -> list[list[float]]:
    spans = sorted((start, end) for start, end in intervals if end >= start)
    merged: list[list[float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _union_ms(intervals: list[tuple[float, float]]) -> float:
    """Wall time covered by a set of (possibly overlapping) intervals.

    Concurrent tool calls and concurrent sub-agents overlap in real time;
    summing their durations would over-attribute and could exceed the turn's
    own total.
    """
    return round(sum(end - start for start, end in _merge(intervals)) * 1000, 1)


def _overlap_ms(
    left: list[tuple[float, float]], right: list[tuple[float, float]]
) -> float:
    """Wall time covered by both sets, so one phase can be removed from another.

    A provider that streams a tool call before the round closes leaves the tool's
    own duration inside the round's window. Counting both would attribute those
    seconds twice and let the phases add up to more than the turn.
    """
    a = _merge(left)
    b = _merge(right)
    total = 0.0
    i = j = 0
    while i < len(a) and j < len(b):
        start = max(a[i][0], b[j][0])
        end = min(a[i][1], b[j][1])
        if end > start:
            total += end - start
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return round(total * 1000, 1)


def _is_model_round(meta: dict[str, Any]) -> bool:
    """Whether a ``call_status`` envelope describes a model round.

    The same envelope also reports each tool's execution
    (``call_kind=tool_planning``). Counting those as rounds doubled the round
    count and painted a 62s tool timeout as a 62s model call.
    """
    kind = str(meta.get("call_kind") or "")
    if kind:
        return kind == "agent_loop_round"
    # Rows written before ``call_kind`` existed still carry the trace role.
    return str(meta.get("trace_role") or "") != "tool"


def _tool_name(meta: dict[str, Any], event: dict[str, Any]) -> str:
    """The tool's own name, never the emitting agent's.

    ``event["source"]`` is the agent (``chat``), so it is never a tool name:
    using it labelled every tool identically and left the per-tool table
    unreadable. Unknown names stay ``tool`` rather than guessing.
    """
    for key in ("tool_name", "tool", "name"):
        value = str(meta.get(key) or "").strip()
        if value:
            return value
    return "tool"


def _tool_failed(meta: dict[str, Any]) -> bool:
    """A tool result is a failure when it says so, at either nesting level."""
    if meta.get("is_error") or meta.get("error"):
        return True
    nested = meta.get("tool_metadata")
    return isinstance(nested, dict) and bool(nested.get("error"))


def _pick_ledger_call(
    calls: list[dict[str, Any]],
    used: set[int],
    start: float,
    end: float,
) -> dict[str, Any] | None:
    """Pair a round with the ledger line that actually ran inside it.

    The ``reason`` tool issues its own model call under the same turn, so
    nearest-start matching could hand a round the nested call's line — which
    produced generation times longer than the round containing them. Prefer a
    line fully contained in the round, then the largest overlap.
    """
    best: tuple[tuple[int, float, float], int] | None = None
    for index, call in enumerate(calls):
        if index in used:
            continue
        call_start = float(call.get("started_at") or 0.0)
        usage = call.get("usage") or {}
        duration = _num(usage.get("duration_seconds")) or 0.0
        call_end = call_start + duration
        delta = abs(call_start - start)
        if delta > _CALL_MATCH_WINDOW_SECONDS:
            continue
        contained = 1 if (call_start >= start - 1.0 and call_end <= end + 1.0) else 0
        overlap = max(0.0, min(call_end, end) - max(call_start, start))
        score = (contained, overlap, -delta)
        if best is None or score > best[0]:
            best = (score, index)
    if best is None:
        return None
    used.add(best[1])
    return calls[best[1]]


def _done_timings(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Coarse Python-side phases published on the DONE envelope by the executor."""
    timings: dict[str, Any] = {}
    for event in events:
        if event["type"] != "done":
            continue
        raw = event["metadata"].get("phase_timings")
        if isinstance(raw, dict):
            timings = raw
    return timings


def derive_turn(
    turn: dict[str, Any],
    events: list[dict[str, Any]],
    llm_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Turn rows -> the structure the UI renders. Tolerates partial data."""
    calls = list(llm_calls or [])
    created = _num(turn.get("created_at")) or 0.0
    finished = _num(turn.get("finished_at"))
    stamps = [float(e["timestamp"]) for e in events if _num(e.get("timestamp")) is not None]
    last_stamp = max(stamps) if stamps else created
    end_ts = finished if finished and finished >= created else last_stamp

    timings = _done_timings(events)

    # ---- model rounds -----------------------------------------------------
    # Only model rounds belong here. The same envelope reports tool execution
    # too, so the filter is what keeps "3 model calls" from reading as 7.
    rounds: list[dict[str, Any]] = []
    open_rounds: dict[str, float] = {}
    for event in events:
        meta = event["metadata"]
        if meta.get("trace_kind") != "call_status":
            continue
        if not _is_model_round(meta):
            continue
        call_id = str(meta.get("call_id") or "")
        if not call_id:
            continue
        state = str(meta.get("call_state") or "")
        if state == "running":
            open_rounds[call_id] = float(event["timestamp"])
            continue
        started = open_rounds.pop(call_id, None)
        if started is None:
            continue
        rounds.append(
            {
                "call_id": call_id,
                "start": started,
                "end": float(event["timestamp"]),
                "status": state or "complete",
            }
        )
    for call_id, started in open_rounds.items():
        rounds.append({"call_id": call_id, "start": started, "end": end_ts, "status": "unknown"})
    rounds.sort(key=lambda item: item["start"])

    used_calls: set[int] = set()
    for index, rnd in enumerate(rounds, start=1):
        rnd["n"] = index
        rnd["ms"] = round((rnd["end"] - rnd["start"]) * 1000, 1)
        ledger = _pick_ledger_call(calls, used_calls, rnd["start"], rnd["end"])
        usage = (ledger or {}).get("usage") or {}
        rnd["ttft_ms"] = _seconds_to_ms(usage.get("ttft_seconds"))
        rnd["gen_ms"] = _seconds_to_ms(usage.get("generation_seconds"))
        rnd["completion_tokens"] = usage.get("completion_tokens")
        rnd["tools"] = 0

    # ---- tools ------------------------------------------------------------
    # A parallel batch reports ``tool_call``/``tool_result`` with batch-level
    # timestamps: every call in the batch shares the batch's start and end, so
    # a search that finished in 1s beside an 82s timeout sibling inherited the
    # sibling's 82s. The ``tool_planning`` progress envelopes carry real
    # per-call open/close stamps and are the primary source; the batch events
    # remain the fallback for turns recorded before those existed.
    tools: list[dict[str, Any]] = []
    planned: dict[str, dict[str, Any]] = {}
    for event in events:
        meta = event["metadata"]
        if event["type"] != "progress":
            continue
        if str(meta.get("call_kind") or "") != "tool_planning":
            continue
        call_id = str(meta.get("call_id") or "")
        if not call_id:
            continue
        state = str(meta.get("call_state") or "")
        item = planned.get(call_id)
        if state == "running":
            if item is None:
                planned[call_id] = {
                    "call_id": call_id,
                    "name": _tool_name(meta, event),
                    "start": float(event["timestamp"]),
                }
            else:
                item["start"] = min(item["start"], float(event["timestamp"]))
            continue
        if item is None or "end" in item:
            continue
        item["end"] = float(event["timestamp"])
        item["status"] = "error" if state == "error" else "ok"
    for item in planned.values():
        if "end" not in item:
            # Interrupted mid-flight: keep the wall time, flag the outcome.
            item["end"] = end_ts
            item["status"] = "cancelled"
        tools.append(item)

    # Fallback: turns without per-tool envelopes only have the batch events.
    open_tools: dict[str, dict[str, Any]] = {}
    for event in events:
        meta = event["metadata"]
        if event["type"] == "tool_call":
            call_id = str(meta.get("call_id") or meta.get("tool_call_id") or f"seq:{event['seq']}")
            if call_id in planned:
                continue
            open_tools[call_id] = {
                "call_id": call_id,
                "name": _tool_name(meta, event),
                "start": float(event["timestamp"]),
            }
        elif event["type"] == "tool_result":
            call_id = str(meta.get("call_id") or meta.get("tool_call_id") or "")
            item = open_tools.pop(call_id, None)
            if item is None:
                continue
            item["end"] = float(event["timestamp"])
            item["status"] = "error" if _tool_failed(meta) else "ok"
            tools.append(item)
    for item in open_tools.values():
        # A batch interrupted mid-flight: count the wall time, flag the outcome.
        item["end"] = end_ts
        item["status"] = "cancelled"
        tools.append(item)
    tools.sort(key=lambda item: item["start"])
    for item in tools:
        item["ms"] = round((item["end"] - item["start"]) * 1000, 1)

    # 工具属于「请求它的那一轮」：工具是在轮次关闭之后才执行的，所以先按
    # 包含关系找，找不到就归给最近一个已结束的轮次。否则这一列永远是 0。
    for item in tools:
        owner: dict[str, Any] | None = None
        for rnd in rounds:
            if rnd["start"] <= item["start"] <= rnd["end"]:
                owner = rnd
                break
        if owner is None:
            for rnd in rounds:
                if rnd["end"] <= item["start"]:
                    owner = rnd
        if owner is not None:
            owner["tools"] += 1

    # ---- user waits -------------------------------------------------------
    waits: list[dict[str, float]] = []
    pending: float | None = None
    for event in events:
        if event["type"] == "wait_for_input":
            pending = float(event["timestamp"])
            continue
        if pending is None:
            continue
        closes = event["type"] == "tool_result" or (
            event["type"] == "progress" and event["metadata"].get("call_state") == "running"
        )
        if closes:
            waits.append({"start": pending, "end": float(event["timestamp"])})
            pending = None
    if pending is not None:
        waits.append({"start": pending, "end": end_ts})
    wait_ms = round(sum(max(0.0, w["end"] - w["start"]) for w in waits) * 1000, 1)
    wait_intervals = [(w["start"], w["end"]) for w in waits]

    # ---- stages -----------------------------------------------------------
    stages: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    for event in events:
        name = str(event["metadata"].get("stage") or event["stage"] or "")
        if event["type"] == "stage_start":
            stack.append({"name": name, "start": float(event["timestamp"])})
        elif event["type"] == "stage_end":
            for index in range(len(stack) - 1, -1, -1):
                if stack[index]["name"] == name:
                    item = stack.pop(index)
                    stages.append(
                        {
                            "name": name,
                            "start": item["start"],
                            "end": float(event["timestamp"]),
                            "ms": round((float(event["timestamp"]) - item["start"]) * 1000, 1),
                        }
                    )
                    break

    # ---- breakdown --------------------------------------------------------
    round_intervals = [(rnd["start"], rnd["end"]) for rnd in rounds]
    tool_intervals = [(item["start"], item["end"]) for item in tools]

    # A round's window is the provider call, not the work the round caused. When
    # a streamed tool call opens before the round closes, its duration sits
    # inside that window; subtract it so the phases partition the turn instead of
    # double-counting the same seconds.
    model_ms = round(
        max(
            0.0,
            _union_ms(round_intervals)
            - _overlap_ms(round_intervals, tool_intervals + wait_intervals),
        ),
        1,
    )
    tool_ms = _union_ms(tool_intervals)

    context_ms = _num(timings.get("context_ms"))
    execute_ms = _num(timings.get("execute_ms"))
    persist_ms = _num(timings.get("persist_ms"))
    total_ms = _num(timings.get("total_ms"))
    estimated = not timings

    # Queue time: turn created -> the turn's first persisted event. The
    # ``session`` event is emitted before any capability work, so it marks the
    # real start; without it the "queue" figure would absorb context assembly.
    session_stamp = next(
        (float(e["timestamp"]) for e in events if e["type"] == "session"), None
    )
    staged = [float(e["timestamp"]) for e in events if e["type"] in {"stage_start", "progress"}]
    first_stamp = (
        session_stamp
        if session_stamp is not None
        else (min(staged) if staged else (min(stamps) if stamps else created))
    )
    queue_ms = round(max(0.0, (first_stamp - created) * 1000), 1)

    # Older turns carry no ``phase_timings``. Derive a context proxy from the
    # gap between the session event and the first capability event, so the page
    # still splits "waiting to start" from "preparing to call the model".
    if context_ms is None and session_stamp is not None and staged:
        later = [stamp for stamp in staged if stamp >= session_stamp]
        if later:
            context_ms = round(max(0.0, (min(later) - session_stamp) * 1000), 1)

    if total_ms is None or total_ms <= 0:
        total_ms = round(max(0.0, (end_ts - created) * 1000), 1)

    other_exec_ms: float | None = None
    if execute_ms is not None:
        # Capability work that is neither a model round, a tool, nor a user
        # wait: prompt assembly, context guards, capability bookkeeping.
        other_exec_ms = round(max(0.0, execute_ms - model_ms - tool_ms - wait_ms), 1)

    attributed = (
        queue_ms
        + (context_ms or 0.0)
        + model_ms
        + tool_ms
        + wait_ms
        + (other_exec_ms or 0.0)
        + (persist_ms or 0.0)
    )
    unattributed_ms = round(max(0.0, total_ms - attributed), 1)

    breakdown = {
        "queue_ms": queue_ms,
        "context_ms": _rounded(context_ms),
        "model_ms": model_ms,
        "tool_ms": tool_ms,
        "wait_ms": wait_ms,
        "other_exec_ms": _rounded(other_exec_ms),
        "persist_ms": _rounded(persist_ms),
        "unattributed_ms": unattributed_ms,
    }

    # 本轮问题是「这是哪一轮」的唯一可辨认标识；turn_id 对人没有意义。
    # 取数在 store.load_prompts，这里是纯函数，只做截断与透传。
    prompt_text = str(turn.get("user_message") or "")
    prompt_id = _num(turn.get("user_message_id"))

    # Failed tool calls are a *subset* of ``tool_ms``, so they stay out of the
    # breakdown (the phases must keep summing to the turn) but are reported
    # beside it: this is the number that says "time was spent, nothing was
    # gained". A timeout costs the same as a slow success in wall time and is
    # strictly worse in outcome.
    failed_tools = [item for item in tools if item["status"] == "error"]
    failed_tool_ms = _union_ms([(item["start"], item["end"]) for item in failed_tools])
    tool_failure_rate = round(len(failed_tools) / len(tools), 3) if tools else None

    result = {
        "turn_id": str(turn.get("turn_id") or turn.get("id") or ""),
        "session_id": str(turn.get("session_id") or ""),
        "capability": str(turn.get("capability") or ""),
        "status": str(turn.get("status") or ""),
        "created_at": round(created, 3),
        "total_ms": total_ms,
        "estimated": estimated,
        "user_message_id": int(prompt_id) if prompt_id is not None else None,
        "user_message": prompt_text[:USER_MESSAGE_CAP],
        "user_message_truncated": len(prompt_text) > USER_MESSAGE_CAP,
        "breakdown": breakdown,
        "failed_tool_ms": failed_tool_ms,
        "tool_failure_rate": tool_failure_rate,
        "rounds": [
            {
                "n": rnd["n"],
                "ms": rnd["ms"],
                "status": rnd["status"],
                "ttft_ms": rnd["ttft_ms"],
                "gen_ms": rnd["gen_ms"],
                "completion_tokens": rnd["completion_tokens"],
                "tools": rnd["tools"],
            }
            for rnd in rounds
        ],
        "tools": [
            {"name": item["name"], "ms": item["ms"], "status": item["status"]} for item in tools
        ],
        "waits": [{"ms": round(max(0.0, w["end"] - w["start"]) * 1000, 1)} for w in waits],
        "stages": [{"name": s["name"], "ms": s["ms"]} for s in stages],
        "findings": _findings(
            breakdown,
            tools,
            rounds,
            total_ms,
            failed_tool_ms=failed_tool_ms,
            failed_calls=len(failed_tools),
        ),
    }
    result["top_bottleneck"] = _top_bottleneck(breakdown)
    return result


_BOTTLENECK_ORDER: tuple[tuple[str, str], ...] = (
    ("model_ms", "model"),
    ("tool_ms", "tool"),
    ("wait_ms", "wait"),
    ("context_ms", "context"),
    ("persist_ms", "persist"),
    ("other_exec_ms", "other_exec"),
    ("queue_ms", "queue"),
)


def _top_bottleneck(breakdown: dict[str, Any]) -> str:
    """Human label for the largest measured phase (the list view's sort key)."""
    best_label = "unattributed"
    best_value = float(breakdown.get("unattributed_ms") or 0.0)
    for key, label in _BOTTLENECK_ORDER:
        value = breakdown.get(key)
        if value is None:
            continue
        if float(value) > best_value:
            best_value = float(value)
            best_label = label
    return f"{best_label} ({round(best_value, 1)} ms)"


def _share(part: float | None, total: float) -> float:
    if not part or total <= 0:
        return 0.0
    return round(100.0 * part / total, 1)


def _findings(
    breakdown: dict[str, Any],
    tools: list[dict[str, Any]],
    rounds: list[dict[str, Any]],
    total_ms: float,
    *,
    failed_tool_ms: float = 0.0,
    failed_calls: int = 0,
) -> list[dict[str, str]]:
    """Threshold statements only; this never asserts a cause."""
    out: list[dict[str, str]] = []
    model = _share(breakdown.get("model_ms"), total_ms)
    tool = _share(breakdown.get("tool_ms"), total_ms)
    wait = _share(breakdown.get("wait_ms"), total_ms)
    context = _share(breakdown.get("context_ms"), total_ms)

    # Reported before the plain "tools are slow" line: a failed call is the
    # actionable finding, a slow success is not.
    if tools and failed_calls:
        failed_share = _share(failed_tool_ms, total_ms)
        out.append(
            {
                "code": "tools_failing",
                "severity": "warning" if failed_share >= 15 else "info",
                "text": (
                    f"{failed_calls} of {len(tools)} tool call(s) failed, "
                    f"costing {round(failed_tool_ms, 1)} ms ({failed_share}% of the turn)"
                ),
            }
        )
    if model >= 70:
        out.append(
            {
                "code": "model_dominant",
                "severity": "info",
                "text": f"model time is {model}% of the turn across {len(rounds)} round(s)",
            }
        )
    if tool >= 40:
        out.append(
            {
                "code": "tools_dominant",
                "severity": "warning",
                "text": f"tool execution is {tool}% of the turn across {len(tools)} call(s)",
            }
        )
    if wait >= 30:
        out.append(
            {
                "code": "waiting_on_user",
                "severity": "info",
                "text": f"{wait}% of the turn was spent waiting for a user reply",
            }
        )
    if context >= 20:
        out.append(
            {
                "code": "context_heavy",
                "severity": "warning",
                "text": f"context assembly is {context}% of the turn",
            }
        )
    tool_ms = float(breakdown.get("tool_ms") or 0.0)
    if tools and tool_ms > 0:
        slowest = max(tools, key=lambda item: item["ms"])
        if slowest["ms"] >= 0.5 * tool_ms:
            out.append(
                {
                    "code": "single_tool_dominant",
                    "severity": "warning",
                    "text": (
                        f"{slowest['name']} alone took {round(slowest['ms'], 1)} ms "
                        f"({_share(slowest['ms'], tool_ms)}% of tool time)"
                    ),
                }
            )
    if not tools and len(rounds) >= 3:
        out.append(
            {
                "code": "rounds_without_tools",
                "severity": "info",
                "text": f"{len(rounds)} model rounds produced no tool calls",
            }
        )
    return out


def derive_session(
    session_id: str,
    *,
    limit: int = 20,
    db_path: Any = None,
    usage_path: Any = None,
) -> list[dict[str, Any]]:
    """Every turn in a conversation, newest first, fully derived."""
    from . import store

    return _derive_rows(
        store.list_turns(session_id, limit=limit, db_path=db_path),
        db_path=db_path,
        usage_path=usage_path,
    )


def derive_recent(
    *,
    limit: int = 20,
    session_id: str = "",
    db_path: Any = None,
    usage_path: Any = None,
) -> list[dict[str, Any]]:
    """Newest turns across every conversation (or one), fully derived."""
    from . import store

    return _derive_rows(
        store.list_recent_turns(
            limit=limit, session_id=session_id, db_path=db_path
        ),
        db_path=db_path,
        usage_path=usage_path,
    )


def _derive_rows(
    turns: list[dict[str, Any]], *, db_path: Any = None, usage_path: Any = None
) -> list[dict[str, Any]]:
    from . import store

    # 一次批量取本轮问题，避免每轮一次查询（列表默认 20 轮、上限 100 轮）。
    prompts = store.load_prompts(
        [str(turn["turn_id"]) for turn in turns], db_path=db_path
    )
    traces: list[dict[str, Any]] = []
    for turn in turns:
        turn.update(prompts.get(str(turn["turn_id"]), {}))
        events = store.load_events(turn["turn_id"], db_path=db_path)
        calls = store.load_llm_calls(turn["turn_id"], usage_path=usage_path)
        traces.append(derive_turn(turn, events, calls))
    return traces


def derive_turn_by_id(
    turn_id: str, *, db_path: Any = None, usage_path: Any = None
) -> dict[str, Any] | None:
    from . import store

    turn = store.load_turn(turn_id, db_path=db_path)
    if turn is None:
        return None
    turn.update(store.load_prompts([turn_id], db_path=db_path).get(turn_id, {}))
    events = store.load_events(turn_id, db_path=db_path)
    calls = store.load_llm_calls(turn_id, usage_path=usage_path)
    return derive_turn(turn, events, calls)



