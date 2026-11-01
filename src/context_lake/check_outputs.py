#!/usr/bin/env python3
"""Check a summarizer output table against its input table.

For every output row: it parses and validates against schemas/output.schema.json,
its unit_id is one of the inputs, and every evidence quote appears in that input
row's excerpt or mentions. Every input row must have exactly one output row.
Prints one line per row and exits 1 if any row fails.

Quote checks preserve case, punctuation, decimal points, signs and operators.
Only Unicode typography, whitespace and alphabetic PDF line-end hyphenation
are normalized. Quotes may not start or end inside a word or numeric value.

Usage: python3 check_outputs.py inputs.csv outputs.csv
       (outputs.csv from run_model.py; exit 1 if any row fails)
"""
import csv
import json
import re
import sys
import unicodedata
from pathlib import Path

import jsonschema

SCHEMA = json.loads((Path(__file__).resolve().parent / "schemas" / "output.schema.json").read_text())
LIST_COLUMNS = ["about", "entities", "refers_to", "evidence"]
_TYPOGRAPHIC = str.maketrans({"‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"',
                              "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
                              "​": "", "‌": "", "‍": "", "﻿": "", "­": ""})


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


def quote_norm(s):
    """Normalize typography and PDF word wrapping without changing measurements."""
    text = unicodedata.normalize("NFC", s).translate(_TYPOGRAPHIC)
    text = re.sub(r"(?<=\w)-[ \t]*\r?\n[ \t]*(?=\w)",
                  lambda match: "" if (match.string[match.start() - 1].isalpha()
                                      and match.string[match.end()].isalpha()) else match[0], text)
    return norm(text)


def quote_found(q, source):
    quote = quote_norm(q)
    if not any(char.isalnum() for char in quote):
        return False
    before = r"(?<![\w.+-])" if quote[0].isdigit() else r"(?<!\w)" if quote[0].isalnum() else ""
    after = r"(?!\w|\.\d)" if quote[-1].isdigit() else r"(?!\w)" if quote[-1].isalnum() else ""
    return re.search(before + re.escape(quote) + after, quote_norm(source)) is not None


def main(inputs_path, outputs_path):
    csv.field_size_limit(sys.maxsize)
    inputs = {r["unit_id"]: r for r in csv.DictReader(open(inputs_path, newline=""))}
    seen, failed = set(), 0
    for row in csv.DictReader(open(outputs_path, newline="")):
        uid, problems = row.get("unit_id", ""), []
        try:
            for col in LIST_COLUMNS:
                row[col] = json.loads(row[col])
            jsonschema.validate(row, SCHEMA)
        except (json.JSONDecodeError, jsonschema.ValidationError, KeyError) as e:
            problems.append(f"schema: {getattr(e, 'message', e)}")
        if uid not in inputs:
            problems.append("unit_id not in inputs")
        elif uid in seen:
            problems.append("duplicate unit_id")
        else:
            source = inputs[uid]["excerpt"] + " " + " ".join(json.loads(inputs[uid].get("mentions") or "[]"))
            for q in row["evidence"] if isinstance(row.get("evidence"), list) else []:
                if not isinstance(q, str) or not quote_found(q, source):
                    problems.append(f"quote not found: {str(q)[:60]!r}")
        seen.add(uid)
        failed += bool(problems)
        print(("FAIL  " if problems else "PASS  ") + uid + "".join(f"\n      {p}" for p in problems))
    for uid in inputs.keys() - seen:
        failed += 1
        print(f"FAIL  {uid}\n      no output row")
    print(f"\n{len(inputs) - failed if failed <= len(inputs) else 0}/{len(inputs)} input rows pass")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0 if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help") else 2)
    main(*sys.argv[1:])
