"""Exercise 5 — Streamlit approval UI for the HITL PR review agent.

Run with:
    uv run streamlit run app.py

Goal: wrap the LangGraph built in exercises 1–4 in a web UI that adapts to
the confidence bucket of each PR.

Routing thresholds (common/schemas.py):
    > 72%        auto_approve     UI shows a success card; reviewer does nothing
    58 – 72%     human_approval   UI shows Approve / Reject / Edit buttons
    <  58%       escalate         UI shows a question form for the reviewer
"""

from __future__ import annotations

import asyncio
import uuid

import streamlit as st
from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

import sqlite3
from common.db import db_path
from exercises.exercise_4_audit import build_graph


load_dotenv()


# ─── Session state ─────────────────────────────────────────────────────────
if "thread_id" not in st.session_state:
    st.session_state.thread_id = None
if "pr_url" not in st.session_state:
    st.session_state.pr_url = ""
if "interrupt_payload" not in st.session_state:
    st.session_state.interrupt_payload = None
if "final" not in st.session_state:
    st.session_state.final = None


# ─── Page setup ────────────────────────────────────────────────────────────
st.set_page_config(page_title="Đánh giá PR tự động (HITL)", layout="wide")
st.title("Trợ lý đánh giá PR (HITL PR Review Agent)")


