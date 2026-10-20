"""Is a person at the keyboard? The gate's human acts — `apply --yes`,
`approve`, loosening the policy — are the developer's, and an agent's shell
has no console. Ported from pythia unchanged in substance.
"""
import os
import sys

# The test seam: None in production. A Python-level switch an agent cannot
# set from a shell; the suites set it so they mean the same thing whether
# they run in a terminal or in CI.
FORCE_CONSOLE = None


def _real_console():
    """stdin is a console a person types into. isatty() alone is not enough
    on Windows: NUL is a character device, so a subprocess launched with
    stdin=DEVNULL reports isatty() == True. Only a real console answers
    GetConsoleMode, so ask that too."""
    if FORCE_CONSOLE is not None:
        return bool(FORCE_CONSOLE)
    try:
        if not sys.stdin.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.GetStdHandle(-10)  # STD_INPUT
            mode = ctypes.c_ulong()
            return bool(ctypes.windll.kernel32.GetConsoleMode(
                handle, ctypes.byref(mode)))
        except Exception:   # noqa: BLE001 — any failure means "not a console"
            return False
    return True


def ci_variable(engine):
    return f"{engine.env_prefix}_CI"


def human_at_the_keyboard(engine, env=None):
    """A person at a real console, or a pipeline that says it is one with
    <PREFIX>_CI=1 (each engine has its own, as it has its own connections)."""
    env = os.environ if env is None else env
    if env.get(ci_variable(engine)):
        return True
    return _real_console()


def console_session():
    """A real console, with no environment escape. `approve` gets no
    pipeline door: a pipeline that wants to write already has --yes, and a
    second escape would be the way around this gate."""
    return _real_console()
