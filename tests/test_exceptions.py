"""Tests for rdeploy.exceptions module."""
import pytest

from rdeploy.exceptions import ReleaseError, ExecuteError


class TestExceptions:
    def test_release_error_is_raised(self):
        with pytest.raises(ReleaseError):
            raise ReleaseError("No tags found")

    def test_release_error_message(self):
        with pytest.raises(ReleaseError, match="No tags found"):
            raise ReleaseError("No tags found")

    def test_execute_error_is_raised(self):
        with pytest.raises(ExecuteError):
            raise ExecuteError("Deployment not found")

    def test_execute_error_message(self):
        with pytest.raises(ExecuteError, match="Deployment not found"):
            raise ExecuteError("Deployment not found")
