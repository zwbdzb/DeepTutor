"""单轮时延派生逻辑：配对键、时间守恒、以及数据缺失时的降级。

派生层是「原始 turn_events」与「页面/CLI 上每个数字」之间唯一的中间层，
所以这里把几条让数字可信的规则钉死：

* **按 call_id 配对**，绝不按顺序——否则并行工具批次会张冠李戴；
* **时间用区间并集**，并发跨度不能重复累加；
* **不编造**该轮没有记录的阶段（旧数据就老实标成估算值）。
"""

from __future__ import annotations

import pytest

from deeptutor.services.tracing import derive


def _event(seq: int, kind: str, at: float, **metadata: object) -> dict:
    """构造一条简化事件，字段与 store.load_events 的输出一致。"""
    return {
        "seq": seq,
        "type": kind,
        "source": "",
        "stage": "",
        "timestamp": at,
        "metadata": metadata,
    }


def _round_start(seq: int, at: float, call_id: str) -> dict:
    """模型轮次开始：progress + call_status=running。"""
    return _event(
        seq, "progress", at, trace_kind="call_status", call_id=call_id, call_state="running"
    )


def _round_end(seq: int, at: float, call_id: str, state: str = "complete") -> dict:
    """模型轮次结束：同一 call_id + call_state 为 complete/failed。"""
    return _event(
        seq, "progress", at, trace_kind="call_status", call_id=call_id, call_state=state
    )


def _tool_call(seq: int, at: float, call_id: str, name: str) -> dict:
    """工具调用开始。"""
    return _event(seq, "tool_call", at, call_id=call_id, tool=name)


def _tool_result(seq: int, at: float, call_id: str, error: bool = False) -> dict:
    """工具调用结束。"""
    return _event(seq, "tool_result", at, call_id=call_id, is_error=error)


def _turn(created: float, finished: float | None = None) -> dict:
    """构造一行 turns 表数据。"""
    return {
        "turn_id": "turn-1",
        "session_id": "s-1",
        "capability": "chat",
        "status": "completed" if finished else "running",
        "created_at": created,
        "finished_at": finished,
    }

def test_parallel_tools_pair_by_call_id_not_order() -> None:
    """两个重叠的工具必须各自保留自己的时长。

    如果按位置配对，第一个调用会拿到第二个的结束时间，第二个则永远悬挂——
    这正是 call_id 这个键存在的意义。
    """
    events = [
        _round_start(1, 1000.0, "m1"),
        _tool_call(2, 1001.0, "t-a", "web_search"),
        _tool_call(3, 1001.5, "t-b", "rag"),
        _tool_result(4, 1006.0, "t-b"),
        _tool_result(5, 1010.0, "t-a"),
        _round_end(6, 1012.0, "m1"),
    ]

    trace = derive.derive_turn(_turn(1000.0, 1012.0), events)

    by_name = {tool["name"]: tool for tool in trace["tools"]}
    assert by_name["web_search"]["ms"] == 9000.0
    assert by_name["rag"]["ms"] == 4500.0


def test_concurrent_tool_time_is_a_union_not_a_sum() -> None:
    """并发工具的时间是并集，不是求和：不会超过它们实际占据的墙钟时间。"""
    events = [
        _tool_call(1, 1000.0, "t-a", "web_search"),
        _tool_call(2, 1000.0, "t-b", "paper_search"),
        _tool_result(3, 1004.0, "t-a"),
        _tool_result(4, 1004.0, "t-b"),
        _event(5, "done", 1004.0, phase_timings={"execute_ms": 4000.0}),
    ]

    trace = derive.derive_turn(_turn(1000.0, 1004.0), events)

    assert trace["breakdown"]["tool_ms"] == 4000.0
    assert sum(tool["ms"] for tool in trace["tools"]) == 8000.0


def test_phases_come_from_done_timings_when_present() -> None:
    """DONE 事件带了 phase_timings 时，直接用精确值，不再估算。"""
    events = [
        _event(
            1,
            "done",
            1005.0,
            phase_timings={
                "started_at": 1000.0,
                "context_ms": 1000.0,
                "execute_ms": 3000.0,
                "persist_ms": 500.0,
                "total_ms": 5000.0,
            },
        )
    ]

    trace = derive.derive_turn(_turn(1000.0, 1005.0), events)

    assert trace["estimated"] is False
    assert trace["breakdown"]["context_ms"] == 1000.0
    assert trace["breakdown"]["persist_ms"] == 500.0
    assert trace["total_ms"] == 5000.0


