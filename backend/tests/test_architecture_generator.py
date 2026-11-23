import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.architecture.generator import _merge_interpretation, _readme_context, _source_evidence, interpret_architecture_graph
from app.services.architecture.graph import build_architecture_graph, cached_architecture_graph
from app.services.ai.groq_service import GroqRateLimitError


def point(path, content, start=1):
    return SimpleNamespace(payload={"repository_id": 91, "file_path": path, "content": content,
                                    "start_line": start, "end_line": start + len(content.splitlines()) - 1,
                                    "symbol_name": "source", "symbol_type": "module"})


class ArchitectureGeneratorTests(unittest.TestCase):
    def test_architecture_context_selects_functional_files_and_redacts_secret_values(self):
        points = [
            point("app.py", "from flask import Flask\nAPI_KEY = 'secret-value-should-not-escape'\n"
                             "model = joblib.load(MODEL_PATH)\nprobability = model.predict_proba(data)\n"
                             "return jsonify({'probability': probability})"),
            point("templates/index.html", "<form id='fraudForm'></form>\n"
                                              "const response = await fetch('/predict', {method: 'POST'});\n"
                                              "const data = await response.json();"),
            point("README.md", "general readme not source"),
        ]
        context, refs = _source_evidence(points)
        self.assertIn("app.py", context)
        self.assertIn("templates/index.html", context)
        self.assertNotIn("secret-value-should-not-escape", context)
        self.assertTrue(all(ref["file_path"] != "README.md" for ref in refs.values()))
        self.assertTrue(all(ref["citation"]["start_line"] <= ref["start_line"] <= ref["end_line"] <= ref["citation"]["end_line"]
                            for ref in refs.values()))

    def test_readme_is_bounded_orientation_context_not_code_evidence(self):
        readme = point("README.md", "# Project\nA Streamlit model prediction application.\n" + "details\n" * 1000)
        context = _readme_context([readme], limit_chars=1000)
        self.assertIn("README supporting context only; not implementation evidence", context)
        self.assertIn("README.md", context)
        self.assertLessEqual(len(context), 1000)
        source_context, evidence = _source_evidence([readme])
        self.assertEqual(source_context, "")
        self.assertEqual(evidence, {})

    def test_architecture_evidence_selection_includes_indexed_training_workflows(self):
        training = point("training/train_model.py", "data = pd.read_csv('data/heart.csv')\n"
                         "X_train, X_test, y_train, y_test = train_test_split(X, y)\n"
                         "model.fit(X_train, y_train)\nmodel.save_model('models/heart.json')")
        context, evidence = _source_evidence([training])
        self.assertIn("training/train_model.py", context)
        self.assertIn("read_csv", context)
        self.assertTrue(evidence)

    def test_architecture_context_uses_file_blocks_and_fair_cross_file_coverage(self):
        # The former context allocation sorted every chunk globally, allowing
        # files with many matching cues to crowd out other workflow stages.
        points = [
            point("web/app.py", "from flask import Flask\n@app.route('/predict')\ndef predict(): pass\n" * 5),
            point("web/templates/index.html", "fetch('/predict')\nconst result = await response.json()"),
            point("ml/features.py", "def engineer_features(row):\n    return row['distance']"),
            point("ml/preprocessor.py", "features = preprocessor.transform(features)"),
            point("ml/predictor.py", "probability = model.predict_proba(features)"),
            point("ml/threshold.py", "is_fraud = probability > threshold"),
        ]

        context, evidence = _source_evidence(points)

        self.assertIn("FILE: web/app.py", context)
        self.assertIn("CODE:\n", context)
        self.assertEqual({item["file_path"] for item in evidence.values()}, {item.payload["file_path"] for item in points})
        self.assertLessEqual(len(context), 24000)

    def test_architecture_prompt_follows_context_to_flowchart_generation_pattern(self):
        source = point("app.py", "from flask import Flask\napp = Flask(__name__)\n@app.post('/predict')\ndef predict(): return jsonify({})")
        graph = build_architecture_graph("owner/ml", 920, [source])
        response = json.dumps({"summary": "A source-backed prediction API.", "summary_refs": ["E1"],
                               "components": [], "relationships": []})
        with patch("app.services.architecture.generator.settings.groq_api_key", "configured"), \
             patch("app.services.architecture.generator.settings.groq_model", "configured-model"), \
             patch("app.services.architecture.generator.complete_with_metadata", return_value=(response, "stop", {})) as call:
            interpret_architecture_graph(graph, [source])

        system_prompt = call.call_args.args[0]
        self.assertIn("top-to-bottom architecture flowchart", system_prompt)
        self.assertIn("execution and data flow", system_prompt)
        self.assertIn("source_refs", system_prompt)

    def test_ai_components_require_indexed_name_evidence_and_relationship_evidence(self):
        source = point("app.py", "preprocessed = preprocessor.transform(input_data)\n"
                                  "probability = model.predict_proba(preprocessed)")
        graph = build_architecture_graph("owner/ml", 91, [source])
        context, evidence = _source_evidence([source])
        ref = next(iter(evidence))
        response = {"summary": "The source transforms input before prediction.", "summary_refs": [ref],
                    "components": [
                        {"id": "ai:preprocessor", "name": "Preprocessor", "type": "service",
                         "responsibility": "Transforms model input.", "source_refs": [ref]},
                        {"id": "ai:invented", "name": "Remote Payments Service", "type": "external",
                         "responsibility": "Unsupported service.", "source_refs": [ref]},
                    ],
                    "relationships": [
                        {"source_id": "ai:preprocessor", "target_id": "ai:invented", "type": "calls_external",
                         "label": "payment", "source_refs": [ref]},
                    ]}
        enriched = _merge_interpretation(graph, response, evidence)
        self.assertIn("ai:preprocessor", {node["id"] for node in enriched["nodes"]})
        self.assertNotIn("ai:invented", {node["id"] for node in enriched["nodes"]})
        self.assertFalse(any(edge["source"] == "ai:preprocessor" for edge in enriched["edges"]))
        self.assertEqual(enriched["summary"], response["summary"])
        self.assertEqual(enriched["metadata"]["ai_architecture_status"], "partially_interpreted")
        self.assertEqual(enriched["metadata"]["ai_architecture_failure_category"], "unsupported_claims_filtered")
        self.assertEqual(enriched["metadata"]["ai_architecture_rejected_components"], 1)

    def test_rate_limit_returns_deterministic_graph_without_retrying(self):
        source = point("app.py", "from flask import Flask\napp = Flask(__name__)")
        graph = build_architecture_graph("owner/ml", 91, [source])
        with patch("app.services.architecture.generator.settings.groq_api_key", "configured"), \
             patch("app.services.architecture.generator.settings.groq_model", "configured-model"), \
             patch("app.services.architecture.generator.complete_with_metadata", side_effect=GroqRateLimitError(60)) as call:
            result = interpret_architecture_graph(graph, [source])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result["metadata"]["ai_architecture_status"], "rate_limited_deterministic_fallback")
        self.assertTrue(result["nodes"])

    def test_missing_groq_configuration_keeps_deterministic_architecture(self):
        source = point("app.py", "from flask import Flask\napp = Flask(__name__)")
        graph = build_architecture_graph("owner/ml", 91, [source])
        with patch("app.services.architecture.generator.settings.groq_api_key", ""), \
             patch("app.services.architecture.generator.complete_with_metadata") as call:
            result = interpret_architecture_graph(graph, [source])
        call.assert_not_called()
        self.assertEqual(result["metadata"]["ai_architecture_status"], "not_configured")

    def test_groq_interpretation_uses_compact_json_evidence_and_is_bounded(self):
        source = point("app.py", "preprocessed = preprocessor.transform(input_data)\n"
                                  "probability = model.predict_proba(preprocessed)")
        graph = build_architecture_graph("owner/ml", 92, [source])
        response = json.dumps({"summary": "Preprocessing feeds the prediction call.", "summary_refs": ["E1"],
                               "components": [{"id": "ai:preprocessor", "name": "Preprocessor", "type": "service",
                                              "responsibility": "Transforms model inputs.", "source_refs": ["E1"]}],
                               "relationships": []})
        with patch("app.services.architecture.generator.settings.groq_api_key", "configured"), \
             patch("app.services.architecture.generator.settings.groq_model", "configured-model"), \
             patch("app.services.architecture.generator.complete_with_metadata", return_value=(response, "stop", {})) as call:
            result = interpret_architecture_graph(graph, [source])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.kwargs["max_retries"], 0)
        self.assertEqual(call.call_args.kwargs["json_mode"], True)
        self.assertEqual(call.call_args.kwargs["max_tokens"], 3000)
        self.assertIn("app.py", call.call_args.args[1])
        self.assertIn("E1", call.call_args.args[1])
        self.assertEqual(result["metadata"]["ai_architecture_status"], "interpreted")
        self.assertIn("ai:preprocessor", {node["id"] for node in result["nodes"]})

    def test_truncated_architecture_output_falls_back_with_specific_safe_category(self):
        source = point("app.py", "from flask import Flask\napp = Flask(__name__)")
        graph = build_architecture_graph("owner/ml", 920, [source])
        with patch("app.services.architecture.generator.settings.groq_api_key", "configured"), \
             patch("app.services.architecture.generator.settings.groq_model", "configured-model"), \
             patch("app.services.architecture.generator.complete_with_metadata", return_value=("{", "length", {})):
            result = interpret_architecture_graph(graph, [source])
        self.assertEqual(result["metadata"]["ai_architecture_status"], "deterministic_fallback")
        self.assertEqual(result["metadata"]["ai_architecture_failure_category"], "truncated_output")

    def test_invalid_json_architecture_response_is_reported_as_invalid_json(self):
        source = point("app.py", "from flask import Flask\napp = Flask(__name__)")
        graph = build_architecture_graph("owner/ml", 921, [source])
        with patch("app.services.architecture.generator.settings.groq_api_key", "configured"), \
             patch("app.services.architecture.generator.settings.groq_model", "configured-model"), \
             patch("app.services.architecture.generator.complete_with_metadata", return_value=("not json", "stop", {})):
            result = interpret_architecture_graph(graph, [source])
        self.assertEqual(result["metadata"]["ai_architecture_failure_category"], "invalid_json")

    def test_invalid_architecture_schema_is_reported_as_validation_rejection(self):
        source = point("app.py", "from flask import Flask\napp = Flask(__name__)")
        graph = build_architecture_graph("owner/ml", 922, [source])
        with patch("app.services.architecture.generator.settings.groq_api_key", "configured"), \
             patch("app.services.architecture.generator.settings.groq_model", "configured-model"), \
             patch("app.services.architecture.generator.complete_with_metadata", return_value=(
                 json.dumps({"summary": "not enough schema", "components": {}, "relationships": []}), "stop", {})):
            result = interpret_architecture_graph(graph, [source])
        self.assertEqual(result["metadata"]["ai_architecture_failure_category"], "invalid_components_or_relationships")

    def test_incomplete_ai_interpretation_cannot_drop_deterministic_components_or_relationships(self):
        source = point("api/main.py", "from flask import Flask\napp = Flask(__name__)\n@app.get('/health')\ndef health(): return {'ok': True}")
        graph = build_architecture_graph("owner/api", 93, [source])
        expected_nodes = {node["id"] for node in graph["nodes"]}
        expected_edges = {edge["id"] for edge in graph["edges"]}
        response = json.dumps({"summary": "A source-backed Flask API.", "summary_refs": ["E1"],
                               "components": [], "relationships": []})
        with patch("app.services.architecture.generator.settings.groq_api_key", "configured"), \
             patch("app.services.architecture.generator.settings.groq_model", "configured-model"), \
             patch("app.services.architecture.generator.complete_with_metadata", return_value=(response, "stop", {})):
            result = interpret_architecture_graph(graph, [source])
        self.assertTrue(expected_nodes.issubset({node["id"] for node in result["nodes"]}))
        self.assertTrue(expected_edges.issubset({edge["id"] for edge in result["edges"]}))

    def test_ai_can_add_evidenced_relationship_between_existing_graph_nodes(self):
        source = point("app.py", "from flask import Flask\napp = Flask(__name__)\n")
        graph = build_architecture_graph("owner/api", 94, [source])
        _context, evidence = _source_evidence([source])
        ref = next(iter(evidence))
        response = {"summary": "A Flask application exposes the runtime entry point.",
                    "summary_refs": [ref], "components": [],
                    "relationships": [{"source_id": "layer:backend", "target_id": "technology:flask",
                                       "type": "uses_service", "label": "runs on Flask", "source_refs": [ref]}]}
        enriched = _merge_interpretation(graph, response, evidence)
        self.assertTrue(any(edge["source"] == "layer:backend" and edge["target"] == "technology:flask"
                            and edge["label"] == "runs on Flask" for edge in enriched["edges"]))
        self.assertNotEqual(enriched["metadata"].get("ai_architecture_failure_category"), "unsupported_claims_filtered")

    def test_architecture_cache_is_scoped_to_repository_commit(self):
        source = point("app.py", "from flask import Flask\napp = Flask(__name__)")
        calls = []
        def interpreter(graph, _points):
            calls.append(graph["repository"]["name"])
            return graph
        cached_architecture_graph("owner/cache-test", 92001, "generation", [source],
                                  commit_sha="commit-a", interpreter=interpreter)
        cached_architecture_graph("owner/cache-test", 92001, "generation", [source],
                                  commit_sha="commit-a", interpreter=interpreter)
        cached_architecture_graph("owner/cache-test", 92001, "generation-v2", [source],
                                  commit_sha="commit-a", interpreter=interpreter)
        cached_architecture_graph("owner/cache-test", 92001, "generation", [source],
                                  commit_sha="commit-b", interpreter=interpreter)
        self.assertEqual(calls, ["owner/cache-test", "owner/cache-test", "owner/cache-test"])


if __name__ == "__main__":
    unittest.main()
