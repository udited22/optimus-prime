"""Regenerate schemas/strategyspec.schema.json from the pydantic models."""

from pathlib import Path

from project100c.spec import write_json_schema

if __name__ == "__main__":
    out = Path(__file__).resolve().parents[1] / "schemas" / "strategyspec.schema.json"
    write_json_schema(out)
    print(f"wrote {out}")
