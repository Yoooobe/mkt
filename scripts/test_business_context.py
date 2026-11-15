import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import business_context

class ContextTests(unittest.TestCase):
    def test_catalog(self):
        r = business_context.resolve(objective='welcome kits para onboarding')
        self.assertEqual(r['business_unit'], 'catalogo')
        self.assertIn('kits personalizados', r['text'])
        self.assertNotIn('24h', r['text'])
    def test_api(self):
        r = business_context.resolve(objective='Integração API para SaaS')
        self.assertEqual(r['business_unit'], 'corporate_api')
        self.assertIn('discovery', r['text'])
    def test_international(self):
        r = business_context.resolve(objective='API for international teams in Brazil')
        self.assertEqual(r['language'], 'en')
        self.assertIn('Brasil', r['text'])
    def test_no_partial_blocks(self):
        r = business_context.resolve(max_chars=7000, optional=[('huge', 'X' * 50000)])
        self.assertNotIn('XXXXX', r['text'])
        self.assertEqual(r['excluded'][0]['name'], 'huge')
        self.assertLessEqual(len(r['text']), 7000)
    def test_required_overflow(self):
        with self.assertRaises(ValueError):
            business_context.resolve(max_chars=10)
    def test_unknown_unit(self):
        with self.assertRaises(ValueError):
            business_context.resolve(business_unit='invalid')
    def test_stable_version(self):
        self.assertEqual(business_context.resolve()['version'], business_context.resolve()['version'])

if __name__ == '__main__': unittest.main()
