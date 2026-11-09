"""Offline tests: no network, game image, compiler, claims, or credentials."""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import matchflow as m


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = m.connect(self.root / "ledger.sqlite3")
        self.addCleanup(self.db.close)
        self.pack = self.root / "pack"
        self.pack.mkdir()
        self.metadata = {"address": "0x00123456", "flags": ["/MT", "/O2"], "size": 42}
        self.write_pack()
        (self.pack / "unit.cpp").write_text("void f();\n")
        self.source = self.root / "candidate.cpp"
        self.source.write_text("void f() {}\n")
        self.claim = "clm_test"

    def write_pack(self):
        (self.pack / "pack.json").write_text(json.dumps(self.metadata))

    def register(self, prior_unfit=False):
        return m.register(self.db, argparse.Namespace(
            deadline="2099-01-01T00:00:00Z", pack=str(self.pack),
            address="0x00123456", claim=self.claim,
            review=dict(history_reviewed=True, history_sources=['fixture'], prior_verdicts=[],
                        verdict_history='reviewed', report_scope='none', report_evidence=None,
                        prior_unfit=prior_unfit,
                        next_experiment=None if not prior_unfit else 'Repair the full-unit type clash',
                        integration_plan=None if not prior_unfit else 'Repair the full-unit type clash'),
            model="Test Model", variant="max", harness="Offline Test"))

    def check(self, status="wip", stage="baseline", extra=None, exit_code=None, stderr=""):
        result = {"status": status, "checker": "checker: f3check test, compiler VS2005 SP1",
                  "extent": 42, "diffs": 0 if status == "match" else 3, "ndiff_insn": 0 if status == "match" else 2}
        result.update(extra or {})
        stdout = "work notes: informational\n" + json.dumps(result) + "\n"
        run = subprocess.CompletedProcess([], exit_code if exit_code is not None else int(status != "match"), stdout, stderr)
        args = argparse.Namespace(claim=self.claim, stage=stage, source=str(self.source),
                                  flags=None, evidence_dir=str(self.root / "evidence"))
        with patch.object(m.subprocess, "run", return_value=run):
            return m.check(self.db, args)

    def gate(self, decision="continue", hypothesis="declare a named pointer", expected="one fewer reload",
             integration_plan=None):
        return m.gate(self.db, argparse.Namespace(claim=self.claim, decision=decision,
                      hypothesis=hypothesis, expected=expected, reason="baseline reviewed",
                      integration_plan=integration_plan))

    def ready(self, extra=None):
        self.register()
        self.check()
        self.gate()
        self.check("match", "final", extra)
        return m.promote(self.db, argparse.Namespace(claim=self.claim, output=str(self.root / "final" / "match.cpp")))

    def record(self, kind, identifier="sub_test", checkpoint=None):
        return m.record(self.db, argparse.Namespace(claim=self.claim, kind=kind,
                        identifier=identifier, checkpoint=checkpoint, note="test"))

    def test_rubric_records_facts_not_a_lane(self):
        item = {"address": "00123456", "state": "open", "size": 2000,
                "near_misses": 9, "history_reviewed": True}
        self.assertNotIn("lane", m.classify(item))
        self.assertEqual(m.classify(item)["review"]["report_scope"], "unknown")
        item["give_ups"] = 3
        self.assertIn("Repeated attempts", m.classify(item)["reasons"][0])
        item["prior_verdicts"] = ["unfit"]
        self.assertTrue(m.classify(item)["review"]["prior_unfit"])

    def test_rubric_requires_review_and_availability(self):
        item = {"address": "00123456", "state": "open"}
        self.assertFalse(m.classify(item)["dispatch_ready"])
        item.update(history_reviewed=True, state="being worked on")
        self.assertFalse(m.classify(item)["dispatch_ready"])

    def test_register_checks_address(self):
        self.metadata["address"] = "0x00123457"
        self.write_pack()
        with self.assertRaisesRegex(ValueError, "address"):
            self.register()

    def test_fingerprint_changes_when_header_added(self):
        before = m.tree_digest(self.pack)
        (self.pack / "new.h").write_text("int f();")
        self.assertNotEqual(before, m.tree_digest(self.pack))

    def test_gate_requires_baseline(self):
        self.register()
        with self.assertRaisesRegex(ValueError, "baseline"):
            self.gate()

    def test_baseline_and_gate_required_for_final(self):
        self.register()
        with self.assertRaisesRegex(ValueError, "baseline"):
            self.check("match", "final")
        self.check()
        with self.assertRaisesRegex(ValueError, "hypothesis"):
            self.gate(hypothesis=None)

    def test_prior_unfit_gate_requires_specific_plan(self):
        self.register(prior_unfit=True)
        self.check()
        with self.assertRaisesRegex(ValueError, "integration plan"):
            self.gate()
        self.gate(integration_plan="obtain full unit error and compare affected neighbour")

    def test_stop_does_not_release_remote_claim(self):
        self.register()
        self.check()
        self.gate("stop")
        self.assertEqual(m.attempt(self.db, self.claim)["state"], "stop_requested")
        self.record("given_up", "local/service-ack.json")
        self.assertEqual(m.attempt(self.db, self.claim)["state"], "given_up")

    def test_checks_record_measured_duration_and_no_diff_in_db(self):
        self.register()
        self.check(extra={"diff": "PRIVATE LOCAL DIFF"})
        row = self.db.execute("SELECT * FROM checks").fetchone()
        self.assertGreaterEqual(row["seconds"], 0)
        self.assertNotIn("PRIVATE LOCAL DIFF", row["result_json"])
        self.assertIn("PRIVATE LOCAL DIFF", Path(row["evidence_path"]).read_text())

    def test_match_requires_success_exit_and_official_checker(self):
        self.register()
        bad = self.check("match", exit_code=1)
        self.assertFalse(bad["valid"])
        bad = self.check("match", extra={"checker": "some other checker"})
        self.assertFalse(bad["valid"])

    def test_missing_json_is_not_match(self):
        with self.assertRaises(ValueError):
            m.parse_result("MATCH\n")

    def test_multiple_json_results_are_not_accepted(self):
        with self.assertRaises(ValueError):
            m.parse_result('{"status":"match"}\n{"status":"wip"}')

    def test_pack_mutation_blocks_check(self):
        self.register()
        (self.pack / "unit.cpp").write_text("void changed();")
        with self.assertRaisesRegex(ValueError, "pack inputs changed"):
            self.check()

    def test_source_changed_during_check_invalidates_result(self):
        self.register()
        real = m.digest
        def unstable(path):
            value = real(path)
            if Path(path) == self.source:
                self.source.write_text(self.source.read_text() + " ")
            return value
        with patch.object(m, "digest", side_effect=unstable):
            self.assertFalse(self.check("match")["valid"])

    def test_promote_requires_actual_final_match(self):
        self.register()
        self.check()
        self.gate()
        with self.assertRaisesRegex(ValueError, "final official MATCH"):
            m.promote(self.db, argparse.Namespace(claim=self.claim, output=str(self.root / "match.cpp")))

    def test_normal_and_auto_repair_matches_not_verified(self):
        result = self.ready(extra={"errors": ["AUTO-REPAIR: class replaced"]})
        self.assertEqual(result["state"], "submit_ready")
        self.assertIn("not proof", result["integration_warning"])
        self.assertEqual(m.summary(self.db)["attempts"][0]["latest"]["auto_repair"], True)

    def test_accepted_alternate_is_normal_submission(self):
        self.metadata["accept_also"] = [["/MT", "/O1"]]
        self.write_pack()
        self.assertEqual(self.ready(extra={"accepted_flags": ["/MT", "/O1"]})["state"], "submit_ready")

    def test_unaccepted_flags_require_proof(self):
        self.assertEqual(self.ready(extra={"accepted_flags": ["/MT", "/O1"]})["state"], "proof_ready")
        with self.assertRaisesRegex(ValueError, "submit_ready"):
            self.record("submitted")
        with self.assertRaisesRegex(ValueError, "checkpoint"):
            self.record("flag_review", "flag_test")
        self.record("flag_review", "flag_test", "chk_test")

    def test_changed_final_source_cannot_promote(self):
        self.register()
        self.check()
        self.gate()
        self.check("match", "final")
        self.source.write_text("void another_function() {}")
        with self.assertRaisesRegex(ValueError, "source changed"):
            m.promote(self.db, argparse.Namespace(claim=self.claim, output=str(self.root / "match.cpp")))

    def test_changed_promoted_artifact_cannot_submit(self):
        result = self.ready()
        Path(result["artifact"]).write_text("// a near miss is not a match")
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            self.record("submitted")

    def test_existing_different_match_cpp_is_not_overwritten(self):
        self.register()
        self.check()
        self.gate()
        self.check("match", "final")
        dest = self.root / "match.cpp"
        dest.write_text("USER WORK")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            m.promote(self.db, argparse.Namespace(claim=self.claim, output=str(dest)))
        self.assertEqual(dest.read_text(), "USER WORK")

    def test_status_import_records_unfit_without_credit(self):
        self.ready()
        self.record("submitted")
        status = self.root / "status.json"
        payload = {"recent": [{"id": "sub_test", "addr": "0x00123456", "verdict": "unfit", "detail": "full unit differs"}]}
        status.write_text(json.dumps(payload))
        m.import_status(self.db, argparse.Namespace(file=str(status)))
        self.assertEqual(m.attempt(self.db, self.claim)["state"], "unfit")
        payload["recent"][0]["verdict"] = None
        status.write_text(json.dumps(payload))
        m.import_status(self.db, argparse.Namespace(file=str(status)))
        self.assertEqual(m.summary(self.db)["server_results"][0]["verdict"], "unfit")

    def test_absence_from_status_is_not_a_verdict(self):
        self.ready()
        self.record("submitted")
        status = self.root / "status.json"
        status.write_text('{"recent": []}')
        m.import_status(self.db, argparse.Namespace(file=str(status)))
        self.assertEqual(m.attempt(self.db, self.claim)["state"], "submitted")

    def test_historical_agent_report_is_not_promoted(self):
        folder = self.root / "dsmax_example"
        folder.mkdir()
        (folder / "report.json").write_text(json.dumps({"address": "0x00123456", "result": "MATCH"}))
        m.import_reports(self.db, argparse.Namespace(directory=str(self.root)))
        m.import_reports(self.db, argparse.Namespace(directory=str(self.root)))
        info = m.summary(self.db)
        self.assertEqual(info["historical_reports"], 1)
        self.assertEqual(info["attempts"], [])
        self.assertEqual(info["server_results"], [])

    def test_invalid_baselines_never_shadow_latest_valid_baseline(self):
        self.register()
        self.check(extra={'diffs': 7})
        self.check('match', exit_code=15)
        self.assertEqual(m.summary(self.db)['attempts'][0]['baseline']['diffs'], 7)
        self.gate()

    def test_exact_direct_check_import_requires_provenance_and_duration(self):
        self.register()
        original = self.check()
        args = argparse.Namespace(claim=self.claim, stage='baseline', source=str(self.source), flags=None,
                                  evidence_dir=str(self.root/'imported'), import_evidence=original['evidence'])
        with patch.object(m.subprocess, 'run') as runner:
            imported = m.check(self.db, args)
            self.assertTrue(imported['valid'])
            runner.assert_not_called()
        self.source.write_text('void changed() {}')
        with self.assertRaisesRegex(ValueError, 'provenance'):
            m.check(self.db, args)


if __name__ == "__main__":
    unittest.main()
