# -*- coding: utf-8 -*-
"""Prove the core pipeline runs with no third-party packages installed.

Inserts a meta-path finder that raises ImportError for Pillow, numpy, playwright
and pyautogui, then imports the core modules and runs a dry-run. If any of them
is reachable in practice, the import fails loudly instead of being silently
satisfied by whatever this machine happens to have installed.

This is narrower than stripping sys.path: the C extension modules that ship with
CPython (`_socket` and friends) live in the DLL directory and are part of the
standard library, so removing them would break `import smtplib` for reasons that
have nothing to do with third-party packages.

    python .tools/_check_core_imports.py
"""
import os
import subprocess
import sys

PY = sys.executable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BLOCKED = ("PIL", "numpy", "playwright", "pyautogui")

BLOCKER = '''
import sys

BLOCKED = {blocked!r}


class Blocker:
    """Make the named third-party packages look absent."""

    def find_module(self, name, path=None):
        return self.find_spec(name, path)

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(
                "{{}} is blocked: this check asserts the core pipeline does not "
                "depend on it".format(name))
        return None


sys.meta_path.insert(0, Blocker())
for _name in list(sys.modules):
    if _name.split(".")[0] in BLOCKED:
        del sys.modules[_name]
'''.format(blocked=BLOCKED)


def run(label, code, timeout, inverted=False):
    """Run a check. `inverted=True` means "zero exit is the FAILURE case"."""
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    print("=== %s ===" % label, flush=True)
    proc = subprocess.run([PY, "-c", BLOCKER + code], cwd=ROOT,
                          capture_output=True, text=True, env=env, timeout=timeout)
    print((proc.stdout.strip() or "(no stdout)"))
    if proc.returncode != 0:
        print("  stderr tail: " + proc.stderr.strip()[-500:])
    print("  exit: %d\n" % proc.returncode)
    passed = (proc.returncode != 0) if inverted else (proc.returncode == 0)
    return passed


# Each blocked name must raise ImportError; the snippet exits 0 when none of them
# is importable, so a zero exit here is the PASS case.
blocked_ok = run(
    "self-check: each blocked name must raise ImportError",
    "import importlib.util as u\n"
    "missing = []\n"
    "for n in %r:\n"
    "    try:\n"
    "        u.find_spec(n); missing.append(n)\n"
    "    except ImportError:\n"
    "        pass\n"
    "print('still importable:', missing or 'none')\n"
    "raise SystemExit(1 if missing else 0)\n" % (BLOCKED,),
    60)

ok_core = run("core modules import with third-party blocked",
              "import sys; sys.path.insert(0, 'scripts');"
              "import generate_research_brief, publisher_feeds,"
              "fetch_paper_attachments, deepseek_client;"
              "print('core imports OK -- standard library only')", 120)

ok_dry = run("dry run with third-party blocked",
             "import sys; sys.argv = ['generate_research_brief',"
             " '--days-back', '1', '--dry-run'];"
             "sys.path.insert(0, 'scripts');"
             "import generate_research_brief as g;"
             "raise SystemExit(g.main() or 0)", 420)

print("=" * 62)
print("third-party blocked?   ", "yes" if blocked_ok else "NO (check is broken)")
print("core modules import?   ", "yes" if ok_core else "NO")
print("core dry-run?          ", "yes" if ok_dry else "NO")
print("=" * 62)
raise SystemExit(0 if (blocked_ok and ok_core and ok_dry) else 1)