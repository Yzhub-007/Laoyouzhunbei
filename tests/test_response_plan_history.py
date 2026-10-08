"""History remains reachable after hundreds of versions and concurrent appends."""
import json
import sqlite3
import unittest

import response_plans as rp
from engine import ValidationError


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        rp.initialize(self.db)

    def seed(self, count):
        for n in range(1, count + 1):
            self.append(n)

    def append(self, n):
        doc = {'id': f'{n:032x}', 'revision': n, 'title': f'North 入口 100%_ {n}',
               'created_at': '2026-10-04T00:00:00+00:00', 'created_by': 'test-author',
               'status': 'draft' if n % 2 else 'reviewed_draft', 'status_label': '草案',
               'parent_id': None, 'scope': {'point_ids': ['P-A' if n % 3 else 'P-B']}}
        self.db.execute('INSERT INTO response_plan_versions VALUES (?,?,?,?)',
                        (doc['id'], n, doc['created_at'], json.dumps(doc, ensure_ascii=False)))

    def test_every_version_reachable_beyond_200_without_duplicates(self):
        self.seed(245)
        revisions, before = [], None
        while True:
            page = rp.list_versions(self.db, limit=30, before=before)
            self.assertEqual(page['total'], 245)
            self.assertEqual(page['revision'], 245)
            self.assertLessEqual(len(page['records']), 30)
            revisions.extend(row['revision'] for row in page['records'])
            if not page['has_more']:
                self.assertIsNone(page['next_before'])
                break
            before = page['next_before']
        self.assertEqual(revisions, list(range(245, 0, -1)))

    def test_new_save_between_pages_cannot_shift_or_duplicate_old_results(self):
        self.seed(35)
        first = rp.list_versions(self.db, limit=30)
        self.append(36)
        second = rp.list_versions(self.db, limit=30, before=first['next_before'])
        self.assertEqual([r['revision'] for r in second['records']], [5, 4, 3, 2, 1])
        self.assertEqual(second['revision'], 36)
        self.assertEqual(second['total'], 36)

    def test_title_filters_are_literal_and_case_insensitive_for_ascii(self):
        self.seed(15)
        self.assertEqual(rp.list_versions(self.db, q='north 入口 100%_')['total'], 15)
        self.assertEqual(rp.list_versions(self.db, q='100Xx')['total'], 0)
        self.assertEqual(rp.list_versions(self.db, q="' OR 1=1 --")['records'], [])
        self.assertEqual(rp.list_versions(self.db, q='入口')['total'], 15)

    def test_filters_combine_with_cursor_and_keep_global_revision(self):
        self.seed(45)
        first = rp.list_versions(self.db, point_id='P-B', status='reviewed_draft', limit=3)
        self.assertEqual(first['total'], 7)
        self.assertEqual(first['revision'], 45)
        self.assertEqual([r['revision'] for r in first['records']], [42, 36, 30])
        second = rp.list_versions(self.db, point_id='P-B', status='reviewed_draft', limit=3,
                                  before=first['next_before'])
        self.assertEqual([r['revision'] for r in second['records']], [24, 18, 12])
        self.assertEqual(second['total'], 7)
        self.assertEqual(rp.list_versions(self.db, point_id='P')['total'], 0)

    def test_empty_and_end_page_are_unambiguous(self):
        self.assertEqual(rp.list_versions(self.db), {'revision': 0, 'records': [], 'total': 0,
                                                   'has_more': False, 'next_before': None})
        self.seed(30)
        self.assertFalse(rp.list_versions(self.db)['has_more'])
        self.assertEqual(rp.list_versions(self.db, before=1)['records'], [])

    def test_invalid_bounds_and_filters_are_rejected(self):
        invalid = [{'limit': n} for n in (0, 101, -1, True, 1.5, '30')]
        invalid += [{'before': n} for n in (0, -1, True, 2**63, '1')]
        invalid += [{'q': 'a' * 101}, {'q': '\n'}, {'point_id': []}, {'status': 'published'}]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValidationError):
                rp.list_versions(self.db, **options)

    def test_query_parameter_smuggling_and_invalid_numbers_are_rejected(self):
        for query in ({'limit': ['1', '100']}, {'unknown': ['x']}, {'before': ['']},
                      {'before': ['1.5']}, {'before': ['-1']}, {'limit': ['１']}, {'q': []}):
            with self.subTest(query=query), self.assertRaises(ValidationError):
                rp.listing_options(query)
        self.assertEqual(rp.listing_options({'limit': ['30'], 'before': ['123'], 'q': ['入口']}),
                         {'limit': 30, 'before': 123, 'q': '入口'})


if __name__ == '__main__':
    unittest.main()
