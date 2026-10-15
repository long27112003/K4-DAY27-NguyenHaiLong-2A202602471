"""Exercise 1 — Confidence scoring + routing.

Build a small LangGraph that fetches a PR, analyzes it, then routes to one of
three terminal nodes by confidence. Goal: see the three branches print
different messages on different PRs.

"""

from __future__ import annotations

import argparse

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from rich.console import Console

from common.github import fetch_pr
from common.llm import get_llm
from common.schemas import (
    AUTO_APPROVE_THRESHOLD,
    ESCALATE_THRESHOLD,
    PRAnalysis,
    ReviewState,
)


console = Console()


def node_fetch_pr(state: ReviewState) -> dict:
    console.print("[cyan]→ fetch_pr[/cyan]")
    with console.status("[dim]Fetching PR from GitHub...[/dim]"):
        pr = fetch_pr(state["pr_url"])
    console.print(f"  [green]✓[/green] {len(pr.files_changed)} files, head {pr.head_sha[:7]}")
    return {
        "pr_title": pr.title, "pr_diff": pr.diff,
        "pr_files": pr.files_changed, "pr_head_sha": pr.head_sha,
    }


def node_analyze(state: ReviewState) -> dict:
    console.print("[cyan]→ analyze[/cyan]")
    # Khởi tạo mô hình LLM từ factory và ép kiểu đầu ra thành cấu trúc PRAnalysis (Pydantic model)
    llm = get_llm().with_structured_output(PRAnalysis)
    
    # Gọi LLM phân tích tiêu đề và nội dung diff thay đổi của PR
    with console.status("[dim]LLM thinking...[/dim]"):
        analysis = llm.invoke([
            {
                "role": "system",
                "content": (
                    "You are a senior code reviewer. Structured output. "
                    "Calibrate your confidence carefully: "
                    "Rate confidence > 0.75 for straightforward, safe PRs (e.g. minor field additions). "
                    "Rate confidence between 0.60 and 0.72 for moderate PRs needing human verification. "
                    "Rate confidence < 0.58 for high-risk PRs with security flaws (e.g. MD5 hashing, plain text secrets, SQL injection, missing tests)."
                ),
            },
            {"role": "user", "content": f"Title: {state['pr_title']}\nDiff:\n{state['pr_diff']}"},
        ])
    
    console.print(f"  [green]✓[/green] confidence={analysis.confidence:.0%}, {len(analysis.comments)} comment(s)")
    # Cập nhật kết quả phân tích vào state của LangGraph
    return {"analysis": analysis}


def node_route(state: ReviewState) -> dict:
    console.print("[cyan]→ route[/cyan]")
    # Lấy điểm tự tin (confidence: 0.0 - 1.0) do LLM tự đánh giá từ bước analyze
    c = state["analysis"].confidence
    
    # Phân loại nhánh dựa trên các ngưỡng định sẵn:
    # - Nếu confidence >= AUTO_APPROVE_THRESHOLD (0.73) -> auto_approve (an toàn, tự duyệt)
    # - Nếu confidence < ESCALATE_THRESHOLD (0.58) -> escalate (rủi ro cao, cần hỏi thêm người)
    # - Từ 58% đến 72% -> human_approval (rủi ro trung bình, cần người duyệt trực tiếp)
    if c >= AUTO_APPROVE_THRESHOLD:
        decision = "auto_approve"
    elif c < ESCALATE_THRESHOLD:
        decision = "escalate"
    else:
        decision = "human_approval"
        
    console.print(f"  [green]✓[/green] decision=[bold]{decision}[/bold] (confidence={c:.0%})")
    return {"decision": decision}


def node_auto_approve(state: ReviewState) -> dict:
    console.print("[green]✓ AUTO APPROVE[/green] — high confidence, no human needed")
    return {"final_action": "auto_approved"}


def node_human_approval(state: ReviewState) -> dict:
    console.print("[yellow]✓ HUMAN APPROVAL[/yellow] — placeholder, exercise 2 will pause here")
    return {"final_action": "pending_human_approval"}


def node_escalate(state: ReviewState) -> dict:
    console.print("[red]✓ ESCALATE[/red] — placeholder, exercise 3 will ask the reviewer questions")
    return {"final_action": "pending_escalation"}


def build_graph():
    # Khởi tạo đồ thị StateGraph với lược đồ dữ liệu ReviewState
    g = StateGraph(ReviewState)
    
    # 1. Khai báo các Node tham gia vào đồ thị
    for name, fn in [
        ("fetch_pr", node_fetch_pr),
        ("analyze", node_analyze),
        ("route", node_route),
        ("auto_approve", node_auto_approve),
        ("human_approval", node_human_approval),
        ("escalate", node_escalate),
    ]:
        g.add_node(name, fn)
        
    # 2. Tạo luồng chạy tuần tự ban đầu: Bắt đầu -> Lấy PR -> Phân tích -> Định tuyến
    g.add_edge(START, "fetch_pr")
    g.add_edge("fetch_pr", "analyze")
    g.add_edge("analyze", "route")
    
    # 3. Tạo cạnh rẽ nhánh có điều kiện (conditional edges):
    # Dựa vào giá trị state['decision'] trả về từ node_route để chuyển hướng sang node tương ứng
    g.add_conditional_edges(
        "route",
        lambda s: s["decision"],
        {
            "auto_approve": "auto_approve",
            "human_approval": "human_approval",
            "escalate": "escalate",
        },
    )
    
    # 4. Cả 3 nhánh đích sau khi thực hiện xong đều kết thúc đồ thị (END)
    g.add_edge("auto_approve", END)
    g.add_edge("human_approval", END)
    g.add_edge("escalate", END)
    
    # Biên dịch đồ thị
    return g.compile()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", required=True)
    args = parser.parse_args()

    console.rule("[bold]Exercise 1 — confidence routing[/bold]")
    console.print(f"[dim]PR: {args.pr}[/dim]\n")

    app = build_graph()
    final = app.invoke({"pr_url": args.pr})

    console.rule("Final")
    console.print(f"confidence = {final['analysis'].confidence:.0%}")
    console.print(f"action     = {final.get('final_action')}")


if __name__ == "__main__":
    main()
