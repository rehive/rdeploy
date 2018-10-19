import os
from distutils.util import strtobool
from invoke.exceptions import ParseError

import yaml
from invoke import task
import semver
import json
import re


# Cluster Activation:
#####################
@task
def set_project(ctx, config):
    """Sets the active gcloud project"""
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    ctx.run('gcloud config set project {project}'.format(project=config_dict['cloud_project']), echo=True)


@task
def set_cluster(ctx, config):
    """Sets the active cluster"""
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    ctx.run('gcloud container clusters get-credentials {cluster} --zone europe-west1-c --project {project}'.format(
        cluster=config_dict['cluster'],
        project=config_dict['cloud_project']),
        echo=True)


@task
def set_context(ctx, config):
    """Sets active project, cluster and namespace"""
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_project(ctx, config)
    set_cluster(ctx, config)
    ctx.run('kubectl config set-context $(kubectl config current-context) --namespace={namespace}'.format(
        namespace=config_dict['namespace']),
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
        bump = 'patch'

    increment = {'pre': semver.bump_prerelease,
                 'patch': semver.bump_patch,
                 'minor': semver.bump_minor,
                 'major': semver.bump_major}

    incremented = increment[bump](latest_tag)

    return incremented


@task
def latest_version(ctx):
    """Checks the git tags and returns the current latest version"""
    ctx.run('git fetch && git fetch --tags')
    result = ctx.run('git tag --sort=-v:refname', hide='both')
    tags = result.stdout.split('\n')

    regex = re.compile(r'^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-(0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(\.(0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*)?(\+[0-9a-zA-Z-]+(\.[0-9a-zA-Z-]+)*)?$')
    version_tags = filter(regex.search, tags)

    latest_tag = next(version_tags)

    if not latest_tag:
        raise ReleaseError('No tags found in repository')
    return latest_tag


# Kubernetes and GCloud Commands
################################
@task
def create_namespace(ctx, config):
    """
    Updates kubernetes deployment to use specified version
    """
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_project(ctx, config)
    set_cluster(ctx, config)

    ctx.run(
        'kubectl create namespace {namespace}'.format(
            namespace=config_dict['namespace']), echo=True)


@task
def upload_secrets(ctx, config, env_file):
    """
    Updates kubernetes deployment to use specified version
    """
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_context(ctx, config)

    ctx.run(
        'kubectl delete secret {project_name}'.format(
            project_name=config_dict['project_name'],
            namespace=config_dict['namespace']), warn=True)

    ctx.run(
        'kubectl create secret generic {project_name} --from-env-file {env_file}'.format(
            project_name=config_dict['project_name'],
            env_file=env_file))


@task
def create_volume(ctx, name, zone='europe-west1-c', size='100', type='pd-standard'):
    ctx.run('gcloud compute disks create {name} --zone {zone} --size {size} --type {type}'.format(name=name,
                                                                                                  size=size,
                                                                                                  zone=zone,
                                                                                                  type=type))


@task
def create_volume_claim(ctx, name, disk):
    ctx.run(
        'helm install --name {name} rehive/gce-persistent-volume --set volumeName={name},gcePersistentDiskName={disk},claimName={name}')


@task
def upload_static(ctx, config, bucket_name):
    """Upload static files to gcloud bucket"""
    set_context(ctx, config)
    ctx.run('echo "yes\n" | python src/manage.py collectstatic')
    ctx.run('gsutil -m rsync -d -r var/www/static gs://' + bucket_name + '/')


@task
def create_bucket(ctx, config, bucket_name):
    """Creates gcloud bucket for static files"""
    set_context(ctx, config)

    ctx.run('gsutil mb gs://{bucket_name}'.format(bucket_name=bucket_name), echo=True)


@task
def create_public_bucket(ctx, config, bucket_name):
    """Creates gcloud bucket for static files"""
    set_project(ctx, config)
    set_cluster(ctx, config)

    ctx.run('gsutil mb gs://{bucket_name}'.format(bucket_name=bucket_name))
    ctx.run('gsutil defacl set public-read gs://{bucket_name}'.format(bucket_name=bucket_name))


@task
def install(ctx, config):
    """
    Installs kubernetes deployment
    """

    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_context(ctx, config)

    ctx.run('helm repo add rehive https://rehive.github.io/charts', echo=True)
    ctx.run('helm install --name {project_name} '
            '-f {helm_values_path} '
            '{helm_chart} '
            '--version {helm_chart_version}'.format(project_name=config_dict['project_name'],
                                                    helm_values_path=config_dict['helm_values_path'],
                                                    helm_chart=config_dict['helm_chart'],
                                                    helm_chart_version=config_dict['helm_chart_version']),
            echo=True)


@task
def upgrade(ctx, config, version):
    """
    Upgrades kubernetes deployment
    """

    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_context(ctx, config)

    ctx.run('helm upgrade {project_name} '
            '{helm_chart} '
            '-f {helm_values_path} '
            '--set image.tag={version} '
            '--version {helm_chart_version}'.format(project_name=config_dict['project_name'],
                                                    helm_chart=config_dict['helm_chart'],
                                                    helm_values_path=config_dict['helm_values_path'],
                                                    version=version,
                                                    helm_chart_version=config_dict['helm_chart_version']),
            echo=True)

@task 
def live_image(ctx, config):
    """Displays the current docker image and version deployed"""
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_context(ctx, config)
    result = ctx.run('kubectl get deployment {project_name} --output=json'.format(project_name=config_dict['project_name']),
                        echo=True,
                        hide='stdout')
    server_config = json.loads(result.stdout)
    image = server_config['spec']['template']['spec']['containers'][0]['image']
    print(image)

@task
def proxy(ctx, config, port=8001):
    """Proxy in to the kubernetes API"""
    set_context(ctx, config)
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    ctx.run('kubectl proxy -p {port}'.format(port=port),
            echo=True)


@task
def bash(ctx, config):
    """Exec into the management container"""
    set_context(ctx, config)
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    ctx.run('kubectl exec -i -t {project_name}-management sh'.format(project_name=config_dict['project_name']),
            pty=True,
            echo=True)


@task
def manage(ctx, config, cmd):
    """Exec into the management container"""
    set_context(ctx, config)
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    ctx.run('kubectl exec -i -t {project_name}-management /venv/bin/python manage.py {cmd}'.format(
        project_name=config_dict['project_name'],
        cmd=cmd),
        pty=True,
        echo=True)


@task
def compose(ctx, cmd, tag):
    """Wrapper for docker-compose"""
    ctx.run('VERSION={tag} docker-compose {cmd}'.format(
        cmd=cmd,
        tag=tag),
        echo=True)


# Build commands
################
@task
def git_release(ctx, version_bump):
    """
    Bump version, push git tag
    N.B. Commit changes first
    """
    confirm('Did you remember to commit all changes? ')

    bumped_version = next_version(ctx, bump=version_bump)
    tag = bumped_version
    comment = 'Version ' + bumped_version

    # Create an push git tag:
    ctx.run("git tag '%s' -m '%s'" % (tag, comment), echo=True)
    ctx.run("git push origin %s" % tag, echo=True)

    print('Tag: {}\n'.format(tag))


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
     Build project's docker image using google cloud builder and pushes to remote repo
     """
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_project(ctx, config)
    image_name = config_dict['docker_image'].split(':')[0]
    ctx.run('gcloud builds submit . --config etc/docker/cloudbuild.yaml '
            '--substitutions _IMAGE={image_name},TAG_NAME={tag_name}'.format(image_name=image_name,
                                                                             tag_name=tag), echo=True)


@task
def cloudbuild_initial(ctx, config, tag):
    """
     Build project's docker image using google cloud builder and pushes to remote repo
     """
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_project(ctx, config)
    image_name = config_dict['docker_image'].split(':')[0]
    ctx.run('gcloud builds submit . --config etc/docker/cloudbuild-no-cache.yaml '
            '--substitutions _IMAGE={image_name},TAG_NAME={tag_name}'.format(image_name=image_name,
                                                                             tag_name=tag), echo=True)


# Utility functions
###################
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


def get_settings():
    """Import project settings"""
    with open('rdeploy.yaml', 'r') as stream:
        settings_dict = yaml.load(stream)

    return settings_dict


def confirm(prompt='Continue?\n', failure_prompt='User cancelled task'):
    '''
    Prompt the user to continue. Repeat on unknown response. Raise
    ParseError on negative response
    '''
    response = input(prompt)
    response_bool = False

    try:
        response_bool = strtobool(response)
    except ValueError:
        print('Unkown Response. Confirm with y, yes, t, true, on or 1; cancel with n, no, f, false, off or 0.')
        return confirm(prompt, failure_prompt)

    if not response_bool:
        raise ParseError(failure_prompt)


# Exceptions
############
def ReleaseError(Exception):
    pass