def test_turn_without_phase_timings_is_flagged_estimated() -> None:
    """旧数据没有 phase_timings：相关阶段留空（None），并标为估算，不瞎猜。"""
    trace = derive.derive_turn(_turn(1000.0, 1010.0), [_event(1, "session", 1001.0)])

    assert trace["estimated"] is True
    assert trace["breakdown"]["context_ms"] is None
    assert trace["breakdown"]["persist_ms"] is None
    assert trace["total_ms"] == 10000.0


def test_parts_never_exceed_the_measured_total() -> None:
    """守恒：各阶段之和 + 未归因 = 这一轮的总时长。

    这里的用例数据是刻意自洽的（排队 + 上下文 + 执行 + 写入 == 记录的总时长），
    这样一旦少算/多算某一相，就会在这里失败，而不是在页面上变成 105% 的条。
    """
    events = [
        _event(1, "session", 1000.5),
        _round_start(2, 1002.0, "m1"),
        _event(3, "wait_for_input", 1002.5),
        _tool_result(4, 1005.0, "t-answer"),
        _round_end(5, 1008.0, "m1"),
        _tool_call(6, 1009.0, "t-a", "rag"),
        _tool_result(7, 1012.0, "t-a"),
        _event(
            8,
            "done",
            1020.0,
            phase_timings={
                "started_at": 1000.0,
                "context_ms": 1500.0,
                "execute_ms": 17800.0,
                "persist_ms": 200.0,
                "total_ms": 20000.0,
            },
        ),
    ]

    trace = derive.derive_turn(_turn(1000.0, 1020.0), events)
    breakdown = trace["breakdown"]
    attributed = (
        breakdown["queue_ms"]
        + (breakdown["context_ms"] or 0.0)
        + breakdown["model_ms"]
        + breakdown["tool_ms"]
        + breakdown["wait_ms"]
        + (breakdown["other_exec_ms"] or 0.0)
        + (breakdown["persist_ms"] or 0.0)
        + breakdown["unattributed_ms"]
    )

    assert attributed == pytest.approx(trace["total_ms"], abs=0.2)
    assert breakdown["unattributed_ms"] >= 0.0


def test_tool_inside_a_round_window_is_not_double_counted() -> None:
    """流式工具调用可能在轮次关闭前就发出了 tool_call。

    不扣掉这段重叠，同一批秒数会同时落进「模型」和「工具」，
    各阶段加起来就比这一轮本身还长。
    """
    events = [
        _round_start(1, 1000.0, "m1"),
        _tool_call(2, 1002.0, "t-a", "web_search"),
        _tool_result(3, 1005.0, "t-a"),
        _round_end(4, 1010.0, "m1"),
    ]

    trace = derive.derive_turn(_turn(1000.0, 1010.0), events)

    assert trace["breakdown"]["model_ms"] == 7000.0
    assert trace["breakdown"]["tool_ms"] == 3000.0
    assert trace["breakdown"]["model_ms"] + trace["breakdown"]["tool_ms"] == 10000.0


def test_tool_outside_a_round_window_keeps_full_model_time() -> None:
    """轮次之间跑的工具不能反过来削减模型时间（只扣真正重叠的部分）。"""
    events = [
        _round_start(1, 1000.0, "m1"),
        _round_end(2, 1005.0, "m1"),
        _tool_call(3, 1005.0, "t-a", "rag"),
        _tool_result(4, 1008.0, "t-a"),
        _round_start(5, 1008.0, "m2"),
        _round_end(6, 1010.0, "m2"),
    ]

    trace = derive.derive_turn(_turn(1000.0, 1010.0), events)

    assert trace["breakdown"]["model_ms"] == 7000.0
    assert trace["breakdown"]["tool_ms"] == 3000.0


def test_user_wait_is_measured_separately_from_model_time() -> None:
    """等人（ask_user）的时间单列，不能算进模型时间。"""
    events = [
        _event(1, "wait_for_input", 2000.0),
        _tool_result(2, 2030.0, "t-answer"),
    ]

    trace = derive.derive_turn(_turn(1000.0, 2030.0), events)

    assert trace["breakdown"]["wait_ms"] == 30000.0
    assert trace["waits"] == [{"ms": 30000.0}]
    assert trace["breakdown"]["model_ms"] == 0.0


