"""Exercise 4 — Structured SQLite audit trail + durable checkpointer.

Zero setup — SQLite stores everything in a single file (`./hitl_audit.db`).
The audit_events schema is created automatically on first connection.

Goals:

1. Use AsyncSqliteSaver so the graph can resume after a crash.
2. Define and emit an `AuditEntry` (common/schemas.py) for every meaningful step,
   so the full session can be replayed.
3. Verify with `uv run python -m audit.replay --thread <id>`.

Approach:
    - Read `node_fetch_pr` below — it is the one fully-worked example. Pay attention
      to *why* each AuditEntry field has the value it does at that step.
    - For every other node, you decide what to log. Field reference:
      `common/schemas.py:AuditEntry`. Helper: `risk_level_for(confidence)`.
    - Implement the one-line body of `audit()`. Everything else (graph wiring,
      checkpointer setup, interrupt/resume loop) is already done for you.

Implemented audit steps (10 total): the audit() body, ONE AuditEntry per node for
analyze/route/commit/auto_approve/synthesize, TWO each for human_approval and
escalate (before + after the interrupt).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
import uuid

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from rich.console import Console
from rich.panel import Panel

from common.db import checkpoint_saver, write_audit_event
from common.github import fetch_pr, post_review_comment
from common.llm import get_llm
from common.schemas import (
    AUTO_APPROVE_THRESHOLD,
    ESCALATE_THRESHOLD,
    AuditEntry,
    PRAnalysis,
    ReviewState,
    risk_level_for,
)


console = Console()
AGENT_ID = "pr-review-agent@v0.1"


async def audit(state, entry: AuditEntry) -> None:
    """Ghi một dòng AuditEntry có cấu trúc vào bảng `audit_events` trong SQLite.
    
    Lấy `thread_id` và `pr_url` trực tiếp từ state để caller chỉ cần truyền thông tin entry.
    """
    await write_audit_event(thread_id=state["thread_id"], pr_url=state["pr_url"], entry=entry)


# ─── Reference example — read this carefully ───────────────────────────────
async def node_fetch_pr(state):
    console.print("[cyan]→ fetch_pr[/cyan]")
    t0 = time.monotonic()
    with console.status("[dim]Fetching PR from GitHub...[/dim]"):
        pr = fetch_pr(state["pr_url"])
    console.print(f"  [green]✓[/green] {len(pr.files_changed)} files, head {pr.head_sha[:7]}")
    # We've only fetched the diff, not analyzed it. So:
    #   - confidence is unknown → 0.0
    #   - risk_level can't be derived from confidence yet → "med" as neutral default
    #   - decision is "pending" — nothing has been decided
    #   - reviewer_id is None — no human is involved at this stage
    #   - reason is a short human-readable summary of what happened
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="fetch_pr",
        confidence=0.0,
        risk_level="med",
        decision="pending",
        reason=f"Fetched {len(pr.files_changed)} files, head={pr.head_sha[:7]}",
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {
        "pr_title": pr.title,
        "pr_diff": pr.diff,
        "pr_files": pr.files_changed,
        "pr_head_sha": pr.head_sha,
    }
# ───────────────────────────────────────────────────────────────────────────


async def node_analyze(state):
    console.print("[cyan]→ analyze[/cyan]")
    t0 = time.monotonic()
    llm = get_llm().with_structured_output(PRAnalysis)
    with console.status("[dim]LLM reviewing the diff...[/dim]"):
        a: PRAnalysis = await llm.ainvoke([
            {"role": "system", "content": (
                "You are a senior code reviewer. Structured output.\n"
                "QUAN TRỌNG: Toàn bộ nội dung phân tích (summary, confidence_reasoning, risk_factors, comments, escalation_questions) "
                "BẮT BUỘC PHẢI ĐƯỢC VIẾT HOÀN TOÀN BẰNG TIẾNG VIỆT tự nhiên, chuẩn kỹ thuật phần mềm.\n"
                "Calibrate your confidence carefully:\n"
                "- Đặt confidence > 0.75 cho các PR đơn giản, an toàn.\n"
                "- Đặt confidence trong khoảng 0.60 đến 0.72 cho các PR có độ phức tạp vừa phải cần người kiểm tra.\n"
                "- Đặt confidence < 0.58 cho các PR rủi ro cao, có lỗ hổng bảo mật (ví dụ: băm mật khẩu bằng MD5, lưu token dạng plain text, SQL injection, thiếu test).\n"
                "Nếu confidence < 60%, bạn BẮT BUỘC phải tạo 2–4 câu hỏi chất vấn (escalation_questions) HOÀN TOÀN BẰNG TIẾNG VIỆT cụ thể, rõ ràng, "
                "trích dẫn rõ file và đoạn code trong diff để hỏi reviewer con người."
            )},
            {"role": "user", "content": f"Title: {state['pr_title']}\nDiff:\n{state['pr_diff']}"},
        ])
    console.print(f"  [green]✓[/green] confidence={a.confidence:.0%}, {len(a.comments)} comment(s)")
    
    # Ghi nhận AuditEntry cho bước analyze
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="analyze",
        confidence=a.confidence,
        risk_level=risk_level_for(a.confidence),
        reviewer_id=None,
        decision="pending",
        reason=a.confidence_reasoning,
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {"analysis": a}


async def node_route(state):
    console.print("[cyan]→ route[/cyan]")
    t0 = time.monotonic()
    c = state["analysis"].confidence
    if c >= AUTO_APPROVE_THRESHOLD:
        decision = "auto_approve"
    elif c < ESCALATE_THRESHOLD:
        decision = "escalate"
    else:
        decision = "human_approval"
    console.print(f"  [green]✓[/green] decision=[bold]{decision}[/bold] (confidence={c:.0%})")
    
    # Ghi nhận AuditEntry cho bước route
    route_decision = "auto" if decision == "auto_approve" else ("escalate" if decision == "escalate" else "pending")
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="route",
        confidence=c,
        risk_level=risk_level_for(c),
        reviewer_id=None,
        decision=route_decision,
        reason=f"Confidence {c:.0%} -> routed to {decision}",
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    if decision in ("human_approval", "escalate"):
        await audit(state, AuditEntry(
            agent_id=AGENT_ID,
            action=decision,
            confidence=c,
            risk_level=risk_level_for(c),
            decision="pending" if decision == "human_approval" else "escalate",
            reason="Awaiting reviewer input",
            execution_time_ms=0,
        ))
    return {"decision": decision}


async def node_human_approval(state):
    a = state["analysis"]

    t_resume = time.monotonic()
    resp = interrupt({
        "kind": "approval_request",
        "pr_url": state["pr_url"],
        "confidence": a.confidence,
        "confidence_reasoning": a.confidence_reasoning,
        "summary": a.summary,
        "comments": [c.model_dump() for c in a.comments],
        "diff_preview": state["pr_diff"][:2000],
    })

    # Record the reviewer's decision after resuming.
    reviewer = os.environ.get("GITHUB_USER") or "reviewer"
    choice = resp.get("choice", "unknown")
    feedback = resp.get("feedback") or ""
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="human_approval",
        confidence=a.confidence,
        risk_level=risk_level_for(a.confidence),
        reviewer_id=reviewer,
        decision=choice,
        reason=feedback or f"Reviewer chose {choice}",
        execution_time_ms=int((time.monotonic() - t_resume) * 1000),
    ))
    return {"human_choice": resp.get("choice"), "human_feedback": resp.get("feedback")}


def _render_comment_body(state) -> str:
    a = state["analysis"]
    lines = [f"### Automated review (confidence {a.confidence:.0%})", "", a.summary, ""]
    for c in a.comments:
        lines.append(f"- **[{c.severity}]** `{c.file}:{c.line or '?'}` — {c.body}")
    if state.get("human_feedback"):
        lines.append(f"\n_Reviewer note: {state['human_feedback']}_")
    if state.get("escalation_answers"):
        lines.append("\n_Reviewer answered escalation questions:_")
        for q, ans in state["escalation_answers"].items():
            lines.append(f"> **{q}** {ans}")
    return "\n".join(lines)


def _post(state) -> str:
    try:
        post_review_comment(state["pr_url"], _render_comment_body(state))
        console.print(f"  [green]✓[/green] posted comment to {state['pr_url']}")
        return "committed"
    except Exception as e:
        console.print(f"  [red]✗[/red] post failed: {e}")
        return "commit_failed"


async def node_commit(state):
    console.print("[cyan]→ commit[/cyan]")
    t0 = time.monotonic()
    # Two paths converge here:
    #   1. human_approval → commit (only post if approved)
    #   2. escalate → synthesize → final human approval → commit
    if state.get("human_choice") == "approve":
        action = _post(state)
    else:
        console.print(f"  [yellow]·[/yellow] skipping comment (choice={state.get('human_choice')})")
        action = "rejected"
    
    # Ghi nhận AuditEntry tổng kết những gì đã commit (hoặc reject)
    commit_decision = "approve" if action in ("committed", "committed_after_escalation", "auto_approved") else "reject"
    conf = state["analysis"].confidence if state.get("analysis") else 0.0
    reviewer = os.environ.get("GITHUB_USER") if state.get("human_choice") or state.get("escalation_answers") else None
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="commit",
        confidence=conf,
        risk_level=risk_level_for(conf),
        reviewer_id=reviewer,
        decision=commit_decision,
        reason=f"Action: {action}",
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {"final_action": action}


async def node_auto_approve(state):
    console.print("[cyan]→ auto_approve[/cyan]  [dim]high confidence — posting directly[/dim]")
    t0 = time.monotonic()
    a = state["analysis"]
    action = _post(state)
    # Ghi nhận AuditEntry khi tự động duyệt (không có người can thiệp, decision="auto")
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="auto_approve",
        confidence=a.confidence,
        risk_level=risk_level_for(a.confidence),
        reviewer_id=None,
        decision="auto",
        reason=f"High confidence ({a.confidence:.0%}) - auto approved",
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {"final_action": f"auto_{action}"}


async def node_escalate(state):
    a = state["analysis"]
    questions = a.escalation_questions or ["What is the intent of this PR?"]

    t_resume = time.monotonic()
    answers = interrupt({
        "kind": "escalation",
        "pr_url": state["pr_url"],
        "confidence": a.confidence,
        "confidence_reasoning": a.confidence_reasoning,
        "summary": a.summary,
        "risk_factors": a.risk_factors,
        "questions": questions,
    })

    # Record the supplied answers after resuming.
    reviewer = os.environ.get("GITHUB_USER") or "reviewer"
    summary_answers = "; ".join(f"{q}: {ans}" for q, ans in answers.items()) if isinstance(answers, dict) else str(answers)
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="escalate",
        confidence=a.confidence,
        risk_level=risk_level_for(a.confidence),
        reviewer_id=reviewer,
        decision="escalate",
        reason=f"Reviewer answered: {summary_answers[:200]}",
        execution_time_ms=int((time.monotonic() - t_resume) * 1000),
    ))
    return {"escalation_answers": answers}


async def node_synthesize(state):
    console.print("[cyan]→ synthesize[/cyan]")
    t0 = time.monotonic()
    qa = "\n".join(f"Q: {q}\nA: {a}" for q, a in (state.get("escalation_answers") or {}).items())
    llm = get_llm().with_structured_output(PRAnalysis)
    with console.status("[dim]LLM refining review with reviewer answers...[/dim]"):
        refined: PRAnalysis = await llm.ainvoke([
            {
                "role": "system",
                "content": (
                    "You are a senior code reviewer synthesizing reviewer answers. "
                    "QUAN TRỌNG: Toàn bộ nội dung bản đánh giá tinh chỉnh (summary, comments, reasoning) "
                    "BẮT BUỘC PHẢI ĐƯỢC VIẾT HOÀN TOÀN BẰNG TIẾNG VIỆT tự nhiên, chuẩn kỹ thuật phần mềm. "
                    "Structured output."
                ),
            },
            {"role": "user", "content": f"Diff:\n{state['pr_diff']}\n\nQ&A:\n{qa}"},
        ])
    console.print(f"  [green]✓[/green] refined confidence={refined.confidence:.0%}")
    # Ghi nhận AuditEntry với độ tin cậy mới (refined.confidence)
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="synthesize",
        confidence=refined.confidence,
        risk_level=risk_level_for(refined.confidence),
        reviewer_id=None,
        decision="pending",
        reason=f"Refined review synthesized (confidence updated to {refined.confidence:.0%})",
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="human_approval",
        confidence=refined.confidence,
        risk_level=risk_level_for(refined.confidence),
        decision="pending",
        reason="Awaiting final approval of refined review",
        execution_time_ms=0,
    ))
    return {"analysis": refined}


async def node_edit_review(state):
    """Rewrite the proposed review from human feedback, then ask for approval again."""
    feedback = (state.get("human_feedback") or "").strip()
    if not feedback:
        raise ValueError("Edit requires reviewer feedback describing the requested change")

    t0 = time.monotonic()
    previous = state["analysis"]
    llm = get_llm().with_structured_output(PRAnalysis)
    refined: PRAnalysis = await llm.ainvoke([
        {
            "role": "system",
            "content": (
                "You are a senior code reviewer. Rewrite the proposed review in natural "
                "Vietnamese using the human reviewer's feedback. Check every comment "
                "against the PR diff; do not invent files or line numbers. Return structured output."
            ),
        },
        {
            "role": "user",
            "content": (
                f"PR diff:\n{state['pr_diff']}\n\nPrevious review:\n"
                f"{previous.model_dump_json()}\n\nReviewer edits:\n{feedback}"
            ),
        },
    ])
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="edit_review",
        confidence=refined.confidence,
        risk_level=risk_level_for(refined.confidence),
        reviewer_id=os.environ.get("GITHUB_USER") or "reviewer",
        decision="pending",
        reason="Rewrote review from reviewer feedback; awaiting final approval",
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="human_approval",
        confidence=refined.confidence,
        risk_level=risk_level_for(refined.confidence),
        decision="pending",
        reason="Awaiting approval of edited review",
        execution_time_ms=0,
    ))
    return {"analysis": refined, "human_choice": None}


def build_graph(checkpointer):
    g = StateGraph(ReviewState)
    for name, fn in [
        ("fetch_pr", node_fetch_pr), ("analyze", node_analyze), ("route", node_route),
        ("auto_approve", node_auto_approve), ("human_approval", node_human_approval),
        ("commit", node_commit), ("escalate", node_escalate), ("synthesize", node_synthesize),
        ("edit_review", node_edit_review),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "fetch_pr")
    g.add_edge("fetch_pr", "analyze")
    g.add_edge("analyze", "route")
    g.add_conditional_edges(
        "route", lambda s: s["decision"],
        {"auto_approve": "auto_approve", "human_approval": "human_approval", "escalate": "escalate"},
    )
    g.add_edge("auto_approve", END)
    g.add_conditional_edges(
        "human_approval",
        lambda s: "edit_review" if s.get("human_choice") == "edit" else "commit",
        {"edit_review": "edit_review", "commit": "commit"},
    )
    g.add_edge("edit_review", "human_approval")
    g.add_edge("commit", END)
    g.add_edge("escalate", "synthesize")
    g.add_edge("synthesize", "human_approval")
    return g.compile(checkpointer=checkpointer)


def handle_interrupt(payload):
    kind = payload["kind"]
    if kind == "approval_request":
        console.print(Panel.fit(
            payload["summary"], title=f"conf={payload['confidence']:.0%}", border_style="green",
        ))
        choice = console.input("approve/reject/edit? ").strip().strip("\ufeff").lower()
        return {"choice": choice, "feedback": console.input("Feedback: ").strip().strip("\ufeff")}
    return {q: console.input(f"Q: {q}\nA: ").strip().strip("\ufeff") for q in payload["questions"]}


async def run(pr_url: str, thread_id: str | None):
    thread_id = thread_id or str(uuid.uuid4())
    console.rule("[bold]Exercise 4 — SQLite audit trail[/bold]")
    console.print(f"[dim]PR: {pr_url}[/dim]")
    console.print(f"[dim]thread_id = {thread_id}[/dim]\n")

    async with checkpoint_saver() as cp:
        app = build_graph(cp)
        cfg = {"configurable": {"thread_id": thread_id}}

        result = await app.ainvoke({"pr_url": pr_url, "thread_id": thread_id}, cfg)
        while "__interrupt__" in result:
            payload = result["__interrupt__"][0].value
            result = await app.ainvoke(Command(resume=handle_interrupt(payload)), cfg)

        console.rule("Final")
        console.print(f"final_action = {result.get('final_action')}")
        console.print(f"\n[dim]Replay:[/dim] uv run python -m audit.replay --thread {thread_id}")


def main():
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--pr", required=True)
    p.add_argument("--thread", help="Resume an existing thread")
    args = p.parse_args()
    asyncio.run(run(args.pr, args.thread))


if __name__ == "__main__":
    main()
