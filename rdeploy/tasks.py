import sys
import tarfile
import zipfile
import urllib.request
import io

from invoke import task
import semver
import json
import re

from packaging import version
from rdeploy.exceptions import ReleaseError

from rdeploy.utils import get_settings, confirm, get_helm_bin, yaml_decode_data_fields, build_management_cmd

# Cluster Activation:
#####################
@task
def set_project(ctx, config):
    """Sets the active gcloud project"""
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    if settings_dict.get('version') and version.parse(str(settings_dict['version'])) > version.parse('1'):
        provider_data = config_dict.get('cloud_provider')
        if not provider_data or provider_data.get('name') != 'gcp':
            sys.exit("Unsupported cloud provider: {}."
                     " Only 'gcp' is supported since rdeploy 0.2.0"
                     " (Azure support was removed)."
                     .format(provider_data.get('name') if provider_data else None))
        ctx.run('gcloud config set project {project}'
            .format(project=provider_data['project']), echo=True)
    else:
        ctx.run('gcloud config set project {project}'
                .format(project=config_dict['cloud_project']), echo=True)

@task
def set_cluster(ctx, config):
    """Sets the active cluster"""
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]

    if settings_dict.get('version') and version.parse(str(settings_dict['version'])) > version.parse('1'):
        provider_data = config_dict.get('cloud_provider')
        if not provider_data or provider_data.get('name') != 'gcp':
            sys.exit("Unsupported cloud provider: {}."
                     " Only 'gcp' is supported since rdeploy 0.2.0"
                     " (Azure support was removed)."
                     .format(provider_data.get('name') if provider_data else None))

        if provider_data.get('zone'):
            zone_or_region_param = '--zone {}'.format(provider_data['zone'])
            zone = provider_data['zone']
        elif provider_data.get('region'):
            zone_or_region_param = '--region {}'.format(provider_data['region'])
            zone = provider_data['region']
        else:
            sys.exit("cloud_provider requires either 'zone' or 'region' in rdeploy.yaml.")

        ctx.run('gcloud container clusters get-credentials {cluster}'
                ' --project {project} {zone_or_region_param}'
                .format(cluster=provider_data['kube_cluster'],
                        project=provider_data['project'],
                        zone_or_region_param=zone_or_region_param),
                echo=True)

        ctx.run('kubectl config rename-context gke_{project}_{zone}_{cluster}'
                ' {name}_{project}_{cluster}_{zone}'
                .format(cluster=provider_data['kube_cluster'],
                        project=provider_data['project'],
                        name=provider_data['name'],
                        zone=zone),
                echo=True)
    else:
        if config_dict.get('cloud_zone'):
            zone_or_region_param = '--zone {}'.format(config_dict['cloud_zone'])
            zone = config_dict['cloud_zone']
        elif config_dict.get('cloud_region'):
            zone_or_region_param = '--region {}'.format(config_dict['cloud_region'])
            zone = config_dict['cloud_region']
        else:
            zone_or_region_param = '--zone europe-west1-c'
            zone = 'europe-west1-c'

        ctx.run('gcloud container clusters get-credentials {cluster}'
                ' --project {project} {zone_or_region_param}'
                .format(cluster=config_dict['cluster'],
                        project=config_dict['cloud_project'],
                        zone_or_region_param=zone_or_region_param),
                echo=True)

        ctx.run('kubectl config rename-context gke_{project}_{zone}_{cluster}'
                ' gcp_{project}_{cluster}_{zone}'
                .format(cluster=config_dict['cluster'],
                        project=config_dict['cloud_project'],
                        zone=zone),
                echo=True)


@task()
def activate(ctx, config):
    """Fetches and sets the project, cluster and namespace"""
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_project(ctx, config)
    set_cluster(ctx, config)
    ctx.run('kubectl config use-context $(kubectl config current-context)'
            ' --namespace={namespace}'
            .format(namespace=config_dict['namespace']),
            echo=True)


