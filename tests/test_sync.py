"""Deterministic reconciliation, source safety and snapshot recovery contracts."""
import json

import pytest

from context_lake import sync
from context_lake.cli import main


def test_plan_is_stable_and_read_only_then_apply_is_idempotent(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'b.md').write_text('Second document')
    (source / 'a.md').write_text('First document')
    output = tmp_path / 'lake'
    first = sync.plan(source, output, 'example')
    assert first == sync.plan(source, output, 'example')
    assert not output.exists()
    assert first[1]['added'] == ['a.md', 'b.md']
    applied = sync.apply(source, output, 'example')
    assert applied == first[1]
    pointer = (output / 'CURRENT.json').read_bytes()
    before = sorted(p.relative_to(output).as_posix() for p in output.rglob('*'))
    again = sync.apply(source, output, 'example')
    assert again['revision'] == applied['revision']
    assert again['added'] == again['changed'] == again['removed'] == []
    assert (output / 'CURRENT.json').read_bytes() == pointer
    assert sorted(p.relative_to(output).as_posix() for p in output.rglob('*')) == before
    assert (source / 'a.md').read_text() == 'First document'


def test_add_edit_delete_keeps_previous_snapshot(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'first.txt').write_text('original')
    (source / 'removed.txt').write_text('retained in history')
    output = tmp_path / 'lake'
    old = sync.apply(source, output, 'example')
    (source / 'first.txt').write_text('edited')
    (source / 'removed.txt').unlink()
    (source / 'new.txt').write_text('added')
    changed = sync.apply(source, output, 'example')
    assert changed['changed'] == ['first.txt'] and changed['removed'] == ['removed.txt']
    assert changed['added'] == ['new.txt']
    assert (output / 'revisions' / old['revision'] / 'files/removed.txt').read_text() == 'retained in history'
    latest = output / 'revisions' / changed['revision']
    assert not (latest / 'files/removed.txt').exists()
    assert (latest / 'files/first.txt').read_text() == 'edited'


@pytest.mark.parametrize('case', ['inside', 'outside', 'same', 'symlink', 'foreign'])
def test_rejects_unsafe_destinations_and_sources(tmp_path, case):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'file').write_text('source')
    output = tmp_path / 'output'
    if case == 'inside':
        output = source / 'nested'
    elif case == 'outside':
        output = tmp_path
    elif case == 'same':
        output = source
    elif case == 'symlink':
        (source / 'link').symlink_to(source / 'file')
    else:
        output.mkdir()
        (output / 'personal.txt').write_text('do not overwrite')
    with pytest.raises(sync.SyncError):
        sync.apply(source, output, 'example')
    assert (source / 'file').read_text() == 'source'


def test_copy_failure_never_promotes_incomplete_revision(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'file').write_text('original')
    output = tmp_path / 'lake'
    sync.apply(source, output, 'example')
    pointer = (output / 'CURRENT.json').read_bytes()
    (source / 'file').write_text('updated')
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(sync.shutil, 'copyfile', fail)
    with pytest.raises(OSError):
        sync.apply(source, output, 'example')
    assert (output / 'CURRENT.json').read_bytes() == pointer
    assert not list((output / 'revisions').glob('.pending-*'))


def test_source_change_during_copy_does_not_promote(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'file').write_text('original')
    output = tmp_path / 'lake'
    original = sync.shutil.copyfile
    def changed(origin, target):
        original(origin, target)
        target.write_text('raced contents')
    monkeypatch.setattr(sync.shutil, 'copyfile', changed)
    with pytest.raises(sync.SyncError, match='changed while copying'):
        sync.apply(source, output, 'example')
    assert not (output / 'CURRENT.json').exists()


def test_source_identity_and_tampered_revision_are_rejected(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'file').write_text('original')
    output = tmp_path / 'lake'
    result = sync.apply(source, output, 'example')
    with pytest.raises(sync.SyncError, match='different source'):
        sync.plan(source, output, 'other')
    path = output / 'revisions' / result['revision'] / 'manifest.json'
    path.write_text('{}')
    with pytest.raises(sync.SyncError, match='hash mismatch'):
        sync.plan(source, output, 'example')


def test_cli_defaults_to_plan_and_requires_apply(tmp_path, capsys):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'file').write_text('original')
    output = tmp_path / 'lake'
    argv = ['sync', str(source), str(output), '--source-id', 'example']
    assert main(argv) == 0 and not output.exists()
    assert json.loads(capsys.readouterr().out)['added'] == ['file']
    assert main(argv + ['--apply']) == 0 and (output / 'CURRENT.json').is_file()



def test_initial_copy_failure_can_be_retried(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'file').write_text('original')
    output = tmp_path / 'lake'
    original = sync.shutil.copyfile
    def fail(*args):
        raise OSError('temporary failure')
    monkeypatch.setattr(sync.shutil, 'copyfile', fail)
    with pytest.raises(OSError):
        sync.apply(source, output, 'example')
    monkeypatch.setattr(sync.shutil, 'copyfile', original)
    assert sync.apply(source, output, 'example')['added'] == ['file']
    assert (output / 'CURRENT.json').is_file()


def test_reusing_revision_rejects_unmanifested_content(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'file').write_text('original')
    output = tmp_path / 'lake'
    result = sync.apply(source, output, 'example')
    unlisted = output / 'revisions' / result['revision'] / 'files/untracked.txt'
    unlisted.write_text('unmanifested')
    with pytest.raises(sync.SyncError, match='unexpected'):
        sync.apply(source, output, 'example')
