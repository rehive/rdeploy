import json
import os
import re
import semver

from invoke import task
import semver
import json
import re
from packaging import version
from invoke.exceptions import ParseError
from rdeploy.exceptions import ReleaseError
from rdeploy.utils import get_settings, confirm


# Cluster Activation:
#####################
@task
def set_project(ctx, config):
    """Sets the active gcloud project"""
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    if version.parse(str(settings_dict['version'])) > version.parse('1'):
        provider_data = config_dict.get('cloud_provider')
        if provider_data and provider_data['name'] == 'azure':
            ctx.run('az account set -s {subscription}'
                .format(subscription=provider_data['subscription_id']), echo=True)
        elif provider_data and provider_data['name'] == 'gcp':
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

    if version.parse(str(settings_dict['version'])) > version.parse('1'):
        provider_data = config_dict.get('cloud_provider')
        if provider_data and provider_data['name'] == 'azure':
            ctx.run('az aks get-credentials -g {group} -n {cluster} --context aks-{region}-{cluster} --overwrite-existing'
                    .format(group=provider_data['resource_group'],
                            cluster=provider_data['kube_cluster'],
                            region=provider_data['region'],
                            ), echo=True)

        if provider_data and provider_data['name'] == 'gcp':
            if provider_data.get('zone'):
                zone_or_region_param = '--zone {}'.format(config_dict['cloud_zone'])
            elif provider_data.get('region'):
                zone_or_region_param = '--region {}'.format(config_dict['cloud_region'])

            ctx.run('gcloud container clusters get-credentials {cluster}'
                    ' --project {project} {zone_or_region_param}'
                    .format(cluster=provider_data['kube_cluster'],
                            project=provider_data['project'],
                            zone_or_region_param=zone_or_region_param),
                    echo=True)

    
    else:
        if config_dict.get('cloud_zone'):
            zone_or_region_param = '--zone {}'.format(config_dict['cloud_zone'])
        elif config_dict.get('cloud_region'):
            zone_or_region_param = '--region {}'.format(config_dict['cloud_region'])
        else:
            zone_or_region_param = '--zone europe-west1-c'

        ctx.run('gcloud container clusters get-credentials {cluster}'
                ' --project {project} {zone_or_region_param}'
                .format(cluster=config_dict['cluster'],
                        project=config_dict['cloud_project'],
                        zone_or_region_param=zone_or_region_param),
                echo=True)


