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

    if settings_dict.get('version') and version.parse(str(settings_dict['version'])) > version.parse('1'):
        provider_data = config_dict.get('cloud_provider')
        if provider_data['name'] == 'azure':
            ctx.run('az aks get-credentials -g {group} -n {cluster} --context aks-{region}-{cluster} --context {name}_{cluster}_{region}  --overwrite-existing'
                    .format(group=provider_data['resource_group'],
                            cluster=provider_data['kube_cluster'],
                            region=provider_data['region'],
                            name=provider_data['name']
                            ), echo=True)
        elif provider_data['name'] == 'gcp':
            if provider_data.get('zone'):
                zone_or_region_param = '--zone {}'.format(provider_data['zone'])
                zone = provider_data['zone']
            elif provider_data.get('region'):
                zone_or_region_param = '--region {}'.format(provider_data['region'])
                zone = provider_data['region']

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
            sys.exit(f"Unsupported provider: {provider_data['name']}")
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


@task(aliases=['set-context'])
def set_context(ctx, config):
    """Switch cluster and namespace"""
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    provider_data = config_dict.get('cloud_provider')

    if settings_dict.get('version') == '1' or settings_dict.get('version') == 1:
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


    elif settings_dict.get('version') == '2' or settings_dict.get('version') == 2:
        if provider_data.get('name') == 'gcp':
            if provider_data.get('zone') is not None:
                get_zone = provider_data['zone']
            elif provider_data.get('region') is not None:
                get_zone = provider_data['region']

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

        elif provider_data.get('name') == 'azure':
            ctx.run('kubectl config use-context {name}_{cluster}_{region}'
                ' --namespace={namespace}'
                .format(namespace=config_dict['namespace'],
                        cluster=provider_data['kube_cluster'],
                        name=provider_data['name'],
                        region=provider_data['region']),
                echo=True)
            ctx.run('kubectl config set-context --current'
                ' --namespace={namespace}'
                .format(namespace=config_dict['namespace']),
                echo=True)
        else:
            sys.exit(f"Invalid provider name in rdeploy file: {provider_data.get('name')}")
    else:
        sys.exit(f"Invalid rdeploy version in rdeploy file: {settings_dict.get('version')}")


# Versioning Helpers
####################
@task(aliases=['next-version'])
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


@task(aliases=['latest-version'])
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
@task(aliases=['create-namespace'])
def create_namespace(ctx, config):
    """
    Updates kubernetes deployment to use specified version
    """
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_context(ctx, config)

    ctx.run('kubectl create namespace {namespace}'
            .format(namespace=config_dict['namespace']),
            echo=True)


@task(aliases=['upload-secrets'])
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


@task(aliases=['decode-secret'])
def decode_secret(ctx, config, secret):
    """
    Prints the decoded values of a kubernetes secret
    """
    set_context(ctx, config)
    o = io.StringIO()
    ctx.run('kubectl get secret {secret} -o yaml'.format(secret=secret),out_stream=o)
    print(yaml_decode_data_fields(o.getvalue()))


@task(aliases=['create-volume'])
def create_volume(ctx, name,
                  zone='europe-west1-c',
                  size='100',
                  type='pd-standard'):
    ctx.run('gcloud compute disks create {name}'
            ' --zone {zone} --size {size} --type {type}'
            .format(name=name, size=size, zone=zone, type=type))


@task(aliases=['upload-static'])
def upload_static(ctx, config, bucket_name):
    """Upload static files to gcloud bucket"""
    set_project(ctx, config)

    ctx.run('echo "yes\n" | python src/manage.py collectstatic')
    ctx.run('gsutil -m rsync -d -r var/www/static gs://{bucket_name}'
            .format(bucket_name=bucket_name), echo=False)


@task(aliases=['create-bucket'])
def create_bucket(ctx, config, bucket_name):
    """Creates gcloud bucket for static files"""
    set_project(ctx, config)

    ctx.run('gsutil mb gs://{bucket_name}'
            .format(bucket_name=bucket_name), echo=False)
    ctx.run('gsutil defacl set private gs://{bucket_name}'
            .format(bucket_name=bucket_name), echo=False)



@task(aliases=['create-public-bucket'])
def create_public_bucket(ctx, config, bucket_name):
    """Creates gcloud bucket for static files"""
    set_project(ctx, config)

    ctx.run('gsutil mb -b on gs://{bucket_name}'.format(bucket_name=bucket_name))
    ctx.run('gsutil iam ch allUsers:objectViewer gs://{bucket_name}'
            .format(bucket_name=bucket_name))


