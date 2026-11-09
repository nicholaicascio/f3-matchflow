"""Exercise CI's Windows 8.3 temp aliases even on long-path developer installs."""
import ctypes
import io
import os
import tempfile
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.name == 'nt', 'Windows short paths only')
class WindowsPathTests(unittest.TestCase):
    def test_core_fixtures_under_short_temp_alias(self):
        with tempfile.TemporaryDirectory(prefix='matchflow CI path ') as folder:
            buffer = ctypes.create_unicode_buffer(32768)
            if not ctypes.windll.kernel32.GetShortPathNameW(folder, buffer, len(buffer)):
                self.skipTest('8.3 aliases unavailable on this volume')
            if buffer.value == folder:
                self.skipTest('volume does not generate 8.3 aliases')
            names = [
                'test_bootstrap.BootstrapTests.test_bridge_loads_policy_and_mcp_without_reading_key',
                'test_matchflow.WorkflowTests.test_source_changed_during_check_invalidates_result',
                'test_matchflow.WorkflowTests.test_import_rejects_moved_or_external_full_diff_and_preserves_valid_capture',
                'test_restricted.BrokerTests.test_mocked_scoped_check_promote_submit_notes_report_and_audit',
            ]
            output = io.StringIO()
            with patch.object(tempfile, 'tempdir', buffer.value):
                suite = unittest.defaultTestLoader.loadTestsFromNames(names)
                result = unittest.TextTestRunner(stream=output).run(suite)
            self.assertTrue(result.wasSuccessful(), output.getvalue())


if __name__ == '__main__':
    unittest.main()
