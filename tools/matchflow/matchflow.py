#!/usr/bin/env python3
"""Local-only matching triage and evidence ledger. No service writes or claims."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

import flags as flagset
import lifecycle
import review
from config import workspace_root, REPOSITORY
from security import scoped

ROOT = workspace_root()
DEFAULT_DB = ROOT / "work" / "matchflow" / "ledger.sqlite3"


def now():
    return lifecycle.stamp()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def tree_digest(path):
    """Fingerprint compile inputs, including headers added after an earlier check."""
    root = Path(path)
    files = sorted(p for p in root.rglob("*") if p.is_file()
                   and p.suffix.lower() in {".cpp", ".c", ".h", ".hpp", ".inl", ".json"})
    if not (root / "pack.json").is_file():
        raise ValueError("pack.json is missing")
    h = hashlib.sha256()
    for p in files:
        h.update(p.relative_to(root).as_posix().encode())
        h.update(b"\0")
        h.update(digest(p).encode())
    return h.hexdigest()


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{6,8}", value):
        raise ValueError("expected a hexadecimal function address")
    return f"0x{int(value, 16):08x}"


def flags(value):
    return [x.strip() for x in value.split(",") if x.strip()] if value else None


def same_flags(a, b):
    # Preserve order: conflicting compiler options can be order-sensitive.
    return [x.lower() for x in a] == [x.lower() for x in b]


def accepted_sets(pack):
    return [pack["flags"]] + pack.get("accept_also", [])


def classify(item):
    """Reviewed facts, not a lane and not a prediction that a function will match."""
    reasons = []
    verdicts = item.get("prior_verdicts", [])
    if isinstance(verdicts, str):
        raise ValueError("prior_verdicts must be a list")
    prior_unfit = "unfit" in verdicts or bool(item.get("integration_failure"))
    if prior_unfit:
        reasons.append("Prior integration failure: local MATCH does not settle file compatibility.")
    if (item.get("flags_suspected") or item.get("repeated_blocker") or
            item.get("give_ups", 0) >= 3 or item.get("open_problem_reports", 0)):
        reasons.append("Repeated attempts or an unresolved report on this function.")
    if item.get("file_problem_reports", 0):
        reasons.append("Reports exist on sibling functions; scope is unknown until reviewed.")
    if item.get("near_misses", 0):
        reasons.append("Near misses are starting material, not a selection preference.")
    if not item.get("history_reviewed", False):
        reasons.append("History has not been reviewed: do not dispatch yet.")
    state = item.get("state", "unknown")
    eligible = state == "open" and bool(item.get("history_reviewed"))
    if state != "open":
        reasons.append(f"Availability is {state!r}; refresh before any claim.")
    review = dict(history_reviewed=bool(item.get("history_reviewed")),
                  prior_verdicts=list(verdicts), verdict_history=item.get("verdict_history", "unknown"),
                  report_scope=item.get("report_scope", "unknown"),
                  report_evidence=item.get("report_evidence"),
                  prior_unfit=prior_unfit,
                  next_experiment=item.get("next_experiment"),
                  integration_plan=item.get("integration_plan"))
    return {"address": address(item["address"]), "review": review,
            "dispatch_ready": eligible, "reasons": reasons,
            "size": item.get("size"), "source_file": item.get("source_file")}


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS attempts (
            claim_id TEXT PRIMARY KEY, address TEXT NOT NULL, lane TEXT NOT NULL,
            model TEXT NOT NULL, variant TEXT NOT NULL, harness TEXT NOT NULL,
            pack_path TEXT NOT NULL, pack_hash TEXT NOT NULL,
            deadline TEXT NOT NULL, created TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'baseline_needed',
            gate TEXT, gate_reason TEXT,
            submission_id TEXT UNIQUE, checkpoint_id TEXT,
            artifact_path TEXT, artifact_hash TEXT, final_check_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES attempts(claim_id),
            stage TEXT NOT NULL, created TEXT NOT NULL, seconds REAL NOT NULL,
            source_path TEXT NOT NULL, source_hash TEXT NOT NULL,
            pack_hash TEXT NOT NULL, flags_json TEXT NOT NULL,
            command_json TEXT NOT NULL, result_json TEXT NOT NULL,
            exit_code INTEGER NOT NULL, valid INTEGER NOT NULL,
            evidence_path TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES attempts(claim_id),
            created TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS legacy_reports (
            path TEXT PRIMARY KEY, hash TEXT NOT NULL, address TEXT,
            claim_id TEXT, imported TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS server_results (
            submission_id TEXT PRIMARY KEY, address TEXT NOT NULL,
            verdict TEXT, detail TEXT, observed TEXT NOT NULL, snapshot_hash TEXT NOT NULL
        );
    """)
    attempt_columns = {r[1] for r in db.execute('PRAGMA table_info(attempts)')}
    if 'assignment_path' not in attempt_columns:
        db.execute('ALTER TABLE attempts ADD COLUMN assignment_path TEXT')
        db.commit()
    if 'review_json' not in attempt_columns:
        # `lane` stays for historical rows/reports.  New attempts record the facts
        # that justified the assignment; no mutually exclusive lane is written.
        db.execute('ALTER TABLE attempts ADD COLUMN review_json TEXT')
        db.commit()
    if 'source_snapshot_path' not in {r[1] for r in db.execute('PRAGMA table_info(checks)')}:
        db.execute('ALTER TABLE checks ADD COLUMN source_snapshot_path TEXT')
        db.commit()
    return db


