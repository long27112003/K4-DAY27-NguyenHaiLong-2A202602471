# Day27 live verification — Nguyễn Hải Long (2A202602471)

Verified with Python 3.11, GitHub REST API and OpenRouter `openai/gpt-4o-mini`. API keys were supplied through process environment variables; no key is stored in this repository.

| Check | Result |
|---|---|
| Exercise 1, PR-Demo #1 | GitHub returned 3 changed files; LLM confidence 68%; `human_approval` branch. |
| Exercise 1, PR-Demo #2 | GitHub returned 4 changed files; LLM confidence 55%; `escalate` branch. |
| Exercise 2, PR-Demo #1 | `interrupt()` displayed diff and review; resumed with `reject`; final action `rejected`; no comment posted. |
| Exercise 3, PR-Demo #2 | Asked four escalation questions, synthesized a refined review (40% confidence), required a final approval decision, then resumed with `reject`; no comment posted. |
| Exercise 4, PR-Demo #1 | `AsyncSqliteSaver` resumed a rejected session; `audit_events` contains fetch, analyze, route, human decision and commit records. |
| Streamlit UI smoke test | `AppTest` loaded the app without errors, displayed the live PR #1 approval card, resumed from the Reject button, and showed the final rejected state without posting a comment. |
| Python source | `python -m compileall -q common exercises app.py audit` passed. |

The test deliberately selected `reject` on the instructor's public demo PRs to avoid publishing an unsolicited comment. The posting path and manual browser interaction have not been verified end to end. A model output cap of 2048 tokens is configured because the default 16384-token reservation exceeded the temporary OpenRouter key's $1 limit before generating a response.

The lab scaffold's optional bonus challenges (checkpoint time travel, confidence calibration, multi-reviewer fan-out and auto-edit) are not evidenced here.
