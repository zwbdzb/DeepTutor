"""``deeptutor trace`` — read-only turn latency analysis.

Derived from events the runtime already persists, so this works on historical
turns with no instrumentation and never writes to the databases it reads.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import typer

from deeptutor.services.tracing import derive, store

from .common import console

app = typer.Typer(
    help="Inspect turn latency from persisted events (read-only, no re-run).",
    no_args_is_help=True,
)

_PHASES: tuple[tuple[str, str], ...] = (
    ("queue_ms", "queue"),
    ("context_ms", "context"),
    ("model_ms", "model"),
    ("tool_ms", "tool"),
    ("wait_ms", "wait"),
    ("other_exec_ms", "other"),
    ("persist_ms", "persist"),
    ("unattributed_ms", "unattr"),
)


def _seconds(value: float | None) -> str:
    return "    -" if value is None else f"{value / 1000:6.2f}s"


def _share(value: float | None, total: float) -> str:
    if value is None or total <= 0:
        return "   -"
    return f"{100.0 * value / total:3.0f}%"


def _print_turn(trace: dict) -> None:
    total = float(trace.get("total_ms") or 0.0)
    header = (
        f"{trace['turn_id']}  {trace['capability'] or '?'}  {trace['status']}"
        f"  total {total / 1000:.2f}s"
    )
    if trace.get("estimated"):
        header += "  (estimated: no phase timings recorded)"
    console.print(f"[bold]{header}[/bold]")

    # 问题文本是「这是哪一轮」的标识，放在标题下面一行。
    prompt = str(trace.get("user_message") or "").strip()
    if prompt:
        console.print(f"  [dim]question:[/dim] {prompt}")
    else:
        console.print("  [dim]question: (not recorded for this turn)[/dim]")

    out = []
    for key, label in _PHASES:
        value = trace["breakdown"].get(key)
        out.append(f"  {label:<8}{_seconds(value)}  {_share(value, total):>4}")
    for line in out:
        console.print(line)

    rounds = trace.get("rounds") or []
    if rounds:
        console.print("\n[bold]Rounds[/bold]")
        for rnd in rounds:
            detail = f"  #{rnd['n']}  {rnd['status']:<9}{_seconds(rnd['ms'])}"
            if rnd.get("ttft_ms") is not None:
                detail += f"  ttft {rnd['ttft_ms'] / 1000:.2f}s"
            if rnd.get("gen_ms") is not None:
                detail += f"  gen {rnd['gen_ms'] / 1000:.2f}s"
            if rnd.get("completion_tokens"):
                detail += f"  {rnd['completion_tokens']} tok"
            detail += f"  {rnd.get('tools', 0)} tool(s)"
            console.print(detail)

    tools = trace.get("tools") or []
    if tools:
        console.print("\n[bold]Tools[/bold]")
        for item in tools:
            console.print(f"  {item['name']:<22}{_seconds(item['ms'])}  {item['status']}")

    stages = trace.get("stages") or []
    if stages:
        console.print("\n[bold]Stages[/bold]")
        for stage in stages:
            console.print(f"  {stage['name']:<22}{_seconds(stage['ms'])}")

    findings = trace.get("findings") or []
    if findings:
        console.print("\n[bold]Findings[/bold]")
        for finding in findings:
            console.print(f"  [{finding['severity']}] {finding['text']}")


def _print_rows(traces: list[dict]) -> None:
    """列表视图：每行下面带问题摘要，否则看不出「哪一轮卡」。"""
    for trace in traces:
        when = datetime.fromtimestamp(trace["created_at"]).strftime("%m-%d %H:%M")
        row = (
            f"  {when}  {trace['capability'] or '?':<12}{trace['status']:<10}"
            f"{trace['total_ms'] / 1000:7.2f}s   {trace['top_bottleneck']}"
        )
        if trace.get("estimated"):
            row += "  (est)"
        console.print(row)
        prompt = " ".join(str(trace.get("user_message") or "").split())
        if prompt:
            if len(prompt) > 60:
                prompt = prompt[:60] + "…"
            console.print(f"           [dim]{prompt}[/dim]")


@app.command("turn")
def trace_turn(
    turn_id: str = typer.Argument(..., help="Turn id (from the session store)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the raw structure."),
    db: Path | None = typer.Option(None, "--db", help="Override chat_history.db path."),
    usage_db: Path | None = typer.Option(None, "--usage-db", help="Override usage.sqlite3."),
) -> None:
    """Time breakdown for a single turn."""
    try:
        trace = derive.derive_turn_by_id(turn_id, db_path=db, usage_path=usage_db)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    if trace is None:
        console.print(f"[red]No turn with id {turn_id}[/red]")
        raise typer.Exit(code=1)
    if as_json:
        console.print_json(json.dumps(trace, ensure_ascii=False))
        return
    _print_turn(trace)


@app.command("session")
def trace_session(
    session_id: str | None = typer.Argument(None, help="Defaults to the latest session."),
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=200),
    as_json: bool = typer.Option(False, "--json", help="Emit the raw structure."),
    db: Path | None = typer.Option(None, "--db", help="Override chat_history.db path."),
    usage_db: Path | None = typer.Option(None, "--usage-db", help="Override usage.sqlite3."),
) -> None:
    """Per-turn breakdown for a whole conversation, newest first."""
    try:
        resolved = session_id or store.latest_session_id(db_path=db)
        if not resolved:
            console.print("[yellow]No turns recorded yet.[/yellow]")
            raise typer.Exit(code=1)
        traces = derive.derive_session(
            resolved, limit=limit, db_path=db, usage_path=usage_db
        )
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    if as_json:
        console.print_json(json.dumps(traces, ensure_ascii=False))
        return

    console.print(f"[bold]{resolved}[/bold]  ({len(traces)} turn(s))")
    _print_rows(traces)


@app.command("recent")
def trace_recent(
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=200),
    session_id: str = typer.Option("", "--session", help="Restrict to one conversation."),
    as_json: bool = typer.Option(False, "--json", help="Emit the raw structure."),
    db: Path | None = typer.Option(None, "--db", help="Override chat_history.db path."),
    usage_db: Path | None = typer.Option(None, "--usage-db", help="Override usage.sqlite3."),
) -> None:
    """Newest turns across every conversation, newest first."""
    try:
        traces = derive.derive_recent(
            limit=limit, session_id=session_id, db_path=db, usage_path=usage_db
        )
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    if as_json:
        console.print_json(json.dumps(traces, ensure_ascii=False))
        return

    if not traces:
        console.print("[yellow]No turns recorded yet.[/yellow]")
        raise typer.Exit(code=1)

    console.print(f"[bold]{len(traces)} turn(s)[/bold]")
    _print_rows(traces)


def register(root: typer.Typer) -> None:
    root.add_typer(app, name="trace")
