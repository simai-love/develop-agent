"""Check the response schemas accepted by strict structured output."""

import json
import unittest
from pathlib import Path


SCHEMA_ROOT = Path(__file__).resolve().parents[1] / ".codex" / "skills" / "research-workflow" / "references"


class ResponseSchemaTests(unittest.TestCase):
    """Keep every defined object property required by the CLI response format."""

    def test_all_object_properties_are_required(self):
        """Read both response schemas and reject incomplete object requirements."""
        for name in ("plan-response.schema.json", "validation-response.schema.json"):
            with self.subTest(schema=name):
                schema = json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))
                self._check_schema(schema)

    def _check_schema(self, schema: dict) -> None:
        """Inspect schema and nested objects for strict output compatibility."""
        if schema.get("type") == "object":
            self.assertEqual(set(schema["properties"]), set(schema["required"]))
            self.assertIs(schema["additionalProperties"], False)
            for value in schema["properties"].values():
                self._check_schema(value)
        elif schema.get("type") == "array":
            self._check_schema(schema["items"])


if __name__ == "__main__":
    unittest.main()
