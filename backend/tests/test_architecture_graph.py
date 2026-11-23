import unittest
from types import SimpleNamespace

from app.services.architecture.graph import build_architecture_graph, validate_architecture_graph


def point(path, content, start=1, end=None, symbol=None, symbol_type="module"):
    return SimpleNamespace(payload={
        "repository_id": 1,
        "file_path": path,
        "content": content,
        "start_line": start,
        "end_line": end or start + max(0, len(content.splitlines()) - 1),
        "symbol_name": symbol or path.rsplit("/", 1)[-1],
        "symbol_type": symbol_type,
        "language": path.rsplit(".", 1)[-1],
    })


class ArchitectureGraphTests(unittest.TestCase):
    def setUp(self):
        self.points = [
            point("web/src/pages/Checkout.jsx", "import axios from 'axios';\nconst response = await axios.post(\n  `${API_URL}/api/orders`,\n  payload\n)", 1, 5),
            point("server/index.js", "const orderRoutes = require('./routes/orders');\napp.use('/api', orderRoutes);\nmongoose.connect(process.env.MONGODB_URI)", 1, 3, "index", "entrypoint"),
            point("server/routes/orders.js", "const auth = require('../middleware/auth');\nconst Order = require('../models/Order');\nrouter.post('/orders', auth, async (req, res) => res.status(201).json(await Order.create(req.body)))", 1, 3, "POST /orders", "route"),
            point("server/middleware/auth.js", "module.exports = function authenticate(req, res, next) { next() }", 1, 1, "authenticate", "middleware"),
            point("server/models/Order.js", "const OrderSchema = new Schema({ product: { type: ObjectId, ref: 'Product' } })", 1, 1, "Order", "model"),
            point("server/models/Product.js", "const ProductSchema = new Schema({ name: String })", 1, 1, "Product", "model"),
            point("server/routes/payments.js", "router.post('/payments/verify', verifyPayment)\nconst gateway = 'https://api.razorpay.com/v1/orders'", 1, 2, "POST /payments/verify", "route"),
            point("web/package.json", '{\n  "dependencies": {\n    "react": "^18.0.0"\n  }\n}', 1, 5),
        ]

    def test_graph_discovers_applications_routes_models_and_verified_relationships(self):
        graph = build_architecture_graph("owner/Hastakala", 1, self.points)
        nodes = {node["id"]: node for node in graph["nodes"]}
        edges = graph["edges"]
        self.assertIn("layer:frontend", nodes)
        self.assertIn("layer:backend", nodes)
        self.assertEqual(nodes["layer:backend"]["label"], "Backend API")
        self.assertIn("database:mongodb", nodes)
        self.assertIn("external:razorpay", nodes)
        checkout = "file:web/src/pages/Checkout.jsx"
        order_route = "file:server/routes/orders.js"
        payment_route = "file:server/routes/payments.js"
        order_model = "file:server/models/Order.js"
        product_model = "file:server/models/Product.js"
        auth = "file:server/middleware/auth.js"
        self.assertTrue(any(edge["source"] == checkout and edge["target"] == order_route and edge["type"] == "api_call" for edge in edges))
        self.assertTrue(any(edge["source"] == order_route and edge["target"] == auth and edge["type"] == "uses_middleware" for edge in edges))
        self.assertTrue(any(edge["source"] == order_route and edge["target"] == order_model and edge["type"] == "uses_model" for edge in edges))
        self.assertTrue(any(edge["source"] == order_model and edge["target"] == product_model and edge["type"] == "references_model" for edge in edges))
        api_edge = next(edge for edge in edges if edge["source"] == checkout and edge["target"] == order_route and edge["type"] == "api_call")
        self.assertEqual({citation["file_path"] for citation in api_edge["citations"]},
                         {"web/src/pages/Checkout.jsx", "server/routes/orders.js"})
        self.assertTrue(any(citation["file_path"] == "web/src/pages/Checkout.jsx"
                            and citation["start_line"] <= 2 <= citation["end_line"]
                            for citation in api_edge["citations"]))
        response_edge = next(edge for edge in edges if edge["source"] == "layer:backend"
                             and edge["target"] == "layer:frontend" and edge["label"] == "JSON response")
        self.assertIn("server/routes/orders.js", {citation["file_path"] for citation in response_edge["citations"]})
        self.assertIn("web/src/pages/Checkout.jsx", {citation["file_path"] for citation in response_edge["citations"]})

    def test_graph_nodes_and_edges_are_stable_and_validated(self):
        first = build_architecture_graph("owner/Hastakala", 1, self.points)
        second = build_architecture_graph("owner/Hastakala", 1, self.points)
        self.assertEqual([node["id"] for node in first["nodes"]], [node["id"] for node in second["nodes"]])
        self.assertEqual([(edge["source"], edge["target"], edge["type"]) for edge in first["edges"]],
                         [(edge["source"], edge["target"], edge["type"]) for edge in second["edges"]])
        self.assertIs(validate_architecture_graph(first), first)

    def test_empty_or_unrelated_evidence_does_not_invent_components(self):
        graph = build_architecture_graph("owner/Empty", 5, [])
        self.assertEqual(graph["nodes"], [])
        self.assertEqual(graph["edges"], [])
        self.assertEqual(graph["metadata"]["indexed_source_files"], 0)

    def test_prefixed_repository_folder_names_assign_declared_technologies_to_their_layers(self):
        points = [
            point("craft-app-frontend/package.json", '{"dependencies":{"react":"^18","vite":"^6","tailwindcss":"^3"}}', 1, 1),
            point("craft-app-backend/package.json", '{"scripts":{"start":"node index.js"},"dependencies":{"express":"^4","mongoose":"^8"}}', 1, 1),
        ]
        graph = build_architecture_graph("owner/shop", 9, points)
        nodes = {node["id"]: node for node in graph["nodes"]}
        implementations = {(edge["source"], nodes[edge["target"]]["label"]) for edge in graph["edges"]
                           if edge["type"] == "implemented_with"}
        self.assertEqual(implementations, {
            ("layer:frontend", "React"), ("layer:frontend", "Vite"), ("layer:frontend", "Tailwind CSS"),
            ("layer:backend", "Express"), ("layer:backend", "Mongoose"), ("layer:backend", "Node.js"),
        })

    def test_single_layer_repository_is_named_as_an_application_without_forced_services(self):
        graph = build_architecture_graph("owner/small-python", 10, [
            point("app.py", "def predict(row):\n    return row", 1, 2),
            point("sql/create_table.sql", "CREATE TABLE samples (id INTEGER)", 1, 1),
        ])
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(nodes["layer:shared"]["label"], "Application modules")
        self.assertNotIn("layer:frontend", nodes)
        self.assertNotIn("layer:backend", nodes)
        self.assertFalse(any(node["type"] in {"database", "external"} for node in graph["nodes"]))

    def test_client_browser_node_and_edge_require_indexed_browser_evidence(self):
        without_browser = build_architecture_graph("owner/shop", 11, self.points)
        self.assertFalse(any(node["type"] == "client" for node in without_browser["nodes"]))
        points = [*self.points, point("web/src/App.jsx", "import { BrowserRouter } from 'react-router-dom';", 1, 1)]
        graph = build_architecture_graph("owner/shop", 11, points)
        browser = next(node for node in graph["nodes"] if node["type"] == "client")
        edge = next(edge for edge in graph["edges"] if edge["type"] == "renders_in")
        self.assertEqual((edge["source"], edge["target"]), (browser["id"], "file:web/src/App.jsx"))
        self.assertTrue(all(citation["file_path"] == "web/src/App.jsx" for citation in edge["citations"]))

    def test_urls_in_source_comments_are_not_reported_as_external_integrations(self):
        points = [*self.points, point("web/vite.config.js", "// See https://vite.dev/config/\nexport default {}", 1, 2)]
        graph = build_architecture_graph("owner/shop", 12, points)
        self.assertFalse(any(node["label"] == "vite.dev" for node in graph["nodes"]))
        self.assertFalse(any(edge["type"] == "calls_external" and "vite.dev" in edge["target"]
                             for edge in graph["edges"]))

    def test_constructed_and_invoked_payment_sdk_is_a_verified_backend_integration(self):
        path = "api/routes/payments.js"
        graph = build_architecture_graph("owner/shop", 13, [
            point(path, "const Razorpay = require('razorpay');\nconst client = new Razorpay({});\n"
                         "router.post('/payments/create-order', async (req, res) => {\n"
                         "  const order = await client.orders.create({ amount: 500 });\n"
                         "  res.json(order);\n});", 1, 6, "POST /payments/create-order", "route")
        ])
        node = next(node for node in graph["nodes"] if node["id"] == "external:razorpay")
        edge = next(edge for edge in graph["edges"] if edge["type"] == "calls_external")
        self.assertEqual(node["label"], "Razorpay Payment Gateway")
        self.assertEqual((edge["source"], edge["target"], edge["label"]),
                         ("file:api/routes/payments.js", node["id"], "Create & verify payment"))
        self.assertTrue(edge["citations"])

    def test_factory_created_email_client_is_reported_without_inventing_smtp_provider(self):
        path = "backend/utils/sendEmail.js"
        graph = build_architecture_graph("owner/shop", 14, [point(path,
            "const nodemailer = require('nodemailer');\nconst transporter = nodemailer.createTransport({});\n"
            "await transporter.sendMail({ to: recipient, subject: 'Notice' });", 1, 3)])
        node = next(node for node in graph["nodes"] if node["id"] == "external:nodemailer")
        edge = next(edge for edge in graph["edges"] if edge["type"] == "calls_external")
        self.assertEqual(node["label"], "Nodemailer Email Service")
        self.assertEqual(node["hostnames"], [])
        self.assertEqual(edge["label"], "Email delivery")

    def test_ml_workflow_preserves_ui_flask_feature_preprocessing_inference_and_threshold_stages(self):
        app = '''from flask import Flask, request, jsonify, render_template
import joblib
MODEL_PATH = "models/fraud_xgboost_model.pkl"
PREPROCESSOR_PATH = "models/fraud_preprocessor.pkl"
THRESHOLD_PATH = "models/fraud_threshold.pkl"
model = joblib.load(MODEL_PATH)
preprocessor = joblib.load(PREPROCESSOR_PATH)
threshold = float(joblib.load(THRESHOLD_PATH))
app = Flask(__name__)
def calculate_distance(lat1, lon1, lat2, lon2):
    return 0
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")
@app.route("/predict", methods=["POST"])
def predict():
    distance = calculate_distance(1, 2, 3, 4)
    input_data = pd.DataFrame([[distance]])
    processed_data = preprocessor.transform(input_data)
    probability = model.predict_proba(processed_data)[0][1]
    prediction = "Fraudulent" if probability >= threshold else "Non-Fraudulent"
    return jsonify({"prediction": prediction, "probability": probability})
'''
        template = '''<form id="fraudForm"><input name="amount"></form>
<script>
document.getElementById("fraudForm").addEventListener("submit", async function(event) {
 const response = await fetch("/predict", {method: "POST"});
 const data = await response.json();
});
</script>'''
        graph = build_architecture_graph("owner/credit-card-fraud-detection-system", 22, [
            point("app.py", app, 1, len(app.splitlines()), "predict", "entrypoint"),
            point("templates/index.html", template, 1, len(template.splitlines()), "fraudForm", "component"),
        ])
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertIn("layer:frontend", nodes)
        self.assertIn("layer:backend", nodes)
        self.assertNotIn("external:joblib", nodes)
        self.assertNotIn("external:xgboost", nodes)
        artifacts = {node.get("artifact_path"): node for node in graph["nodes"] if node["type"] == "artifact"}
        self.assertEqual(set(artifacts), {
            "models/fraud_xgboost_model.pkl", "models/fraud_preprocessor.pkl", "models/fraud_threshold.pkl"})
        self.assertTrue(any(node["label"] == "Feature Engineering" for node in graph["nodes"]))
        self.assertFalse(any("Haversine" in node["label"] for node in graph["nodes"]))
        self.assertTrue(all(node["citations"] for node in artifacts.values()))
        edge_pairs = {(edge["source"], edge["target"], edge.get("label")) for edge in graph["edges"]}
        feature = next(node for node in graph["nodes"] if node["label"] == "Feature Engineering")
        prep = artifacts["models/fraud_preprocessor.pkl"]
        model = artifacts["models/fraud_xgboost_model.pkl"]
        threshold = artifacts["models/fraud_threshold.pkl"]
        self.assertIn(("layer:backend", feature["id"], "engineers features"), edge_pairs)
        self.assertIn((feature["id"], prep["id"], "engineered features"), edge_pairs)
        self.assertIn((prep["id"], model["id"], "transformed features"), edge_pairs)
        self.assertIn((model["id"], threshold["id"], "prediction probability"), edge_pairs)
        self.assertIn(("file:templates/index.html", "file:app.py", "HTTP POST /predict"), edge_pairs)
        self.assertIn(("layer:backend", "layer:frontend", "JSON response"), edge_pairs)
        for edge in graph["edges"]:
            self.assertTrue(edge["citations"])

    def test_feature_stage_name_is_repository_independent_and_requires_transform_evidence(self):
        source = '''import joblib
MODEL_PATH = "model.pkl"
model = joblib.load(MODEL_PATH)
def engineer_features(row):
    return [row["amount"]]
def predict(row):
    values = engineer_features(row)
    return model.predict(values)
'''
        graph = build_architecture_graph("owner/health-risk", 25,
                                         [point("predictor.py", source, 1, len(source.splitlines()))])
        feature = next(node for node in graph["nodes"] if node["label"] == "Feature Engineering")
        self.assertEqual(feature["label"], "Feature Engineering")
        self.assertNotIn("Haversine", feature["responsibility"])
        self.assertTrue(feature["citations"])

    def test_dataframe_input_alone_does_not_invent_feature_engineering_stage(self):
        source = '''import joblib
MODEL_PATH = "classifier.pkl"
model = joblib.load(MODEL_PATH)
def predict(row):
    values = pd.DataFrame([row])
    return model.predict(values)
'''
        graph = build_architecture_graph("owner/simple-ml", 26,
                                         [point("app.py", source, 1, len(source.splitlines()))])
        self.assertFalse(any(node["label"] == "Feature Engineering" for node in graph["nodes"]))

    def test_ordinary_ml_packages_never_become_network_integrations(self):
        content = "import joblib\nimport numpy as np\nimport pandas as pd\nfrom sklearn.preprocessing import StandardScaler\nimport xgboost\n"
        graph = build_architecture_graph("owner/ml", 23, [point("app.py", content, 1, 5)])
        external_labels = {node["label"] for node in graph["nodes"] if node["type"] == "external"}
        self.assertFalse(external_labels & {"Joblib API", "Numpy API", "Pandas API", "Sklearn API", "Xgboost API"})

    def test_small_non_web_ml_repository_gets_a_functional_inference_graph(self):
        content = '''import joblib
MODEL_PATH = "models/classifier.pkl"
model = joblib.load(MODEL_PATH)
def main(row):
    return model.predict_proba(row)
'''
        graph = build_architecture_graph("owner/small-ml", 24, [point("main.py", content, 1, 5)])
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(nodes["layer:backend"]["label"], "ML Inference Application")
        self.assertIn("artifact:models/classifier.pkl", nodes)
        self.assertTrue(any(edge["source"] == "layer:backend" and edge["target"] == "artifact:models/classifier.pkl"
                            for edge in graph["edges"]))

    def test_streamlit_ml_repository_is_named_as_an_application_and_keeps_model_evidence(self):
        content = '''import streamlit as st
import joblib
MODEL_PATH = "models/heart_model.pkl"
model = joblib.load(MODEL_PATH)
st.title("Heart Disease Risk Screening")
age = st.number_input("Age")
if st.button("Predict"):
    result = model.predict([[age]])
    st.write(result)
'''
        graph = build_architecture_graph("owner/heart-disease-risk-screening", 27,
                                         [point("app.py", content, 1, len(content.splitlines()))])
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(nodes["layer:backend"]["label"], "Streamlit Application")
        self.assertIn("technology:streamlit", nodes)
        self.assertIn("artifact:models/heart_model.pkl", nodes)
        self.assertIsNone(nodes["artifact:models/heart_model.pkl"]["artifact_exists"])
        self.assertTrue(nodes["artifact:models/heart_model.pkl"]["label"].startswith("Configured model path"))
        self.assertFalse(any(node["type"] == "external" and node["label"].lower() == "streamlit"
                             for node in graph["nodes"]))
        self.assertNotIn("external:joblib", nodes)
        self.assertTrue(any(edge["source"] == "layer:backend" and edge["target"] == "artifact:models/heart_model.pkl"
                            for edge in graph["edges"]))
        source_keys = {(source["file_path"], source["start_line"], source["end_line"]) for source in graph["sources"]}
        self.assertTrue(all((citation["file_path"], citation["start_line"], citation["end_line"]) in source_keys
                            for node in graph["nodes"] for citation in node.get("citations", [])))
        self.assertTrue(all((citation["file_path"], citation["start_line"], citation["end_line"]) in source_keys
                            for edge in graph["edges"] for citation in edge.get("citations", [])))

    def test_streamlit_architecture_includes_user_boundary_and_training_workflow(self):
        app = '''import streamlit as st
import joblib
MODEL_PATH = "models/heart_disease_model.joblib"
model = joblib.load(MODEL_PATH)
st.title("Heart Disease Risk Screening")
age = st.number_input("Age")
if st.button("Predict"):
    result = model.predict([[age]])
    st.write(result)
'''
        notebook_code = '''import pandas as pd
from sklearn.model_selection import train_test_split
data = pd.read_csv("data/heart.csv")
X_train, X_test, y_train, y_test = train_test_split(data, labels)
model.fit(X_train, y_train)
joblib.dump(model, "models/heart_disease_model.joblib")
'''
        points = [
            point("app.py", app, 1, len(app.splitlines())),
            point("notebooks/model_development.ipynb", notebook_code, 1, len(notebook_code.splitlines())),
        ]
        points[0].payload["repository_file_inventory"] = [
            "app.py", "notebooks/model_development.ipynb", "data/heart.csv", "models/heart_disease_model.joblib"
        ]
        graph = build_architecture_graph("owner/heart-disease-risk-screening", 28, points)
        nodes = {node["id"]: node for node in graph["nodes"]}
        edges = {(edge["source"], edge["target"], edge.get("label")) for edge in graph["edges"]}

        self.assertIn("client:browser", nodes)
        self.assertIn(("client:browser", "layer:backend", "user interaction"), edges)
        self.assertIn("artifact:dataset:data/heart.csv", nodes)
        self.assertTrue(nodes["artifact:dataset:data/heart.csv"]["artifact_exists"])
        self.assertTrue(nodes["artifact:models/heart_disease_model.joblib"]["artifact_exists"])
        development = nodes["process:notebooks/model_development.ipynb:model-development"]
        self.assertEqual(development["label"], "Jupyter Notebook Model Development")
        self.assertIn(("artifact:dataset:data/heart.csv", development["id"], "training data"), edges)
        self.assertIn((development["id"], "artifact:models/heart_disease_model.joblib", "trained model artifact"), edges)
        source_keys = {(row["file_path"], row["start_line"], row["end_line"]) for row in graph["sources"]}
        self.assertTrue(all((citation["file_path"], citation["start_line"], citation["end_line"]) in source_keys
                            for node in graph["nodes"] for citation in node.get("citations", [])))
        self.assertTrue(all((citation["file_path"], citation["start_line"], citation["end_line"]) in source_keys
                            for edge in graph["edges"] for citation in edge.get("citations", [])))

    def test_streamlit_single_file_workflow_detects_literal_model_load_and_inline_features(self):
        app = '''import streamlit as st
import pandas as pd
import joblib
@st.cache_resource
def load_model():
    return joblib.load("heart_disease_model.joblib")
model = load_model()
with st.form("risk"):
    age = st.number_input("Age")
    cholesterol = st.number_input("Cholesterol")
    submitted = st.form_submit_button("Predict")
if submitted:
    input_data = pd.DataFrame([{"chol_per_age": cholesterol / (age + 1e-9)}])
    prediction = model.predict(input_data)[0]
    score = model.predict_proba(input_data)[0][1]
    st.markdown("## Screening Results")
    st.write(prediction, score)
'''
        graph = build_architecture_graph("owner/heart-screening", 77,
                                         [point("app.py", app, 1, len(app.splitlines()))])
        nodes = {node["id"]: node for node in graph["nodes"]}
        expected = {
            "process:app.py:user-input", "process:app.py:feature-engineering",
            "artifact:heart_disease_model.joblib", "process:app.py:model-inference",
            "process:app.py:prediction-output",
        }
        self.assertTrue(expected.issubset(nodes))
        self.assertIsNone(nodes["artifact:heart_disease_model.joblib"]["artifact_exists"])
        edges = {(edge["source"], edge["target"], edge.get("label")) for edge in graph["edges"]}
        self.assertIn(("layer:backend", "process:app.py:user-input", "accepts input"), edges)
        self.assertIn(("process:app.py:user-input", "process:app.py:feature-engineering", "builds model input"), edges)
        self.assertIn(("process:app.py:feature-engineering", "process:app.py:model-inference", "predicts from features"), edges)
        self.assertIn(("artifact:heart_disease_model.joblib", "process:app.py:model-inference", "loaded model"), edges)
        self.assertIn(("process:app.py:model-inference", "process:app.py:prediction-output", "displays prediction"), edges)
        source_keys = {(source["file_path"], source["start_line"], source["end_line"]) for source in graph["sources"]}
        for node_id in expected:
            self.assertTrue(nodes[node_id]["citations"])
            self.assertTrue(all((c["file_path"], c["start_line"], c["end_line"]) in source_keys
                                for c in nodes[node_id]["citations"]))
        load_citation = nodes["artifact:heart_disease_model.joblib"]["citations"][0]
        self.assertEqual(load_citation["start_line"], load_citation["end_line"])

    def test_referenced_model_path_is_not_claimed_to_exist_when_inventory_says_absent(self):
        content = '''import joblib
MODEL_PATH = "models/missing_model.pkl"
model = joblib.load(MODEL_PATH)
def predict(value):
    return model.predict(value)
'''
        source = point("app.py", content, 1, len(content.splitlines()))
        source.payload["repository_file_inventory"] = ["app.py"]
        graph = build_architecture_graph("owner/ml", 31, [source])
        artifact = next(node for node in graph["nodes"] if node.get("artifact_path") == "models/missing_model.pkl")
        self.assertIs(artifact["artifact_exists"], False)
        self.assertIn("absent", artifact["responsibility"])
        self.assertIn("Configured model path", artifact["label"])

    def test_static_google_api_asset_url_is_not_promoted_to_external_service(self):
        content = '''import streamlit as st
st.image("https://www.googleapis.com/static/logo.png")
'''
        graph = build_architecture_graph("owner/small-app", 29, [point("app.py", content, 1, 2)])
        self.assertFalse(any(node.get("type") == "external" and "google" in node["label"].lower()
                             for node in graph["nodes"]))

    def test_explicit_http_call_still_produces_verified_external_service(self):
        content = '''import requests
response = requests.get("https://api.example.test/v1/status")
'''
        graph = build_architecture_graph("owner/api-client", 30, [point("app.py", content, 1, 2)])
        self.assertTrue(any(node.get("type") == "external" and node["id"] == "external:test"
                            for node in graph["nodes"]))

    def test_framework_citation_uses_original_line_after_indexed_chunk_gap(self):
        graph = build_architecture_graph("owner/heart-disease-risk-screening", 27, [
            point("app.py", "import streamlit as st", 4, 4),
            point("app.py", "st.title('Heart Risk')\nst.button('Predict')", 10, 11),
        ])
        node = next(node for node in graph["nodes"] if node["id"] == "technology:streamlit")
        self.assertEqual(node["citations"], [{"file_path": "app.py", "start_line": 4, "end_line": 4}])
        self.assertIn(node["citations"][0], graph["sources"])
        validate_architecture_graph(graph)

    def test_validation_rejects_duplicate_node_ids_and_unsupported_edges(self):
        graph = build_architecture_graph("owner/Hastakala", 1, self.points)
        duplicate = {**graph, "nodes": graph["nodes"] + [dict(graph["nodes"][0])]}
        with self.assertRaisesRegex(ValueError, "Duplicate architecture node"):
            validate_architecture_graph(duplicate)
        invalid_edge = {**graph, "edges": [*graph["edges"], {
            "id": "bad", "source": graph["nodes"][0]["id"], "target": graph["nodes"][0]["id"],
            "type": "invented_relationship", "verified": True, "citations": graph["nodes"][0]["citations"][:1]}]}
        with self.assertRaisesRegex(ValueError, "Unsupported architecture relationship"):
            validate_architecture_graph(invalid_edge)

    def test_validation_error_identifies_missing_node_evidence_by_file_and_range(self):
        graph = build_architecture_graph("owner/Hastakala", 1, self.points)
        broken = {**graph, "nodes": [dict(node) for node in graph["nodes"]]}
        broken["nodes"][0] = {**broken["nodes"][0], "citations": [
            {"file_path": "missing/source.js", "start_line": 8, "end_line": 12}
        ]}
        with self.assertRaisesRegex(ValueError, r"node .* citation is absent from graph sources: 'missing/source\.js' lines 8-12"):
            validate_architecture_graph(broken)

    def test_validation_error_identifies_bad_source_line_range(self):
        graph = build_architecture_graph("owner/Hastakala", 1, self.points)
        broken = {**graph, "sources": [*graph["sources"], {
            "file_path": "server/broken.js", "start_line": 0, "end_line": 2,
        }]}
        with self.assertRaisesRegex(ValueError, r"Invalid source evidence #\d+: file_path='server/broken\.js' lines=0-2"):
            validate_architecture_graph(broken)

    def test_bad_index_line_metadata_names_the_file_and_field(self):
        malformed = point("src/broken.py", "def run(): return True", 1, 1)
        malformed.payload["start_line"] = "not-a-line"
        with self.assertRaisesRegex(ValueError, r"Invalid indexed source line metadata for 'src/broken\.py': start_line='not-a-line'"):
            build_architecture_graph("owner/repo", 1, [malformed])


if __name__ == "__main__":
    unittest.main()