def test_interrupted_tool_is_counted_and_flagged() -> None:
    """没有结果的工具（执行中断）也消耗了墙钟时间：要计入并标记，不能丢。"""
    trace = derive.derive_turn(_turn(1000.0, 1003.0), [_tool_call(1, 1000.0, "t-a", "exec")])

    assert trace["tools"] == [{"name": "exec", "ms": 3000.0, "status": "cancelled"}]


def test_event_without_metadata_is_tolerated() -> None:
    """turn_events.metadata_json 的建表默认值是空串，解析时不能抛异常。"""
    trace = derive.derive_turn(_turn(1000.0, 1001.0), [_event(1, "result", 1001.0)])

    assert trace["turn_id"] == "turn-1"
    assert isinstance(trace["findings"], list)




def _planning_start(seq: int, at: float, call_id: str, tool: str) -> dict:
    """工具执行开始：progress + call_kind=tool_planning（工具自己的时钟）。"""
    return _event(
        seq,
        "progress",
        at,
        trace_kind="call_status",
        call_kind="tool_planning",
        call_id=call_id,
        call_state="running",
        tool_name=tool,
    )


def _planning_end(seq: int, at: float, call_id: str, tool: str, state: str = "complete") -> dict:
    """工具执行结束：同一 call_id，complete 或 error。"""
    return _event(
        seq,
        "progress",
        at,
        trace_kind="call_status",
        call_kind="tool_planning",
        call_id=call_id,
        call_state=state,
        tool_name=tool,
    )


def test_tool_execution_envelope_is_not_a_model_round() -> None:
    """工具的 call_status 不能算成模型往返。

    模型轮次与工具执行共用同一个 call_status 信封，只靠 trace_kind 区分。
    把 tool_planning 也算进去，一次模型调用会被数成三次，而 62 秒的搜索
    超时会被画成「模型想了 62 秒」。
    """
    events = [
        _event(1, "progress", 1000.0, trace_kind="call_status",
               call_kind="agent_loop_round", call_id="m1", call_state="running"),
        _event(2, "progress", 1005.0, trace_kind="call_status",
               call_kind="agent_loop_round", call_id="m1", call_state="complete"),
        _planning_start(3, 1005.1, "tool-1", "web_search"),
        _planning_end(4, 1067.0, "tool-1", "web_search", state="error"),
    ]

    trace = derive.derive_turn(_turn(1000.0, 1067.0), events)

    assert len(trace["rounds"]) == 1
    assert trace["rounds"][0]["ms"] == 5000.0
    assert [tool["name"] for tool in trace["tools"]] == ["web_search"]


def test_batch_timestamps_do_not_leak_between_parallel_tools() -> None:
    """并行批次里，快工具不能继承慢兄弟的时长。

    tool_call/tool_result 是批次级时间戳：同一批的每个调用共享批次的开始
    与结束。一个 1 秒就完成的 paper_search 挨着 82 秒超时的 web_search，
    若用批次事件配对就会显示成 82 秒。tool_planning 有每次调用自己的时钟。
    """
    events = [
        # 批次级事件：两个调用共享同一对时间戳
        _tool_call(1, 2000.0, "t-slow", "web_search"),
        _tool_call(2, 2000.0, "t-fast", "paper_search"),
        _tool_result(3, 2082.0, "t-slow", error=True),
        _tool_result(4, 2082.0, "t-fast"),
        # 每次调用自己的真实时钟
        _planning_start(5, 2000.1, "t-slow", "web_search"),
        _planning_start(6, 2000.2, "t-fast", "paper_search"),
        _planning_end(7, 2001.2, "t-fast", "paper_search"),
        _planning_end(8, 2082.9, "t-slow", "web_search", state="error"),
    ]

    trace = derive.derive_turn(_turn(2000.0, 2082.9), events)

    by_name = {tool["name"]: tool for tool in trace["tools"]}
    assert by_name["paper_search"]["ms"] == 1000.0
    assert by_name["web_search"]["ms"] == 82800.0
    # 并集仍然只覆盖真实墙钟跨度，而不是两者相加
    assert trace["breakdown"]["tool_ms"] == pytest.approx(82800.0, abs=1.0)


