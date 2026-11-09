import json
from pathlib import Path
import tempfile
import unittest

from symbol_lookup import lookup


class SymbolTests(unittest.TestCase):
    def test_literal_bounded_names_and_addresses_only(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'symbols.json'
            path.write_text(json.dumps(dict(functions=[dict(symbol='tls_one', address='0x00123456', secret='no'),
                                                       dict(symbol='tls_two', address='0x00123457')])))
            self.assertEqual(lookup(path, 'tls', 1), dict(items=[dict(symbol='tls_one', address='0x00123456')], truncated=True))
            self.assertEqual(lookup(path, '.*')['items'], [])
            self.assertEqual(len(lookup(path, '0x00123456')['items']), 1)
            for limit in (0, 101, True):
                with self.assertRaises(ValueError):
                    lookup(path, 'tls', limit)
