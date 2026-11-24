# Optional controller

Use this mode when explicit executable acceptance and bounded retries improve the task. Derive checks from the request and existing regressions after reading the implementation. Existing green tests may not cover the requested feature.

Create a contract with `init task.json --goal '...' --workspace . --check 'python -m unittest discover -s tests'`, then inspect it. Commands become argv arrays, never shell expressions. Set `protected_paths` to existing acceptance files/directories that the implementation must preserve. A check may require `success_pattern`. Workspace paths are relative to the contract; check working directories and protected paths are relative to the workspace.

Use `check task.json` for a starting observation, then `run task.json`. The runner saves bounded process evidence, refreshes context before each attempt, feeds concrete failures back, and stops on unchanged failure or exhausted budget. On `stalled`, inspect the evidence and change the failed hypothesis or decomposition; do not weaken acceptance to manufacture progress.

Resume with `run task.json --run-dir <directory> --resume`. It rechecks current files and preserves the cumulative attempt count; the execution-time budget restarts for this explicit invocation. A changed contract requires a new run. `status <directory>` is historical evidence, not fresh validation.

`checks_passed` means the specified commands passed on the recorded file state. A timeout, truncated check output, protected-file change, or stale digest prevents that status. Distinguish passing checks from untested requirements and overall task correctness.

The controller inherits the installed Codex model and uses workspace-write execution. It does not change global settings. It is not an OS sandbox: runtime graphs, deliberately detached processes, and tamper-proof isolation remain outside its guarantees.
