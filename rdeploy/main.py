from importlib.metadata import version as get_version

from invoke import Argument, Collection, Program

import rdeploy


class MainProgram(Program):
    def core_args(self):
        core_args = super(MainProgram, self).core_args()
        extra_args = [
            Argument(names=('project', 'n'), help="The project/package name being build"),
        ]
        return core_args + extra_args


version = get_version("rdeploy")
program = MainProgram(namespace=Collection.from_module(rdeploy), version=version)
