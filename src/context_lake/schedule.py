"""Install, list and remove scheduled jobs as macOS launchd agents.

  qcl schedule add gmail-daily --at 06:30 -- qcl gmail fetch --query newer_than:2d \\
      --output-dir ~/context/gmail --overwrite
  qcl schedule add sync-hourly --every 1h -- ./sync.sh
  qcl schedule list
  qcl schedule run gmail-daily       start it now, as launchd would
  qcl schedule logs gmail-daily
  qcl schedule remove gmail-daily

The job command follows `--`. `qcl ...` runs with the Python that installed this
qcl; any other program is resolved to an absolute path. Jobs run in the directory
`add` was called from, with the PATH it saw, and append timestamped output to
~/Library/Logs/qcl/NAME.log. A failed run raises a macOS notification.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LABEL_PREFIX = 'com.quiltdata.qcl.'
NAME_RE = re.compile(r'[a-z0-9][a-z0-9-]{0,62}')
ENV_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
WEEKDAYS = {'sun': 0, 'mon': 1, 'tue': 2, 'wed': 3, 'thu': 4, 'fri': 5, 'sat': 6}
UNITS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
QCL = ['-m', 'context_lake']
RUN = subprocess.run  # replaced in tests


class ScheduleError(ValueError):
    """A job definition or launchd operation was rejected."""


# --- Locations -----------------------------------------------------------------------------

def agents_dir():
    return Path.home() / 'Library' / 'LaunchAgents'


def logs_dir():
    return Path.home() / 'Library' / 'Logs' / 'qcl'


def job_name(value):
    if not NAME_RE.fullmatch(value or ''):
        raise ScheduleError(f'job name {value!r} must be lowercase letters, digits and hyphens')
    return value


def label(name):
    return LABEL_PREFIX + job_name(name)


def plist_path(name):
    return agents_dir() / f'{label(name)}.plist'


def log_path(name):
    return logs_dir() / f'{job_name(name)}.log'


# --- Parsing -------------------------------------------------------------------------------

def parse_time(value):
    match = re.fullmatch(r'([01]?\d|2[0-3]):([0-5]\d)', value)
    if not match:
        raise ScheduleError(f'--at {value!r} must be HH:MM in 24-hour local time')
    return int(match[1]), int(match[2])


def parse_weekdays(value):
    days = []
    for item in value.lower().split(','):
        key = item.strip()[:3]
        if key not in WEEKDAYS or not item.strip():
            raise ScheduleError(f'unknown weekday {item.strip()!r}; use mon,tue,wed,thu,fri,sat,sun')
        days.append(WEEKDAYS[key])
    return sorted(set(days))


def parse_interval(value):
    match = re.fullmatch(r'(\d+)([smhd])', value)
    if not match:
        raise ScheduleError(f'--every {value!r} must look like 30m, 2h or 1d')
    seconds = int(match[1]) * UNITS[match[2]]
    if seconds < 60:
        raise ScheduleError('--every must be at least 1m')
    return seconds


def parse_env(items):
    env = {'PATH': os.environ.get('PATH') or '/usr/bin:/bin:/usr/sbin:/sbin'}
    for item in items:
        key, sep, value = item.partition('=')
        if not sep or not ENV_RE.fullmatch(key):
            raise ScheduleError(f'--env {item!r} must be NAME=VALUE')
        env[key] = value
    return env


def resolve_command(argv):
    """Return the absolute argv launchd will run for a `qcl ...` or program command."""
    from .cli import COMMANDS
    if not argv:
        raise ScheduleError('give the job command after --, e.g. -- qcl gmail fetch ...')
    if argv[0] == 'qcl':
        if len(argv) < 2 or argv[1] not in COMMANDS or argv[1] == 'schedule':
            raise ScheduleError(f'{shlex.join(argv[:2])} is not a qcl command that can be scheduled')
        return [sys.executable, *QCL, *argv[1:]]
    program = argv[0]
    path = Path(program).expanduser() if os.sep in program else shutil.which(program)
    if path is None or not Path(path).is_file() or not os.access(path, os.X_OK):
        raise ScheduleError(f'{program!r} is not an executable program')
    return [str(Path(path).resolve()), *argv[1:]]


# --- Plist ---------------------------------------------------------------------------------

def build_plist(name, command, *, times=(), weekdays=(), every=None, env=None, notify=True,
                cwd=None):
    wrapper = [sys.executable, *QCL, 'schedule', 'exec', '--name', job_name(name)]
    if not notify:
        wrapper.append('--no-notify')
    log = str(log_path(name))
    plist = {'Label': label(name), 'ProgramArguments': [*wrapper, '--', *command],
             'WorkingDirectory': str(cwd or Path.cwd()), 'StandardOutPath': log,
             'StandardErrorPath': log, 'ProcessType': 'Background', 'RunAtLoad': False,
             'EnvironmentVariables': dict(env or {})}
    if every:
        plist['StartInterval'] = every
    else:
        slots = [{'Hour': h, 'Minute': m, **({'Weekday': d} if d is not None else {})}
                 for d in (weekdays or [None]) for h, m in times]
        plist['StartCalendarInterval'] = slots[0] if len(slots) == 1 else slots
    return plist


def describe_schedule(plist):
    if 'StartInterval' in plist:
        seconds = plist['StartInterval']
        for unit in 'dhm':
            if seconds % UNITS[unit] == 0:
                return f'every {seconds // UNITS[unit]}{unit}'
        return f'every {seconds}s'
    slots = plist.get('StartCalendarInterval') or []
    slots = [slots] if isinstance(slots, dict) else slots
    times = sorted({f'{s.get("Hour", 0):02d}:{s.get("Minute", 0):02d}' for s in slots})
    names = {v: k for k, v in WEEKDAYS.items()}
    days = sorted({s['Weekday'] % 7 for s in slots if 'Weekday' in s})
    prefix = ','.join(names[d] for d in days) if days else 'daily'
    return f'{prefix} {",".join(times)}'


def describe_command(plist, width=None):
    argv = plist.get('ProgramArguments', [])
    command = argv[argv.index('--') + 1:] if '--' in argv else argv
    if command[1:3] == QCL:
        command = ['qcl', *command[3:]]
    text = shlex.join(command)
    return text if width is None or len(text) <= width else text[:width - 1] + '…'


def write_private(path, data):
    """Atomically write user-only bytes (the plist may carry --env secrets)."""
    temporary = path.with_name(f'.{path.name}.{secrets.token_hex(4)}.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


# --- launchd -------------------------------------------------------------------------------

def require_macos():
    if sys.platform != 'darwin':
        raise ScheduleError('qcl schedule installs launchd agents, which exist only on macOS; '
                            'use `add --print` to see the job, or cron/systemd elsewhere')


def domain():
    return f'gui/{os.getuid()}'


def launchctl(*args, check=True):
    result = RUN(['launchctl', *args], capture_output=True, text=True)
    if check and result.returncode:
        detail = (result.stderr or result.stdout or '').strip()
        raise ScheduleError(f'launchctl {args[0]} failed: {detail or result.returncode}')
    return result


def status(name):
    """Return (loaded, last exit code or None, run count or None) from launchctl."""
    result = launchctl('print', f'{domain()}/{label(name)}', check=False)
    if result.returncode:
        return False, None, None
    exit_code = re.search(r'last exit code = (\S+)', result.stdout)
    runs = re.search(r'\bruns = (\d+)', result.stdout)
    return True, exit_code and exit_code[1], runs and int(runs[1])


def installed():
    return sorted(agents_dir().glob(f'{LABEL_PREFIX}*.plist'))


def load_job(name):
    path = plist_path(name)
    if not path.is_file():
        raise ScheduleError(f'no scheduled job named {name!r}; see `qcl schedule list`')
    with path.open('rb') as stream:
        return path, plistlib.load(stream)


# --- Commands ------------------------------------------------------------------------------

def add(args, command):
    name = job_name(args.name)
    if bool(args.at) == bool(args.every):
        raise ScheduleError('give either --at HH:MM (repeatable) or --every INTERVAL')
    if args.weekdays and args.every:
        raise ScheduleError('--weekdays applies only with --at')
    plist = build_plist(
        name, resolve_command(command), times=[parse_time(t) for t in args.at or []],
        weekdays=parse_weekdays(args.weekdays) if args.weekdays else (),
        every=parse_interval(args.every) if args.every else None,
        env=parse_env(args.env), notify=not args.no_notify)
    data = plistlib.dumps(plist)
    if args.print:
        sys.stdout.write(data.decode())
        return 0
    require_macos()
    path = plist_path(name)
    if path.is_symlink() or (path.exists() and not args.replace):
        raise ScheduleError(f'job {name!r} already exists ({path}); use --replace')
    log_path(name).parent.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    if status(name)[0]:
        launchctl('bootout', f'{domain()}/{label(name)}')
    write_private(path, data)
    try:
        launchctl('bootstrap', domain(), str(path))
    except ScheduleError:
        path.unlink(missing_ok=True)
        raise
    when = describe_schedule(plist) + ('' if args.every else ' (local time)')
    print(f'Scheduled {name}: {when}\n'
          f'  command: {describe_command(plist)}\n'
          f'  runs in: {plist["WorkingDirectory"]}\n'
          f'  logs:    {log_path(name)}\n'
          f'Test it now with: qcl schedule run {name}', file=sys.stderr)
    return 0


def list_jobs(_args):
    paths = installed()
    if not paths:
        print('No qcl scheduled jobs. Add one with `qcl schedule add NAME --at HH:MM -- COMMAND`.',
              file=sys.stderr)
        return 0
    for path in paths:
        name = path.name[len(LABEL_PREFIX):-len('.plist')]
        with path.open('rb') as stream:
            plist = plistlib.load(stream)
        loaded, last, runs = status(name) if sys.platform == 'darwin' else (False, None, None)
        state = 'not loaded' if not loaded else (
            'never run' if not runs else f'last exit {last}, {runs} runs')
        print(f'{name:<20} {describe_schedule(plist):<18} {state:<26} {describe_command(plist, 60)}')
    return 0


def remove(args):
    require_macos()
    path, _ = load_job(args.name)
    if status(args.name)[0]:
        launchctl('bootout', f'{domain()}/{label(args.name)}')
    path.unlink()
    print(f'Removed {args.name}; its log remains at {log_path(args.name)}', file=sys.stderr)
    return 0


def run_now(args):
    require_macos()
    load_job(args.name)
    launchctl('kickstart', f'{domain()}/{label(args.name)}')
    print(f'Started {args.name}; follow it with: tail -f {shlex.quote(str(log_path(args.name)))}',
          file=sys.stderr)
    return 0


def logs(args):
    path = log_path(args.name)
    if not path.exists():
        raise ScheduleError(f'no log yet for {args.name!r} ({path})')
    with path.open(encoding='utf-8', errors='replace') as stream:
        sys.stdout.writelines(stream.readlines()[-args.lines:])
    return 0


def notify_failure(name, code):
    message = f'{name} failed (exit {code}). See: qcl schedule logs {name}'
    script = ['-e', 'on run argv', '-e',
              'display notification (item 1 of argv) with title "qcl schedule"', '-e', 'end run']
    try:
        RUN(['osascript', *script, message], capture_output=True, check=False)
    except OSError:
        pass


def execute(name, command, *, notify=True):
    """Run a job under launchd: bracket its output with timestamps and report failure."""
    started = time.monotonic()
    stamp = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')
    print(f'=== {stamp} {name}: start', flush=True)
    try:
        code = RUN(command).returncode
    except OSError as error:
        print(f'cannot start {command[0]}: {error}', file=sys.stderr, flush=True)
        code = 127
    stamp = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')
    print(f'=== {stamp} {name}: exit {code} after {time.monotonic() - started:.0f}s', flush=True)
    if code and notify:
        notify_failure(name, code)
    return code


def build_parser():
    parser = argparse.ArgumentParser(prog='qcl schedule', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='action', required=True)
    new = commands.add_parser('add', help='install a job: add NAME (--at HH:MM | --every N) -- COMMAND',
                              usage='qcl schedule add NAME (--at HH:MM ... | --every INTERVAL) '
                                    '[options] -- COMMAND ...')
    new.add_argument('name', help='job name: lowercase letters, digits and hyphens')
    new.add_argument('--at', action='append', metavar='HH:MM',
                     help='local time to run; repeat for several times a day')
    new.add_argument('--weekdays', metavar='DAYS', help='limit --at to days, e.g. mon,wed,fri')
    new.add_argument('--every', metavar='INTERVAL', help='run every 30m, 2h, 1d, ...')
    new.add_argument('--env', action='append', default=[], metavar='NAME=VALUE',
                     help='extra environment variable (PATH is captured automatically)')
    new.add_argument('--no-notify', action='store_true', help='no macOS notification on failure')
    new.add_argument('--replace', action='store_true', help='replace an existing job of this name')
    new.add_argument('--print', action='store_true', help='print the launchd plist; install nothing')
    commands.add_parser('list', help='show installed jobs, schedules and last exit codes')
    for action, text in (('remove', 'uninstall a job (keeps its log)'),
                         ('run', 'start a job now through launchd')):
        commands.add_parser(action, help=text).add_argument('name')
    tail = commands.add_parser('logs', help="print the end of a job's log")
    tail.add_argument('name')
    tail.add_argument('-n', '--lines', type=int, default=40)
    wrapper = commands.add_parser('exec', help='(used by launchd) run a job command with logging')
    wrapper.add_argument('--name', required=True)
    wrapper.add_argument('--no-notify', action='store_true')
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = []
    if '--' in argv:
        split = argv.index('--')
        argv, command = argv[:split], argv[split + 1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    if command and args.action not in ('add', 'exec'):
        parser.error(f'{args.action} takes no command after --')
    try:
        if args.action == 'add':
            return add(args, command)
        if args.action == 'exec':
            if not command:
                raise ScheduleError('exec needs a command after --')
            return execute(job_name(args.name), command, notify=not args.no_notify)
        return {'list': list_jobs, 'remove': remove, 'run': run_now, 'logs': logs}[args.action](args)
    except (ScheduleError, OSError, plistlib.InvalidFileException) as error:
        parser.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
