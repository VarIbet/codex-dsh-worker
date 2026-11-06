<!-- BEGIN CODEX-DSH-WORKER -->
## Codex + DSH Worker

Codex remains the primary agent: understand requirements, make architecture/security decisions, split tasks, review outputs, integrate changes and deliver the result. DSH is a lower-cost execution assistant. Actively evaluate it for token-heavy reading, code/test generation, repetitive work and bounded local iteration, including simple but large tasks. Prefer deterministic scripts when sufficient. Optimize total cost, quality and waiting time rather than delegation count.

All delegated DSH calls must go through `{{WORKER_ROOT}}/bin/dsh-worker`. Read `{{WORKER_ROOT}}/README.md` first. Never bypass the Gateway or disable its outer Seatbelt sandbox. Use a sanitized, minimal input snapshot; never mount or directly edit the primary project. Do not send unrelated private data, credentials or full conversation history. User daily DSH configuration must remain separate.

Approval mode is mandatory. An explicit user request to use DSH authorizes only its specified task; do not ask again within that scope. Preserve genuine authorization evidence, including its task/budget correspondence, then use `authorize <id> --user-message <file>`. Never invent authorization, reuse old grants or treat a shell auto-review as user approval. This evidence path is an attestation, not independently verified user identity.

When proactively recommending DSH, first present: task, acceptance criteria, reason, expected benefit, API fees, model/request/output/time budgets and workspace permissions; ask for approval and wait for an explicit answer. If rejected, perform the task with Codex. Batch approval covers only the explicitly listed related subtasks. Optional `authorize <id> --dialog` obtains a macOS dialog decision: Return approves; Escape rejects. Do not operate the dialog for the user.

Create one task per scope with `create --request <file> [--input <sanitized-directory>]`; authorize it, then `run <id>`. Grants bind task identity/content/budgets, expire after 30 minutes and are consumed once. No unauthorized budget increases, expensive model switches, installations, background services, other agents or elevated permissions. The sole supported model is deepseek-flash, with bounded retries and one concurrent task.

Review output/summary.json, output/metrics.json and necessary diffs before expanding detailed logs. A completed model turn is not acceptance. Inspect code, run meaningful verification, then integrate results yourself; avoid rereading all intermediate work. Report actual usage and distinguish price estimates from unknown account debits.

AGENTS.md is a behavioral instruction, not a security boundary. The Gateway validates grants; the outer OS sandbox limits task processes. Same-user modification, GUI automation, OS/runtime vulnerabilities and escaped process groups remain limitations. DSH's inner danger-full-access mode is permitted only under the Gateway's enforced outer Seatbelt, to avoid nested-sandbox failure. Never run it independently with that mode.
<!-- END CODEX-DSH-WORKER -->
