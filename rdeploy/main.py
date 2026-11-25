import shlex
import sys
from importlib.metadata import version as get_version

from invoke import Argument, Collection, Program

import rdeploy
from rdeploy import secret_manager


def secret_argv(argv):
    """
    Let `rdeploy secret <action> ...` stand for invoke's
    `rdeploy secret.<action> ...`.
    """
    argv = shlex.split(argv) if isinstance(argv, str) else list(argv)
    for i in range(1, len(argv) - 1):
        if argv[i] == 'secret' and argv[i + 1] in secret_manager.ACTIONS:
            return argv[:i] + ['secret.' + argv[i + 1]] + argv[i + 2:]
    return argv


class MainProgram(Program):
    def core_args(self):
        core_args = super(MainProgram, self).core_args()
        extra_args = [
            Argument(names=('project', 'n'), help="The project/package name being build"),
        ]
        return core_args + extra_args

    def run(self, argv=None, exit=True):
        return super(MainProgram, self).run(
            secret_argv(sys.argv if argv is None else argv), exit)


def build_namespace():
    namespace = Collection.from_module(rdeploy)
    namespace.add_collection(
        Collection.from_module(secret_manager, name='secret'))
    return namespace


version = get_version("rdeploy")
program = MainProgram(namespace=build_namespace(), version=version)