@task
def set_context(ctx, config):
    """Sets active project, cluster and namespace"""
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_project(ctx, config)
    set_cluster(ctx, config)
    ctx.run('kubectl config set-context $(kubectl config current-context)'
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

    increment = {
        'build': semver.bump_build,
        'pre': semver.bump_prerelease,
        'patch': semver.bump_patch,
        'minor': semver.bump_minor,
        'major': semver.bump_major
    }

    incremented = increment[bump](latest_tag)

    return incremented


@task
def latest_version(ctx):
    """Checks the git tags and returns the current latest version"""
    ctx.run('git fetch --tags')
    result = ctx.run('git tag --sort=-v:refname', hide='both')
    tags = result.stdout.split('\n')

    regex = re.compile(r'^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)'\
                       '(-(0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)'\
                       '(\.(0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*)'\
                       '?(\+[0-9a-zA-Z-]+(\.[0-9a-zA-Z-]+)*)?$')

    version_tags = filter(regex.search, tags)
    try:
        latest_tag = next(version_tags)
        return latest_tag
    except StopIteration:
        raise ReleaseError('No valid semver tags found in repository')


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

    ctx.run('kubectl create namespace {namespace}'
            .format(namespace=config_dict['namespace']),
            echo=True)


@task
def upload_secrets(ctx, config, env_file):
    """
    Updates kubernetes deployment to use specified version
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
def create_volume(ctx, name,
                  zone='europe-west1-c',
                  size='100',
                  type='pd-standard'):
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
    """Creates gcloud bucket for static files"""
    set_project(ctx, config)

    ctx.run('gsutil mb gs://{bucket_name}'
            .format(bucket_name=bucket_name), echo=False)
    ctx.run('gsutil defacl set private gs://{bucket_name}'
            .format(bucket_name=bucket_name), echo=False)



@task
def create_public_bucket(ctx, config, bucket_name):
    """Creates gcloud bucket for static files"""
    set_project(ctx, config)

    ctx.run('gsutil mb -b on gs://{bucket_name}'.format(bucket_name=bucket_name))
    ctx.run('gsutil iam ch allUsers:objectViewer gs://{bucket_name}'
            .format(bucket_name=bucket_name))


@task
def template_install(ctx, config):
    """
    Installs kubernetes deployment from a helm template
    """

    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_context(ctx, config)

    repository = 'https://rehive.github.io/charts'
    chart_directory = "var/helm/chart/{config}".format(config=config)
    manifest_directory = "var/helm/manifests/{config}".format(config=config)
    log_directory = "var/helm/log/{config}".format(config=config)
    err_file = "{log_dir}/template_install.error.log".format(
        log_dir=log_directory)
    out_file = "{log_dir}/template_install.out.log".format(
        log_dir=log_directory)
    chart_name = config_dict["helm_chart"]

    if len(chart_name.split('/')) > 0:
        chart_name = chart_name.spliat('/')[1]

    ctx.run('mkdir -p {chart_dir} {manifest_dir} {log_dir}'
            .format(chart_dir=chart_directory,
                    manifest_dir=manifest_directory,
                    log_dir=log_directory),
            echo=False)

    ctx.run('helm fetch --repo {repository} --untar '
            '--untardir {chart_dir} '
            '--version {version} {chart_name}'
            .format(repository=repository, chart_dir=chart_directory,
                    version=config_dict['helm_chart_version'],
                    chart_name=chart_name),
            echo=True)

    ctx.run('helm template {chart_dir}/{chart_name} '
            '--values {values_file} '
            '--name {release_name} '
            '--namespace {namespace} '
            '--output-dir {manifest_dir} '
            .format(chart_dir=chart_directory,
                    chart_name=chart_name,
                    manifest_dir=manifest_directory,
                    namespace=config_dict['namespace'],
                    release_name=config_dict['project_name'],
                    values_file=config_dict['helm_values_path']),
            err_stream=open(err_file, 'w'),
            out_stream=open(out_file, 'w'), echo=True)

    ctx.run('kubectl create namespace {namespace} || '
            'kubectl apply --recursive '
            '--filename {manifest_dir}/{chart_name} '
            '--namespace {namespace} '
            .format(chart_name=chart_name,
                    manifest_dir=manifest_directory,
                    namespace=config_dict['namespace']),
            echo=True)

@task
def install(ctx, config):
    """
    Installs kubernetes deployment
    """
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_context(ctx, config)

    install_flag = ''
    if str(config_dict.get('helm_version')) != '3':
        install_flag = " --name"

    ctx.run('helm repo add rehive https://rehive.github.io/charts', echo=True)
    ctx.run('helm install{helm_install_flag} {project_name} '
            '--values {helm_values_path} '
            '--version {helm_chart_version} {helm_chart}'
            .format(project_name=config_dict['project_name'],
                    helm_install_flag=install_flag,
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
            '--values {helm_values_path} '
            '--set image.tag={version} '
            '--version {helm_chart_version} {helm_chart}'
            .format(project_name=config_dict['project_name'],
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

    result = ctx.run('kubectl get deployment {project_name} --output=json'
                     .format(project_name=config_dict['project_name']),
                     echo=True, hide='stdout')
    server_config = json.loads(result.stdout)
    image = server_config['spec']['template']['spec']['containers'][0]['image']
    print(image)


@task
def bash(ctx, config):
    """Exec into the management container"""
    set_context(ctx, config)
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    ctx.run('kubectl exec -i -t {project_name}-management -- '
            '/bin/sh -c "/bin/bash || /bin/sh"'
            .format(project_name=config_dict['project_name']),
            pty=True, warn=False, echo=True)


@task
def manage(ctx, config, cmd):
    """Exec into the management container"""
    set_context(ctx, config)
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    ctx.run('kubectl exec -i -t {project_name}-management'
            ' -- python  manage.py {cmd}'
            .format(project_name=config_dict['project_name'], cmd=cmd),
            pty=True, echo=True)


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

    bumped_version = next_version(ctx, bump=version_bump)
    tag = bumped_version
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
    Build project's docker image using google cloud builder and pushes to remote repo
    """
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_project(ctx, config)
    image_name = config_dict['docker_image'].split(':')[0]

    if version.parse(str(settings_dict['version'])) > version.parse('1'):
        provider_data = config_dict.get('cloud_provider')
        if provider_data and provider_data['name'] == 'azure':
            ctx.run('az acr run'
                ' -r {container_registry}'
                ' -f ./etc/docker/acr.yaml'
                ' --set IMAGE={image_name}'
                ' --set TAG_NAME={tag_name}'
                ' .'
                .format(container_registry=provider_data['container_registry'],
                        image_name=image_name, 
                        tag_name=tag), echo=True)
        else:
            log_dir = "gs://{project}-cloudbuild-logs/{image}/{tag_name}/".format(
            project=provider_data['project'], image=image_name, tag_name=tag)
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
            ' --config etc/docker/cloudbuild.yaml'
            ' --substitutions _IMAGE={image_name},TAG_NAME={tag_name}'
            ' --gcs-log-dir {log_dir}'
            .format(image_name=image_name, tag_name=tag, log_dir=log_dir),
            echo=True)
        

@task
def cloudbuild_initial(ctx, config, tag):
    """
    Build project's docker image using google cloud builder and pushes to remote repo
    """
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_project(ctx, config)
    image_name = config_dict['docker_image'].split(':')[0]

    if version.parse(str(settings_dict['version'])) > version.parse('1'):
        provider_data = config_dict.get('cloud_provider')
        if provider_data and provider_data['name'] == 'azure':
            ctx.run('az acr run'
                ' -r {container_registry}'
                ' -f ./etc/docker/acr-no-cache.yaml'
                ' --set IMAGE={image_name}'
                ' --set TAG_NAME={tag_name}'
                ' .'
                .format(container_registry=provider_data['container_registry'],
                        image_name=image_name, 
                        tag_name=tag), echo=True)
        else:
            log_dir = "gs://{project}-cloudbuild-logs/{image}/{tag_name}/".format(
            project=provider_data['project'], image=image_name, tag_name=tag)
            ctx.run('gcloud builds submit .'
                    ' --config etc/docker/cloudbuild-no-cache.yaml'
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
        settings_dict = yaml.load(stream, Loader=yaml.FullLoader)

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
        print('Confirm with y, yes, t, true, on or 1; '
              'cancel with n, no, f, false, off or 0.')
        return confirm(prompt, failure_prompt)

    if not response_bool:
        raise ParseError(failure_prompt)


# Exceptions
############
class ReleaseError(BaseException):
    pass

