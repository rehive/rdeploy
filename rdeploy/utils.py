import os
import yaml
try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader


from distutils.util import strtobool
from invoke.exceptions import ParseError

def get_path():
    file_path = os.path.dirname(os.path.realpath(__file__))
    root_path = os.path.dirname(os.path.dirname(file_path))
    return root_path


def format_yaml(template, config):
    """Replace in ${ENV_VAR} in template with value"""
    formatted = template
    for k, v in config.items():
        formatted = formatted.replace('${%s}' % k, v)
    return formatted


def get_settings(path="rdeploy.yaml"):
    """Import project settings"""
    with open(path, 'r') as stream:
        settings_dict = yaml.load(stream, Loader=Loader)

    return settings_dict


def confirm(prompt='Continue?\n', failure_prompt='User cancelled task'):
    """
    Prompt the user to continue. Repeat on unknown response. Raise
    ParseError on negative response
    """
    response = input(prompt)
    response_bool = False

    try:
        response_bool = strtobool(response)
    except ValueError:
        print('Confirm with y, yes, t, true, on or 1; '
              'cancel with n, no, f, false, off or 0.')
        return confirm(prompt, failure_prompt)

    if not response_bool:
        raise ParseError(failure_prompt)