@task
def set_context(ctx, config):
    """Switch cluster and namespace"""
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    provider_data = config_dict.get('cloud_provider')

    # Check for future versions
    if settings_dict.get('version') and version.parse(str(settings_dict['version'])) > version.parse('3'):
        sys.exit(f"Unsupported rdeploy.yaml version, please upgrade rdeploy or double check the version number.")

    # v3 of the config file uses the kube_context value to set the kubernetes cluster context
    elif str(settings_dict.get('version')) == '3' and config_dict.get('kube_context'):
        ctx.run('kubectl config use-context {kube_context}'
                    .format(kube_context = config_dict['kube_context']),
                    echo=True)
        ctx.run('kubectl config set-context --current --namespace={namespace}'
                    .format(namespace=config_dict['namespace']),
                    echo=True)

    # v2 (or v3 when kube_context is not specified)
    elif version.parse(str(settings_dict['version'])) >= version.parse('2'):
        if not provider_data or provider_data.get('name') != 'gcp':
            sys.exit("Unsupported cloud provider: {}."
                     " Only 'gcp' is supported since rdeploy 0.2.0"
                     " (Azure support was removed)."
                     .format(provider_data.get('name') if provider_data else None))

        if provider_data.get('zone') is not None:
            get_zone = provider_data['zone']
        elif provider_data.get('region') is not None:
            get_zone = provider_data['region']
        else:
            sys.exit("cloud_provider requires either 'zone' or 'region' in rdeploy.yaml.")

        ctx.run('kubectl config use-context {name}_{project}_{cluster}_{zone}'
            ' --namespace={namespace}'
            .format(namespace=config_dict['namespace'],
                    project=provider_data['project'],
                    cluster=provider_data['kube_cluster'],
                    name=provider_data['name'],
                    zone=get_zone),
            echo=True)
        ctx.run('kubectl config set-context --current'
            ' --namespace={namespace}'
            .format(namespace=config_dict['namespace']),
            echo=True)

    # Config file v1 or no version
    else:
        ctx.run('kubectl config use-context gcp_{cloud_project}_{cluster}_europe-west1-c'
            ' --namespace={namespace}'
            .format(namespace=config_dict['namespace'],
                    cloud_project=config_dict['cloud_project'],
                    cluster=config_dict['cluster']),
            echo=True)
        ctx.run('kubectl config set-context --current'
            ' --namespace={namespace}'
            .format(namespace=config_dict['namespace']),
            echo=True)

# Versioning Helpers
####################
@task
def next_version(ctx, bump):
    """
    Returns incremented version number by looking at git tags
    """
    # Get latest git tag:
    try:
        latest_tag = latest_version(ctx)
    except ReleaseError:
        latest_tag = '0.0.0'

    ver = semver.Version.parse(latest_tag)

    increment = {
        'build': lambda v: str(v.bump_build()),
        'patch': lambda v: str(v.bump_patch()),
        'minor': lambda v: str(v.bump_minor()),
        'major': lambda v: str(v.bump_major()),
    }

    if bump in ['pre-patch', 'pre-minor', 'pre-major']:
        base_bump = bump[4:]  # e.g. 'patch', 'minor', 'major'
        incremented = str(getattr(ver, f'bump_{base_bump}')())
        try:
            # Check for existing pre-releases and increment
            pre_ver = semver.Version.parse(latest_prerelease(ctx, incremented))
            incremented = str(pre_ver.bump_prerelease())
        except (ReleaseError, ValueError):
            # No existing pre-release, so create one
            incremented = str(semver.Version.parse(incremented).bump_prerelease())
    else:
        incremented = increment[bump](ver)

    return incremented


@task
def latest_version(ctx):
    """Checks the git tags and returns the current latest version"""
    ctx.run('git fetch --tags')
    result = ctx.run('git tag --sort=-v:refname', hide='both')
    tags = result.stdout.split('\n')

    regex = re.compile(r'^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')

    version_tags = filter(regex.search, tags)
    try:
        latest_tag = next(version_tags)
        return latest_tag[1:] if latest_tag.startswith('v') else latest_tag
    except StopIteration:
        raise ReleaseError('No valid semver tags found in repository')

