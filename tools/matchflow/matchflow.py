#!/usr/bin/env python3
"""Local-only matching triage and evidence ledger. No service writes or claims."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

import lifecycle
from config import workspace_root, REPOSITORY

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
    """A conservative rubric, not a prediction that a function will match."""
    reasons = []
    verdicts = item.get("prior_verdicts", [])
    if isinstance(verdicts, str):
        raise ValueError("prior_verdicts must be a list")
    if "unfit" in verdicts or item.get("integration_failure"):
        lane = "integration"
        reasons.append("Prior integration failure: local MATCH does not settle file compatibility.")
    elif (item.get("flags_suspected") or item.get("repeated_blocker")
          or item.get("give_ups", 0) >= 3
          or item.get("open_problem_reports", 0) or item.get("file_problem_reports", 0)):
        lane = "research"
        reasons.append("Repeated attempts, suspected metadata defect, or an unresolved report.")
    else:
        lane = "production"
        reasons.append("No supplied integration/repeated-blocker evidence; baseline is still required.")
    if item.get("near_misses", 0):
        reasons.append("Near misses are starting material, not a readiness score.")
    if not item.get("history_reviewed", False):
        reasons.append("History has not been reviewed: do not dispatch yet.")
    state = item.get("state", "unknown")
    eligible = state == "open" and bool(item.get("history_reviewed"))
    if state != "open":
        reasons.append(f"Availability is {state!r}; refresh before any claim.")
    return {"address": address(item["address"]), "lane": lane,
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
    if 'assignment_path' not in {r[1] for r in db.execute('PRAGMA table_info(attempts)')}:
        db.execute('ALTER TABLE attempts ADD COLUMN assignment_path TEXT')
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
    with db:
        db.execute("""INSERT INTO attempts(claim_id,address,lane,model,variant,harness,
            pack_path,pack_hash,deadline,created) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                   (args.claim, addr, args.lane, args.model, args.variant, args.harness,
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
        if args.stage == 'final':
            if lifecycle.phase(state) == 'reconcile':
                raise ValueError('closure deadline passed; reconcile without compiling')
        else:
            lifecycle.require_experiment(assigned.parent.parent, assigned.parent.name)
    if args.stage != "baseline" and row["gate"] != "continue":
        raise ValueError("record a baseline and a continue gate before further checks")
    pack_path = Path(row["pack_path"])
    pack_hash = tree_digest(pack_path)
    if pack_hash != row["pack_hash"]:
        raise ValueError("pack inputs changed since registration; obtain/review the correct pack")
    source = Path(args.source).resolve()
    source_hash = digest(source)
    pack = load(pack_path / "pack.json")
    requested = flags(args.flags)
    from checker import command
    cmd = command(["--pack", wsl_path(pack_path), "--claim", args.claim, "--json"])
    if requested:
        cmd += ["--flags", ",".join(requested)]
    for key in ("change", "observed", "why", "expect", "regions"):
        if getattr(args, key, None):
            cmd += ["--" + key, getattr(args, key)]
    cmd += [wsl_path(source)]
    imported = getattr(args, 'import_evidence', None)
    if imported:
        raw_import = load(imported)
        if (raw_import.get('command') != cmd or raw_import.get('source_hash') != source_hash or
                raw_import.get('pack_hash') != pack_hash or raw_import.get('unchanged') is not True):
            raise ValueError('imported command/source/pack provenance does not match this check')
        elapsed = raw_import.get('seconds')
        if not isinstance(elapsed, (int, float)) or elapsed < 0:
            raise ValueError('imported evidence needs a measured nonnegative seconds field')
        run = subprocess.CompletedProcess(cmd, raw_import['exit_code'], raw_import['stdout'], raw_import['stderr'])
    else:
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
    unchanged = digest(source) == source_hash and tree_digest(pack_path) == pack_hash
    valid = unchanged and result["status"] not in ('tool_error', 'rejected')
    if result["status"] == "match":
        valid = valid and run.returncode == 0 and "VS2005 SP1" in result.get("checker", "")
    evidence = Path(args.evidence_dir or DEFAULT_DB.parent / "checks")
    evidence.mkdir(parents=True, exist_ok=True)
    # No diff or source enters the database. Raw official output stays in private work/.
    raw = {"command": cmd, "stdout": run.stdout, "stderr": run.stderr,
           "source_hash": source_hash, "pack_hash": pack_hash,
           "exit_code": run.returncode, "unchanged": unchanged, "seconds": elapsed,
           "captured_utc": now(), "imported_from": str(imported) if imported else None}
    evidence_path = evidence / f"{args.claim}_{time.time_ns()}.json"
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
        if row["lane"] == "integration" and not args.integration_plan:
            raise ValueError("integration lane needs a plan for the prior full-unit failure")
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
    with db:
        db.execute("UPDATE attempts SET lane='integration', gate=NULL, state='baseline_needed' WHERE claim_id=?",
                   (args.claim,))
        event(db, args.claim, 'reclassified', dict(lane='integration', integration_plan=args.integration_plan))
    return dict(lane='integration', gate=None, note='Record a new integration gate before further checks or promotion.')


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
    a.add_argument("--lane", choices=["production", "research", "integration"], required=True)
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
                commands = {"register": register, "check": check, "gate": gate,
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
