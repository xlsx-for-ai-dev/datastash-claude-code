"""The offline test harness for test_status.py (XLI-337; the same guards as test_plugin.py, EPM 11-20): imported before
the plugin, it gives every test a throwaway HOME, datastash folder and Claude Code config folder (never the real
~/.claude, ~/.claude.json or ~/.config), and fails a test on any real network call or on a process started outside
the throwaway folder, by it or by any process it starts."""
import os
import socket
import subprocess
import tempfile
import unittest

ROOT = os.path.realpath(tempfile.mkdtemp(prefix="ds-status-test-"))
CONF = os.path.join(ROOT, "conf")
FAKE_HOME = os.path.join(ROOT, "home")
os.makedirs(CONF, mode=0o700)
os.makedirs(FAKE_HOME, mode=0o700)
os.environ["HOME"] = FAKE_HOME
os.environ["USERPROFILE"] = FAKE_HOME
for _k, _d in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"), ("XDG_STATE_HOME", ".local/state"),
               ("XDG_CACHE_HOME", ".cache")):
    os.environ[_k] = os.path.join(FAKE_HOME, _d)
os.environ["DATASTASH_CONFIG_DIR"] = CONF
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(FAKE_HOME, ".claude")
os.environ["DATASTASH_NO_NETWORK"] = "1"
os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)
os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)

# Harness guards. A breach is recorded (and the call refused); the test that made it fails (see _run_guarded).
VIOLATIONS = []


def _under_tmp(path):
    if not isinstance(path, str):
        return False
    real = os.path.realpath(path)
    return real == ROOT or real.startswith(ROOT + os.sep)


def _breach(what):
    VIOLATIONS.append(what)
    return PermissionError("test harness: " + what)


_real_connect, _real_connect_ex = socket.socket.connect, socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _guard_connect(self, address):
    if self.family == getattr(socket, "AF_UNIX", None):
        return _real_connect(self, address)
    raise _breach(f"a real network connection to {address!r}")


def _guard_connect_ex(self, address):
    if self.family == getattr(socket, "AF_UNIX", None):
        return _real_connect_ex(self, address)
    raise _breach(f"a real network connection to {address!r}")


def _guard_getaddrinfo(host, *a, **k):
    raise _breach(f"a real name lookup of {host!r}")


socket.socket.connect = _guard_connect
socket.socket.connect_ex = _guard_connect_ex
socket.getaddrinfo = _guard_getaddrinfo
socket.create_connection = lambda address, *a, **k: (_ for _ in ()).throw(_breach(f"a real network connection to {address!r}"))

_real_popen_init = subprocess.Popen.__init__


def _guard_popen_init(self, args, *a, **k):
    env = k.get("env")
    env = os.environ if env is None else env
    argv0 = args if isinstance(args, str) else (list(args)[0] if args else "")
    if os.path.basename(str(argv0)).split()[0:1] == ["claude"]:
        raise _breach("a test started Claude Code itself")
    for name in ("HOME", "CLAUDE_CONFIG_DIR", "DATASTASH_CONFIG_DIR"):
        if not _under_tmp(env.get(name)):
            raise _breach(f"a process started with {name} outside the throwaway folder")
    if env.get("DATASTASH_NO_NETWORK") != "1":
        raise _breach("a process started without DATASTASH_NO_NETWORK=1")
    return _real_popen_init(self, args, *a, **k)


subprocess.Popen.__init__ = _guard_popen_init

_real_run_test = unittest.TestCase.run


def _run_guarded(self, result=None):
    before = len(VIOLATIONS)

    def check():
        if len(VIOLATIONS) > before:
            raise AssertionError("test harness breach: " + "; ".join(VIOLATIONS[before:]))
    self.addCleanup(check)
    return _real_run_test(self, result)


unittest.TestCase.run = _run_guarded
