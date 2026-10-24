# mergeq

A local, sequential GitHub pull request merge queue. Add PR numbers, start the worker, and let it update branches, wait for required checks and approvals, and merge one at a time. A PR that is already current and ready can merge immediately.

Runs on macOS and Linux with Python 3.9+ and the [GitHub CLI](https://cli.github.com/). No Python runtime dependencies, hosted service, or GitHub App required. Supports github.com repositories.

## Install

Install and authenticate `gh`, then install mergeq:

```sh
gh auth login
pipx install git+https://github.com/bpeterman/mergeq.git
mergeq --help
```

Or install into a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install git+https://github.com/bpeterman/mergeq.git
```

## Repository setup

On the target branch, configure **required status checks** and **Require branches to be up to date before merging**, using a repository ruleset or classic branch protection. Keep your required reviews and other policies. Disable **Require merge queue**: this tool performs normal immediate merges rather than adding PRs to GitHub's native queue.

Your `gh` account needs access to read branch rules and protection, update PR branches, and merge PRs. The worker refuses to run if it cannot verify strict required checks. There is no admin-bypass option. Use an account that cannot bypass protections for GitHub to enforce those rules independently of the script; audit any existing bypass permissions on your account.

## Usage

From a checkout of the repository:

```sh
mergeq add 123 456 789
mergeq status
mergeq start
```

PRs are processed in insertion order. The default merge method is squash. Branch updates merge the target branch into the PR branch, equivalent to GitHub's **Update branch** action.

```text
#123  Merge accepted; confirming…
#123  Merged ✓
#456  Branch updated; waiting for fresh checks…
#456  Waiting for required checks… 4/6 passed
```

Add more PRs from another terminal while the worker runs:

```sh
mergeq add 812 913
```

PR URLs are accepted too. Specify a repository to operate outside its checkout:

```sh
mergeq add https://github.com/octocat/example/pull/123 --repo octocat/example
mergeq start --repo octocat/example
mergeq status --repo octocat/example
```

All URLs in a batch must belong to the selected repository. Duplicate active PRs are ignored. Queues and worker locks are scoped by repository; target branches are recorded when you add each PR.

```sh
mergeq remove 789       # Remove a queued or blocked PR
mergeq retry 456        # Clear a block without changing queue order
mergeq start            # Resume processing
mergeq stop             # Ask the worker to stop; retain the queue
```

`start` runs in the foreground and exits when the queue is empty. If the worker has already exited, run `start` again after adding new PRs. Ctrl-C also stops it. A stop or removal cannot cancel a GitHub API request already in flight; an update or merge may complete. Keep your computer awake and the terminal open, or run it in a terminal multiplexer.

## Failures and restarts

Conflicts, failed or cancelled required checks, requested review changes, permission/API errors, and timeouts block the first PR and stop the worker. Later PRs stay queued. Fix the issue, run `mergeq retry NUMBER`, then `mergeq start`. Missing approvals and pending checks wait until the timeout. Transient network errors also stop safely; retry explicitly when connectivity returns.

The SQLite queue survives terminal closure and restarts. The worker re-reads remote PR state on every pass, so a PR that merged before a crash is recognized as complete on restart. One worker per repository is enforced with an OS file lock. Separate state directories or different computers do not share locks; use a single queue worker for a repository.

The worker checks the PR again after reading CI results and sends the expected head SHA with each update and merge. A concurrent push invalidates that attempt. Strict up-to-date protection lets GitHub reject a merge if the target branch advances in the final race window. GitHub remains responsible for enforcing required checks and approvals at merge time. Other people can still merge directly; their changes may cause another update/check cycle.

Polling defaults to 15 seconds and the per-PR timeout to two hours (reset on worker restart):

```sh
mergeq start --interval 10 --timeout 3600 --method squash
```

Available methods: `squash`, `merge`, `rebase`; the selected method must be allowed by the repository. Polling affects the time to notice readiness; there is no extra merge-group CI run.

State defaults to `~/.local/state/mergeq/`. Override it with `MERGEQ_STATE_DIR` or `--state-dir PATH` on each command. Logs go to the foreground terminal. Completed and removed entries remain in the local database as history; `status` shows active entries only. GitHub credentials are managed by `gh` and are never stored in the queue database.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 -m mergeq --help
```

Tests use simulated GitHub responses; they never update or merge real PRs. Coverage includes queue persistence and order, worker locking, blocked heads, stale CI/head races, protection verification, stop requests, and restart recovery.

## License

MIT. See [LICENSE](LICENSE).
