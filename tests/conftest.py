"""Shared pytest fixtures and configuration."""
import os
import pytest

# Set required environment variables before importing rollout_operator
# This prevents ValidationError when the module is imported
os.environ.setdefault("RO_TARGET_NAMESPACE", "test-namespace")
os.environ.setdefault("RO_TARGET_STATEFUL_SET", "test-sts")

# Pytest markers are defined in pytest.ini