@task(aliases=['template-install'])
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

    helm_bin = get_helm_bin(config_dict)

    install_flag = ''

    if config_dict.get('helm_version') and version.parse(str(config_dict['helm_version'])) <= version.parse('3'):
        install_flag = " --name"

    ctx.run('{helm_bin} repo add rehive https://rehive.github.io/charts'.format(helm_bin=helm_bin), echo=True)
    ctx.run('{helm_bin} install{helm_install_flag} {project_name} '
            '--values {helm_values_path} '
            '--version {helm_chart_version} {helm_chart}'
            .format(helm_bin=helm_bin,
                    project_name=config_dict['project_name'],
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

    helm_bin = get_helm_bin(config_dict)

    ctx.run('{helm_bin} upgrade {project_name} '
            '--values {helm_values_path} '
            '--set image.tag={version} '
            '--version {helm_chart_version} {helm_chart}'
            .format(helm_bin=helm_bin,
                    project_name=config_dict['project_name'],
                    helm_chart=config_dict['helm_chart'],
                    helm_values_path=config_dict['helm_values_path'],
                    version=version,
                    helm_chart_version=config_dict['helm_chart_version']),
            echo=True)


@task
def helm(ctx, config, command):
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    set_context(ctx, config)

    helm_bin = get_helm_bin(config_dict)

    ctx.run('{helm_bin} {command}'.format(helm_bin=helm_bin,
                                          command=command),
            echo=True)


@task(aliases=['helm-setup'])
def helm_setup(ctx, config):
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
    ctx.run('{helm_bin} repo add stable https://charts.helm.sh/stable'.format(helm_bin=helm_bin), echo=True)
    ctx.run('{helm_bin} repo add rehive https://rehive.github.io/charts'.format(helm_bin=helm_bin), echo=True)

    print('Successfully installed helm to opt/helm-v{version}/{os_string}/ \n'
          'Please make sure this directory has been added to .gitignore.'. format(version=helm_version,
                                                                                  os_string=os_string))


@task(aliases=['live-image'])
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


@task(aliases=['bash'])
def shell(ctx, config):
    """Exec into the management container"""
    set_context(ctx, config)
    settings_dict = get_settings()
    config_dict = settings_dict['configs'][config]
    management_cmd = build_management_cmd(config_dict, "/bin/bash")
    ctx.run(management_cmd, pty=True, warn=False, echo=True)


@task
def manage(ctx, config, cmd, tag=None):
    """Exec into the management container"""
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
@task(aliases=['git-release'])
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

    if settings_dict.get('version') and version.parse(str(settings_dict['version'])) > version.parse('1'):
        provider_data = config_dict.get('cloud_provider')

        def azure_image_build(container_registry, image_name, tag):
            ctx.run('az acr run'
                ' -r {container_registry}'
                ' -f ./etc/docker/acr.yaml'
                ' --set IMAGE={image_name}'
                ' --set TAG_NAME={tag_name}'
                ' .'
                .format(container_registry=container_registry,
                        image_name=image_name,
                        tag_name=tag), echo=True)

        def google_image_build(project, image_name, tag):
            log_dir = "gs://{project}-cloudbuild-logs/{image}/{tag_name}/".format(
            project=project, image=image_name, tag_name=tag)
            ctx.run('gcloud builds submit .'
                ' --config etc/docker/cloudbuild.yaml'
                ' --substitutions _IMAGE={image_name},TAG_NAME={tag_name}'
                ' --gcs-log-dir {log_dir}'
                .format(image_name=image_name, tag_name=tag, log_dir=log_dir),
                echo=True)

        if config_dict.get('container_registry_provider') == 'azure':
            azure_image_build(provider_data['container_registry'], image_name, tag)

        elif config_dict.get('container_registry_provider') == 'google':
            project = config_dict['docker_image'].split('/')[1]
            google_image_build(project, image_name, tag)

        else:
            if provider_data and provider_data['name'] == 'azure':
                azure_image_build(provider_data['container_registry'], image_name, tag)
            else:
                google_image_build(provider_data['project'], image_name, tag)

    else:
        log_dir = "gs://{project}-cloudbuild-logs/{image}/{tag_name}/".format(
        project=config_dict['cloud_project'], image=image_name, tag_name=tag)
        ctx.run('gcloud builds submit .'
                ' --config etc/docker/cloudbuild-no-cache.yaml'
                ' --substitutions _IMAGE={image_name},TAG_NAME={tag_name}'
                ' --gcs-log-dir {log_dir}'
                .format(image_name=image_name, tag_name=tag, log_dir=log_dir),
                echo=True)
