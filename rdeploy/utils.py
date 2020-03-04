import os
import yaml
import json
import base64
try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader
from sys import platform


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

def get_helm_bin(config_dict: dict) -> str:
    if config_dict.get('use_system_helm', True):
        helm_bin = 'helm'
    else:
        helm_version = config_dict['helm_version']
        if platform == 'linux' or platform == 'linux2':
            folder = 'linux-amd64'
            helm_bin = 'opt/helm-v{version}/{folder}/helm'.format(version=helm_version, folder=folder)
        elif platform == 'darwin':
            folder = 'darwin-amd64'
            helm_bin = 'opt/helm-v{version}/{folder}/helm'.format(version=helm_version, folder=folder)
        elif platform == 'win32':
            folder = 'windows-amd64'
            helm_bin = 'opt/helm-v{version}/{folder}/helm.exe'.format(version=helm_version, folder=folder)

    return helm_bin

def json_decode_data_fields(secret_json):
    return json.dumps(decode_data_fields(json.loads(secret_json)), indent=2)


def yaml_decode_data_fields(secret_yaml):
    return yaml.safe_dump(decode_data_fields(yaml.safe_load(secret_yaml)), indent=2)


def decode_data_fields(secret):
    decoded_data = {}
    for key, value in secret['data'].items():
        decoded_data[key] = decode_data_value(value)
    secret['data'] = decoded_data
    return secret


def decode_data_value(encoded_value):
    decoded_value = base64.b64decode(encoded_value).decode()
    try:
        return json.loads(decoded_value)
    except ValueError:
        return decoded_value
