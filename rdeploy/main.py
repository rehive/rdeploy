import pkg_resources
from invoke import Argument, Collection, Program

import rdeploy


class MainProgram(Program):
    def core_args(self):
        core_args = super(MainProgram, self).core_args()
        extra_args = [
            Argument(names=('project', 'n'), help="The project/package name being build"),
        ]
        return core_args + extra_args


version = pkg_resources.get_distribution("rdeploy").version
program = MainProgram(namespace=Collection.from_module(rdeploy), version=version)
