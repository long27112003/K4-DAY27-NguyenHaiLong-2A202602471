"""Demonstrate parallel escalation to two independent reviewer threads.

Usage: uv run python -m audit.bonus_multi_reviewer

The answers here are a local simulation for the instructor's PR #2 risk questions.
No GitHub comment or other external side effect is performed.
"""

from __future__ import annotations

import asyncio
import operator
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt


QUESTIONS = [
    "Vì sao dùng MD5 cho mật khẩu trong PR #2?",
    "Token đồng bộ lưu dạng rõ ở đâu và có kế hoạch bảo vệ không?",
    "Truy vấn Storage.add có tham số hóa để tránh SQL injection không?",
]
REVIEWER_ANSWERS = {
    "security": {
        QUESTIONS[0]: "MD5 không phù hợp để lưu mật khẩu; cần Argon2id hoặc bcrypt.",
        QUESTIONS[1]: "Token dạng rõ là rủi ro; cần kho bí mật và cơ chế xoay vòng.",
        QUESTIONS[2]: "Cần kiểm tra query thực tế và dùng tham số hóa trước khi merge.",
    },
    "maintainer": {
        QUESTIONS[0]: "Cần migration từ hash cũ và test đăng nhập sau nâng cấp.",
        QUESTIONS[1]: "Chưa thấy test bảo vệ token hoặc quy trình thu hồi trong diff.",
        QUESTIONS[2]: "Nên thêm test với đầu vào chứa dấu nháy trước khi duyệt.",
    },
}


class ReviewerState(TypedDict, total=False):
    reviewer: str
    questions: list[str]
    answers: dict[str, str]


class FanoutState(TypedDict, total=False):
    parent_thread: str
    questions: list[str]
    reviewer: str
    responses: Annotated[list[dict[str, object]], operator.add]
    summary: dict[str, object]


def ask_reviewer(state: ReviewerState) -> ReviewerState:
    answers = interrupt({"reviewer": state["reviewer"], "questions": state["questions"]})
    return {"answers": answers}


def reviewer_graph():
    graph = StateGraph(ReviewerState)
    graph.add_node("ask", ask_reviewer)
    graph.add_edge(START, "ask")
    graph.add_edge("ask", END)
    return graph.compile(checkpointer=InMemorySaver())


def fanout(state: FanoutState) -> list[Send]:
    return [
        Send("reviewer", {
            "parent_thread": state["parent_thread"],
            "questions": state["questions"],
            "reviewer": name,
        })
        for name in ("security", "maintainer")
    ]


async def run_reviewer(state: FanoutState) -> FanoutState:
    name = state["reviewer"]
    thread_id = f"{state['parent_thread']}:{name}"
    graph = reviewer_graph()
    cfg = {"configurable": {"thread_id": thread_id}}
    pending = await graph.ainvoke({"reviewer": name, "questions": state["questions"]}, cfg)
    assert "__interrupt__" in pending
    answers = {q: REVIEWER_ANSWERS[name][q] for q in state["questions"]}
    completed = await graph.ainvoke(Command(resume=answers), cfg)
    return {"responses": [{"reviewer": name, "thread_id": thread_id,
                            "answers": completed["answers"]}]}


def summarize(state: FanoutState) -> FanoutState:
    responses = state["responses"]
    if len(responses) != 2 or {r["reviewer"] for r in responses} != {"security", "maintainer"}:
        raise ValueError("Both reviewer threads must answer before synthesis")
    return {"summary": {
        "reviewer_threads": [r["thread_id"] for r in responses],
        "questions": len(state["questions"]),
        "answers_received": sum(len(r["answers"]) for r in responses),
        "decision": "human_follow_up_required",
    }}


async def main() -> None:
    graph = StateGraph(FanoutState)
    graph.add_node("dispatch", lambda _: {})
    graph.add_node("reviewer", run_reviewer)
    graph.add_node("summarize", summarize)
    graph.add_edge(START, "dispatch")
    graph.add_conditional_edges("dispatch", fanout)
    graph.add_edge("reviewer", "summarize")
    graph.add_edge("summarize", END)
    app = graph.compile(checkpointer=InMemorySaver())
    result = await app.ainvoke(
        {"parent_thread": "pr-demo-2-fanout", "questions": QUESTIONS, "responses": []},
        {"configurable": {"thread_id": "pr-demo-2-fanout"}},
    )
    print(result["summary"])


if __name__ == "__main__":
    asyncio.run(main())
