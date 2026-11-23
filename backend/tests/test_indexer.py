import tempfile
import unittest
from pathlib import Path

from app.services.ingestion.indexer import _repository_file_inventory


class RepositoryInventoryTests(unittest.TestCase):
    def test_inventory_records_existing_safe_paths_without_reading_file_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "app.py").write_text("source")
            (root / "models").mkdir()
            (root / "models" / "model.joblib").write_bytes(b"\x00binary")
            (root / "data").mkdir()
            (root / "data" / "heart.csv").write_text("x,y\n1,0")
            (root / ".env").write_text("secret=value")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "package.json").write_text("{}")

            inventory = _repository_file_inventory(root, sorted(root.rglob("*")))

        self.assertEqual(inventory, ["app.py", "data/heart.csv", "models/model.joblib"])


if __name__ == "__main__":
    unittest.main()