def get_recent_threads():
    """Lấy danh sách các phiên duyệt gần nhất từ bảng audit_events trong SQLite."""
    try:
        conn = sqlite3.connect(db_path())
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT thread_id,
                   pr_url,
                   MIN(timestamp)        AS started,
                   MAX(timestamp)        AS last_event,
                   MAX(risk_level)       AS worst_risk,
                   COUNT(*)              AS events
              FROM audit_events
             GROUP BY thread_id, pr_url
             ORDER BY MAX(timestamp) DESC
             LIMIT 15
        """)
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception:
        return []


# ─── Sidebar — recent sessions ─────────────────────────────────────────────
with st.sidebar:
    st.header("Lịch sử phiên duyệt")
    threads = get_recent_threads()
    if threads:
        for t in threads:
            short_id = t["thread_id"][:8]
            pr_name = t["pr_url"].rstrip("/").split("/")[-1]
            risk_badge = {"low": "🟢 Thấp", "med": "🟡 Vừa", "high": "🔴 Cao"}.get(t["worst_risk"], "⚪")
            label = f"{risk_badge} | PR #{pr_name} ({short_id})"
            if st.button(label, key=f"session_{t['thread_id']}", use_container_width=True):
                st.session_state.thread_id = t["thread_id"]
                st.session_state.pr_url = t["pr_url"]
                st.session_state.interrupt_payload = None
                st.session_state.final = None
                st.rerun()
    else:
        st.caption("Chưa có phiên duyệt nào được lưu trong database.")


# ─── Top form — start a new review ─────────────────────────────────────────
with st.form("start"):
    pr_url = st.text_input(
        "Đường dẫn Pull Request trên GitHub (PR URL)",
        value=st.session_state.pr_url,
        placeholder="https://github.com/VinUni-AI20k/PR-Demo/pull/1",
    )
    submitted = st.form_submit_button("Bắt đầu đánh giá PR", type="primary")


# ─── Renderers per interrupt kind ──────────────────────────────────────────
def render_approval_card(payload: dict) -> dict | None:
    """Nhánh 58–72%: hiển thị kết quả phân tích và 3 nút Duyệt / Từ chối / Chỉnh sửa."""
    conf = payload["confidence"]
    st.subheader(f"Yêu cầu phê duyệt từ Reviewer — Độ tin cậy: {conf:.0%}")
    st.caption(f"Lý do tự đánh giá: {payload['confidence_reasoning']}")
    st.markdown(f"**Tóm tắt nội dung thay đổi:**\n\n{payload['summary']}")

    severity_trans = {
        "blocker": "Ngăn chặn (Blocker)",
        "issue": "Lỗi (Issue)",
        "suggestion": "Góp ý",
        "nit": "Chi tiết nhỏ",
    }
    for c in payload.get("comments", []):
        sev_label = severity_trans.get(c["severity"], c["severity"])
        st.markdown(f"- **[{sev_label}]** `{c['file']}:{c.get('line') or '?'}` — {c['body']}")

    with st.expander("Xem chi tiết thay đổi code (Diff)"):
        st.code(payload.get("diff_preview", ""), language="diff")

    feedback = st.text_input("Ý kiến phản hồi / ghi chú bổ sung (không bắt buộc)", key="approval_feedback")
    col1, col2, col3 = st.columns(3)
    if col1.button("Duyệt (Approve)", type="primary", use_container_width=True):
        return {"choice": "approve", "feedback": feedback}
    if col2.button("Từ chối (Reject)", use_container_width=True):
        return {"choice": "reject", "feedback": feedback}
    if col3.button("Chỉnh sửa (Edit)", use_container_width=True):
        return {"choice": "edit", "feedback": feedback}
    return None


def render_escalation_card(payload: dict) -> dict | None:
    """Nhánh < 58%: hiển thị cảnh báo rủi ro cao và form các câu hỏi chất vấn từ LLM."""
    conf = payload["confidence"]
    st.subheader(f"Cảnh báo rủi ro cao: Cần giải trình — Độ tin cậy: {conf:.0%}")
    st.caption(f"Lý do: {payload['confidence_reasoning']}")
    if payload.get("risk_factors"):
        st.error("⚠️ Các yếu tố rủi ro phát hiện: " + ", ".join(payload["risk_factors"]))
    st.markdown(f"**Tóm tắt:** {payload['summary']}")
    st.write("**Vui lòng trả lời các câu hỏi làm rõ dưới đây để Agent tái tổng hợp bản review:**")

    with st.form("escalation"):
        answers: dict[str, str] = {}
        for i, q in enumerate(payload.get("questions", [])):
            answers[q] = st.text_input(f"Câu hỏi {i+1}: {q}", key=f"q_{i}")
        submitted_answers = st.form_submit_button("Gửi câu trả lời", type="primary")
        if submitted_answers:
            return answers
    return None


# ─── Drive the graph ───────────────────────────────────────────────────────
async def run_graph(pr_url: str, thread_id: str, resume_value=None):
    """Thực thi đồ thị LangGraph với checkpointer AsyncSqliteSaver."""
    async with AsyncSqliteSaver.from_conn_string(db_path()) as cp:
        await cp.setup()
        app = build_graph(cp)
        cfg = {"configurable": {"thread_id": thread_id}}

        if resume_value is None:
            result = await app.ainvoke({"pr_url": pr_url, "thread_id": thread_id}, cfg)
        else:
            result = await app.ainvoke(Command(resume=resume_value), cfg)
        return result


# ─── Main flow ─────────────────────────────────────────────────────────────
if submitted and pr_url:
    st.session_state.pr_url = pr_url
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.interrupt_payload = None
    st.session_state.final = None

    with st.spinner("Đang tải dữ liệu PR và gọi AI phân tích mã nguồn..."):
        result = asyncio.run(run_graph(pr_url, st.session_state.thread_id))

    if "__interrupt__" in result:
        st.session_state.interrupt_payload = result["__interrupt__"][0].value
    else:
        st.session_state.final = result

# Render the current interrupt card, if any
payload = st.session_state.interrupt_payload
if payload is not None:
    kind = payload["kind"]
    answer = render_approval_card(payload) if kind == "approval_request" else render_escalation_card(payload)
    if answer is not None:
        with st.spinner("Đang gửi phản hồi và tiếp tục quy trình..."):
            result = asyncio.run(run_graph(
                st.session_state.pr_url, st.session_state.thread_id, resume_value=answer,
            ))
        if "__interrupt__" in result:
            st.session_state.interrupt_payload = result["__interrupt__"][0].value
        else:
            st.session_state.interrupt_payload = None
            st.session_state.final = result
        st.rerun()

# Render final state, if reached
if st.session_state.final is not None:
    final = st.session_state.final
    action = final.get("final_action", "?")
    if action.startswith("auto") or action.startswith("committed"):
        st.success(f"✓ Đã hoàn tất ({action}) — Nhận xét review đã được đăng thành công lên {st.session_state.pr_url}")
    elif action == "rejected":
        st.warning("Đã từ chối PR — Không đăng nhận xét lên GitHub")
    else:
        st.info(f"Kết quả xử lý: {action}")
    st.caption(f"Mã phiên (thread_id): `{st.session_state.thread_id}`  ·  Lệnh phát lại lịch sử: "
               f"`uv run python -m audit.replay --thread {st.session_state.thread_id}`")