def event(db, claim, kind, payload):
    db.execute("INSERT INTO events(claim_id,created,kind,payload) VALUES(?,?,?,?)",
               (claim, now(), kind, json.dumps(payload)))


def attempt(db, claim):
    row = db.execute("SELECT * FROM attempts WHERE claim_id=?", (claim,)).fetchone()
    if not row:
        raise ValueError("unknown claim; register an existing claim first")
    return dict(row)


def ensure_active(row):
    if row["state"] in {"given_up", "verified", "unfit", "failed", "needs_review",
                        "submitted", "flag_review"}:
        raise ValueError("attempt is closed or awaiting server review")
    if lifecycle.timestamp(row["deadline"]) <= datetime.now(timezone.utc):
        raise ValueError("local deadline has passed; do not work an expired claim")


def register(db, args):
    deadline = lifecycle.timestamp(args.deadline)
    if deadline.tzinfo is None or deadline <= datetime.now(timezone.utc):
        raise ValueError("deadline must be a future timestamp with a timezone")
    pack_path = Path(args.pack).resolve()
    pack = load(pack_path / "pack.json")
    addr = address(args.address)
    if address(pack["address"]) != addr:
        raise ValueError("pack address does not match assignment")
    assignment = getattr(args, 'assignment', None)
    if assignment:
        assignment = Path(assignment).resolve()
        assigned_data = load(assignment)
        expected_pack = Path(assigned_data['workspace']) / 'packs' / args.claim
        if pack_path != expected_pack.resolve():
            raise ValueError('managed pack must be packs/CLAIM_ID for this exact claim, not an earlier pack')
        state = lifecycle.snapshot(assignment.parent.parent, assignment.parent.name)
        if state['claim_id'] != args.claim or state['address'] != addr or state['state'] != 'claimed':
            raise ValueError('assignment does not own this claim')
    facts = getattr(args, 'review', None)
    if isinstance(facts, str):
        try:
            facts = json.loads(facts)
        except ValueError:
            raise ValueError('review facts must be a JSON object') from None
    if facts is None:
        raise ValueError('reviewed facts are required; a lane is not an assignment')
    if not isinstance(facts, dict):
        raise ValueError('review facts must be an object')
    facts = review.normalized(dict(review=facts, give_ups=getattr(args, 'give_ups', 0),
                                   open_problem_reports=getattr(args, 'open_problem_reports', 0)),
                              strict=False)
    with db:
        # The historical lane column is not an active rule; keep it empty for new
        # attempts and store the reviewed facts separately.
        db.execute("""INSERT INTO attempts(claim_id,address,lane,review_json,model,variant,harness,
            pack_path,pack_hash,deadline,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                   (args.claim, addr, '', json.dumps(facts) if facts is not None else None,
                    args.model, args.variant, args.harness,
                    str(pack_path), tree_digest(pack_path), deadline.isoformat(), now()))
        event(db, args.claim, "registered", {"ownership_confirmed_by_operator": True})
        if assignment:
            db.execute('UPDATE attempts SET assignment_path=? WHERE claim_id=?', (str(assignment), args.claim))
    return {"claim_id": args.claim, "state": "baseline_needed"}


def parse_result(stdout):
    results = []
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "status" in obj:
            results.append(obj)
    if len(results) != 1:
        raise ValueError("expected exactly one f3check JSON result")
    return results[0]


def diagnose_result(stdout, stderr, exit_code):
    """Fail closed; never turn a process failure into a parser workaround."""
    try:
        result = parse_result(stdout)
    except ValueError as exc:
        if not stdout.strip() and exit_code != 0:
            kind = 'process_failure'
        elif not stdout.strip():
            kind = 'missing_output'
        else:
            kind = 'invalid_or_ambiguous_output'
        return dict(status='tool_error', failure_kind=kind, reason=str(exc),
                    process_exit_code=exit_code, stderr_present=bool(stderr.strip()))
    status = result.get('status')
    if status not in ('match', 'wip', 'compile_error', 'rejected', 'no_symbol', 'literal_mismatch', 'literal_address'):
        return dict(status='tool_error', failure_kind='unknown_status', reported_status=status)
    if (exit_code not in (0, 1) or (status == 'match' and exit_code != 0) or
            'VS2005 SP1' not in str(result.get('checker', ''))):
        return dict(status='tool_error', failure_kind='process_or_checker_mismatch',
                    process_exit_code=exit_code, reported_status=status)
    return result


def wsl_path(path):
    s = str(Path(path).resolve())
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", s)
    if m:
        return "/mnt/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/")
    if s.startswith("/"):
        return s
    raise ValueError("cannot translate path to WSL")


def check(db, args):
    row = attempt(db, args.claim)
    ensure_active(row)
    if row.get('assignment_path'):
        assigned = Path(row['assignment_path'])
        state = lifecycle.snapshot(assigned.parent.parent, assigned.parent.name)
        if state['claim_id'] != args.claim or state['state'] != 'claimed':
            raise ValueError('lifecycle does not own an active claim')
        # A stage is an evidence label, not authority to bypass stopping controls.
        # Closure permits saving/promoting already checked inputs and releasing the
        # lease, never another compilation of worker-selected source or flags.
        lifecycle.require_experiment(assigned.parent.parent, assigned.parent.name)
    if args.stage != "baseline" and row["gate"] != "continue":
        raise ValueError("record a baseline and a continue gate before further checks")
    pack_path = Path(row["pack_path"])
    pack_hash = tree_digest(pack_path)
    if pack_hash != row["pack_hash"]:
        raise ValueError("pack inputs changed since registration; obtain/review the correct pack")
    source = Path(args.source).resolve()
    source_bytes = source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    source_stable = digest(source) == source_hash
    from snapshots import freeze
    evidence = Path(args.evidence_dir or DEFAULT_DB.parent / "checks")
    snapshot = freeze(evidence.parent, args.claim, source_bytes)
    pack = load(pack_path / "pack.json")
    requested = flags(args.flags)
    from checker import command
    cmd = command(["--pack", wsl_path(pack_path), "--claim", args.claim, "--json"])
    full_diff = None
    if not getattr(args, 'import_evidence', None):
        evidence.mkdir(parents=True, exist_ok=True)
        full_diff = evidence / (args.claim + '_' + str(time.time_ns()) + '.diff.txt')
        cmd += ['--full-diff', wsl_path(full_diff)]
    if requested:
        cmd += ["--flags", ",".join(requested)]
    for key in ("change", "observed", "why", "expect", "regions"):
        if getattr(args, key, None):
            value = getattr(args, key)
            if not isinstance(value, str):
                raise ValueError('checker note fields must be strings')
            cmd += ["--" + key + '=' + value]
    cmd += [wsl_path(source)]
    imported = getattr(args, 'import_evidence', None)
    invocation_evidence_path = None
    if imported:
        raw_import = load(imported)
        invocation_evidence_path = raw_import.get('invocation_evidence_path') or str(Path(imported).resolve())
        imported_cmd = list(raw_import.get('command') or [])
        compare_cmd = list(imported_cmd)
        imported_diff = None
        if '--full-diff' in compare_cmd:
            # Only the canonical checker option position, after the trusted
            # runner boundary and --json, may differ from legacy invocations.
            index = cmd.index('--json') + 1
            if compare_cmd.count('--full-diff') != 1 or compare_cmd[index:index+1] != ['--full-diff'] or index+1 >= len(compare_cmd):
                raise ValueError('imported full-diff command provenance is not canonical')
            name = compare_cmd[index+1].replace('\\', '/').rsplit('/', 1)[-1]
            if not re.fullmatch(re.escape(args.claim) + r'_\d+\.diff\.txt', name):
                raise ValueError('imported full-diff destination is not host-owned')
            from security import scoped
            # The command describes the ORIGINAL invocation. Captures can be
            # relocated during an import without pretending another compile ran.
            imported_diff = scoped(Path(invocation_evidence_path).resolve().parent, name)
            if compare_cmd[index+1] != wsl_path(imported_diff):
                raise ValueError('imported full-diff destination provenance mismatch')
            del compare_cmd[index:index+2]
        if (compare_cmd != cmd or raw_import.get('source_hash') != source_hash or
                raw_import.get('pack_hash') != pack_hash or raw_import.get('unchanged') is not True):
            raise ValueError('imported command/source/pack provenance does not match this check')
        cmd = imported_cmd
        if raw_import.get('full_diff'):
            full = raw_import['full_diff']
            captured = scoped(Path(imported).resolve().parent, Path(full['path']).name, exists=True)
            if (imported_diff is None or Path(full['path']) != captured or
                    not re.fullmatch(re.escape(args.claim) + r'_\d+\.diff\.txt', captured.name) or
                    digest(captured) != full['sha256']):
                raise ValueError('imported full-diff evidence provenance mismatch')
            evidence.mkdir(parents=True, exist_ok=True)
            full_diff = evidence / (args.claim + '_' + str(time.time_ns()) + '.diff.txt')
            full_diff.write_bytes(captured.read_bytes())
        elapsed = raw_import.get('seconds')
        if not isinstance(elapsed, (int, float)) or elapsed < 0:
            raise ValueError('imported evidence needs a measured nonnegative seconds field')
        run = subprocess.CompletedProcess(cmd, raw_import['exit_code'], raw_import['stdout'], raw_import['stderr'])
    else:
        if row.get('assignment_path'):
            # Hashing/loading inputs must not turn an earlier guard into permission
            # to start a compile after STOP or supervisor loss arrives meanwhile.
            lifecycle.require_experiment(assigned.parent.parent, assigned.parent.name)
        start = time.monotonic()
        try:
            run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or ''
            if isinstance(output, bytes):
                output = output.decode('utf-8', errors='replace')
            run = subprocess.CompletedProcess(cmd, -1, output, 'checker timed out; reconcile any surviving WSL process')
        except OSError as exc:
            run = subprocess.CompletedProcess(cmd, -1, '', str(exc))
        elapsed = time.monotonic() - start
    result = diagnose_result(run.stdout, run.stderr, run.returncode)
    used = result.get("accepted_flags", requested or pack["flags"])
    unchanged = source_stable and digest(source) == source_hash and tree_digest(pack_path) == pack_hash
    valid = unchanged and result["status"] not in ('tool_error', 'rejected')
    if result["status"] == "match":
        valid = valid and run.returncode == 0 and "VS2005 SP1" in result.get("checker", "")
    evidence.mkdir(parents=True, exist_ok=True)
    # No diff or source enters the database. Raw official output stays in private work/.
    raw = {"command": cmd, "stdout": run.stdout, "stderr": run.stderr,
           "source_hash": source_hash, "pack_hash": pack_hash,
           "exit_code": run.returncode, "unchanged": unchanged, "seconds": elapsed,
           "captured_utc": now(), "imported_from": str(imported) if imported else None,
            "source_snapshot_path": str(snapshot)}
    if full_diff is not None and full_diff.is_file():
        raw['full_diff'] = dict(path=str(full_diff), sha256=digest(full_diff))
    evidence_path = evidence / f"{args.claim}_{time.time_ns()}.json"
    raw['invocation_evidence_path'] = invocation_evidence_path or str(evidence_path.resolve())
    evidence_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    compact = {k: v for k, v in result.items() if k not in {"diff", "slots", "errors"}}
    compact["auto_repair"] = any("AUTO-REPAIR" in str(e) for e in result.get("errors", []) or [])
    compact["normal_flags"] = any(same_flags(used, f) for f in accepted_sets(pack))
    with db:
        cur = db.execute("""INSERT INTO checks(claim_id,stage,created,seconds,source_path,
            source_hash,pack_hash,flags_json,command_json,result_json,exit_code,valid,evidence_path)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (args.claim, args.stage, now(), elapsed, str(source), source_hash,
                          pack_hash, json.dumps(used), json.dumps(cmd), json.dumps(compact),
                          run.returncode, int(valid), str(evidence_path)))
        db.execute('UPDATE checks SET source_snapshot_path=? WHERE id=?', (str(snapshot), cur.lastrowid))
        event(db, args.claim, "check", {"check_id": cur.lastrowid, "stage": args.stage})
    if row.get('assignment_path') and valid and args.stage != 'final':
        assigned = Path(row['assignment_path'])
        # A check can finish after the gate closed. Preserve its evidence but
        # never let that late improvement reopen the experiment window.
        try:
            lifecycle.progress(assigned.parent.parent, assigned.parent.name,
                               used, result.get('ndiff_insn'))
        except ValueError:
            pass
    return {"check_id": cur.lastrowid, "valid": valid, "result": compact,
            "seconds": round(elapsed, 2), "evidence": str(evidence_path)}


