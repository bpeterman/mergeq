"""Persistent local queue; all remote operations use the authenticated gh CLI."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time


class QueueError(Exception):
    pass


class GitHub:
    def run(self, *args):
        try:
            result = subprocess.run(
                ["gh", *args], capture_output=True, text=True, timeout=60
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise QueueError(str(exc)) from exc
        if result.returncode:
            raise QueueError(result.stderr.strip() or result.stdout.strip() or "gh failed")
        return result.stdout

    def api(self, path, method="GET", **fields):
        args = ["api", path, "--method", method]
        for key, value in fields.items():
            args.extend(["-f", f"{key}={value}"])
        return json.loads(self.run(*args))

    def repo(self):
        return json.loads(self.run("repo", "view", "--json", "nameWithOwner"))["nameWithOwner"]

    def pr(self, repo, number):
        return json.loads(self.run(
            "pr", "view", str(number), "--repo", repo, "--json",
            "number,state,isDraft,baseRefName,headRefOid,mergeable,mergeStateStatus,reviewDecision,url",
        ))

    def require_protection(self, repo, branch):
        # Effective rules include inherited organization rulesets. Classic branch
        # protection is checked separately because it is not a ruleset.
        from urllib.parse import quote
        branch = quote(branch, safe="")
        rules = self.api(f"repos/{repo}/rules/branches/{branch}")
        if any(rule["type"] == "merge_queue" for rule in rules):
            raise QueueError("Disable Require merge queue for this target branch first.")
        strict = any(
            rule["type"] == "required_status_checks"
            and rule.get("parameters", {}).get("strict_required_status_checks_policy")
            and rule.get("parameters", {}).get("required_status_checks")
            for rule in rules
        )
        if strict:
            return
        try:
            protection = self.api(f"repos/{repo}/branches/{branch}/protection")
        except QueueError as exc:
            raise QueueError(
                "Cannot verify branch protection. Enable required checks with "
                "Require branches to be up to date, and grant permission to read rules. "
                + str(exc)
            ) from exc
        checks = protection.get("required_status_checks") or {}
        if not (checks.get("strict") and (checks.get("checks") or checks.get("contexts"))):
            raise QueueError(
                "Target branch must require at least one status check and "
                "Require branches to be up to date before merging."
            )

    def update(self, repo, number, sha):
        # Compare-and-swap avoids updating a different head after a concurrent push.
        self.api(f"repos/{repo}/pulls/{number}/update-branch", "PUT", expected_head_sha=sha)

    def checks(self, repo, number):
        # gh uses nonzero exits for pending/failed checks, even with JSON output.
        try:
            result = subprocess.run([
                "gh", "pr", "checks", str(number), "--repo", repo,
                "--required", "--json", "name,bucket",
            ], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise QueueError(str(exc)) from exc
        try:
            checks = json.loads(result.stdout)
        except ValueError as exc:
            raise QueueError(result.stderr.strip() or "Unable to read required checks") from exc
        if not isinstance(checks, list) or result.returncode not in (0, 1, 8):
            raise QueueError(result.stderr.strip() or "Unable to read required checks")
        return checks

    def merge(self, repo, number, sha, method):
        # REST performs an immediate merge or fails; it never enables auto-merge.
        result = self.api(f"repos/{repo}/pulls/{number}/merge", "PUT", sha=sha, merge_method=method)
        if not result.get("merged"):
            raise QueueError(result.get("message", "GitHub rejected the merge"))


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.directory / "queue.sqlite3"), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo TEXT NOT NULL, number INTEGER NOT NULL, branch TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', message TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX IF NOT EXISTS active_pr ON entries(repo,number)
                WHERE status IN ('queued','blocked');
            CREATE TABLE IF NOT EXISTS controls (
                repo TEXT PRIMARY KEY, stop INTEGER NOT NULL DEFAULT 0
            );
        """)

    def add(self, repo, number, branch):
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO entries(repo,number,branch) VALUES (?,?,?)",
                (repo, number, branch),
            )
        return cursor.rowcount > 0

    def entries(self, repo):
        return self.db.execute(
            "SELECT * FROM entries WHERE repo=? AND status IN ('queued','blocked') ORDER BY id",
            (repo,),
        ).fetchall()

    def set(self, entry, status, message=""):
        with self.db:
            cursor = self.db.execute(
                "UPDATE entries SET status=?,message=? WHERE id=? AND status IN ('queued','blocked')",
                (status, message, entry["id"]),
            )
        return cursor.rowcount > 0

    def stop(self, repo, value):
        with self.db:
            self.db.execute(
                "INSERT INTO controls(repo,stop) VALUES (?,?) "
                "ON CONFLICT(repo) DO UPDATE SET stop=excluded.stop", (repo, int(value)),
            )

    def stopping(self, repo):
        row = self.db.execute("SELECT stop FROM controls WHERE repo=?", (repo,)).fetchone()
        return bool(row and row[0])

    def active(self, entry):
        row = self.db.execute("SELECT status FROM entries WHERE id=?", (entry["id"],)).fetchone()
        return bool(row and row[0] == "queued")

    @contextlib.contextmanager
    def lock(self, repo):
        name = hashlib.sha256(repo.encode()).hexdigest() + ".lock"
        with (self.directory / name).open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise QueueError("A worker is already running for this repository.") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def log(number, message):
    print(f"#{number}  {message}", flush=True)