def test_failed_tool_is_detected_from_nested_metadata() -> None:
    """失败标记藏在 tool_metadata.error 里也要能认出来。

    只看顶层 is_error/error 会把每个超时都报成 ok，于是「本轮 58% 的时间
    浪费在失败调用上」这个最该被看到的结论永远不会出现。
    """
    events = [
        _tool_call(1, 3000.0, "t-a", "web_search"),
        _event(2, "tool_result", 3001.0, call_id="t-a", tool="web_search",
               tool_metadata={"error": "operation timed out"}),
        _tool_call(3, 3001.0, "t-b", "web_fetch"),
        _tool_result(4, 3003.0, "t-b"),
    ]

    trace = derive.derive_turn(_turn(3000.0, 3003.0), events)

    statuses = {tool["name"]: tool["status"] for tool in trace["tools"]}
    assert statuses == {"web_search": "error", "web_fetch": "ok"}
    assert trace["tool_failure_rate"] == 0.5
    assert trace["failed_tool_ms"] == 1000.0
    codes = [finding["code"] for finding in trace["findings"]]
    assert "tools_failing" in codes


def test_ledger_pairing_prefers_the_call_inside_the_round() -> None:
    """嵌套模型调用（reason 工具）的账本行不能配给外层轮次。

    reason 工具在同一 turn 下再发一次模型请求，账本里就多出一行。按「开始
    时间最近」配对会把这行塞给外层轮次，算出比轮次自身还长的生成时间。
    """
    events = [
        _event(1, "progress", 4000.0, trace_kind="call_status",
               call_kind="agent_loop_round", call_id="m1", call_state="running"),
        _event(2, "progress", 4010.0, trace_kind="call_status",
               call_kind="agent_loop_round", call_id="m1", call_state="complete"),
    ]
    calls = [
        # 外层轮次自己的调用：4.0s 起，10s 长
        {"call_id": "real", "started_at": 4000.0,
         "usage": {"duration_seconds": 10.0, "generation_seconds": 9.0,
                   "ttft_seconds": 1.0, "completion_tokens": 300}},
        # reason 工具内部的嵌套调用：落在轮次窗口内但只有 0.5s
        {"call_id": "nested", "started_at": 4004.0,
         "usage": {"duration_seconds": 0.5, "generation_seconds": 0.4,
                   "ttft_seconds": 0.1, "completion_tokens": 20}},
    ]

    trace = derive.derive_turn(_turn(4000.0, 4010.0), events, calls)

    assert trace["rounds"][0]["completion_tokens"] == 300
    assert trace["rounds"][0]["gen_ms"] == 9000.0
    # 生成时间永远不能超过它所在的轮次
    assert trace["rounds"][0]["gen_ms"] <= trace["rounds"][0]["ms"]


def test_tool_name_never_falls_back_to_the_agent() -> None:
    """没记到工具名时宁可叫 tool，也不能借用 agent 名。

    tool_call 事件的 source 是 agent（chat），把它当工具名会让整张工具表
    全变成 chat——线上第一次看这份数据时就是这样。
    """
    event = _event(1, "tool_call", 5000.0, call_id="t-a")
    event["source"] = "chat"

    trace = derive.derive_turn(
        _turn(5000.0, 5001.0),
        [event, _tool_result(2, 5001.0, "t-a")],
    )

    assert trace["tools"][0]["name"] == "tool"


# ---------------------------------------------------------------------------
# 本轮问题（用户提问）：列表页靠它区分「这是哪一轮」，所以链接不能断。
# ---------------------------------------------------------------------------


def test_question_is_passed_through_and_capped() -> None:
    """问题文本透传到结果里，超长时截断并标记。

    列表一次显示几十轮，不截断会让 payload 膨胀；但必须能知道被截断了。
    """
    short = _turn(6000.0, 6001.0)
    short["user_message"] = "用一句话说明什么是傅里叶变换"
    short["user_message_id"] = 13
    trace = derive.derive_turn(short, [])
    assert trace["user_message"] == "用一句话说明什么是傅里叶变换"
    assert trace["user_message_id"] == 13
    assert trace["user_message_truncated"] is False

    long_prompt = "长" * (derive.USER_MESSAGE_CAP + 40)
    long_turn = _turn(6000.0, 6001.0)
    long_turn["user_message"] = long_prompt
    trace = derive.derive_turn(long_turn, [])
    assert len(trace["user_message"]) == derive.USER_MESSAGE_CAP
    assert trace["user_message_truncated"] is True


def test_question_defaults_to_empty_when_absent() -> None:
    """旧数据/进行中的轮次没有问题文本时给空串，不能抛异常。"""
    trace = derive.derive_turn(_turn(6000.0, 6001.0), [])

    assert trace["user_message"] == ""
    assert trace["user_message_id"] is None
    assert trace["user_message_truncated"] is False