def best_scored_check(db, claim):
    """Best valid same-process metric, with its exact flags and source hash."""
    rows = db.execute("SELECT * FROM checks WHERE claim_id=? AND valid=1 ORDER BY id", (claim,)).fetchall()
    scored = []
    for row in rows:
        result = json.loads(row["result_json"])
        ndiff = result.get("ndiff_insn")
        if type(ndiff) is int and ndiff >= 0:
            scored.append((ndiff, -row["id"], row, result))
    if not scored:
        return None
    ndiff, _, row, result = min(scored)
    return dict(check_id=row["id"], ndiff_insn=ndiff, stage=row["stage"],
                flags=json.loads(row["flags_json"]), normal_flags=bool(result.get("normal_flags")),
                source_sha256=row["source_hash"], status=result.get("status"))


def near_miss(db, claim):
    """A close residual is advisory for the next experiment and required review."""
    best = best_scored_check(db, claim)
    return best if best and best["ndiff_insn"] <= review.NEAR_MISS_DIFFS else None


def permute_runs(db, claim):
    """Durable bounded permutation runs, separate from ordinary checker checks."""
    value = []
    for row in db.execute("SELECT payload FROM events WHERE claim_id=? AND kind='permute' ORDER BY id", (claim,)):
        item = json.loads(row["payload"])
        path = Path(item.get("record_path", ""))
        try:
            record = load(path)
        except (OSError, ValueError):
            record = None
        value.append(dict(item, record=record))
    return value


