"""A setuptools based setup module.
See:
https://packaging.python.org/en/latest/distributing.html
"""

from codecs import open
from os import chdir, pardir, path

from setuptools import find_packages, setup

here = path.abspath(path.dirname(__file__))

# Get the long description from the README file
with open(path.join(here, 'README.rst'), encoding='utf-8') as f:
    long_description = f.read()

# allow setup.py to be run from any path
chdir(path.normpath(path.join(path.abspath(__file__), pardir)))

setup(
    name='rdeploy',

    # Versions should comply with PEP440.  For a discussion on single-sourcing
    # the version across setup.py and the project code, see
    # https://packaging.python.org/en/latest/single_source_version.html
    version=open('VERSION').read().strip(),
    # version=version_string,
    # version='1.0.6',

    description=(
        "CLI that makes it easy to build and deploy an application to k8s."),

    long_description=long_description,

    # The project's main homepage.
    url='https://github.com/rehive/rdeploy',

    # Author details
    author='Rehive',
    author_email='info@rehive.com',

    # Choose your license
    license='MIT License',

    # See https://pypi.python.org/pypi?:action=list_classifiers
    classifiers=[
        # How mature is this project? Common values are
        #   3 - Alpha
        #   4 - Beta
        #   5 - Production/Stable
        'Development Status :: 4 - Beta',

        # Indicate who your project is intended for
        'Intended Audience :: System Administrators',

        #  Topics
        'Topic :: Software Development :: Build Tools',
        'Topic :: System :: Installation/Setup',
        'Topic :: System :: Software Distribution',

        # Environment
        'Operating System :: OS Independent',

        # Pick your license as you wish (should match "license" above)
        'License :: OSI Approved :: MIT License',

        # Specify the Python versions you support here. In particular, ensure
        # that you indicate whether you support Python 2, Python 3 or both.
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.6',
    ],

    # What does your project relate to?
    keywords='docker python helm kubernetes build automation deploy',

    # You can just specify the packages manually here if your project is
    # simple. Or you can use find_packages().
    packages=find_packages(exclude=['docs', 'tests']),


    # Alternatively, if you want to distribute just a my_module.py, uncomment
    # this:
    # py_modules=["tasks"],

    # If setuptools_scm is installed, this automatically adds everything in version control
    include_package_data=True,

    zip_safe=True,

    setup_requires=[
        'wheel',
    ],

    # List run-time dependencies here.  These will be installed by pip when
    # your project is installed. For an analysis of "install_requires" vs pip's
    # requirements files see:
    # https://packaging.python.org/en/latest/requirements.html
    install_requires=[
        'invoke<=0.18.1,>=0.15',
        'pyyaml',
        'python-dotenv>=0.5.1',
        'semver',
    ],

    # List additional groups of dependencies here (e.g. development
    # dependencies). You can install these using the following syntax,
    # for example:
    # $ pip install -e .[dev,test]
    extras_require={
        'dev': [
            'invoke<=0.18.1,>=0.15',
            'pyyaml',
            'python-dotenv>=0.5.1',
            'semver',
        ],
        'test': ['coverage'],
    },

    # test_suite='nose.collector',
    # tests_require=['nose'],

    # If there are data files included in your packages that need to be
    # installed, specify them here.  If using Python 2.6 or less, then these
    # have to be included in MANIFEST.in as well.
    # package_data={
    #     'sample': ['package_data.dat'],
    # },

    # Although 'package_data' is the preferred approach, in some case you may
    # need to place data files outside of your packages. See:
    # http://docs.python.org/3.4/distutils/setupscript.html#installing-additional-files # noqa
    # In this case, 'data_file' will be installed into '<sys.prefix>/my_data'
    # data_files=[('my_data', ['data/data_file'])],

    # To provide executable scripts, use entry points in preference to the
    # "scripts" keyword. Entry points provide cross-platform support and allow
    # pip to create the appropriate form of executable for the target platform.
    entry_points={
        'console_scripts': [
            'rdeploy = rdeploy.main:program.run'
        ],
    },
)
