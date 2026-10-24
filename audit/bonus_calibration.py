"""Compare model confidence with observed human approval in local audit logs.

Usage: uv run python -m audit.bonus_calibration

Approval reflects reviewer choice, not ground-truth review correctness.
"""

from __future__ import annotations

import sqlite3

from common.db import db_path


def main() -> None:
    with sqlite3.connect(db_path()) as conn:
        rows = conn.execute("""
            SELECT a.thread_id, a.confidence,
                   (SELECT h.decision FROM audit_events AS h
                     WHERE h.thread_id = a.thread_id
                       AND h.action = 'human_approval'
                       AND h.decision IN ('approve', 'reject')
                     ORDER BY h.id DESC LIMIT 1) AS final_choice
              FROM audit_events AS a
             WHERE a.action = 'analyze'
               AND a.id = (SELECT MIN(b.id) FROM audit_events AS b
                            WHERE b.thread_id = a.thread_id AND b.action = 'analyze')
        """).fetchall()
    completed = [(confidence, choice) for _, confidence, choice in rows if choice]
    if not completed:
        raise SystemExit("No completed human review sessions found")

    n = len(completed)
    mean_confidence = sum(conf for conf, _ in completed) / n
    approval_rate = sum(choice == "approve" for _, choice in completed) / n
    print(f"N={n} mean_model_confidence={mean_confidence:.3f} human_approval_rate={approval_rate:.3f}")
    print(f"difference={mean_confidence - approval_rate:+.3f}")
    print("This is a descriptive comparison, not a calibration verdict: human approvals are")
    print("not correctness labels. Deliberately rejecting demo PRs biases the approval rate.")


if __name__ == "__main__":
    main()
