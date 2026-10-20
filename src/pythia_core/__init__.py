"""pythia-core: the part of pythia that is the same for every database.

Engine packages (pythia-postgres first) build on it; see DESIGN.md. pythia
for Oracle is a separate, self-contained tool and does not use this package.
"""
__version__ = "0.4.0.dev0"     # kept equal to pyproject.toml (tests/test_core.py)
