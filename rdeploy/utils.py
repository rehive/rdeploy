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
from json import dumps
from .exceptions import ExecuteError


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
            helm_bin = f'opt/helm-v{helm_version}/{folder}/helm'
        elif platform == 'darwin':
            folder = 'darwin-amd64'
            helm_bin = f'opt/helm-v{helm_version}/{folder}/helm'
        elif platform == 'win32':
            folder = 'windows-amd64'
            helm_bin = f'opt/helm-v{helm_version}/{folder}/helm.exe'

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


def build_management_cmd(config_dict: dict, cmd: str = "", tag: str = "") -> str:
    from kubernetes import client, config
    from kubernetes.client.models import V1Container
    from kubernetes.client.rest import ApiException
    print(tag)

    config.load_kube_config()
    app_v1_api = client.AppsV1Api()

    try:
        deployment = app_v1_api.read_namespaced_deployment(
            config_dict['project_name'],
            config_dict['namespace'],
            pretty=False
        )
    except ApiException as e:
       raise ExecuteError('AppsV1Api deployment not installed')

    container_v1 = deployment.spec.template.spec.containers[0]
    image_pull_secrets = None

    try:
        image_pull_secrets = deployment.spec.template.spec.image_pull_secrets[0]
    except ApiException as e:
        raise ExecuteError('Exception when calling AppsV1Api->image_pull_secret \n')
    except TypeError as e:
        print(f'image_pull_secrets does not exist and its ok, carrying on. Info: {e}')
        pass

    if tag:
        image_tag = container_v1.image.rsplit(':',1)[0]
        image_tag = f'{image_tag}:{tag}'
        container = V1Container(env_from=container_v1.env_from, env=container_v1.env,
                                image=image_tag, command=cmd.split(), args=[],
                                name="management", stdin=True, tty=True)
    else:
        container = V1Container(env_from=container_v1.env_from, env=container_v1.env,
                                image=container_v1.image, command=cmd.split(), args=[],
                                name="management", stdin=True, tty=True)


    def create_dict_json_attributes(obj):
        if not hasattr(obj, 'to_dict'):
            return obj
        obj_dict = obj.to_dict()
        ret = dict()
        for key, value in obj_dict.items():
            attrib = getattr(obj, key)
            if attrib is None:
                # Don't patch with null values
                continue

            if isinstance(attrib, str) \
                    or isinstance(attrib, int) \
                    or isinstance(attrib, float) \
                    or isinstance(attrib, bool):
                ret[obj.attribute_map[key]] = value
            elif isinstance(attrib, list):
                ret[obj.attribute_map[key]] = [create_dict_json_attributes(a) for a in attrib]
            else:
                ret[obj.attribute_map[key]] = create_dict_json_attributes(attrib)
        return ret

    # Convert V1_k8s_object to dictionary
    container_dict = create_dict_json_attributes(container)
    # Create dictionary
    overrides = dict(spec=dict(containers=[container_dict]))

    if image_pull_secrets:
        image_p_s_dict = create_dict_json_attributes(image_pull_secrets)
        overrides["spec"]["imagePullSecrets"] = [image_p_s_dict]

    overrides_str = dumps(overrides)

    return f'kubectl run management --rm --tty=true --stdin=true '\
        f'--image={container.image} '\
        f'--overrides=\'{overrides_str}\' '\
        f'--output yaml --command -- \'\''
