"""Numeric PII candidates must be independent of revision and identifier tokens."""
from pathlib import Path
import runpy
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCAN = runpy.run_path(str(ROOT/'openubmc-doc-review/resources/analyzers/security/scripts/prescan.py'))


class SecurityIdentifierTests(unittest.TestCase):
    def test_revision_digits_do_not_hide_a_separate_phone_on_the_same_line(self):
        revision = 'bfd9ca17942369058feaeec9a4eccccccccccccccc'
        phone = '17942369058'
        for line in (f'"revision": "{revision}"', f'build_{phone}', f'{phone}_build',
                     f'key{phone}', f'{phone}key'):
            with self.subTest(line=line):
                self.assertEqual(SCAN['check_pii'](line, [line], set(), (0, 0)), [])
        for line in (f'手机：{phone}', f'"phone":"{phone}"',
                     f'{{"revision":"{revision}","phone":"{phone}"}}'):
            with self.subTest(line=line):
                hits = SCAN['check_pii'](line, [line], set(), (0, 0))
                phones = [hit for hit in hits if hit['rule_name'] == '手机号泄露']
                self.assertEqual(len(phones), 1)
                self.assertEqual(phones[0]['error'], phone)

    def test_id_and_bank_candidates_inside_identifiers_are_excluded(self):
        for pattern, value in ((SCAN['RE_ID_CARD'], '110101199002032345'),
                               (SCAN['RE_BANK_CARD'], '4532015112830366')):
            with self.subTest(value=value):
                self.assertIsNotNone(pattern.search(f'"value":"{value}"'))
                self.assertIsNone(pattern.search(f'ab{value}cd'))


if __name__ == '__main__':
    unittest.main()
