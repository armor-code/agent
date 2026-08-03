"""
Regression tests for ENG-134499 — agent task pool size default.

Kept in a dedicated module (not test_worker.py) because that file's autouse
fixture patches globals (e.g. teleport_semaphore) that are absent from this
version of worker.py, which errors out the whole module. These tests exercise
get_initial_config directly and have no such dependency.
"""

import sys
import argparse
from unittest.mock import patch

import worker


def _build_config(argv):
    """Build config via get_initial_config with a controlled argv and isolated rate_limiter."""
    parser = argparse.ArgumentParser()
    with patch.object(sys, 'argv', argv), patch('worker.rate_limiter'):
        config, _agent_index, _debug_mode, _stdout_logging = worker.get_initial_config(parser)
    return config


def test_default_pool_size_is_10():
    """PREVENTS ENG-134499 regression: pool must default to 10 when --poolSize is absent."""
    config = _build_config(['worker.py'])
    assert config['thread_pool_size'] == 10


def test_explicit_pool_size_overrides_default():
    """An explicit --poolSize still overrides the default."""
    config = _build_config(['worker.py', '--poolSize', '3'])
    assert config['thread_pool_size'] == 3
