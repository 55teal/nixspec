"""Textual-free helpers for Nixspec: CLI parsing, FastFlags view logic
(search / filter / sort / reset) and the Sober launcher.

Everything here is pure or injectable so it can be unit-tested without a
terminal UI. nixspec.py is deliberately not imported.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

USAGE = """usage: nixspec [--config PATH] [--show-path] [--version] [--help]

  --config PATH   edit this Sober config.json instead of the default
                  (also settable with $NIXSPEC_CONFIG)
  --show-path     print which config file would be edited, then exit
  --version       print the version, then exit
  -h, --help      show this help"""


class CliError(ValueError):
    pass


@dataclass
class CliArgs:
    config: Path
    source: str
    show_path: bool = False
    show_version: bool = False
    show_help: bool = False


def parse_cli(argv, environ, default):
    """Strict argument parsing.

    The old resolver silently ignored `--config` with no value (so the real
    config was edited by accident) and ignored unknown arguments. Both are
    now errors.
    """
    args = list(argv)
    config, source = None, None
    show_path = show_version = show_help = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--config":
            if i + 1 >= len(args) or not args[i + 1].strip() or args[i + 1].startswith("--"):
                raise CliError("--config needs a file path")
            config, source = Path(args[i + 1]).expanduser(), "--config"
            i += 2
            continue
        if arg.startswith("--config="):
            value = arg.split("=", 1)[1]
            if not value.strip():
                raise CliError("--config= needs a file path")
            config, source = Path(value).expanduser(), "--config"
        elif arg == "--show-path":
            show_path = True
        elif arg == "--version":
            show_version = True
        elif arg in ("-h", "--help"):
            show_help = True
        else:
            raise CliError(f"unknown argument {arg!r}")
        i += 1
    if config is None:
        env = (environ.get("NIXSPEC_CONFIG") or "").strip()
        if env:
            config, source = Path(env).expanduser(), "$NIXSPEC_CONFIG"
        else:
            config, source = Path(default), "default"
    return CliArgs(config, source, show_path, show_version, show_help)


# --------------------------------------------------------------------------
# FastFlags view: type, status, search, filter, sort, reset
# --------------------------------------------------------------------------

TYPE_FILTERS = ("all", "string", "number", "boolean")
SORT_MODES = ("name", "name-desc", "value", "value-desc")


def flag_type(value):
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "other"


def _same(a, b):
    """Type-aware equality: True != 1 and "240" != 240."""
    return type(a) is type(b) and a == b


def flag_status(name, staged, baseline):
    """'added' / 'changed' / 'removed' / 'same' relative to the baseline."""
    in_s, in_b = name in staged, name in baseline
    if in_s and not in_b:
        return "added"
    if in_b and not in_s:
        return "removed"
    return "same" if _same(staged[name], baseline[name]) else "changed"


@dataclass(frozen=True)
class FlagRow:
    name: str
    value: object
    status: str
    type: str


def _value_text(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).lower()


def matches_query(name, value, query):
    """Every whitespace-separated term must appear in the name or the value
    (case-insensitive). An empty query matches everything."""
    terms = query.lower().split()
    if not terms:
        return True
    haystack = (name.lower(), _value_text(value))
    return all(any(term in part for part in haystack) for term in terms)


def _value_key(value):
    if isinstance(value, bool):
        return (0, float(value), "")
    if isinstance(value, (int, float)):
        try:
            return (1, float(value), "")
        except OverflowError:
            return (1, float("inf") if value > 0 else float("-inf"), "")
    return (2, 0.0, str(value).lower())


def view_rows(staged, baseline, query="", type_filter="all",
              modified_only=False, sort="name"):
    """The rows the FastFlags table should show. Pure; mutates nothing.

    Flags pending removal are only listed when `modified_only` is on (they are
    no longer in `staged`); that is where they can be restored.
    """
    if type_filter not in TYPE_FILTERS:
        raise ValueError(f"unknown type filter {type_filter!r}")
    if sort not in SORT_MODES:
        raise ValueError(f"unknown sort mode {sort!r}")
    names = set(staged)
    if modified_only:
        names |= set(baseline)
    rows = []
    for name in names:
        status = flag_status(name, staged, baseline)
        if modified_only and status == "same":
            continue
        value = staged[name] if name in staged else baseline[name]
        ftype = flag_type(value)
        if type_filter != "all" and ftype != type_filter:
            continue
        if not matches_query(name, value, query):
            continue
        rows.append(FlagRow(name, value, status, ftype))
    desc = sort.endswith("-desc")
    rows.sort(key=lambda r: (r.name.lower(), r.name))  # stable base order
    if sort.startswith("value"):
        rows.sort(key=lambda r: _value_key(r.value), reverse=desc)
    elif desc:
        rows.reverse()
    return rows


def reset_flag(staged, baseline, name):
    """Return (new_staged, action). `staged` is never mutated.

    action: 'reverted'  - changed flag set back to its baseline value
            'discarded' - newly added flag dropped
            'restored'  - flag pending removal brought back
            'unchanged' - already equal to the baseline
            'unknown'   - not a known flag
    """
    new = dict(staged)
    if name not in staged and name not in baseline:
        return new, "unknown"
    status = flag_status(name, staged, baseline)
    if status == "same":
        return new, "unchanged"
    if status == "added":
        del new[name]
        return new, "discarded"
    new[name] = baseline[name]
    return new, "restored" if status == "removed" else "reverted"


def prune_broken(path, keep=10):
    """Delete all but the newest `keep` `<config>.broken-*.bak` files, which
    restore_backup() creates and nothing else ever cleaned up."""
    path = Path(path)
    found = sorted(path.parent.glob(path.name + ".broken-*.bak"), reverse=True)
    removed = []
    for old in found[max(keep, 1):]:
        try:
            old.unlink()
            removed.append(old)
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------
# Launching Sober (Flatpak: org.vinegarhq.Sober)
# --------------------------------------------------------------------------

SOBER_APP_ID = "org.vinegarhq.Sober"
LAUNCH_CHECK_SECONDS = 2.0   # watch this long for an immediate crash
LAUNCH_COOLDOWN = 3.0        # ignore repeat presses for this long


@dataclass
class LaunchResult:
    ok: bool
    message: str
    pid: int | None = None


class LaunchGuard:
    """Blocks a second launch while one is running and for a short cooldown."""

    def __init__(self, cooldown=LAUNCH_COOLDOWN, clock=time.monotonic):
        self.cooldown, self.clock = cooldown, clock
        self.busy, self._until = False, 0.0

    def try_begin(self):
        if self.busy or self.clock() < self._until:
            return False
        self.busy = True
        return True

    def finish(self):
        self.busy = False
        self._until = self.clock() + self.cooldown


def sober_install_problem(run=subprocess.run, which=shutil.which):
    """'' if Sober is installed, else a message saying what is missing."""
    flatpak = which("flatpak")
    if not flatpak:
        return ("Flatpak was not found in PATH. Install it (Arch: sudo pacman "
                f"-S flatpak), then: flatpak install flathub {SOBER_APP_ID}")
    try:
        res = run([flatpak, "info", SOBER_APP_ID], capture_output=True,
                  text=True, timeout=10, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return "'flatpak info' timed out; Flatpak may be busy. Try again."
    except OSError as exc:
        return f"Could not run flatpak: {exc}"
    if res.returncode != 0:
        return (f"Sober ({SOBER_APP_ID}) is not installed. Install it with: "
                f"flatpak install flathub {SOBER_APP_ID}")
    return ""


def launch_sober(run=subprocess.run, popen=subprocess.Popen,
                 which=shutil.which, check_seconds=LAUNCH_CHECK_SECONDS):
    """Blocking; call from a worker thread. Argument list, no shell, no root."""
    problem = sober_install_problem(run, which)
    if problem:
        return LaunchResult(False, problem)
    flatpak = which("flatpak")
    err = tempfile.TemporaryFile()  # a file: a chatty child can't fill a pipe
    try:
        try:
            proc = popen(
                [flatpak, "run", SOBER_APP_ID],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=err, start_new_session=True,
            )
        except OSError as exc:
            return LaunchResult(False, f"Could not start Sober: {exc}")
        try:
            code = proc.wait(timeout=check_seconds)
        except subprocess.TimeoutExpired:
            threading.Thread(target=proc.wait, daemon=True).start()  # reap
            return LaunchResult(True, "Sober was launched.", proc.pid)
        err.seek(0)
        tail = err.read().decode("utf-8", "replace").strip()[-400:]
        if code == 0:
            return LaunchResult(True, "Sober started and handed off to its own process.", proc.pid)
        return LaunchResult(False, f"Sober exited immediately (code {code}). {tail or 'No error output.'}")
    finally:
        err.close()
