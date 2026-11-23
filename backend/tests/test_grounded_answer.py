import unittest

from app.services.retrieval.grounded_answer import (
    IncompleteGroundedAnswer,
    generate_grounded_answer,
    render_grounded_answer,
)
from app.services.retrieval.workflow_context import business_workflow_stage_requirements


class GroundedAnswerTests(unittest.TestCase):
    def setUp(self):
        self.sources = [
            {"citation_id": "S1", "file_path": "routes/auth.js", "start_line": 10, "end_line": 20, "source_kind": "source_code"},
            {"citation_id": "S2", "file_path": "README.md", "start_line": 30, "end_line": 36, "source_kind": "documentation"},
        ]

    def test_claims_keep_precise_valid_citations_and_source_categories(self):
        payload = {
            "complete": True,
            "source_code_facts": [{"claim": "The login route validates the request.", "citations": ["S1"]}],
            "documentation_facts": [{"claim": "The README documents account setup.", "citations": ["S2"]}],
            "inferences": [{"claim": "The route likely feeds the client session.", "citations": ["S1"]}],
            "not_found": [],
        }
        result = render_grounded_answer(payload, self.sources)
        self.assertIn("Verified source-code facts", result.markdown)
        self.assertIn("The login route validates the request. [S1]", result.markdown)
        self.assertIn("Documentation-derived facts", result.markdown)
        self.assertIn("The README documents account setup. [S2]", result.markdown)
        self.assertIn("Inferences", result.markdown)
        self.assertEqual(result.cited_source_ids, {"S1", "S2"})

    def test_empty_documentation_section_is_omitted(self):
        result = render_grounded_answer({
            "complete": True,
            "source_code_facts": [{"claim": "The route exists.", "citations": ["S1"]}],
            "documentation_facts": [], "inferences": [], "not_found": [],
        }, self.sources)
        self.assertNotIn("Documentation-derived facts", result.markdown)

    def test_auth_workflow_retries_when_available_files_are_not_cited_or_are_called_missing(self):
        required = ["Register.jsx", "PrivateRoute.jsx", "AdminRoute.jsx", "CreatorRoute.jsx"]
        sources = [
            {"citation_id": "S1", "file_path": "routes/auth.js", "source_kind": "source_code"},
            *[{"citation_id": f"S{i+2}", "file_path": f"auth/{path}", "source_kind": "source_code"}
              for i, path in enumerate(required)],
        ]
        missing_payload = {
            "complete": True,
            "source_code_facts": [{"claim": "The backend exposes authentication routes.", "citations": ["S1"]}],
            "documentation_facts": [], "inferences": [],
            "not_found": ["Register.jsx and the three route guards were not retrieved."],
        }
        covered_payload = {
            "complete": True,
            "source_code_facts": [
                {"claim": "The backend exposes authentication routes.", "citations": ["S1"]},
                *[{"claim": f"{path} contains a route-guard implementation.", "citations": [f"S{i+2}"]}
                  for i, path in enumerate(required)],
            ],
            "documentation_facts": [], "inferences": [], "not_found": [],
        }
        import json
        replies = iter([
            (json.dumps(missing_payload), "stop"),
            (json.dumps(covered_payload), "stop"),
        ])
        prompts = []

        def complete(system, prompt):
            prompts.append(prompt)
            return next(replies)

        result = generate_grounded_answer("system", "auth-flow context", sources, complete, required_source_paths=[s["file_path"] for s in sources])
        self.assertEqual(len(prompts), 2)
        self.assertIn("Coverage correction", prompts[1])
        self.assertIn("Register.jsx contains a route-guard implementation. [S2]", result.markdown)
        self.assertIn("PrivateRoute.jsx contains a route-guard implementation. [S3]", result.markdown)
        self.assertNotIn("were not retrieved", result.markdown)

    def test_order_payment_answer_citing_only_auth_middleware_is_rejected(self):
        question=('Explain the complete order placement and payment verification flow from the Checkout frontend '
                  'through the backend order and payment routes to the database models.')
        sources=[
            {"citation_id":"S1","file_path":"web/src/pages/Checkout.jsx","source_kind":"source_code"},
            {"citation_id":"S2","file_path":"server/routes/orders.js","source_kind":"source_code"},
            {"citation_id":"S3","file_path":"server/routes/payments.js","source_kind":"source_code"},
            {"citation_id":"S4","file_path":"server/models/Order.js","source_kind":"source_code"},
            {"citation_id":"S5","file_path":"server/middleware/auth.js","source_kind":"source_code"},
        ]
        requirements,unavailable=business_workflow_stage_requirements(question,sources)
        self.assertFalse(unavailable)
        import json
        payload=json.dumps({"complete":True,"facts":[{"kind":"source_code","stage":"authentication middleware",
            "claim":"The middleware checks an authorization token.","citations":["S5"]}],"not_found":[]})
        with self.assertRaises(IncompleteGroundedAnswer) as error:
            generate_grounded_answer("system","order/payment evidence",sources,
                lambda *_:(payload,"stop"),required_workflow_stages=requirements)
        self.assertIn("required_workflow_stages_missing",error.exception.reasons)

    def test_uncited_unknown_and_wrong_kind_claims_are_not_rendered_as_facts(self):
        payload = {
            "complete": True,
            "source_code_facts": [
                {"claim": "Unsupported claim.", "citations": []},
                {"claim": "Documentation is code.", "citations": ["S2"]},
                {"claim": "Fabricated source claim. [S99]", "citations": ["S99"]},
            ],
            "documentation_facts": [], "inferences": [], "not_found": ["The requested route guard was not retrieved."],
        }
        result = render_grounded_answer(payload, self.sources)
        self.assertNotIn("Unsupported claim.", result.markdown)
        self.assertNotIn("Documentation is code.", result.markdown)
        self.assertNotIn("Fabricated source claim.", result.markdown)
        self.assertNotIn("S99", result.markdown)
        self.assertIn("omitted because it lacked a valid citation", result.markdown)
        self.assertIn("route guard was not retrieved", result.markdown)
        self.assertEqual(result.cited_source_ids, frozenset())

    def test_output_limit_retries_compactly_and_delivers_only_complete_json(self):
        payload = '{"complete":true,"source_code_facts":[{"claim":"The route exists.","citations":["S1"]}],"documentation_facts":[],"inferences":[],"not_found":[]}'
        replies = iter([("truncated", "length"), (payload, "stop")])
        prompts = []

        def complete(system, prompt):
            prompts.append(system)
            return next(replies)

        result = generate_grounded_answer("system", "evidence prompt", self.sources, complete)
        self.assertIn("The route exists. [S1]", result.markdown)
        self.assertEqual(len(prompts), 2)
        self.assertIn("compact retry", prompts[1])

    def test_repeated_truncation_returns_explicit_incomplete_failure(self):
        calls = []

        def complete(system, prompt):
            calls.append((system, prompt))
            return '{"complete":false', "length"

        with self.assertRaises(IncompleteGroundedAnswer):
            generate_grounded_answer("system", "evidence prompt", self.sources, complete)
        self.assertEqual(len(calls), 2)

    def test_invalid_json_retries_then_fails_instead_of_returning_a_partial_answer(self):
        calls = []

        def complete(system, prompt):
            calls.append(1)
            return "A partial uncited answer", "stop"

        with self.assertRaises(IncompleteGroundedAnswer):
            generate_grounded_answer("system", "evidence prompt", self.sources, complete)
        self.assertEqual(len(calls), 2)

    def test_compact_large_workflow_combines_citations_and_validates_each_stage(self):
        stages = {
            "registration": [["S1", "S2"]],
            "email verification and activation": [["S3", "S4"]],
            "login, password and token": [["S5", "S6"]],
            "frontend authentication state": [["S7"]],
            "frontend route guards": [["S8"], ["S9"], ["S10"]],
        }
        sources = [{"citation_id":f"S{i}","file_path":f"auth/source{i}.jsx","source_kind":"source_code"}
                  for i in range(1,11)]
        import json
        payload={"complete":True,"facts":[
            {"kind":"source_code","stage":"registration","claim":"Registration submits account details to the backend.","citations":["S1","S2"]},
            {"kind":"source_code","stage":"email verification and activation","claim":"Verification activates the matching account.","citations":["S3","S4"]},
            {"kind":"source_code","stage":"login, password and token","claim":"Login compares the password and returns a token.","citations":["S5","S6"]},
            {"kind":"source_code","stage":"frontend authentication state","claim":"The auth context stores the signed-in user.","citations":["S7"]},
            {"kind":"source_code","stage":"frontend route guards","claim":"The route guards check access by role.","citations":["S8","S9","S10"]}],"not_found":[]}
        calls=[]
        result=generate_grounded_answer("compact system","large evidence",sources,
            lambda system,prompt:(calls.append((system,prompt)) or (json.dumps(payload),"stop",{"completion_tokens":320})),
            required_workflow_stages=stages)
        self.assertEqual(len(calls),1)
        self.assertEqual(len(result.cited_source_ids),10)
        self.assertIn("Verification activates the matching account. [S3] [S4]",result.markdown)
        self.assertIn("The route guards check access by role. [S8] [S9] [S10]",result.markdown)

    def test_missing_required_workflow_stage_is_retried_then_rejected(self):
        sources=[{"citation_id":"S1","file_path":"routes/auth.js","source_kind":"source_code"}]
        stages={"registration":[["S1"]],"email verification and activation":[["S1"]]}
        payload={"complete":True,"facts":[{"kind":"source_code","stage":"registration",
                 "claim":"Registration is handled by the auth route.","citations":["S1"]}],"not_found":[]}
        import json
        calls=[]
        def complete(system,prompt):
            calls.append(prompt)
            return json.dumps(payload),"stop"
        with self.assertRaises(IncompleteGroundedAnswer) as caught:
            generate_grounded_answer("system","prompt",sources,complete,required_workflow_stages=stages)
        self.assertEqual(len(calls),2)
        self.assertIn("Coverage correction",calls[1])
        self.assertIn("required_workflow_stages_missing",caught.exception.reasons)

    def test_uncited_compact_fact_is_not_accepted_as_a_partial_success(self):
        payload={"complete":True,"facts":[
            {"kind":"source_code","stage":"registration","claim":"Supported fact.","citations":["S1"]},
            {"kind":"source_code","stage":"registration","claim":"Uncited extra claim.","citations":[]}],"not_found":[]}
        import json
        calls=[]
        def complete(system,prompt):
            calls.append(1)
            return json.dumps(payload),"stop"
        with self.assertRaises(IncompleteGroundedAnswer):
            generate_grounded_answer("system","prompt",self.sources,complete)
        self.assertEqual(len(calls),2)


if __name__ == "__main__":
    unittest.main()
