import unittest

from app.api.routes import _is_reviewable_source_path, _parse_review_json, _redact_review_secrets, _review_static_findings, _validate_review_findings


class CodeReviewTests(unittest.TestCase):
    def test_invalid_json_is_rejected_for_controlled_retry(self):
        with self.assertRaises(ValueError):
            _parse_review_json('not json')

    def test_json_fence_and_object_shape_are_supported(self):
        self.assertEqual(_parse_review_json('```json\n{"findings":[]}\n```'), [])

    def test_json_parser_ignores_surrounding_model_commentary(self):
        self.assertEqual(_parse_review_json('Result follows:\n```json\n{"findings":[]}\n```\nDone.'), [])

    def test_paths_and_line_ranges_are_validated_against_actual_source(self):
        items = [
            {'title': 'Valid', 'severity': 'High', 'category': 'Security', 'file_path': 'src/a.py', 'start_line': 11, 'end_line': 11},
            {'title': 'Wrong path', 'severity': 'High', 'category': 'Security', 'file_path': '../secrets', 'start_line': 11, 'end_line': 11},
            {'title': 'Wrong line', 'severity': 'High', 'category': 'Security', 'file_path': 'src/a.py', 'start_line': 18, 'end_line': 19},
        ]
        result = _validate_review_findings(items, 'src/a.py', 'safe = True', ['Security'], offset=10)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]['start_line'], result[0]['end_line']), (1, 1))
        self.assertEqual(result[0]['evidence'], 'safe = True')

    def test_deterministic_secret_check_redacts_value(self):
        result = _review_static_findings('config.py', 'api_key = "very-secret-value"', ['Security'])
        self.assertEqual(len(result), 1)
        self.assertNotIn('very-secret-value', result[0]['evidence'])
        self.assertEqual(result[0]['source'], 'static')

    def test_review_scope_excludes_documentation_and_redacts_model_context(self):
        self.assertTrue(_is_reviewable_source_path('src/pages/Cart.jsx'))
        self.assertFalse(_is_reviewable_source_path('README.md'))
        self.assertFalse(_is_reviewable_source_path('dist/app.min.js'))
        self.assertFalse(_is_reviewable_source_path('src/generated/client.ts'))
        sanitized = _redact_review_secrets('API_KEY = "secret-value-123"\nconst ok = true')
        self.assertNotIn('secret-value-123', sanitized)
        self.assertEqual(len(sanitized.splitlines()), 2)


if __name__ == '__main__':
    unittest.main()