@task
def latest_prerelease(ctx, ver):
    """Checks the git tags and returns the current latest pre-release version"""
    ctx.run('git fetch --tags')
    result = ctx.run('git tag --sort=-v:refname', hide='both')
    tags = result.stdout.split('\n')

    regex = re.compile(r'^v?{}(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$'.format(re.escape(ver)))
    version_tags = filter(regex.search, tags)

    try:
        latest_tag = next(version_tags)
        return latest_tag[1:] if latest_tag.startswith('v') else latest_tag
    except StopIteration:
        raise ReleaseError('No valid semver tags found in repository')


# Kubernetes and GCloud Commands
################################
@task
def create_namespace(ctx, config):
    """
    Creates a kubernetes namespace
    """
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_context(ctx, config)

    ctx.run('kubectl create namespace {namespace}'
            .format(namespace=config_dict['namespace']),
            echo=True)


@task
def upload_secrets(ctx, config, env_file):
    """
    Uploads secrets from an env file to kubernetes
    """
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_context(ctx, config)

    ctx.run('kubectl delete secret {project_name}'\
            .format(project_name=config_dict['project_name'],
                    namespace=config_dict['namespace']),
            warn=True)

    ctx.run('kubectl create secret generic {project_name}'
            ' --from-env-file {env_file}'
            .format(project_name=config_dict['project_name'],
                    env_file=env_file))


@task
def decode_secret(ctx, config, secret):
    """
    Prints the decoded values of a kubernetes secret
    """
    set_context(ctx, config)
    o = io.StringIO()
    ctx.run('kubectl get secret {secret} -o yaml'.format(secret=secret),out_stream=o)
    print(yaml_decode_data_fields(o.getvalue()))


@task
def create_volume(ctx, name,
                  zone='europe-west1-c',
                  size='100',
                  type='pd-standard'):
    """Creates a GCE persistent disk"""
    ctx.run('gcloud compute disks create {name}'
            ' --zone {zone} --size {size} --type {type}'
            .format(name=name, size=size, zone=zone, type=type))


@task
def upload_static(ctx, config, bucket_name):
    """Upload static files to gcloud bucket"""
    set_project(ctx, config)

    ctx.run('echo "yes\n" | python src/manage.py collectstatic')
    ctx.run('gsutil -m rsync -d -r var/www/static gs://{bucket_name}'
            .format(bucket_name=bucket_name), echo=False)


@task
def create_bucket(ctx, config, bucket_name):
    """Creates a private gcloud bucket"""
    set_project(ctx, config)

    ctx.run('gsutil mb gs://{bucket_name}'
            .format(bucket_name=bucket_name), echo=False)
    ctx.run('gsutil defacl set private gs://{bucket_name}'
            .format(bucket_name=bucket_name), echo=False)


@task
def create_public_bucket(ctx, config, bucket_name):
    """Creates a public gcloud bucket"""
    set_project(ctx, config)

    ctx.run('gsutil mb -b on gs://{bucket_name}'.format(bucket_name=bucket_name))
    ctx.run('gsutil iam ch allUsers:objectViewer gs://{bucket_name}'
            .format(bucket_name=bucket_name))


@task
def install(ctx, config):
    """
    Installs kubernetes deployment
    """
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_context(ctx, config)

    helm_bin = get_helm_bin(config_dict)

    install_flag = ''

    if config_dict.get('helm_version') and version.parse(str(config_dict['helm_version'])) <= version.parse('3'):
        install_flag = " --name"

    provider_data = config_dict.get('cloud_provider')
    helm_registry = provider_data.get('helm_registry')
    if helm_registry:
        # Authenticate with the GCP Artifact Registry
        ctx.run('gcloud auth print-access-token | {helm_bin} registry login -u oauth2accesstoken --password-stdin https://{helm_registry}'.format(helm_registry=helm_registry, helm_bin=helm_bin), echo=True)
        helm_chart = 'oci://{helm_registry}/{gcp_project}/{helm_chart}'.format(helm_registry=helm_registry, gcp_project=provider_data['project'], helm_chart=config_dict['helm_chart'])
    else:
        # Add the Rehive Helm Repo
        ctx.run('{helm_bin} repo add rehive https://rehive.github.io/charts'.format(helm_bin=helm_bin), echo=True)
        helm_chart = config_dict['helm_chart']

    ctx.run('{helm_bin} install{helm_install_flag} {project_name} '
            '--values {helm_values_path} '
            '--version {helm_chart_version} {helm_chart}'
            .format(helm_bin=helm_bin,
                    project_name=config_dict['project_name'],
                    helm_install_flag=install_flag,
                    helm_values_path=config_dict['helm_values_path'],
                    helm_chart=helm_chart,
                    helm_chart_version=config_dict['helm_chart_version']),
            echo=True)


