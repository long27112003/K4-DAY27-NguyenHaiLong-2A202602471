"""Offline checks for the previously unexercised PR posting path.

No request leaves this process: GitHub uses an httpx mock transport and the
LangGraph commit node replaces its posting and audit dependencies.
"""

import json
import os
import unittest
from unittest.mock import patch

import httpx

from common.github import post_review_comment
from common.schemas import PRAnalysis, ReviewComment
from exercises import exercise_4_audit as audit_graph


PR_URL = "https://github.com/example/project/pull/7"


class TestGithubCommentTransport(unittest.TestCase):
    def test_approved_comment_targets_pull_discussion(self):
        seen = []

        def handle(request):
            seen.append(request)
            return httpx.Response(201, json={"id": 1})

        client = httpx.Client(transport=httpx.MockTransport(handle))
        with patch("common.github.httpx.Client", return_value=client):
            with patch.dict(os.environ, {"GITHUB_TOKEN": "offline-test-token"}):
                post_review_comment(PR_URL, "Review body")

        self.assertEqual(len(seen), 1)
        request = seen[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            str(request.url),
            "https://api.github.com/repos/example/project/issues/7/comments",
        )
        self.assertEqual(json.loads(request.content), {"body": "Review body"})


class TestAuditCommit(unittest.TestCase):
    def setUp(self):
        self.state = {
            "pr_url": PR_URL,
            "human_choice": "approve",
            "analysis": PRAnalysis(
                summary="Tóm tắt kiểm tra ngoại tuyến.",
                confidence=0.68,
                confidence_reasoning="Cần người duyệt.",
                comments=[ReviewComment(
                    file="app.py", line=10, severity="suggestion",
                    body="Kiểm tra đầu vào.",
                )],
            ),
        }

    @staticmethod
    def finish_immediate(coroutine):
        # The mocked audit completes immediately, so no Windows event loop or
        # loopback socket is needed in a restricted offline environment.
        try:
            coroutine.send(None)
        except StopIteration as done:
            return done.value
        raise AssertionError("Unexpected pending await in offline commit check")

    def test_approval_posts_once_and_records_commit(self):
        recorded = []

        async def fake_audit(_state, entry):
            recorded.append(entry)

        with patch.object(audit_graph, "post_review_comment") as post:
            with patch.object(audit_graph, "audit", new=fake_audit):
                result = self.finish_immediate(audit_graph.node_commit(self.state))

        self.assertEqual(result, {"final_action": "committed"})
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], PR_URL)
        self.assertIn("Kiểm tra đầu vào.", post.call_args.args[1])
        self.assertEqual(recorded[0].decision, "approve")

    def test_rejection_never_posts(self):
        recorded = []

        async def fake_audit(_state, entry):
            recorded.append(entry)

        self.state["human_choice"] = "reject"
        with patch.object(audit_graph, "post_review_comment") as post:
            with patch.object(audit_graph, "audit", new=fake_audit):
                result = self.finish_immediate(audit_graph.node_commit(self.state))

        self.assertEqual(result, {"final_action": "rejected"})
        post.assert_not_called()
        self.assertEqual(recorded[0].decision, "reject")


if __name__ == "__main__":
    unittest.main()
