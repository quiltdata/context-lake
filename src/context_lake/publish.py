#!/usr/bin/env python3
"""Publish an approved CSV plan to plain S3; dry run unless --yes is supplied.

Columns: name,source,readme,message. Names contain namespace/name. source is an
S3 prefix (referenced in place) or a local export directory. Optional readme is
a local file. Relative paths resolve against the CSV directory. Each row is a
complete replacement of its named package; omitted entries disappear from the
new revision, but older revisions remain. Review the whole plan before --yes.
"""
import argparse
import csv
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


NAME = re.compile(r'[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+')


def local_path(value, base):
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def read_plan(path):
    path = Path(path).resolve()
    with path.open(newline='') as fh:
        rows = list(csv.DictReader(fh))
    if not rows or not {'name', 'source'} <= rows[0].keys():
        raise ValueError('plan needs rows with name,source columns')
    seen = set()
    for row in rows:
        for key in ('name', 'source', 'readme', 'message'):
            row[key] = (row.get(key) or '').strip()
        if not NAME.fullmatch(row['name']) or row['name'] in seen:
            raise ValueError(f'invalid or duplicate package name: {row["name"]}')
        seen.add(row['name'])
        if row['source'].startswith('s3://'):
            uri = urlsplit(row['source'])
            if not uri.netloc or uri.query or uri.fragment:
                raise ValueError('S3 sources must be bucket/prefix URIs')
        else:
            source = local_path(row['source'], path.parent)
            if not row['source'] or not source.is_dir():
                raise ValueError(f'export directory missing: {source}')
            if any(p.is_symlink() for p in [source, *source.rglob('*')]):
                raise ValueError('export directories must not contain symlinks')
            row['source'] = str(source)
        if row['readme']:
            readme = local_path(row['readme'], path.parent)
            if not readme.is_file():
                raise ValueError(f'README missing: {readme}')
            row['readme'] = str(readme)
    return rows


def build(row):
    import quilt3
    pkg = quilt3.Package()
    src = row['source']
    pkg.set_dir('/', src.rstrip('/') + '/' if src.startswith('s3://') else src)
    if row.get('readme'):
        pkg.set('README.md', row['readme'])
    return pkg


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan')
    parser.add_argument('--registry', required=True, help='s3://target-bucket')
    parser.add_argument('--workflow', default=None, help="optional target registry workflow id")
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--log', default='publication-log.jsonl')
    args = parser.parse_args(argv)
    target = urlsplit(args.registry)
    if target.scheme != 's3' or not target.netloc or target.path not in ('', '/') or target.query or target.fragment:
        parser.error('--registry must be s3://bucket')
    try:
        rows = read_plan(args.plan)  # validate all rows before writing anything
    except ValueError as exc:
        parser.error(str(exc))
    log_path = Path(args.log).expanduser().resolve()
    if log_path.exists() and log_path.stat().st_nlink > 1:
        parser.error('--log must not be a hard-linked file')
    protected_inputs = [Path(args.plan).expanduser().resolve()]
    protected_inputs.extend(Path(row['readme']).resolve() for row in rows if row.get('readme'))
    if log_path in protected_inputs:
        parser.error('--log must not replace the publication plan or a README input')
    for row in rows:
        if not row['source'].startswith('s3://'):
            source = Path(row['source']).resolve()
            if log_path == source or source in log_path.parents:
                parser.error('--log must be outside every local publication source')
    for row in rows:
        if not args.yes:
            print(f'DRY RUN  {row["name"]} <- {row["source"]} -> {args.registry}')
            continue
        import quilt3
        pkg = build(row)
        kwargs = {} if args.workflow is None else {'workflow': args.workflow}
        pushed = pkg.push(row['name'], registry=args.registry,
                          message=row['message'] or 'Publish approved context-lake plan',
                          selector_fn=quilt3.Package.selector_fn_copy_local,
                          force=True, **kwargs)
        top_hash = pushed if isinstance(pushed, str) else pushed.top_hash
        with log_path.open('a') as fh:
            fh.write(json.dumps({'name': row['name'], 'registry': args.registry,
                                 'top_hash': top_hash}) + '\n')
        print(f'PUSHED {row["name"]}@{top_hash}')


if __name__ == '__main__':
    main()
