#!/usr/bin/env python3
"""Run the summarizer over an input table (make_inputs.py) with a pluggable model backend.

Batches share a prefix and file type, capped by --batch-rows and --batch-chars
(or one row each with --per-file). Oversized single rows are rejected. The system
prompt is ../prompts/summarizer.md; each output row must match ../schemas/output.schema.json.

Backends (--backend):
  bedrock       DEFAULT. Amazon Bedrock Converse with a forced tool whose input is the output
                table; falls back to an offered tool, then to JSON in the reply text, if the
                model refuses. Uses your own AWS credentials (standard AWS chain).
  bedrock-chat  Bedrock's OpenAI-compatible Chat Completions with a strict json_schema
                (--service-tier, --reasoning-effort).
  openai        any OpenAI-compatible server (--base-url), e.g. a local vLLM; no cost recorded.

Cost is an estimate: tokens x public list prices in model_rates.json (cache tokens at the
input rate, an upper bound); models without a listed rate get no estimate. Run on a few
batches first (--limit) and show the estimate before a full run.

Writes outputs.csv, calls.csv and run.json to out_dir. Then check quotes with check_outputs.py.

Example:
  python3 run_model.py inputs.csv runs/first --model us.anthropic.claude-sonnet-5-5 \
      --region us-east-1 --limit 2
"""
import argparse
import copy
import csv
import json
import re
import statistics
import sys
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parent
SYSTEM = (SKILL / "prompts" / "summarizer.md").read_text()
ROW_SCHEMA = json.loads((SKILL / "schemas" / "output.schema.json").read_text())
LIST_COLUMNS = ["about", "entities", "refers_to", "evidence"]
OUT_COLUMNS = ["unit_id", "what", "summary", "about", "entities", "refers_to", "evidence"]
CALL_COLUMNS = ["batch", "prefix", "file_type", "rows", "model", "mode", "attempts", "stop_reason", "input_tokens",
                "output_tokens", "cache_write_tokens", "cache_read_tokens", "rows_out", "latency_s", "cost_usd", "error"]
UNSUPPORTED = {"$schema", "$id", "title", "description", "minLength", "maxLength", "minItems", "maxItems"}
TEXT_INSTRUCTION = ('\n\nReply with only a JSON object of the form {"rows": [...]}, one object per input row with '
                    "the output columns above, and nothing else.")


def strip(schema):
    if isinstance(schema, dict):
        return {k: strip(v) for k, v in schema.items() if k not in UNSUPPORTED}
    if isinstance(schema, list):
        return [strip(v) for v in schema]
    return schema


API_SCHEMA = {"type": "object", "properties": {"rows": {"type": "array", "items": strip(copy.deepcopy(ROW_SCHEMA))}},
              "required": ["rows"], "additionalProperties": False}


def batches(rows, per_file=False, max_rows=4, max_chars=24000):
    """Bound each request by row count and serialized characters before any API call."""
    groups = OrderedDict()
    for row in rows:
        groups.setdefault((row["prefix"], row["file_type"]), []).append(row)
    result = []
    for key, group in groups.items():
        batch = []
        for row in group:
            if len(user_message([row])) > max_chars:
                raise ValueError(f"input row exceeds --batch-chars: {row['unit_id']}; shorten the excerpt or raise the approved budget")
            if batch and (per_file or len(batch) >= max_rows or len(user_message(batch + [row])) > max_chars):
                result.append((key, batch))
                batch = []
            batch.append(row)
        if batch:
            result.append((key, batch))
    return result


def user_message(rows):
    table = [{**r, "members": int(r["members"]), "size_bytes": int(r["size_bytes"]),
              "example_keys": json.loads(r["example_keys"]), "mentions": json.loads(r["mentions"])} for r in rows]
    return "Input table, one JSON object per row:\n" + json.dumps(table, ensure_ascii=False)


