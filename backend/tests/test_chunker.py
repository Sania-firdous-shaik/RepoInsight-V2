import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.parsing import chunker
from app.services.parsing.chunker import chunks, EXT_LANG, SKIP_DIRS, SKIP_NAMES, SECRET_SUFFIXES, is_sensitive_file

PARSER_AVAILABLE = importlib.util.find_spec("tree_sitter_language_pack") is not None

class ChunkerTests(unittest.TestCase):
    def assert_complete_coverage(self, source, result):
        covered=set()
        for item in result:
            self.assertLessEqual(item['end_line']-item['start_line']+1, chunker.MAX_SYMBOL_LINES)
            covered.update(range(item['start_line'],item['end_line']+1))
            self.assertTrue(item['file_path'])
            self.assertTrue(item['content_hash'])
        for line_no,line in enumerate(source.splitlines(),1):
            if line.strip(): self.assertIn(line_no,covered,f"source line {line_no} disappeared")

    def test_fallback_chunking_keeps_ranges_and_complete_source(self):
        source='\n'.join(f'line {i}' for i in range(1, 181))
        with patch.object(chunker,"_syntax_candidates",side_effect=RuntimeError("parser unavailable")):
            result=chunks('src/example.py', source)
        self.assertGreaterEqual(len(result), 2)
        self.assertEqual(result[0]['start_line'], 1)
        self.assert_complete_coverage(source,result)

    def test_python_functions_and_classes_when_grammar_is_installed(self):
        if not PARSER_AVAILABLE: self.skipTest("tree-sitter-language-pack not installed")
        source='''from dataclasses import dataclass\n\nclass Order:\n    def save(self, db):\n        def inner():\n            return db.add(self)\n        return inner()\n\ndef verifyPayment(signature):\n    return bool(signature)\n'''
        result=chunks('models/order.py',source)
        self.assertIn('Order',[c['symbol_name'] for c in result])
        self.assertIn('save',[c['symbol_name'] for c in result])
        self.assertIn('verifyPayment',[c['symbol_name'] for c in result])
        self.assertIn('method',[c['symbol_type'] for c in result])
        self.assert_complete_coverage(source,result)

    def test_javascript_functions_arrow_react_and_express_route(self):
        if not PARSER_AVAILABLE: self.skipTest("tree-sitter-language-pack not installed")
        source='''import React from "react";\nfunction handlePlaceOrder(order) { return api.post("/api/orders", order); }\nconst Checkout = ({items}) => <main>{items.map(x => <div>{x}</div>)}</main>;\nrouter.post("/api/payments/verify", async (req, res) => { res.json({ok:true}); });\n'''
        result=chunks('src/Checkout.jsx',source)
        names=[c['symbol_name'] for c in result]
        self.assertIn('handlePlaceOrder',names)
        self.assertIn('Checkout',names)
        self.assertIn('POST /api/payments/verify',names)
        self.assertIn('component',[c['symbol_type'] for c in result])
        self.assertIn('route',[c['symbol_type'] for c in result])
        self.assert_complete_coverage(source,result)

    def test_fallback_covers_source_when_no_supported_nodes_exist(self):
        source='\n'.join(["import os"]+[f"setting_{i}: value_{i}" for i in range(120)])
        with patch.object(chunker,"_syntax_candidates",return_value=[]):
            result=chunks('settings.yaml',source)
        self.assert_complete_coverage(source,result)

    def test_markdown_headings_are_section_boundaries(self):
        source="# Intro\nAlpha\n## Install\nRun it\n"
        result=chunks('README.md',source)
        self.assertEqual([x['symbol_name'] for x in result],['Intro','Install'])

    def test_unsupported_and_empty_files_are_ignored(self):
        self.assertEqual(chunks('image.png', 'contents'), [])
        self.assertEqual(chunks('empty.py', '  \n'), [])

    def test_secret_and_generated_filters(self):
        self.assertEqual(EXT_LANG['.tsx'], 'TypeScript')
        self.assertIn('node_modules', SKIP_DIRS)
        self.assertIn('package-lock.json', SKIP_NAMES)
        self.assertIn('.pem', SECRET_SUFFIXES)
        self.assertTrue(is_sensitive_file(Path('config/production-secrets.yaml')))
        self.assertTrue(is_sensitive_file(Path('.env.production')))

    def test_architecture_inputs_include_notebooks_and_safe_config_examples(self):
        notebook = json.dumps({"cells": [
            {"cell_type": "markdown", "source": ["Overview"]},
            {"cell_type": "code", "source": ["import pandas as pd\n", "data = pd.read_csv('data/heart.csv')\n",
                                                "model.fit(data, labels)\n"]},
        ]}, indent=2)
        result = chunks("notebooks/train.ipynb", notebook)
        self.assertTrue(result)
        self.assertTrue(all(item["language"] == "Jupyter Notebook" for item in result))
        self.assertTrue(all(item["symbol_type"] == "notebook_cell" for item in result))
        self.assertTrue(any("read_csv" in item["content"] for item in result))
        self.assertTrue(all(len(item["source_line_map"]) == len(item["content"].splitlines()) for item in result))
        self.assertEqual(chunker.language_for_path("pyproject.toml"), "TOML")
        self.assertEqual(chunker.language_for_path("requirements.txt"), "Python requirements")
        self.assertEqual(chunker.language_for_path(".env.example"), "Environment example")
        self.assertFalse(is_sensitive_file(Path(".env.example")))
        self.assertTrue(is_sensitive_file(Path(".env.production")))

if __name__ == '__main__': unittest.main()
