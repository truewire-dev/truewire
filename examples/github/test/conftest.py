"""Shared fixtures: a real `GitHub` client pointed at the local mock server."""

from pathlib import Path

import pytest

from github import GitHub
from truewire.mock import running_mock_servers


@pytest.fixture
def mock_servers():
  with running_mock_servers(Path(__file__).resolve().parents[1]) as servers:
    yield servers


@pytest.fixture
def client(mock_servers):
  """The generated client, built exactly as a user would, against the mock's base URL."""
  return GitHub.new(base_url=mock_servers.http_base_url, validate=True)