def parse_rows(text):
    """Rows from a JSON reply, tolerating code fences, reasoning before the JSON, or a bare list."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1], text[text.find("["): text.rfind("]") + 1]):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            return data["rows"]
        if isinstance(data, list):
            return data
    raise ValueError("no JSON rows in reply")


class Bedrock:
    """Converse; mode is downgraded once if the model refuses it: tool -> tool-auto -> text."""

    def __init__(self, args):
        from .bedrock_tool import client
        self.args, self.client, self.mode, self.max_tokens = args, client(args.region), "tool", args.max_tokens

    def call(self, rows):
        from .rates import usd
        while True:
            req = dict(modelId=self.args.model, messages=[{"role": "user", "content": [{"text": user_message(rows)}]}],
                       inferenceConfig={"maxTokens": self.max_tokens})
            tool = {"tools": [{"toolSpec": {"name": "record_rows", "inputSchema": {"json": API_SCHEMA}}}]}
            if self.mode == "tool":
                req.update(system=[{"text": SYSTEM}], toolConfig={**tool, "toolChoice": {"tool": {"name": "record_rows"}}})
            elif self.mode == "tool-auto":
                req.update(system=[{"text": SYSTEM + "\n\nAnswer only by calling the `record_rows` tool."}],
                           toolConfig={**tool, "toolChoice": {"auto": {}}})
            else:
                req.update(system=[{"text": SYSTEM + TEXT_INSTRUCTION}])
            try:
                resp = self.client.converse(**req)
                break
            except self.client.exceptions.ValidationException as e:
                msg = str(e).lower()
                if self.mode == "tool" and "tool" in msg:
                    self.mode = "tool-auto"
                elif self.mode != "text" and "tool" in msg:
                    self.mode = "text"
                elif "token" in msg and self.max_tokens > 4096:
                    self.max_tokens //= 2
                else:
                    raise
        u = resp.get("usage", {})
        rec = {"mode": self.mode, "stop_reason": resp.get("stopReason"), "input_tokens": u.get("inputTokens", 0),
               "output_tokens": u.get("outputTokens", 0), "cache_write_tokens": u.get("cacheWriteInputTokens", 0),
               "cache_read_tokens": u.get("cacheReadInputTokens", 0)}
        prompt = rec["input_tokens"] + rec["cache_write_tokens"] + rec["cache_read_tokens"]
        rec["cost_usd"] = usd(self.args.model, prompt, rec["output_tokens"])
        try:
            blocks = resp["output"]["message"]["content"]
            tool_input = next((b["toolUse"]["input"] for b in blocks if "toolUse" in b), None)
            if tool_input is not None:
                out = tool_input.get("rows") if isinstance(tool_input, dict) else tool_input
                out = parse_rows(out) if isinstance(out, str) else out
            else:
                out = parse_rows("".join(b.get("text", "") for b in blocks))
            if not isinstance(out, list):
                raise ValueError("model rows must be a list")
            return out, rec
        except (ValueError, TypeError, KeyError) as error:
            rec["error"] = f"invalid model output: {type(error).__name__}"
            return [], rec



class Chat:
    """Bedrock Chat Completions (bedrock-chat) or any OpenAI-compatible server (openai)."""

    def __init__(self, args):
        self.args, self.mode = args, args.backend

    def call(self, rows):
        from .bedrock_chat import call_json
        out, rec = call_json(self.args.model, self.args.region, SYSTEM, user_message(rows), "record_rows", API_SCHEMA,
                             self.args.max_tokens, self.args.service_tier, self.args.reasoning_effort,
                             self.args.base_url if self.args.backend == "openai" else None)
        rec["mode"] = self.mode
        if not isinstance(out, dict):
            return [], rec
        rows = out.get("rows", [])
        return rows if isinstance(rows, list) else [], rec


BACKENDS = {"bedrock": Bedrock, "bedrock-chat": Chat, "openai": Chat}


def call(backend, args, index, key, rows):
    prefix, file_type = key
    record = {"batch": index, "prefix": prefix, "file_type": file_type, "rows": len(rows), "model": args.model, "error": ""}
    wanted = {r["unit_id"] for r in rows}
    start = time.perf_counter()
    spent = {k: 0 for k in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens", "cost_usd")}
    cost_known = True
    verified = {}
    out = []
    for attempt in (1, 2):  # one retry on an error or missing rows
        record["attempts"] = attempt
        try:
            pending = [r for r in rows if r["unit_id"] not in verified]
            got_rows, rec = backend.call(pending)
            cost_known = cost_known and rec.get("cost_usd") is not None
            for k in spent:
                spent[k] += rec.get(k) or 0
            record.update(rec, **spent)
            import jsonschema
            from .check_outputs import quote_found
            sources = {r["unit_id"]: r["excerpt"] + " " + " ".join(json.loads(r.get("mentions") or "[]")) for r in rows}
            for o in got_rows:
                if isinstance(o, dict) and isinstance(o.get("unit_id"), str) and o["unit_id"] in wanted:
                    try:
                        jsonschema.validate(o, ROW_SCHEMA)
                    except jsonschema.ValidationError:
                        continue
                    if not all(quote_found(q, sources[o["unit_id"]]) for q in o["evidence"]):
                        continue
                    verified.setdefault(o["unit_id"], o)
            out = [verified[r["unit_id"]] for r in rows if r["unit_id"] in verified]
            missing = wanted - set(verified)
            record["error"] = f"missing or invalid unit_id in output: {len(missing)} of {len(wanted)}" if missing else ""
            if not missing:
                break
        except Exception as e:  # recorded; throttling is retried inside boto3
            cost_known = False
            record["error"] = f"{type(e).__name__}: {e}"[:300]
    record.update(latency_s=round(time.perf_counter() - start, 3), rows_out=len(out),
                  cost_usd=round(spent["cost_usd"], 6) if cost_known else None)
    return record, out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", help="inputs.csv from make_inputs.py")
    ap.add_argument("out_dir")
    ap.add_argument("--model", required=True, help="Bedrock model or inference profile id (or the server's model name)")
    ap.add_argument("--backend", choices=sorted(BACKENDS), default="bedrock")
    ap.add_argument("--region", help="AWS region for Bedrock (default: your AWS config)")
    ap.add_argument("--base-url", help="--backend openai: server URL, e.g. http://localhost:8000/v1")
    ap.add_argument("--service-tier", default="default", choices=["default", "flex", "priority"], help="bedrock-chat only")
    ap.add_argument("--reasoning-effort", choices=["low", "medium", "high"], help="bedrock-chat/openai only")
    ap.add_argument("--per-file", action="store_true", help="one row per call (whole documents)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-tokens", type=int, default=16000)
    ap.add_argument("--batch-rows", type=int, default=4, help="maximum input rows per request (default 4)")
    ap.add_argument("--batch-chars", type=int, default=24000, help="maximum serialized input characters per request (default 24000)")
    ap.add_argument("--limit", type=int, help="only the first N batches (pilot / cost estimate)")
    args = ap.parse_args(argv)
    if args.backend == "openai" and not args.base_url:
        ap.error("--backend openai needs --base-url")
    if args.backend != "openai" and not args.region:
        import boto3
        args.region = boto3.Session().region_name
        if not args.region:
            ap.error("--region is required (no default region in your AWS config)")

    if args.workers < 1 or args.max_tokens < 1 or args.batch_rows < 1 or args.batch_chars < 1 or (args.limit is not None and args.limit < 1):
        ap.error("workers, max-tokens, batch-rows, batch-chars and limit must be positive")
    csv.field_size_limit(sys.maxsize)
    rows = list(csv.DictReader(open(args.inputs, newline="")))
    if not rows:
        ap.error("input table has no rows")
    try:
        # Reserve roughly 600 output tokens per summary; exact model tokenization varies.
        row_budget = min(args.batch_rows, max(1, args.max_tokens // 600))
        work = batches(rows, args.per_file, row_budget, args.batch_chars)[: args.limit]
    except ValueError as exc:
        ap.error(str(exc))
    backend = BACKENDS[args.backend](args)
    started, t0 = datetime.now(timezone.utc), time.perf_counter()
    first = [call(backend, args, 0, *work[0])] if work else []  # settle the Converse mode before going parallel
    with ThreadPoolExecutor(args.workers) as pool:
        results = first + list(pool.map(lambda w: call(backend, args, *w), [(i, k, r) for i, (k, r) in enumerate(work)][1:]))
    wall = time.perf_counter() - t0

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "outputs.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for _, outs in results:
            for o in outs:
                w.writerow({c: json.dumps(o.get(c, []), ensure_ascii=False) if c in LIST_COLUMNS else o.get(c, "")
                            for c in OUT_COLUMNS})
    calls = [rec for rec, _ in results]
    with open(out / "calls.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CALL_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(calls)
    lat = [c["latency_s"] for c in calls]
    def total(f):
        return sum(c.get(f) or 0 for c in calls)
    summary = {"started": started.isoformat(timespec="seconds"), "model": args.model, "backend": args.backend,
               "region": args.region, "input_rows": sum(len(r) for _, r in work),
               "output_rows": sum(len(o) for _, o in results), "calls": len(calls),
               "calls_ok": sum(1 for c in calls if not c["error"]), "input_tokens": total("input_tokens"),
               "output_tokens": total("output_tokens"), "estimated_cost_usd": round(total("cost_usd"), 4) if all(c.get("cost_usd") is not None for c in calls) else None,
               "latency_s_per_call": {"median": round(statistics.median(lat), 2), "max": round(max(lat), 2)} if lat else None,
               "wall_clock_s": round(wall, 1)}
    (out / "run.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))
    if any(c["error"] for c in calls):
        sys.exit(1)


if __name__ == "__main__":
    main()
