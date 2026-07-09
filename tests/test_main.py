"""Tests for rdeploy.main module."""
import pytest

from rdeploy.main import MainProgram, program


class TestMainProgram:
    def test_program_exists(self):
        assert program is not None

    def test_program_has_version(self):
        assert program.version is not None

    def test_main_program_has_extra_args(self):
        mp = MainProgram(version="0.0.1")
        args = mp.core_args()
        arg_names = [a.name for a in args]
        assert 'project' in arg_names

    def test_program_namespace_has_tasks(self):
        # The program should have tasks loaded from rdeploy module
        assert program.namespace is not None
        task_names = [t for t in program.namespace.task_names]
        # Check some expected tasks exist
        assert 'set-project' in task_names or 'set_project' in task_names or any('project' in t for t in task_names)
