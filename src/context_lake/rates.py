"""Public list-price cost estimate, shared by the model backends. Rates: model_rates.json (USD per 1M tokens)."""
import json
from pathlib import Path

RATES = json.loads((Path(__file__).resolve().parent / "model_rates.json").read_text())["bedrock"]


def usd(model, prompt_tokens, output_tokens, multiplier=1.0):
    """Estimated USD, or None for a model with no public rate in model_rates.json."""
    r = RATES.get(model)
    return round(multiplier * (prompt_tokens * r["input"] + output_tokens * r["output"]) / 1e6, 6) if r else None