def near_miss_review(db, claim):
    """The last host-recorded near-miss review for this claim, if any."""
    row = db.execute("SELECT payload FROM events WHERE claim_id=? AND kind='near_miss_review' "
                     "ORDER BY id DESC LIMIT 1", (claim,)).fetchone()
    return json.loads(row["payload"]) if row else None


def _permute_score(stdout):
    if re.search(r"^MATCH: written to ", stdout, re.M):
        return 0, True
    match = re.search(r"NO MATCH YET: best score (\d+)", stdout)
    return (int(match.group(1)), False) if match else (None, False)


class PermutationFailure(ValueError):
    """Failed search with safe structured diagnostics for the scoped tool."""
    def __init__(self, message, summary):
        super().__init__(message)
        self.summary = dict(summary, succeeded=False, output=None, verification=None)


def permute(db, args):
    """Run the official bounded permuter, then verify its best source once.

    The permuter is a host capability, not a shell: the worker supplies only an
    owned candidate path and bounded integers.  Inputs are copied to a host-owned
    run directory, stdout/stderr and hashes are retained, and the best source is
    rechecked through the ordinary official-check path before it can be used.
    """
    row = attempt(db, args.claim)
    ensure_active(row)
    assigned = Path(row["assignment_path"]) if row.get("assignment_path") else None
    if assigned:
        folder, worker = assigned.parent.parent, assigned.parent.name
        state = lifecycle.snapshot(folder, worker)
        if state["claim_id"] != args.claim or state["state"] != "claimed":
            raise ValueError("lifecycle does not own an active claim")
        lifecycle.require_experiment(folder, worker)
    else:
        folder = worker = state = None
    if row["gate"] != "continue":
        raise ValueError("record a baseline and a continue gate before a permutation search")
    baseline = db.execute("SELECT 1 FROM checks WHERE claim_id=? AND stage='baseline' AND valid=1 LIMIT 1",
                          (args.claim,)).fetchone()
    if not baseline:
        raise ValueError("a valid recorded baseline is required")
    pack_path = Path(row["pack_path"])
    pack_hash = tree_digest(pack_path)
    if pack_hash != row["pack_hash"]:
        raise ValueError("pack inputs changed since registration; obtain/review the correct pack")
    source = Path(args.source).resolve()
    if assigned:
        files = (folder / worker / "files").resolve()
        try:
            relative = source.relative_to(files)
        except ValueError:
            raise ValueError("permutation source must be inside this worker's files") from None
        source = scoped(folder / worker / "files", relative.as_posix(), exists=True)
    if source.suffix.lower() != ".cpp":
        raise ValueError("C++ source required")
    options = flagset.permutation(dict(trials=args.trials, jobs=args.jobs,
                                       timeout_seconds=args.timeout_seconds))
    requested = flags(args.flags)
    if requested is not None:
        flagset.alternate(args.flags)
    pack = load(pack_path / "pack.json")
    used = requested or list(pack["flags"])
    remaining = lifecycle.remaining_experiment_seconds(state) if state else None
    if remaining is not None and remaining < 90:
        raise ValueError("less than 90 seconds of experiment time remain; no new permutation search")
    timeout = options["timeout_seconds"]
    if remaining is not None:
        timeout = int(min(timeout, max(60, remaining - 30)))
    source_bytes = source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    evidence = Path(args.evidence_dir or DEFAULT_DB.parent / "checks")
    runs = (folder / worker / "permute") if assigned else (DEFAULT_DB.parent / "permute" / args.claim)
    run_id = "run-" + time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + lifecycle.uuid.uuid4().hex[:8]
    run_dir = runs / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    candidate = run_dir / "candidate.cpp"
    candidate.write_bytes(source_bytes)
    from checker import command
    cancel = run_dir / "CANCEL"
    cmd = command(["--pack", wsl_path(pack_path), "--permute",
                   "--trials", str(options["trials"]), "--jobs", str(options["jobs"])],
                  wall_seconds=timeout, watch_file=cancel)
    if requested:
        cmd += ["--flags", ",".join(requested)]
    cmd += [wsl_path(candidate)]
    stdout_path = run_dir / "stdout.txt"
    stderr_path = run_dir / "stderr.txt"
    started = time.monotonic()
    timed_out = stopped_mid = termination_unconfirmed = False
    stdout = stderr = ""
    returncode = -1
    process = None
    try:
        # Hash/copy/command preparation can take a moment; STOP may have arrived.
        if assigned:
            lifecycle.require_experiment(folder, worker)
        # Drain to host-owned files, never pipes: a full pipe would stall a healthy
        # search while the supervisor waits for the process to exit.
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            process = subprocess.Popen(cmd, stdout=out, stderr=err)
            deadline = started + timeout
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                if assigned:
                    try:
                        current = lifecycle.snapshot(folder, worker)
                        if not lifecycle.voluntary_experiment(folder, worker, current):
                            stopped_mid = True
                            break
                    except (ValueError, KeyError, OSError, sqlite3.Error):
                        stopped_mid = True
                        break
                time.sleep(0.25)
            if timed_out or stopped_mid:
                # The WSL runner watches this marker and kills its own process group.
                cancel.touch()
                for _ in range(12):
                    if process.poll() is not None:
                        break
                    time.sleep(0.25)
                if process.poll() is None:
                    # The WSL runner did not honour its own cancellation marker in time.
                    # Treat the tree stop as unconfirmed even if killing wsl.exe succeeds.
                    termination_unconfirmed = True
                    process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                if termination_unconfirmed and os.name == "nt":
                    try:
                        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                                       capture_output=True, timeout=10)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                termination_unconfirmed = True
                process.kill()
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        returncode = process.returncode
        if Path(str(cancel) + '.unconfirmed').exists():
            termination_unconfirmed = True
    except OSError as exc:
        stderr = str(exc)
        for path, name in ((stdout_path, 'stdout'), (stderr_path, 'stderr')):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ''
            if name == 'stdout':
                stdout = text
            else:
                stderr = text
    cleanup_latched = False
    if termination_unconfirmed and assigned:
        try:
            if lifecycle.snapshot(folder, worker)['state'] == 'claimed':
                lifecycle.request_worker_cleanup(folder, worker, 'permuter_termination_unconfirmed')
                cleanup_latched = True
        except (ValueError, KeyError, OSError, sqlite3.Error):
            pass
    elapsed = time.monotonic() - started
    score, matched = _permute_score(stdout)
    output = candidate.with_name(candidate.stem + ".permuted.cpp")
    usable = None
    if output.is_file() and not timed_out and not stopped_mid and not termination_unconfirmed:
        if assigned:
            try:
                lifecycle.require_experiment(folder, worker)
            except ValueError:
                stopped_mid = True
        if not stopped_mid:
            data = output.read_bytes()
            if assigned:
                relative = "permuted-%s.cpp" % run_id[-8:]
                usable = scoped(folder / worker / "files", relative)
                usable.write_bytes(data)
            else:
                usable = output
    verification, verification_error = None, None
    if usable is not None and not timed_out and not stopped_mid and not termination_unconfirmed:
        try:
            if assigned:
                lifecycle.require_experiment(folder, worker)
            vargs = argparse.Namespace(
                claim=args.claim, source=str(usable), stage=args.stage, flags=args.flags,
                evidence_dir=str(evidence), import_evidence=None,
                change="official permuter best candidate (%s diffs); %s"
                       % (score, args.change or 'search best source'),
                observed=args.observed, why=args.why, expect=args.expect, regions=args.regions)
            verification = check(db, vargs)
        except ValueError as exc:
            verification_error = str(exc)
    if verification and verification.get('valid') is not True:
        verification_error = verification_error or 'verification check was not valid evidence'
    record_path = run_dir / "run.json"
    raw_path = run_dir / "raw.json"
    raw = dict(command=cmd, stdout=stdout[:1024 * 1024], stderr=stderr[:65536],
               exit_code=returncode, seconds=elapsed, captured_utc=now(),
               timed_out=timed_out, stopped_mid_run=stopped_mid,
               termination_unconfirmed=termination_unconfirmed)
    raw_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    output_missing = (not output.is_file()) and not timed_out and not stopped_mid and returncode in (0, 1)
    disposition = ('termination_unconfirmed' if termination_unconfirmed else
                   'verified' if verification and verification.get('valid') is True else
                   'failed' if verification_error else
                   'stopped' if stopped_mid else
                   'timeout' if timed_out else
                   'no_output' if output_missing else
                   'search_failed' if returncode not in (0, 1) else 'not_run')
    record = dict(schema_version=1, run_id=run_id, claim_id=args.claim, address=row["address"],
                  created_utc=now(), seconds=round(elapsed, 2), timed_out=timed_out,
                  stopped_mid_run=stopped_mid, termination_unconfirmed=termination_unconfirmed,
                  cleanup_latched=cleanup_latched,
                  exit_code=returncode, trials=options["trials"], jobs=options["jobs"],
                  flags=used, normal_flags=any(same_flags(used, f) for f in accepted_sets(pack)),
                  source_path=str(candidate), source_sha256=source_hash,
                  output_path=str(output) if output.is_file() else None,
                  output_sha256=digest(output) if output.is_file() else None,
                  usable_path=str(usable) if usable else None,
                  output_missing=output_missing,
                  verification_disposition=disposition,
                  score=score, matched=bool(matched), verification=verification,
                  verification_error=verification_error, raw_path=str(raw_path),
                   raw_sha256=digest(raw_path))
    record['diagnostics'] = dict(
        disposition=disposition, requested_timeout_seconds=options['timeout_seconds'],
        effective_timeout_seconds=timeout, output_present=output.is_file(),
        raw_evidence=dict(path=str(raw_path), sha256=digest(raw_path)),
        progress_known=score is not None,
        next_action=('Parent must confirm process-tree termination before further work.'
                     if termination_unconfirmed else
                     'No candidate published or verification started. Inspect saved progress/error tails; '
                     'timeout does not prove all requested trials completed.' if timed_out else
                     'Inspect verification disposition and official check before using output.'))
    lifecycle.save(record_path, record)
    record_sha256 = digest(record_path)
    summary = dict(run_id=run_id, record_path=str(record_path), record_sha256=record_sha256,
                   source_sha256=source_hash,
                   output_sha256=record["output_sha256"], score=score, matched=bool(matched),
                   timed_out=timed_out, stopped_mid_run=stopped_mid,
                   termination_unconfirmed=termination_unconfirmed,
                   cleanup_latched=cleanup_latched,
                   seconds=record["seconds"], trials=options["trials"],
                   jobs=options["jobs"], flags=used, output_missing=output_missing,
                   verification_disposition=disposition,
                   raw_sha256=record["raw_sha256"],
                   verification_check_id=(verification or {}).get("check_id"),
                   verification_error=verification_error)
    summary['diagnostics'] = record['diagnostics']
    with db:
        event(db, args.claim, "permute", summary)
    if termination_unconfirmed:
        raise PermutationFailure("permutation process termination could not be confirmed; cleanup required, "
                                 "evidence preserved", summary)
    if stopped_mid:
        raise PermutationFailure("permutation search stopped by STOP/deadline/supervisor loss; "
                                 "evidence preserved, no candidate published", summary)
    if timed_out:
        raise PermutationFailure("official permuter exceeded its bounded wall clock (best score %s); "
                                 "evidence preserved, no verification" % score, summary)
    if returncode not in (0, 1) and not matched:
        raise PermutationFailure("official permuter failed; inspect retained host evidence, do not retry blindly", summary)
    if output_missing:
        raise PermutationFailure("official permuter produced no candidate; evidence preserved, do not retry blindly", summary)
    if verification_error:
        stopping = any(token in verification_error for token in (
            'STOP', 'closure', 'experiment time', 'watchdog', 'active claim',
            'deadline has passed', 'cleanup barrier'))
        if not stopping:
            raise PermutationFailure('permutation verification failed; inspect retained evidence', summary)
    return dict(**summary, output=str(usable) if usable else None, verification=verification)