def step(gh, store, repo, entry, method):
    """One reconciliation step. Return True only after completion."""
    number = entry["number"]
    def report(message):
        store.set(entry, "queued", message)
        log(number, message)

    pr = gh.pr(repo, number)
    if pr["state"] == "MERGED":
        store.set(entry, "done", "Merged")
        log(number, "Merged ✓")
        return True
    if pr["state"] != "OPEN" or pr["isDraft"]:
        raise QueueError("PR is closed or a draft.")
    if pr["baseRefName"] != entry["branch"]:
        raise QueueError("Target branch changed since this PR was queued; remove and add it again.")
    gh.require_protection(repo, pr["baseRefName"])
    if pr["mergeable"] == "CONFLICTING" or pr["mergeStateStatus"] == "DIRTY":
        raise QueueError("Merge conflict; resolve it and run mergeq retry.")
    if pr["mergeStateStatus"] == "BEHIND":
        if store.stopping(repo) or not store.active(entry):
            return False
        gh.update(repo, number, pr["headRefOid"])
        report("Branch updated; waiting for fresh checks…")
        return False
    if pr["mergeable"] == "UNKNOWN":
        report("Waiting for GitHub to calculate mergeability…")
        return False
    checks = gh.checks(repo, number)
    failed = [c["name"] for c in checks if c["bucket"] in ("fail", "cancel")]
    if failed:
        raise QueueError("Required checks failed: " + ", ".join(failed))
    if not checks or any(c["bucket"] not in ("pass", "skipping") for c in checks):
        passed = sum(c["bucket"] in ("pass", "skipping") for c in checks)
        report(f"Waiting for required checks… {passed}/{len(checks)} passed")
        return False
    if pr.get("reviewDecision") == "CHANGES_REQUESTED":
        raise QueueError("Changes requested in review.")
    if pr.get("reviewDecision") == "REVIEW_REQUIRED":
        report("Waiting for required approvals…")
        return False
    if pr["mergeStateStatus"] not in ("CLEAN", "UNSTABLE"):
        report("Waiting for merge requirements: " + pr["mergeStateStatus"])
        return False
    # Re-read after checks; never use an earlier head's passing results.
    latest = gh.pr(repo, number)
    if latest != pr:
        report("PR changed; checking again…")
        return False
    if store.stopping(repo) or not store.active(entry):
        return False
    gh.merge(repo, number, pr["headRefOid"], method)
    # Confirm on the next step. If interrupted, restart discovers the merged PR.
    report("Merge accepted; confirming…")
    return False


