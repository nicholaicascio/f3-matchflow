import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from config import workspace_root


class ConfigurationTests(unittest.TestCase):
    def test_explicit_workspace_and_local_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base/'repo'
            repo.mkdir()
            external = base/'private-workspace'
            (repo/'matchflow.local.json').write_text(json.dumps(dict(workspace=str(external))))
            with patch.dict('os.environ', {}, clear=True):
                self.assertEqual(workspace_root(repo), external.resolve())
            with patch.dict('os.environ', {'F3_MATCHFLOW_WORKSPACE': str(base/'override')}):
                self.assertEqual(workspace_root(repo), (base/'override').resolve())

    def test_missing_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'Set F3_MATCHFLOW_WORKSPACE'):
                workspace_root(Path(directory))

    def test_relative_and_in_repository_workspaces_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            for value in ('relative-workspace', str(repo), str(repo/'work')):
                with self.subTest(value=value), patch.dict('os.environ', {'F3_MATCHFLOW_WORKSPACE': value}):
                    with self.assertRaises(ValueError):
                        workspace_root(repo)


if __name__ == '__main__':
    unittest.main()
