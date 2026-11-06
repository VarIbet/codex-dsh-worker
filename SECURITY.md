# Security model and limitations

This is an experimental local development tool for trusted, explicitly authorized tasks.
Do not treat it as an isolation boundary for hostile code.

## Separate controls

1. **Codex instructions** ask the supervisor to obtain genuine user authorization, minimize
   task inputs, review results and avoid direct DSH/API calls. Instructions are bypassable.
2. **Gateway validation** binds a single-use grant to task ID, budgets and a hash of the
   request, inputs and initial workspace. Grants expire after 30 minutes and fail closed.
   `--user-message` is a supervisor attestation, not authenticated conversation identity.
   The macOS dialog requires an explicit decision, but same-user GUI automation is possible.
   Return deliberately selects Approve; Escape selects Reject.
3. **Outer macOS Seatbelt** applies to the DSH host and descendants. Only task paths and
   necessary runtime/system paths are readable, only workspace/home/tmp writable, and only
   the task proxy port reachable. DSH's inner full-access preset avoids nested sandboxing;
   it must never be used outside this enforced outer boundary.

The proxy holds the real API key, fixes the model and API origin, rejects redirects,
limits requests/output/request size, strips non-allowlisted top-level fields and does not
serve search, fetch, upload or arbitrary HTTP proxying. Task data is sent to DeepSeek as
necessary model input; do not include material you are not authorized to send.

## Credentials and local data

- Keys are read via hidden terminal input or an explicitly selected file at configuration.
- Keychain is preferred; 0600 file fallback requires an explicit flag. Source files remain.
- Children receive a per-task local proxy token, not the real API credential.
- Logs and persisted sessions can include private prompts, tool output and model reasoning.
  Installation directories default to 0700, newly generated files to 0600.
- Do not commit runtime/config/tasks/logs, user authorization evidence or real test reports.
  Heuristic file-name exclusions do not replace input review.

## Remaining risks

Seatbelt is a legacy macOS interface, not a VM or separate account. Runtime directories,
Homebrew Cellar/opt/lib, system files and broad file metadata remain visible. Processes can
be created inside the boundary; disabling built-in agent tools cannot prevent all scripts
from implementing equivalent behavior. No account-level spend limit is installed.
Node's 2 GiB heap limit is not a hard memory/disk/process limit for the full process tree.

Same-user modification of the gateway, approval files, dependencies or GUI decisions is
outside the protection offered here. OS, dependency or sandbox vulnerabilities remain.
SIGKILL, shutdown and process-group escape may leave orphan processes; recovery does not
blindly kill reused PIDs. A request submitted before cancellation may still incur charges,
and partial/lost usage reports cannot prove the final account debit.

The server buffers bounded upstream responses and has a per-operation timeout. This is
not a production multi-tenant API service and is intended for one local worker at a time.
Do not expose the task proxy on a public interface or grant extra directories without review.
No Linux/Windows or unsandboxed fallback is available.

## Reporting issues

If the repository's Security tab offers private vulnerability reporting, use it. Otherwise,
open an issue requesting a private contact without disclosing credentials or exploit details.
Never include API keys, personal task logs or authorization evidence in an issue.
