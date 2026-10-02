"""The pydantic models of every file the toolchain reads that has a schema (T7).

Each model is the source of the JSON Schema published at `truewire.dev/schemas/`, so an
editor completes the file and a typo fails before it reaches a command. `python -m
truewire.schemas` writes those schemas into `published/`, where they are committed; a test
fails when a model changes and the file does not.

- `docs/`: `docs/docs.yml`, the nav, quickstart and metadata of a project's docs (W10).
- `config/`: `truewire.toml`, the one project file (W13).
"""
from .publish import PUBLISHED, SCHEMA_BASE, published_dir, render

__all__ = ['PUBLISHED', 'SCHEMA_BASE', 'published_dir', 'render']
