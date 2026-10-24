"""Replay checkpoint history and fork a safe alternative reviewer decision.

Usage: uv run python -m audit.bonus_time_travel --thread <id>

The fork always rejects, so this demonstration cannot post a GitHub comment.
"""

from __future__ import annotations

import argparse
import asyncio

from common.db import checkpoint_saver
from exercises.exercise_4_audit import build_graph


async def show_and_fork(thread_id: str) -> None:
    async with checkpoint_saver() as saver:
        graph = build_graph(saver)
        config = {"configurable": {"thread_id": thread_id}}
        history = [snapshot async for snapshot in graph.aget_state_history(config)]
        approval_points = [
            snapshot for snapshot in reversed(history)
            if snapshot.next == ("human_approval",)
            and snapshot.metadata.get("source") == "loop"
        ]
        if not approval_points:
            raise SystemExit("No human approval checkpoint found for this thread")

        original_had_edit = any(snapshot.next == ("edit_review",) for snapshot in history)
        original_final = next(
            (snapshot.values.get("final_action") for snapshot in history
             if snapshot.values.get("final_action")),
            None,
        )
        before_first_approval = approval_points[0]
        fork = await graph.aupdate_state(
            before_first_approval.config,
            {"human_choice": "reject", "human_feedback": "Safe time-travel comparison"},
            as_node="human_approval",
        )
        alternative = await graph.ainvoke(None, fork)
        print(f"thread={thread_id}")
        print(f"checkpoints={len(history)}")
        print(f"original_edit_path={original_had_edit} original_final={original_final}")
        print(f"fork_path=direct_reject fork_final={alternative.get('final_action')}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread", required=True)
    args = parser.parse_args()
    asyncio.run(show_and_fork(args.thread))


if __name__ == "__main__":
    main()
