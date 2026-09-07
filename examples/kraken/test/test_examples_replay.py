"""Generic replay coverage: every recorded HTTP example, through the real client."""
from pathlib import Path

from truewire.testing import build_http_replay_test

test_examples_replay = build_http_replay_test(Path(__file__).resolve().parents[1])
