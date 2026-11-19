import os
import sys
import yaml
import json
import shlex
import base64
import platform as py_platform

from packaging import version


try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader
from sys import platform

from invoke.exceptions import ParseError
from json import dumps
from .exceptions import ExecuteError


def strtobool(val):
    """Convert a string representation of truth to True or False."""
    val = val.strip().lower()
    if val in ('y', 'yes', 't', 'true', 'on', '1'):
        return True
    elif val in ('n', 'no', 'f', 'false', 'off', '0'):
        return False
    else:
        raise ValueError(f"invalid truth value {val!r}")


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

    try:
        response_bool = strtobool(response)
    except ValueError:
        print('Confirm with y, yes, t, true, on or 1; '
              'cancel with n, no, f, false, off or 0.')
        return confirm(prompt, failure_prompt)

    if not response_bool:
        raise ParseError(failure_prompt)


def get_helm_arch() -> str:
    """
    Return the Helm release architecture for this machine ('amd64' or
    'arm64').
    """
    machine = py_platform.machine().lower()
    if machine in ('arm64', 'aarch64'):
        return 'arm64'
    elif machine in ('x86_64', 'amd64'):
        return 'amd64'
    raise ExecuteError(f'Unsupported machine architecture for helm: {machine!r}')


def get_helm_os_string() -> str:
    """
    Return the Helm release '<os>-<arch>' string for this machine, e.g.
    'darwin-arm64'. It is used in the download URL and is also the name of
    the folder the release archive extracts to.
    """
    if platform == 'linux' or platform == 'linux2':
        os_name = 'linux'
    elif platform == 'darwin':
        os_name = 'darwin'
    elif platform == 'win32':
        os_name = 'windows'
    else:
        raise ExecuteError(f'Unsupported platform for helm: {platform!r}')

    return f'{os_name}-{get_helm_arch()}'


def get_helm_bin(config_dict: dict) -> str:
    if config_dict.get('use_system_helm', True):
        helm_bin = 'helm'
    else:
        helm_version = config_dict['helm_version']
        folder = get_helm_os_string()
        binary = 'helm.exe' if platform == 'win32' else 'helm'
        helm_bin = f'opt/helm-v{helm_version}/{folder}/{binary}'

    return helm_bin

def get_kube_target(settings_dict: dict, config_dict: dict) -> tuple:
    """
    Return the (kube context, namespace) a config deploys to, from
    rdeploy.yaml alone.

    Every kubectl and helm call passes these explicitly instead of relying on
    the kubeconfig's current context and namespace. Those are shared, mutable
    state: anything else on the machine (another terminal running `kubens` or
    `kubectl config set-context`) can change them while a command runs, and
    a long command such as an atomic `helm upgrade` resolves the namespace
    again when it rolls back.
    """
    config_version = settings_dict.get('version')
    namespace = config_dict['namespace']

    if config_version and version.parse(str(config_version)) > version.parse('3'):
        sys.exit("Unsupported rdeploy.yaml version, please upgrade rdeploy"
                 " or double check the version number.")

    # v3 of the config file names the kubernetes context directly.
    if str(config_version) == '3' and config_dict.get('kube_context'):
        return config_dict['kube_context'], namespace

    # v2 (or v3 when kube_context is not specified)
    if config_version and version.parse(str(config_version)) >= version.parse('2'):
        provider_data = config_dict.get('cloud_provider')
        if not provider_data or provider_data.get('name') != 'gcp':
            sys.exit("Unsupported cloud provider: {}."
                     " Only 'gcp' is supported since rdeploy 0.2.0"
                     " (Azure support was removed)."
                     .format(provider_data.get('name') if provider_data else None))

        if provider_data.get('zone') is not None:
            zone = provider_data['zone']
        else:
            zone = provider_data.get('region')
        if zone is None:
            sys.exit("cloud_provider requires either 'zone' or 'region' in rdeploy.yaml.")

        return ('{name}_{project}_{cluster}_{zone}'.format(
                    name=provider_data['name'],
                    project=provider_data['project'],
                    cluster=provider_data['kube_cluster'],
                    zone=zone),
                namespace)

    # Config file v1 or no version
    return ('gcp_{cloud_project}_{cluster}_europe-west1-c'.format(
                cloud_project=config_dict['cloud_project'],
                cluster=config_dict['cluster']),
            namespace)


def kubectl_target_flags(settings_dict: dict, config_dict: dict) -> str:
    """`--context ... --namespace ...` for a kubectl command line."""
    context, namespace = get_kube_target(settings_dict, config_dict)
    return '--context {context} --namespace {namespace}'.format(
        context=shlex.quote(context), namespace=shlex.quote(namespace))


def helm_target_flags(settings_dict: dict, config_dict: dict) -> str:
    """`--kube-context ... --namespace ...` for a helm command line."""
    context, namespace = get_kube_target(settings_dict, config_dict)
    return '--kube-context {context} --namespace {namespace}'.format(
        context=shlex.quote(context), namespace=shlex.quote(namespace))


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


def build_management_cmd(config_dict: dict, cmd: str = "", tag: str = "",
                         kube_context: str = None) -> str:
    from kubernetes import client, config
    from kubernetes.client.models import V1Container
    from kubernetes.client.rest import ApiException
    from kubernetes.config.kube_config import KubeConfigMerger, KubeConfigLoader, KUBE_CONFIG_DEFAULT_LOCATION
    from kubernetes.config.config_exception import ConfigException

    config.load_kube_config(context=kube_context)

    # Workaround to read the proxy-url as it is not currently read by load_kube_config()
    try:
        kcfg = KubeConfigMerger(KUBE_CONFIG_DEFAULT_LOCATION)
        k = KubeConfigLoader(config_dict=kcfg.config,
                             active_context=kube_context)
        proxy_url = k._cluster['proxy-url']
    except ConfigException:
        proxy_url = None

    if tag is not None:
        print(tag)

    if proxy_url is not None:
        client.Configuration._default.proxy = proxy_url

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

    # Name the context and namespace explicitly, so the pod is created next to
    # the deployment it was read from whatever the kubeconfig's current
    # namespace is.
    target = ''
    if kube_context:
        target = f'--context {shlex.quote(kube_context)} '
    target += f'--namespace {shlex.quote(config_dict["namespace"])} '

    return f'kubectl {target}run management --rm --tty=true --stdin=true '\
        f'--image={container.image} '\
        f'--overrides=\'{overrides_str}\' '\
        f'--output yaml --command -- \'\''
