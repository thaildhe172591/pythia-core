# Working on pythia-core

pythia-core is the shared core of the pythia family (DESIGN.md). Engine
packages depend on it; pythia for Oracle does not.

## Before calling a change done

- Run every suite, no database needed: `python tests/test_core.py`,
  `python tests/test_cli.py`, `python tests/test_read.py`,
  `python tests/test_gate.py`, `python tests/test_harness.py`,
  `python tests/test_doors.py`, `python tests/test_release.py`,
  `python tests/test_lease.py`, `python tests/test_lint.py`. TDD: the
  failing test comes first; a change to the gate or the guard comes with a
  test that tries to get a write past it.
- CI runs Ubuntu, Windows and macOS on Python 3.9 and 3.13. Build paths with
  `pathlib` / `os.path.join`, derive the interpreter from `sys.executable`,
  and use no stdlib API newer than 3.9.

## The contracts

- **Stdlib only.** `dependencies = []` in pyproject.toml is enforced by a
  test. A driver belongs to the engine package that needs it.
- **Engine-neutral.** Nothing here names a database's catalog, driver or
  error codes. If the answer changes with the database, it is an engine
  method (DESIGN.md §3). A new method goes into a tier in `engine.py`, and
  into DESIGN.md §4, in the same change.
- **Never touch the Oracle tool's state.** An engine's files live under
  `.pythia/<engine>/`, and its environment variables under its own prefix.
- **Truncation is always announced.** A list is windowed with `clip`; what
  it leaves out is said, with how to get it.
- **Docs never contradict code.** A behaviour change updates the docstring,
  README.md, DESIGN.md and CHANGELOG.md in the same change — and
  SECURITY.md when a gate moves.
- **Versions** agree in pyproject.toml and `pythia_core.__version__`.

## Never

- Weaken a gate that is ported from pythia (snapshot before write, no silent
  restore, unrecognised statements refused, exit 3 for written-but-broken, a
  person's approval before a write). When a gate is ported, its adversarial
  tests come with it.
- Put real-world identifiers anywhere (hosts, schemas, tenants,
  credentials). Examples use APP_OWNER, PKG_ORDER, T_ORDER. Everything
  shipped is English.
