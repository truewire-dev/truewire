"""Fixture client public export surface -- hand-curated, never generated (design §4).

Only the root client class is exported here, exactly as a real client's `__init__.py`
curates its own top-level surface rather than re-exporting the whole generated tree.
"""

from .main import FixtureClient
