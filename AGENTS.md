# Working on pythia-core

pythia-core is the shared core of the pythia family (DESIGN.md). Engine
packages depend on it; pythia for Oracle does not.

## Before calling a change done

- Run every suite, no database needed: `python tests/test_core.py`,
  `python tests/test_cli.py`, `python tests/test_read.py`,
  `python tests/test_gate.py`, `python tests/test_harness.py`,
  `python tests/test_doors.py`, `python tests/test_release.py`,
  `python tests/test_lease.py`, `python tests/test_lint.py`,
  `python tests/test_family.py`. With the three engines installed, also
  `python tests/family_check.py [--oracle <pythia checkout>]`. TDD: the
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
  README.md and README.vi.md, DESIGN.md and CHANGELOG.md in the same
  change — and SECURITY.md when a gate moves. A test holds README.vi.md to
  README.md's headings and commands; the prose is translated by hand.
- **Versions** agree in pyproject.toml and `pythia_core.__version__`, and
  the version has its CHANGELOG.md entry (tests/test_core.py).

## Releasing

1. Once per project: a PyPI trusted publisher (pypi.org → your account →
   Publishing → add a pending publisher: project `pythia-core`, owner
   `thaildhe172591`, repository `pythia-core`, workflow `release.yml`,
   environment `pypi`). No token is stored anywhere.
2. The version in pyproject.toml and `__version__`, its CHANGELOG.md entry
   dated, and CI green on main.
3. `git tag vX.Y.Z && git push origin vX.Y.Z`: the `release` workflow runs
   every suite, checks the tag against the version and the changelog, and
   publishes. pythia-core goes first — an engine's release needs the core
   it asks for on PyPI.

## Never

- Weaken a gate that is ported from pythia (snapshot before write, no silent
  restore, unrecognised statements refused, exit 3 for written-but-broken, a
  person's approval before a write). When a gate is ported, its adversarial
  tests come with it.
- Put real-world identifiers anywhere (hosts, schemas, tenants,
  credentials). Examples use APP_OWNER, PKG_ORDER, T_ORDER. Everything
  shipped is English, but for README.vi.md, the README's translation.