def _prompt_db(tmp_path, *, assistant_id: int | None, done_user_id: int | None):
    """建一个只含取问题所需三张表的最小库。"""
    import sqlite3

    path = tmp_path / "chat_history.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE turns (id TEXT PRIMARY KEY, assistant_message_id INTEGER);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, role TEXT, content TEXT, parent_message_id INTEGER
            );
            CREATE TABLE turn_events (
                turn_id TEXT, type TEXT, metadata_json TEXT
            );
            """
        )
        conn.execute("INSERT INTO turns VALUES ('t1', ?)", (assistant_id,))
        conn.execute(
            "INSERT INTO messages VALUES (15, 'user', '搜一下 2025 年诺贝尔物理学奖颁给了谁', NULL)"
        )
        conn.execute(
            "INSERT INTO messages VALUES (16, 'assistant', '结果如下……', 15)"
        )
        if done_user_id is not None:
            import json

            conn.execute(
                "INSERT INTO turn_events VALUES ('t1', 'done', ?)",
                (json.dumps({"user_message_id": done_user_id}),),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def test_load_prompts_follows_the_assistant_parent_link(tmp_path) -> None:
    """主路径：turn → assistant 消息 → parent_message_id → 用户消息。"""
    from deeptutor.services.tracing import store

    path = _prompt_db(tmp_path, assistant_id=16, done_user_id=None)
    prompts = store.load_prompts(["t1"], db_path=path)

    assert prompts["t1"]["user_message_id"] == 15
    assert "诺贝尔" in prompts["t1"]["user_message"]


def test_load_prompts_falls_back_to_the_done_envelope(tmp_path) -> None:
    """assistant_message_id 为空时，用 DONE 信封里的 user_message_id 兜底。"""
    from deeptutor.services.tracing import store

    path = _prompt_db(tmp_path, assistant_id=None, done_user_id=15)
    prompts = store.load_prompts(["t1"], db_path=path)

    assert prompts["t1"]["user_message_id"] == 15
    assert "诺贝尔" in prompts["t1"]["user_message"]


def test_missing_database_yields_empty_results(tmp_path) -> None:
    """干净安装（没有任何数据库）时，分析接口回答「没有数据」而不是 500。"""
    from deeptutor.services.tracing import derive, store

    absent = tmp_path / "not-created.db"

    assert store.load_turn("t1", db_path=absent) is None
    assert store.list_recent_turns(db_path=absent) == []
    assert store.load_events("t1", db_path=absent) == []
    assert store.load_prompts(["t1"], db_path=absent) == {}
    assert derive.derive_recent(db_path=absent) == []
    assert derive.derive_turn_by_id("t1", db_path=absent) is None
    # 只读分析不允许顺手把库建出来
    assert not absent.exists()


def test_recent_list_carries_the_question(tmp_path) -> None:
    """端到端：列表接口拿到的每一轮都带问题文本。

    这条把 _derive_rows → load_prompts → derive_turn 串起来，
    避免只测单点、实际列表里问题列全是空的。
    """
    import sqlite3

    from deeptutor.services.tracing import derive

    db = tmp_path / "chat_history.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE turns (
                id TEXT PRIMARY KEY, session_id TEXT, capability TEXT, status TEXT,
                error TEXT, created_at REAL, updated_at REAL, finished_at REAL,
                failure_code TEXT, retryable INTEGER, assistant_message_id INTEGER
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, role TEXT, content TEXT, parent_message_id INTEGER
            );
            CREATE TABLE turn_events (
                turn_id TEXT, seq INTEGER, type TEXT, source TEXT, stage TEXT,
                metadata_json TEXT, timestamp REAL
            );
            """
        )
        conn.execute(
            "INSERT INTO turns VALUES ('t1','s1','chat','completed','',1000.0,1003.0,1003.0,'',0,2)"
        )
        conn.execute("INSERT INTO messages VALUES (1,'user','用一句话说明什么是傅里叶变换',NULL)")
        conn.execute("INSERT INTO messages VALUES (2,'assistant','就是把信号拆成不同频率的正弦波。',1)")
        conn.commit()
    finally:
        conn.close()

    traces = derive.derive_recent(db_path=db, usage_path=tmp_path / "absent.sqlite3")

    assert len(traces) == 1
    assert traces[0]["user_message"] == "用一句话说明什么是傅里叶变换"
    assert traces[0]["user_message_id"] == 1
    assert traces[0]["total_ms"] > 0