@task
def upgrade(ctx, config, tag):
    """
    Upgrades kubernetes deployment
    """

    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_context(ctx, config)

    helm_bin = get_helm_bin(config_dict)

    provider_data = config_dict.get('cloud_provider')
    helm_registry = provider_data.get('helm_registry')
    if helm_registry:
        # Authenticate with the GCP Artifact Registry
        ctx.run('gcloud auth print-access-token | {helm_bin} registry login -u oauth2accesstoken --password-stdin https://{helm_registry}'.format(helm_registry=helm_registry, helm_bin=helm_bin), echo=True)
        helm_chart = 'oci://{helm_registry}/{gcp_project}/{helm_chart}'.format(helm_registry=helm_registry, gcp_project=provider_data['project'], helm_chart=config_dict['helm_chart'])
    else:
        helm_chart = config_dict['helm_chart']

    ctx.run('{helm_bin} upgrade {project_name} '
            '--values {helm_values_path} '
            '--set image.tag={version} '
            '--version {helm_chart_version} {helm_chart}'
            .format(helm_bin=helm_bin,
                    project_name=config_dict['project_name'],
                    helm_chart=helm_chart,
                    helm_values_path=config_dict['helm_values_path'],
                    version=tag,
                    helm_chart_version=config_dict['helm_chart_version']),
            echo=True)


@task
def helm(ctx, config, command):
    """Run arbitrary helm commands"""
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_context(ctx, config)

    helm_bin = get_helm_bin(config_dict)

    ctx.run('{helm_bin} {command}'.format(helm_bin=helm_bin,
                                          command=command),
            echo=True)


@task
def helm_setup(ctx, config):
    """Download and configure a local Helm binary"""
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]

    helm_version = config_dict.get('helm_version')
    if not helm_version:
        print('Please add the helm_version config to rdeploy.yaml.')
        return

    if config_dict.get('use_system_helm', True):
        print('Please add the following config to rdeploy.yaml:\n'
              'use_system_helm: false')
        return

    if sys.platform == 'linux' or sys.platform == 'linux2':
        os_string = 'linux-amd64'
        archive_tool = tarfile
    elif sys.platform == 'darwin':
        os_string = 'darwin-amd64'
        archive_tool = tarfile
    elif sys.platform == 'win32':
        os_string = 'windows-amd64'
        archive_tool = zipfile

    url = 'https://get.helm.sh/helm-v{version}-{os_string}.tar.gz'.format(version=helm_version,
                                                                          os_string=os_string)
    file_tmp = urllib.request.urlretrieve(url, filename=None)[0]
    tar = archive_tool.open(file_tmp)
    tar.extractall('./opt/helm-v{version}'.format(version=helm_version))

    helm_bin = get_helm_bin(config_dict)

    provider = config_dict.get('cloud_provider')
    helm_registry = provider.get('helm_registry')
    if not helm_registry:
        ctx.run('{helm_bin} repo add stable https://charts.helm.sh/stable'.format(helm_bin=helm_bin), echo=True)
        ctx.run('{helm_bin} repo add rehive https://rehive.github.io/charts'.format(helm_bin=helm_bin), echo=True)

    print('Successfully installed helm to opt/helm-v{version}/{os_string}/ \n'
          'Please make sure this directory has been added to .gitignore.'. format(version=helm_version,
                                                                                  os_string=os_string))


@task
def live_image(ctx, config):
    """Displays the current docker image and version deployed"""
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_context(ctx, config)

    result = ctx.run('kubectl get deployment {project_name} --output=json'
                     .format(project_name=config_dict['project_name']),
                     echo=True, hide='stdout')
    server_config = json.loads(result.stdout)
    image = server_config['spec']['template']['spec']['containers'][0]['image']
    print(image)


