import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bootstrap


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base/'repo'; self.repo.mkdir()
        self.workspace = self.base/'workspace'; self.workspace.mkdir()
        (self.workspace/'AGENTS.md').write_text('Offline fixture policy')
        self.key = self.base/'private-key'
        self.key.touch()  # No credential content, not even needed for generation.

    def test_bridge_loads_policy_and_mcp_without_reading_key(self):
        with patch.object(Path, 'read_text', side_effect=AssertionError('must not read credential')):
            config = bootstrap.bridge(self.workspace, self.key)
        self.assertIn((self.workspace/'AGENTS.md').as_posix(), config['instructions'])
        server = config['mcp']['servers']['f3decomp']
        self.assertEqual(server['url'], 'https://f3decomp.com/mcp')
        self.assertEqual(server['headers']['Authorization'], 'Bearer {file:' + self.key.as_posix() + '}')
        self.assertFalse(server['oauth'])

    def test_install_is_idempotent_and_does_not_replace_custom_config(self):
        path = bootstrap.install(self.repo, self.workspace, self.key)
        original = path.read_bytes()
        bootstrap.install(self.repo, self.workspace, self.key)
        self.assertEqual(original, path.read_bytes())
        path.write_text('{"model":"fixture/custom"}')
        with self.assertRaisesRegex(ValueError, 'refusing to overwrite'):
            bootstrap.install(self.repo, self.workspace, self.key)
        self.assertEqual(json.loads(path.read_text())['model'], 'fixture/custom')

    def test_alternate_config_is_not_shadowed(self):
        (self.repo/'opencode.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'merge'):
            bootstrap.install(self.repo, self.workspace, self.key)

    def test_missing_policy_blocks_incomplete_setup(self):
        with self.assertRaisesRegex(ValueError, 'AGENTS.md'):
            bootstrap.bridge(self.base/'absent', self.key)


if __name__ == '__main__':
    unittest.main()