def worker(gh, store, repo, interval, timeout, method):
    wake = threading.Event()
    old_handlers = {}

    def stop_signal(*_):
        store.stop(repo, True)
        wake.set()

    with store.lock(repo):
        store.stop(repo, False)
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, stop_signal)
        current, began = None, time.monotonic()
        try:
            print(f"Processing {repo}. Ctrl-C stops after any in-flight API request.", flush=True)
            while not store.stopping(repo):
                entries = store.entries(repo)
                if not entries:
                    print("Queue empty.", flush=True)
                    return 0
                entry = entries[0]
                if entry["status"] == "blocked":
                    log(entry["number"], "Blocked: " + entry["message"])
                    return 1
                if current != entry["id"]:
                    current, began = entry["id"], time.monotonic()
                try:
                    if time.monotonic() - began > timeout:
                        raise QueueError("Timed out waiting for this PR; inspect it and run mergeq retry.")
                    if step(gh, store, repo, entry, method):
                        continue
                except QueueError as exc:
                    store.set(entry, "blocked", str(exc))
                    log(entry["number"], "Blocked: " + str(exc))
                    return 1
                # Poll stop state too, so another terminal can stop promptly.
                deadline = time.monotonic() + interval
                while time.monotonic() < deadline and not store.stopping(repo):
                    wake.wait(min(1, max(0, deadline - time.monotonic())))
            print("Stopped. Queue saved.", flush=True)
            return 0
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)


def pr_reference(value, repo):
    if value.isdigit() and int(value) > 0:
        return int(value)
    match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/([1-9][0-9]*)/?", value)
    if not match:
        raise QueueError(f"Invalid PR number or github.com PR URL: {value}")
    if match[1].lower() != repo.lower():
        raise QueueError(f"PR URL belongs to {match[1]}, but selected repository is {repo}.")
    return int(match[2])


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    for name in ("add", "status", "start", "stop", "remove", "retry"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--repo", "-R", help="OWNER/REPO; defaults to current git repository")
        cmd.add_argument("--state-dir", default=os.environ.get("MERGEQ_STATE_DIR", "~/.local/state/mergeq"))
        if name in ("add", "remove", "retry"):
            cmd.add_argument("prs", nargs="+", help="PR numbers or github.com PR URLs")
        if name == "start":
            cmd.add_argument("--interval", type=float, default=15, help="Polling interval in seconds")
            cmd.add_argument("--timeout", type=float, default=7200, help="Maximum seconds per PR per run")
            cmd.add_argument("--method", choices=("squash", "merge", "rebase"), default="squash")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    gh = GitHub()
    store = None
    try:
        repo = args.repo or gh.repo()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise QueueError("Repository must use OWNER/REPO format.")
        repo = repo.lower()
        store = Store(args.state_dir)
        if args.command == "add":
            # Validate the entire batch before adding anything.
            batch = []
            for ref in args.prs:
                number = pr_reference(ref, repo)
                pr = gh.pr(repo, number)
                if pr["state"] != "OPEN" or pr["isDraft"]:
                    raise QueueError(f"#{number} must be an open, non-draft PR.")
                batch.append((number, pr["baseRefName"]))
            for number, branch in batch:
                added = store.add(repo, number, branch)
                log(number, "Queued" if added else "Already queued")
        elif args.command == "status":
            entries = store.entries(repo)
            for position, entry in enumerate(entries, 1):
                print(f"{position}. #{entry['number']} → {entry['branch']}  {entry['status']}  {entry['message']}")
            if not entries:
                print("Queue empty.")
        elif args.command == "stop":
            store.stop(repo, True)
            print("Stop requested. An in-flight update or merge may finish; queue is saved.")
        elif args.command in ("remove", "retry"):
            for ref in args.prs:
                number = pr_reference(ref, repo)
                entry = next((e for e in store.entries(repo) if e["number"] == number), None)
                if entry is None:
                    raise QueueError(f"#{number} is not in the queue.")
                store.set(entry, "removed" if args.command == "remove" else "queued")
                log(number, "Removed" if args.command == "remove" else "Ready to retry; run mergeq start")
        else:
            if args.interval <= 0 or args.timeout <= 0:
                raise QueueError("Interval and timeout must be positive.")
            return worker(gh, store, repo, args.interval, args.timeout, args.method)
        return 0
    except (QueueError, sqlite3.Error, OSError) as exc:
        print(f"mergeq: {exc}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.db.close()


if __name__ == "__main__":
    sys.exit(main())
