"""Reconcile source files into immutable snapshots using content hashes.

No models, network calls, credentials or source modifications. Dry run by default.
An apply writes a new revision then atomically updates CURRENT.json; old revisions
remain available. Normalized connector exports can be used as the source directory.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path


class SyncError(ValueError):
    """Unsafe or inconsistent source/destination state."""


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode()


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def current(output, source_id):
    pointer = output / 'CURRENT.json'
    identity = output / 'SOURCE.json'
    if identity.is_symlink():
        raise SyncError('SOURCE.json must not be a symlink')
    if identity.exists():
        if json.loads(identity.read_text()) != {'format': 1, 'source_id': source_id}:
            raise SyncError('output belongs to a different source or format')
    elif output.exists() and any(output.iterdir()):
        raise SyncError('nonempty output is not a managed context snapshot directory')
    if pointer.is_symlink():
        raise SyncError('CURRENT.json must not be a symlink')
    if not pointer.exists():
        return None
    state = json.loads(pointer.read_text())
    if state.get('format') != 1 or state.get('source_id') != source_id:
        raise SyncError('output belongs to a different source or format')
    revision = state.get('revision', '')
    if not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{64}', revision):
        raise SyncError('invalid current revision')
    paths = [output / 'revisions', output / 'revisions' / revision,
             output / 'revisions' / revision / 'manifest.json']
    if any(path.is_symlink() for path in paths):
        raise SyncError('revision metadata must not contain symlinks')
    manifest = json.loads(paths[-1].read_text())
    if hashlib.sha256(encoded(manifest)).hexdigest() != revision:
        raise SyncError('current manifest hash mismatch')
    if manifest.get('source_id') != source_id or manifest.get('format') != 1:
        raise SyncError('manifest belongs to a different source or format')
    return manifest


def plan(source, output, source_id):
    """Return a deterministic manifest and diff, without creating any outputs."""
    source, output = Path(source).expanduser(), Path(output).expanduser()
    if source.is_symlink() or output.is_symlink():
        raise SyncError('source/output roots must not be symlinks')
    source, output = source.resolve(), output.resolve()
    if not source.is_dir():
        raise SyncError('source must be a directory')
    if source == output or source in output.parents or output in source.parents:
        raise SyncError('source and output must be separate directory trees')
    if not isinstance(source_id, str) or not source_id.strip():
        raise SyncError('source-id must be nonempty')
    previous = current(output, source_id)
    files = []
    for path in sorted(source.rglob('*')):
        if path.is_symlink():
            raise SyncError('source must not contain symlinks')
        if path.is_dir():
            continue
        if not path.is_file():
            raise SyncError('source entries must be regular files')
        files.append({'path': path.relative_to(source).as_posix(), 'size': path.stat().st_size,
                      'sha256': digest(path)})
    manifest = {'format': 1, 'source_id': source_id, 'files': files}
    old = {entry['path']: entry for entry in previous['files']} if previous else {}
    new = {entry['path']: entry for entry in files}
    changes = {'added': sorted(new.keys() - old.keys()), 'removed': sorted(old.keys() - new.keys()),
               'changed': sorted(key for key in new.keys() & old.keys() if new[key] != old[key])}
    return manifest, {'source_id': source_id, 'revision': hashlib.sha256(encoded(manifest)).hexdigest(),
                      **changes}


def verify_revision(destination, manifest):
    # Repeated content must reuse the exact immutable revision.
    if (destination / 'manifest.json').is_symlink() or (destination / 'manifest.json').read_bytes() != encoded(manifest):
        raise SyncError('existing revision metadata differs')
    entries = list(destination.rglob('*'))
    expected = {'manifest.json'} | {'files/' + entry['path'] for entry in manifest['files']}
    actual = {path.relative_to(destination).as_posix() for path in entries if path.is_file()}
    if any(path.is_symlink() or (not path.is_file() and not path.is_dir()) for path in entries) or actual != expected:
        raise SyncError('existing revision contains unexpected or missing entries')
    for entry in manifest['files']:
        target = destination / 'files' / entry['path']
        if target.is_symlink() or any(parent.is_symlink() for parent in target.parents) or digest(target) != entry['sha256']:
            raise SyncError('existing revision content differs')


def apply(source, output, source_id):
    """Copy a validated snapshot and atomically promote its pointer."""
    manifest, changes = plan(source, output, source_id)
    source, output = Path(source).expanduser().resolve(), Path(output).expanduser().resolve()
    revisions = output / 'revisions'
    if revisions.is_symlink():
        raise SyncError('revisions must not be a symlink')
    output.mkdir(parents=True, exist_ok=True)
    identity = output / 'SOURCE.json'
    if not identity.exists():
        with identity.open('xb') as stream:
            stream.write(encoded({'format': 1, 'source_id': source_id}))
    revisions.mkdir(exist_ok=True)
    destination = revisions / changes['revision']
    if destination.is_symlink():
        raise SyncError('revision must not be a symlink')
    stage = None
    try:
        if destination.exists():
            verify_revision(destination, manifest)
        else:
            stage = Path(tempfile.mkdtemp(prefix='.pending-', dir=revisions))
            (stage / 'files').mkdir()
            for entry in manifest['files']:
                relative = entry['path']
                target = stage / 'files' / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                origin = source / relative
                if origin.is_symlink() or any(parent.is_symlink() for parent in origin.parents):
                    raise SyncError('source changed to a symlink while copying')
                shutil.copyfile(origin, target)
                if target.stat().st_size != entry['size'] or digest(target) != entry['sha256']:
                    raise SyncError('source changed while copying; rerun the plan')
            (stage / 'manifest.json').write_bytes(encoded(manifest))
            os.replace(stage, destination)
        pointer = encoded({'format': 1, 'source_id': source_id, 'revision': changes['revision']})
        current_path = output / 'CURRENT.json'
        if current_path.exists() and current_path.read_bytes() == pointer:
            return changes
        descriptor, temporary = tempfile.mkstemp(prefix='.CURRENT-', dir=output)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(pointer)
            os.replace(temporary, current_path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)
    return changes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--source-id', required=True, help='stable source identity in the manifest')
    parser.add_argument('--apply', action='store_true', help='write a new revision and advance CURRENT.json')
    args = parser.parse_args(argv)
    try:
        changes = apply(args.source, args.output, args.source_id) if args.apply else plan(args.source, args.output, args.source_id)[1]
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.error(str(error))
    print(json.dumps(changes, indent=2, sort_keys=True))
    return 0
