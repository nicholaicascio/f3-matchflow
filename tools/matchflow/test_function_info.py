from copy import deepcopy
import unittest

from function_info import co_workers


class FunctionInfoTests(unittest.TestCase):
    def test_supported_locations_preserve_input(self):
        for count in (0, 2):
            for info in (dict(co_workers=count), dict(regions=dict(co_workers=count)),
                         dict(co_workers=count, regions=dict(co_workers=count))):
                with self.subTest(info=info):
                    before = deepcopy(info)
                    self.assertEqual(co_workers(info), count)
                    self.assertEqual(info, before)

    def test_missing_or_malformed_counts_never_default_to_zero(self):
        for info in ({}, dict(regions={}), dict(regions=None), dict(regions=[])):
            with self.subTest(info=info), self.assertRaisesRegex(ValueError, 'regions|co_workers'):
                co_workers(info)
        for invalid in (None, True, False, -1, '0', 0.0, [], {}):
            for info in (dict(co_workers=invalid), dict(regions=dict(co_workers=invalid)),
                         dict(co_workers=0, regions=dict(co_workers=invalid)),
                         dict(co_workers=invalid, regions=dict(co_workers=0))):
                with self.subTest(info=info), self.assertRaisesRegex(ValueError, 'nonnegative integer'):
                    co_workers(info)

    def test_conflicting_counts_are_rejected_in_both_directions(self):
        for flat, nested in ((0, 1), (1, 0)):
            with self.subTest(flat=flat), self.assertRaisesRegex(ValueError, 'conflicting'):
                co_workers(dict(co_workers=flat, regions=dict(co_workers=nested)))
