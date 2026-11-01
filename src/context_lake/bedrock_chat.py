#!/usr/bin/env python3
"""One structured call on Amazon Bedrock's OpenAI-compatible Chat Completions endpoint
(SigV4-signed with your own AWS credentials), with a strict json_schema response format.

Use it for models that answer better here than through Converse tool use, or to set a
service tier (flex is estimated at half rate) or reasoning effort. Also works against any
OpenAI-compatible server with --base-url (no signing, no cost estimate).

Library: call_json(model, region, system, user, name, schema, ...) -> (dict or None, record).

CLI:
  python3 bedrock_chat.py --model MODEL_ID --region REGION --system system.md \
      --user user.txt --schema schema.json [--service-tier default|flex|priority]
      [--reasoning-effort low|medium|high] [--base-url URL]
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from .rates import usd

TIER_MULTIPLIER = {"default": 1.0, "flex": 0.5, "priority": 1.75}
_credentials = None


def _post(url, body, region=None):
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if region:  # sign for Bedrock
        global _credentials
        import boto3
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        if _credentials is None:
            _credentials = boto3.Session().get_credentials()
        req = AWSRequest(method="POST", url=url, data=data, headers=headers)
        SigV4Auth(_credentials.get_frozen_credentials(), "bedrock", region).add_auth(req)
        headers = dict(req.headers)
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method="POST"), timeout=600) as r:
        return json.loads(r.read())


def call_json(model, region, system, user, name, schema, max_tokens=4000, service_tier="default",
              reasoning_effort=None, base_url=None):
    """Returns (parsed object or None, record)."""
    url = (base_url.rstrip("/") + "/chat/completions") if base_url else \
        f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1/chat/completions"
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}}
    if service_tier != "default":
        body["service_tier"] = service_tier
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    start = time.perf_counter()
    record = {"model": model, "error": ""}
    reply = None
    for wait in (5, 15, 45, 120, None):
        try:
            reply = _post(url, body, None if base_url else region)
            break
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or wait is None:
                record.update(latency_s=round(time.perf_counter() - start, 3), error=f"HTTP {e.code}: {e.read()[:200]!r}")
                return None, record
        except Exception as e:
            if wait is None:
                record.update(latency_s=round(time.perf_counter() - start, 3), error=f"{type(e).__name__}: {e}"[:300])
                return None, record
        time.sleep(wait)
    choice = (reply.get("choices") or [{}])[0]
    usage = reply.get("usage") or {}
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    record.update(latency_s=round(time.perf_counter() - start, 3), stop_reason=choice.get("finish_reason"),
                  input_tokens=usage.get("prompt_tokens", 0) - cached, output_tokens=usage.get("completion_tokens", 0),
                  cache_write_tokens=0, cache_read_tokens=cached)
    record["cost_usd"] = None if base_url else usd(model, usage.get("prompt_tokens", 0), record["output_tokens"],
                                                   TIER_MULTIPLIER.get(reply.get("service_tier", service_tier), 1.0))
    text = (choice.get("message") or {}).get("content") or ""
    try:
        return json.loads(text), record
    except json.JSONDecodeError:
        record["error"] = f"no JSON object (stop_reason {choice.get('finish_reason')})"
        return None, record


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--region", help="AWS region (Bedrock); not needed with --base-url")
    ap.add_argument("--base-url", help="OpenAI-compatible server instead of Bedrock, e.g. http://localhost:8000/v1")
    ap.add_argument("--system", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--schema", required=True)
    ap.add_argument("--name", default="record")
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--service-tier", default="default", choices=sorted(TIER_MULTIPLIER))
    ap.add_argument("--reasoning-effort", choices=["low", "medium", "high"])
    a = ap.parse_args(argv)
    if not (a.region or a.base_url):
        ap.error("--region or --base-url is required")
    out, rec = call_json(a.model, a.region, open(a.system).read(), open(a.user).read(), a.name,
                         json.load(open(a.schema)), a.max_tokens, a.service_tier, a.reasoning_effort, a.base_url)
    print(json.dumps({"record": rec, "output": out}, indent=1, ensure_ascii=False))
    if out is None or rec.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
