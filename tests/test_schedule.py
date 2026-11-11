"""Offline checks for `qcl schedule`; launchctl and osascript are faked."""
import os
import plistlib
import subprocess
import sys

import pytest

from context_lake import cli
from context_lake import schedule


class FakeLaunchd:
    """Record commands; model which labels launchd has loaded."""

    def __init__(self, fail=()):
        self.calls, self.loaded, self.fail = [], set(), set(fail)

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        out, code = '', 0
        if argv[0] == 'launchctl':
            action, target = argv[1], argv[-1]
            if action in self.fail:
                return subprocess.CompletedProcess(argv, 5, '', f'{action} refused')
            label = target.rsplit('/', 1)[-1].removesuffix('.plist')
            if action == 'print':
                code = 0 if label in self.loaded else 113
                out = '\tstate = not running\n\truns = 3\n\tlast exit code = 2\n'
            elif action == 'bootstrap':
                self.loaded.add(label)
            elif action == 'bootout':
                self.loaded.discard(label)
        return subprocess.CompletedProcess(argv, code, out, '')


@pytest.fixture
def launchd(tmp_path, monkeypatch):
    fake = FakeLaunchd()
    monkeypatch.setattr(schedule, 'RUN', fake)
    monkeypatch.setattr(schedule.sys, 'platform', 'darwin')
    monkeypatch.setattr(schedule.Path, 'home', lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    return fake


def installed_plist(tmp_path, name):
    path = tmp_path / 'Library' / 'LaunchAgents' / f'com.quiltdata.qcl.{name}.plist'
    with path.open('rb') as stream:
        return path, plistlib.load(stream)


def test_add_qcl_job_writes_private_plist_and_bootstraps(launchd, tmp_path, capsys):
    code = cli.main(['schedule', 'add', 'gmail-daily', '--at', '06:30', '--at', '18:30',
                     '--env', 'AWS_PROFILE=dev', '--', 'qcl', 'gmail', 'fetch',
                     '--query', 'newer_than:2d', '--output-dir', 'out', '--overwrite'])
    assert code == 0
    path, plist = installed_plist(tmp_path, 'gmail-daily')
    assert path.stat().st_mode & 0o077 == 0
    args = plist['ProgramArguments']
    assert args[:6] == [sys.executable, '-m', 'context_lake', 'schedule', 'exec', '--name']
    assert args[args.index('--') + 1:] == [sys.executable, '-m', 'context_lake', 'gmail', 'fetch',
                                          '--query', 'newer_than:2d', '--output-dir', 'out',
                                          '--overwrite']
    assert plist['StartCalendarInterval'] == [{'Hour': 6, 'Minute': 30}, {'Hour': 18, 'Minute': 30}]
    assert plist['WorkingDirectory'] == str(tmp_path)
    assert plist['EnvironmentVariables'] == {'PATH': os.environ['PATH'], 'AWS_PROFILE': 'dev'}
    assert plist['StandardOutPath'] == str(tmp_path / 'Library/Logs/qcl/gmail-daily.log')
    assert (tmp_path / 'Library/Logs/qcl').is_dir()
    assert ['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)] in launchd.calls
    err = capsys.readouterr().err
    assert 'daily 06:30,18:30' in err and 'qcl gmail fetch --query newer_than:2d' in err


def test_program_jobs_resolve_to_absolute_paths_and_weekdays(launchd, tmp_path):
    script = tmp_path / 'job.sh'
    script.write_text('#!/bin/sh\n')
    script.chmod(0o755)
    assert schedule.main(['add', 'weekly', '--at', '7:05', '--weekdays', 'mon,fri',
                          '--', './job.sh', 'x']) == 0
    _, plist = installed_plist(tmp_path, 'weekly')
    assert plist['ProgramArguments'][-2:] == [str(script.resolve()), 'x']
    assert plist['StartCalendarInterval'] == [{'Hour': 7, 'Minute': 5, 'Weekday': 1},
                                              {'Hour': 7, 'Minute': 5, 'Weekday': 5}]
    assert schedule.describe_schedule(plist) == 'mon,fri 07:05'


def test_every_interval_and_print_installs_nothing(launchd, tmp_path, capsys):
    assert schedule.main(['add', 'often', '--every', '2h', '--print', '--', 'qcl', 'index',
                          '--package-dir', 'p']) == 0
    plist = plistlib.loads(capsys.readouterr().out.encode())
    assert plist['StartInterval'] == 7200 and schedule.describe_schedule(plist) == 'every 2h'
    assert schedule.describe_command(plist) == 'qcl index --package-dir p'
    assert launchd.calls == [] and not (tmp_path / 'Library').exists()


@pytest.mark.parametrize('argv, message', [
    (['add', 'Bad_Name', '--at', '06:00', '--', 'qcl', 'index'], 'lowercase'),
    (['add', 'j', '--', 'qcl', 'index'], 'either --at'),
    (['add', 'j', '--at', '6:00', '--every', '1h', '--', 'qcl', 'index'], 'either --at'),
    (['add', 'j', '--at', '25:00', '--', 'qcl', 'index'], 'HH:MM'),
    (['add', 'j', '--every', '30s', '--', 'qcl', 'index'], 'at least 1m'),
    (['add', 'j', '--at', '6:00', '--weekdays', 'funday', '--', 'qcl', 'index'], 'weekday'),
    (['add', 'j', '--at', '6:00', '--', 'qcl', 'schedule', 'list'], 'not a qcl command'),
    (['add', 'j', '--at', '6:00', '--', 'no-such-program-qcl'], 'not an executable'),
    (['add', 'j', '--at', '6:00'], 'after --'),
    (['add', 'j', '--at', '6:00', '--env', 'BAD', '--', 'qcl', 'index'], 'NAME=VALUE'),
])
def test_invalid_jobs_are_rejected(launchd, capsys, argv, message):
    with pytest.raises(SystemExit):
        schedule.main(argv)
    assert message in capsys.readouterr().err
    assert not any(call[1] == 'bootstrap' for call in launchd.calls)


def test_existing_job_needs_replace_which_boots_out_first(launchd, tmp_path):
    job = ['add', 'j', '--at', '06:00', '--', 'qcl', 'index']
    schedule.main(job)
    with pytest.raises(SystemExit):
        schedule.main(job)
    schedule.main([*job[:4], '--replace', *job[4:]])
    actions = [call[1] for call in launchd.calls if call[0] == 'launchctl']
    assert actions[-3:] == ['print', 'bootout', 'bootstrap']


def test_failed_bootstrap_leaves_no_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, 'RUN', FakeLaunchd(fail={'bootstrap'}))
    monkeypatch.setattr(schedule.sys, 'platform', 'darwin')
    monkeypatch.setattr(schedule.Path, 'home', lambda: tmp_path)
    with pytest.raises(SystemExit):
        schedule.main(['add', 'j', '--at', '06:00', '--', 'qcl', 'index'])
    assert not list((tmp_path / 'Library' / 'LaunchAgents').iterdir())


def test_list_run_logs_and_remove(launchd, tmp_path, capsys):
    schedule.main(['add', 'j', '--at', '06:00', '--', 'qcl', 'index', '--package-dir', 'p'])
    capsys.readouterr()
    schedule.main(['list'])
    line = capsys.readouterr().out
    assert line.startswith('j ') and 'daily 06:00' in line and 'last exit 2, 3 runs' in line
    assert 'qcl index --package-dir p' in line
    schedule.main(['run', 'j'])
    assert launchd.calls[-1] == ['launchctl', 'kickstart', f'gui/{os.getuid()}/com.quiltdata.qcl.j']
    (tmp_path / 'Library/Logs/qcl/j.log').write_text(''.join(f'{i}\n' for i in range(50)))
    schedule.main(['logs', 'j', '-n', '2'])
    assert capsys.readouterr().out == '48\n49\n'
    schedule.main(['remove', 'j'])
    assert not (tmp_path / 'Library/LaunchAgents/com.quiltdata.qcl.j.plist').exists()
    assert 'com.quiltdata.qcl.j' not in launchd.loaded
    with pytest.raises(SystemExit):
        schedule.main(['remove', 'j'])


def test_exec_brackets_output_and_notifies_on_failure(monkeypatch, capsys):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 3 if argv[0] == 'job' else 0)

    monkeypatch.setattr(schedule, 'RUN', run)
    assert schedule.main(['exec', '--name', 'j', '--', 'job', '--flag']) == 3
    out = capsys.readouterr().out
    assert 'j: start' in out and 'j: exit 3 after' in out
    assert calls[0] == ['job', '--flag'] and calls[1][0] == 'osascript'
    assert calls[1][-1] == 'j failed (exit 3). See: qcl schedule logs j'
    calls.clear()
    assert schedule.main(['exec', '--name', 'j', '--no-notify', '--', 'job']) == 3
    assert calls == [['job']]


def test_non_macos_explains_and_print_still_works(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(schedule.sys, 'platform', 'linux')
    monkeypatch.setattr(schedule.Path, 'home', lambda: tmp_path)
    with pytest.raises(SystemExit):
        schedule.main(['add', 'j', '--at', '06:00', '--', 'qcl', 'index'])
    assert 'only on macOS' in capsys.readouterr().err
    assert schedule.main(['add', 'j', '--at', '06:00', '--print', '--', 'qcl', 'index']) == 0
