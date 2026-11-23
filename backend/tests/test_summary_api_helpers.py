import unittest

from app.api.routes import SummaryRequest, _summary_prompt, extract_citation_ids


class SummaryHelpersTests(unittest.TestCase):
    def test_extracts_ascii_full_width_and_compact_range_citations(self):
        text = "Claim [S1][S2], another 【S3‑S5】, and [S7, S9]."
        self.assertEqual(extract_citation_ids(text), {"S1", "S2", "S3", "S4", "S5", "S7", "S9"})

    def test_summary_request_models_scope_inputs(self):
        request = SummaryRequest(scope="module", topic="payment verification")
        self.assertEqual(request.scope, "module")
        self.assertEqual(request.topic, "payment verification")

    def test_summary_prompts_are_evidence_grounded_and_avoid_tables(self):
        prompt = _summary_prompt("repository", "owner/repo", final=True)
        self.assertIn("indexed implementation source", prompt)
        self.assertIn("Frontend; Backend;", prompt)
        self.assertIn("Avoid tables", prompt)


if __name__ == "__main__":
    unittest.main()