@task
def shell(ctx, config, tag=None):
    """Exec into the management container"""
    set_context(ctx, config)
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    management_cmd = build_management_cmd(config_dict, "/bin/bash", tag)
    ctx.run(management_cmd, pty=True, warn=False, echo=True)


@task
def manage(ctx, config, cmd, tag=None):
    """Run a Django management command in-cluster"""
    set_context(ctx, config)
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    management_cmd = build_management_cmd(config_dict, f'python manage.py {cmd}', tag)
    ctx.run(management_cmd, pty=True, warn=False, echo=True)


@task
def compose(ctx, cmd, tag):
    """Wrapper for docker-compose"""
    ctx.run('VERSION={tag} docker-compose {cmd}'
            .format(cmd=cmd, tag=tag), echo=True)


# Build commands
################
@task
def git_release(ctx, version_bump, force=False):
    """
    Bump version, push git tag
    N.B. Commit changes first
    the force flag assumes you have committed all changes
    """
    if not force:
        confirm('Did you remember to commit all changes? ')

    if version_bump == 'pre':
        sys.exit("Please specify pre-patch, pre-minor or pre-major for prereleases.")


    bumped_version = next_version(ctx, bump=version_bump)
    tag = 'v' + bumped_version
    comment = 'Version ' + bumped_version

    # Create an push git tag:
    print('Tag: {}\n\n'.format(tag))
    ctx.run("git tag '%s' -m '%s'" % (tag, comment), echo=True)
    ctx.run("git push origin %s" % tag, echo=True)


@task
def build(ctx, config, tag):
    """
    Build project's docker image and pushes to remote repo
    """
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_project(ctx, config)
    image_name = config_dict['docker_image'].split(':')[0]
    image = '{}:{}'.format(image_name, tag)
    ctx.run('docker build -t %s -f etc/docker/Dockerfile .' % image, echo=True)
    ctx.run('gcloud auth configure-docker', echo=True)
    ctx.run('docker push %s' % image, echo=True)
    return image


@task
def cloudbuild(ctx, config, tag):
    """
    Build project's docker image using Google Cloud Build
    """
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    image_name = config_dict['docker_image'].split(':')[0]

    if config_dict.get('container_registry_provider') == 'google':
        project = config_dict['docker_image'].split('/')[1]
        ctx.run('gcloud config set project {project}'
            .format(project=project), echo=True)
    else:
        set_project(ctx, config)

    if settings_dict.get('version') and version.parse(str(settings_dict['version'])) > version.parse('1'):
        provider_data = config_dict.get('cloud_provider')

        if config_dict.get('container_registry_provider') == 'google':
            project = config_dict['docker_image'].split('/')[1]
        elif provider_data and provider_data.get('name') == 'gcp':
            project = provider_data['project']
        else:
            sys.exit("Unsupported cloud provider: {}."
                     " Only 'gcp' is supported since rdeploy 0.2.0"
                     " (Azure support was removed)."
                     .format(provider_data.get('name') if provider_data else None))

        log_dir = "gs://{project}-cloudbuild-logs/{image}/{tag_name}/".format(
            project=project, image=image_name, tag_name=tag)
        ctx.run('gcloud builds submit .'
            ' --config etc/docker/cloudbuild.yaml'
            ' --substitutions _IMAGE={image_name},TAG_NAME={tag_name}'
            ' --gcs-log-dir {log_dir}'
            .format(image_name=image_name, tag_name=tag, log_dir=log_dir),
            echo=True)

    else:
        log_dir = "gs://{project}-cloudbuild-logs/{image}/{tag_name}/".format(
        project=config_dict['cloud_project'], image=image_name, tag_name=tag)
        ctx.run('gcloud builds submit .'
                ' --config etc/docker/cloudbuild-no-cache.yaml'
                ' --substitutions _IMAGE={image_name},TAG_NAME={tag_name}'
                ' --gcs-log-dir {log_dir}'
                .format(image_name=image_name, tag_name=tag, log_dir=log_dir),
                echo=True)