def gate(db, args):
    row = attempt(db, args.claim)
    ensure_active(row)
    baseline = db.execute("SELECT * FROM checks WHERE claim_id=? AND stage='baseline' AND valid=1 ORDER BY id DESC LIMIT 1",
                          (args.claim,)).fetchone()
    if not baseline or not baseline["valid"]:
        raise ValueError("a valid recorded baseline is required")
    if args.decision == "continue":
        if not args.hypothesis or not args.expected:
            raise ValueError("continue needs a testable hypothesis and expected observation")
        facts = json.loads(row["review_json"]) if row.get("review_json") else {}
        if (facts.get("prior_unfit") or row["lane"] == "integration") and not args.integration_plan:
            raise ValueError("prior full-unit failure needs a concrete integration plan")
    payload = {"decision": args.decision, "reason": args.reason,
               "hypothesis": args.hypothesis, "expected": args.expected,
               "integration_plan": args.integration_plan, "baseline_id": baseline["id"]}
    with db:
        db.execute("UPDATE attempts SET gate=?,gate_reason=?,state=? WHERE claim_id=?",
                   (args.decision, json.dumps(payload),
                    "working" if args.decision == "continue" else "stop_requested", args.claim))
        event(db, args.claim, "gate", payload)
    return payload


def promote(db, args):
    row = attempt(db, args.claim)
    ensure_active(row)
    if row["gate"] != "continue":
        raise ValueError("continue gate required before promotion")
    final = db.execute("SELECT * FROM checks WHERE claim_id=? AND stage='final' ORDER BY id DESC LIMIT 1",
                       (args.claim,)).fetchone()
    if not final or not final["valid"] or json.loads(final["result_json"])["status"] != "match":
        raise ValueError("valid final official MATCH required")
    if digest(final["source_path"]) != final["source_hash"]:
        raise ValueError("source changed after final check")
    if tree_digest(row["pack_path"]) != final["pack_hash"]:
        raise ValueError("pack changed after final check")
    source_bytes = Path(final["source_path"]).read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != final["source_hash"]:
        raise ValueError("source changed while preparing final artifact")
    dest = Path(args.output).resolve()
    if dest.name != "match.cpp":
        raise ValueError("promoted artifact must be named match.cpp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if digest(dest) != final["source_hash"]:
            raise ValueError("refusing to overwrite a different final artifact")
    else:
        with dest.open("xb") as f:
            f.write(source_bytes)
    result = json.loads(final["result_json"])
    state = "submit_ready" if result["normal_flags"] else "proof_ready"
    with db:
        db.execute("""UPDATE attempts SET state=?,artifact_path=?,artifact_hash=?,
            final_check_id=? WHERE claim_id=?""",
                   (state, str(dest), final["source_hash"], final["id"], args.claim))
        event(db, args.claim, "promoted", {"state": state, "hash": final["source_hash"]})
    return {"state": state, "artifact": str(dest), "sha256": final["source_hash"],
            "integration_warning": "Local MATCH is not proof of full-unit compatibility."}


def record(db, args):
    row = attempt(db, args.claim)
    if args.kind in {"submitted", "flag_review"}:
        ensure_active(row)
        wanted = "submit_ready" if args.kind == "submitted" else "proof_ready"
        if row["state"] != wanted or not args.identifier:
            raise ValueError(f"{wanted} and a service identifier are required")
        if digest(row["artifact_path"]) != row["artifact_hash"]:
            raise ValueError("final artifact changed; do not record a submission of different code")
        if tree_digest(row["pack_path"]) != row["pack_hash"]:
            raise ValueError("pack changed after final check")
        if args.kind == "flag_review" and not args.checkpoint:
            raise ValueError("flag proof requires its matching checkpoint ID")
    elif args.kind == "given_up":
        if row["state"] in {"submitted", "flag_review", "verified", "unfit", "failed", "given_up"}:
            raise ValueError("cannot give up a locally closed attempt")
        if not args.identifier:
            raise ValueError("record service giveup acknowledgement/evidence path")
    with db:
        db.execute("UPDATE attempts SET state=?,submission_id=?,checkpoint_id=? WHERE claim_id=?",
                   (args.kind, args.identifier if args.kind == "submitted" else row["submission_id"],
                    args.checkpoint, args.claim))
        event(db, args.claim, args.kind, {"service_identifier": args.identifier,
                                         "checkpoint_id": args.checkpoint, "note": args.note})
    return {"state": args.kind, "note": "Ledger updated only; this command makes no API request."}


def reclassify(db, args):
    row = attempt(db, args.claim)
    ensure_active(row)
    if not args.integration_plan.strip():
        raise ValueError('integration plan required for discovered unfit history')
    facts = json.loads(row["review_json"]) if row.get("review_json") else {}
    facts.update(prior_unfit=True, integration_plan=args.integration_plan)
    with db:
        db.execute("UPDATE attempts SET review_json=?, gate=NULL, state='baseline_needed' WHERE claim_id=?",
                   (json.dumps(facts), args.claim))
        event(db, args.claim, 'reclassified', dict(review=facts))
    return dict(review=facts, gate=None,
                note='Record a new gate with the integration plan before further checks or promotion.')


def import_reports(db, args):
    count = 0
    for path in sorted(Path(args.directory).glob("dsmax*/report.json")):
        payload = load(path)
        addr = address(payload["address"])
        with db:
            db.execute("""INSERT INTO legacy_reports(path,hash,address,claim_id,imported,payload)
                VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET hash=excluded.hash,
                imported=excluded.imported,payload=excluded.payload""",
                       (str(path.resolve()), digest(path), addr, payload.get("claim_id"), now(),
                        json.dumps(payload)))
        count += 1
    return {"reports": count, "trust": "historical agent reports, not independently checked or promoted"}


def import_status(db, args):
    snapshot = load(args.file)
    if not isinstance(snapshot.get("recent"), list):
        raise ValueError("expected an official status snapshot with recent[]")
    h = digest(args.file)
    count = 0
    for result in snapshot["recent"]:
        sid, addr, verdict = result["id"], address(result["addr"]), result.get("verdict")
        if verdict not in {None, "verified", "unfit", "failed", "needs_review"}:
            raise ValueError(f"unknown verdict {verdict!r}; review rather than guessing")
        old = db.execute("SELECT * FROM server_results WHERE submission_id=?", (sid,)).fetchone()
        # An older pending snapshot must not erase a known verdict.
        if old and old["verdict"] and verdict is None:
            continue
        row = db.execute("SELECT * FROM attempts WHERE submission_id=?", (sid,)).fetchone()
        if row and row["address"] != addr:
            raise ValueError("server address conflicts with registered submission")
        with db:
            db.execute("""INSERT INTO server_results VALUES(?,?,?,?,?,?)
                ON CONFLICT(submission_id) DO UPDATE SET verdict=excluded.verdict,
                detail=excluded.detail,observed=excluded.observed,snapshot_hash=excluded.snapshot_hash""",
                       (sid, addr, verdict, result.get("detail"), now(), h))
            if row and verdict:
                db.execute("UPDATE attempts SET state=? WHERE claim_id=?", (verdict, row["claim_id"]))
                event(db, row["claim_id"], "server_verdict", result)
        count += 1
    return {"results_imported": count, "scope": "Only recent[] entries; absent submissions are unchanged.",
            "account": {k: snapshot.get(k) for k in ("tier", "verified_matches", "awaiting_verification")}}


def summary(db):
    attempts = [dict(r) for r in db.execute("SELECT * FROM attempts ORDER BY created")]
    for row in attempts:
        checks = [dict(r) for r in db.execute("SELECT * FROM checks WHERE claim_id=? ORDER BY id", (row["claim_id"],))]
        row["check_count"] = len(checks)
        row["checker_seconds"] = round(sum(c["seconds"] for c in checks), 2)
        row["baseline"] = next((json.loads(c["result_json"]) for c in reversed(checks)
                                if c["stage"] == "baseline" and c['valid']), None)
        row["latest"] = json.loads(checks[-1]["result_json"]) if checks else None
    return {"attempts": attempts,
            "historical_reports": db.execute("SELECT count(*) FROM legacy_reports").fetchone()[0],
            "server_results": [dict(r) for r in db.execute("SELECT * FROM server_results ORDER BY address")],
            "limits": "No inferred cost, agent-hours or model ranking; checker timings are measured separately."}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=str(DEFAULT_DB))
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("assess", help="classify reviewed candidate metadata; never claims work")
    a.add_argument("file", help="JSON array of function-info records with review annotations")
    a = sub.add_parser("register", help="register a claim you already own")
    for name in ("claim", "address", "model", "variant", "harness", "pack", "deadline"):
        a.add_argument("--" + name, required=True)
    a.add_argument("--review", required=True, help="JSON reviewed facts; no mutually exclusive lane")
    a.add_argument('--assignment', help='managed worker assignment.json; enforce shared lifecycle deadlines')
    a = sub.add_parser("check", help="run official f3check via WSL and capture immutable evidence")
    a.add_argument("--claim", required=True)
    a.add_argument("--source", required=True)
    a.add_argument("--stage", choices=["baseline", "attempt", "final"], required=True)
    a.add_argument("--flags", help="comma-separated complete alternate flags; omission uses pack flags")
    a.add_argument("--evidence-dir")
    a.add_argument('--import-evidence', help='import an exact invocation with hashes, raw output and measured duration')
    for name in ("change", "observed", "why", "expect", "regions"):
        a.add_argument("--" + name, help="service work-notes field forwarded to f3check")
    a = sub.add_parser("permute", help="bounded official permuter, then one verified check of its best source")
    a.add_argument("--claim", required=True)
    a.add_argument("--source", required=True)
    a.add_argument("--stage", choices=["attempt", "final"], default="attempt")
    a.add_argument("--flags")
    a.add_argument("--trials", type=int, default=120)
    a.add_argument("--jobs", type=int, default=2)
    a.add_argument("--timeout-seconds", type=int, default=240)
    a.add_argument("--evidence-dir")
    for name in ("change", "observed", "why", "expect", "regions"):
        a.add_argument("--" + name, help="service work-notes field forwarded to the verification check")
    a = sub.add_parser('reclassify', help='orchestrator: route discovered unfit history to integration and invalidate gate')
    a.add_argument('--claim', required=True); a.add_argument('--integration-plan', required=True)
    a = sub.add_parser("gate")
    a.add_argument("--claim", required=True)
    a.add_argument("--decision", choices=["continue", "stop"], required=True)
    a.add_argument("--reason", required=True)
    for name in ("hypothesis", "expected", "integration-plan"):
        a.add_argument("--" + name)
    a = sub.add_parser("promote", help="copy an unchanged final MATCH into match.cpp")
    a.add_argument("--claim", required=True)
    a.add_argument("--output", required=True)
    a = sub.add_parser("record", help="record an actual service acknowledgement, never send one")
    a.add_argument("--claim", required=True)
    a.add_argument("--kind", choices=["submitted", "flag_review", "given_up"], required=True)
    a.add_argument("--identifier", required=True)
    a.add_argument("--checkpoint")
    a.add_argument("--note", default="")
    a = sub.add_parser("import-reports")
    a.add_argument("directory")
    a = sub.add_parser("import-status")
    a.add_argument("file")
    sub.add_parser("summary")
    return p


def main():
    args = parser().parse_args()
    try:
        if args.command == "assess":
            result = [classify(item) for item in load(args.file)]
        else:
            db = connect(args.db)
            try:
                commands = {"register": register, "check": check, "permute": permute, "gate": gate,
                            "promote": promote, "record": record,
                             "import-reports": import_reports, "import-status": import_status, 'reclassify': reclassify}
                result = summary(db) if args.command == "summary" else commands[args.command](db, args)
            finally:
                db.close()
        print(json.dumps(result, indent=2))
        if args.command == "check" and (not result["valid"] or result["result"]["status"] == "tool_error"):
            return 2
        return 0
    except (ValueError, OSError, sqlite3.Error, KeyError) as exc:
        print(f"matchflow: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
