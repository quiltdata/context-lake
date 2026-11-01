#!/usr/bin/env python3
"""One structured call on Amazon Bedrock Converse: the model must answer by calling one tool
whose input matches a JSON schema. Default backend for the context-lake skill.

Library: call_tool(model, region, system, messages, name, schema) -> (dict or None, record).
The record has tokens, latency, an estimated cost from public list prices (cache tokens
counted at the input rate, an upper bound), stop reason and error.

If a model rejects a forced tool choice, the tool is offered with toolChoice=auto instead.
Credentials come from the standard AWS chain (env, profile, SSO, instance role); nothing is
hard-coded.

CLI (one call, prints the record and the parsed object as JSON):
  python3 bedrock_tool.py --model MODEL_ID --region REGION --system system.md \
      --user user.txt --schema schema.json [--name record] [--max-tokens N]
"""
import argparse
import json
import sys
import time

from .rates import usd

_clients = {}


def client(region):
    import boto3
    from botocore.config import Config
    if region not in _clients:
        _clients[region] = boto3.client("bedrock-runtime", region_name=region, config=Config(
            read_timeout=600, connect_timeout=10, retries={"max_attempts": 8, "mode": "adaptive"}))
    return _clients[region]


def call_tool(model, region, system, messages, name, schema, max_tokens=4000):
    """Returns (tool input dict or None, record)."""
    start = time.perf_counter()
    record = {"model": model, "error": ""}
    tools = [{"toolSpec": {"name": name, "inputSchema": {"json": schema}}}]
    request = dict(modelId=model, system=[{"text": system}], messages=messages, inferenceConfig={"maxTokens": max_tokens},
                   toolConfig={"tools": tools, "toolChoice": {"tool": {"name": name}}})
    try:
        c = client(region)
        try:
            resp = c.converse(**request)
        except c.exceptions.ValidationException as e:
            if "tool" not in str(e).lower():
                raise
            request["toolConfig"]["toolChoice"] = {"auto": {}}
            request["system"] = [{"text": system + f"\n\nAnswer only by calling the `{name}` tool."}]
            resp = c.converse(**request)
    except Exception as e:  # throttling is retried by botocore; anything left is recorded
        record.update(latency_s=round(time.perf_counter() - start, 3), error=f"{type(e).__name__}: {e}"[:300])
        return None, record
    u = resp.get("usage", {})
    record.update(latency_s=round(time.perf_counter() - start, 3), stop_reason=resp.get("stopReason"),
                  input_tokens=u.get("inputTokens", 0), output_tokens=u.get("outputTokens", 0),
                  cache_write_tokens=u.get("cacheWriteInputTokens", 0), cache_read_tokens=u.get("cacheReadInputTokens", 0))
    prompt = record["input_tokens"] + record["cache_write_tokens"] + record["cache_read_tokens"]
    record["cost_usd"] = usd(model, prompt, record["output_tokens"])
    for block in resp["output"]["message"]["content"]:
        if "toolUse" in block:
            return block["toolUse"]["input"], record
    record["error"] = f"no tool call (stop_reason {resp.get('stopReason')})"
    return None, record


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="Bedrock model or inference profile id")
    ap.add_argument("--region", required=True, help="AWS region with Bedrock access to the model")
    ap.add_argument("--system", required=True, help="file with the system prompt")
    ap.add_argument("--user", required=True, help="file with the user message")
    ap.add_argument("--schema", required=True, help="JSON schema file for the tool input")
    ap.add_argument("--name", default="record")
    ap.add_argument("--max-tokens", type=int, default=4000)
    a = ap.parse_args(argv)
    out, rec = call_tool(a.model, a.region, open(a.system).read(),
                         [{"role": "user", "content": [{"text": open(a.user).read()}]}],
                         a.name, json.load(open(a.schema)), a.max_tokens)
    print(json.dumps({"record": rec, "output": out}, indent=1, ensure_ascii=False))
    if out is None or rec.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
