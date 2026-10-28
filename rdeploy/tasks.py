import sys
import tarfile
import zipfile
import urllib.request
import io
import shutil
import threading
import time
import subprocess

from invoke import task
import semver
import json
import re

from packaging import version
from rdeploy.exceptions import ReleaseError

from rdeploy.utils import get_settings, confirm, get_helm_bin, get_helm_os_string, yaml_decode_data_fields, build_management_cmd

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


class _RolloutDisplay:
    """Pin a live status region to the bottom of a TTY while other output
    scrolls above it.

    The cursor arithmetic only stays consistent if every terminal write goes
    through this object, so helm's stdout/stderr are routed here as well (via
    invoke's out_stream/err_stream) and rendered above the pinned region.
    Uses plain ANSI: ESC[nA moves the cursor up n lines, ESC[K clears to the
    end of the line, ESC[J clears to the end of the screen.
    """

    def __init__(self, stream):
        self._stream = stream
        self._lock = threading.Lock()
        self._region = []
        self._drawn = 0

    def log(self, text):
        """Write scrolling output (complete lines) above the status region."""
        with self._lock:
            self._render(log_lines=text.splitlines())

    def update(self, lines):
        """Replace the pinned status region with the given lines."""
        with self._lock:
            self._region = list(lines)
            self._render()

    def _render(self, log_lines=()):
        # Cursor rests at column 0 just below the region; move to the
        # region's top, write the scrolling lines (overwriting old region
        # rows), redraw the region beneath them, and clear any leftover rows
        # from a previously taller region.
        if self._drawn:
            self._stream.write('\x1b[{n}A'.format(n=self._drawn))
        for line in log_lines:
            self._stream.write(line + '\x1b[K\n')
        # Region lines must not wrap or the cursor-up count breaks, so
        # truncate them to the terminal width. Scrolling lines may wrap
        # freely: they end up above the region either way.
        width = shutil.get_terminal_size((120, 24)).columns
        for line in self._region:
            self._stream.write(line[:width - 1] + '\x1b[K\n')
        self._stream.write('\x1b[J')
        self._stream.flush()
        self._drawn = len(self._region)


class _LogStream(io.TextIOBase):
    """File-like stream for invoke's out_stream/err_stream that forwards
    complete lines to a _RolloutDisplay, buffering partial lines so the
    display's line accounting stays exact."""

    def __init__(self, display):
        self._display = display
        self._buf = ''

    def write(self, s):
        self._buf += s
        if '\n' in self._buf:
            complete, self._buf = self._buf.rsplit('\n', 1)
            self._display.log(complete + '\n')
        return len(s)

    def flush(self):
        pass

    def drain(self):
        """Emit any trailing partial line (call once the command is done)."""
        if self._buf:
            self._display.log(self._buf + '\n')
            self._buf = ''


def _watch_rollout(stop_event, poll_interval=2, display=None):
    """Show live `kubectl get pods` progress while the atomic helm upgrade
    waits, so you can see pods being created, pulled, started and terminated
    instead of a silent wait.

    Pods (not deployments) are polled because for single-replica services the
    deployment counters are blind to almost the whole rollout: while the new
    pod is created and pulls its image the row reads 1/1, identical to steady
    state, and only blinks to 2/1 for the second or two between the new pod
    becoming ready and the old one being deleted. Pod rows change visibly for
    the entire rollout (ContainerCreating during the pull, Running,
    Terminating, CrashLoopBackOff on a bad release) and also surface helm
    hook pods such as migration jobs.

    With a display (stdout is a TTY) the whole pod table is pinned to the
    bottom of the terminal and updated in place every poll, while helm's own
    output scrolls above it. Without one (non-TTY, e.g. CI logs) it prints
    append-only lines: the table once as a baseline, then only rows whose
    READY/STATUS/RESTARTS change (ignoring the ticking AGE and restart-age
    columns).

    Runs in a background daemon thread and is fully decoupled from the
    upgrade: its output is informational only and its exit codes are ignored.
    Every poll reflects the cluster's current state -- pods created by the
    upgrade itself show up automatically, and there are no long-lived child
    processes to tear down: each poll is one short, bounded `kubectl get`.

    set_context() has already pointed kubectl at the right context and
    namespace. The whole body is wrapped in a broad except so a broken
    watcher (kubectl missing, permission error) can never raise into the
    main flow or change the exit code.
    """
    last_rows = {}
    header_printed = False
    failures = 0

    def _note(message):
        line = '[rollout] {message}'.format(message=message)
        if display is not None:
            display.update([])
            display.log(line + '\n')
        else:
            print(line, flush=True)

    try:
        while not stop_event.is_set():
            status = subprocess.run(['kubectl', 'get', 'pods',
                                     '--request-timeout=10s'],
                                    capture_output=True, text=True)
            if status.returncode == 0:
                failures = 0
                lines = status.stdout.splitlines()
                if display is not None:
                    # Live region: redraw the whole current table in place.
                    region = ['[rollout] {line}'.format(line=line)
                              for line in lines]
                    if region:
                        region.append('[rollout] (live view, updated {ts})'
                                      .format(ts=time.strftime('%H:%M:%S')))
                    display.update(region)
                else:
                    # Append-only: baseline table once, then changed rows.
                    to_print = []
                    if lines and not header_printed:
                        header_printed = True
                        to_print.append(lines[0])
                    for row in lines[1:]:
                        fields = row.split()
                        if not fields:
                            continue
                        # Compare only NAME READY STATUS RESTARTS so the
                        # ticking AGE and the '(Xm ago)' restart annotation
                        # don't reprint an otherwise unchanged row.
                        name, key = fields[0], ' '.join(fields[:4])
                        if last_rows.get(name) != key:
                            last_rows[name] = key
                            to_print.append(row)
                    for line in to_print:
                        print('[rollout] {line}'.format(line=line),
                              flush=True)
            else:
                # Tolerate transient API errors, but give up (quietly, with a
                # single note) if kubectl keeps failing.
                failures += 1
                if failures >= 3:
                    error = (status.stderr or '').strip().splitlines()
                    _note('progress display unavailable: {err}'
                          .format(err=error[0] if error else
                                  'kubectl exited with code {rc}'
                                  .format(rc=status.returncode)))
                    return
            stop_event.wait(poll_interval)
    except Exception as err:
        _note('progress display unavailable: {err}'.format(err=err))


@task
def upgrade(ctx, config, tag, atomic=True, timeout='15m', progress=True):
    """
    Upgrades kubernetes deployment

    By default the upgrade runs atomically: helm waits for the rollout to
    complete and rolls back to the previous release if it fails, and the
    task exits with an error. Use --no-atomic to restore the old
    fire-and-forget behaviour, and --timeout to change how long helm waits.

    While the atomic upgrade waits, a live progress display polls
    `kubectl get pods` and shows the pod status table (prefixed with
    [rollout]), so you can watch pods being created, pulled, started and
    terminated instead of a silent wait. On a terminal the table is pinned
    to the bottom of the screen and updates in place while helm's output
    scrolls above it; on a non-TTY it prints the table once, then any row
    that changes. This is purely informational and never affects the
    result; use --no-progress to disable it.
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

    atomic_flags = ' --atomic --timeout {timeout}'.format(timeout=timeout) if atomic else ''

    # Only watch when there is an atomic wait to watch (--no-atomic returns
    # immediately) and progress display is enabled.
    show_progress = atomic and progress
    stop_event = threading.Event()
    watcher = None
    display = None
    run_streams = {}
    if show_progress:
        if sys.stdout.isatty():
            # Live view: the pod table is pinned to the bottom of the
            # terminal and helm's output scrolls above it. Helm's streams
            # must be routed through the display for the cursor arithmetic
            # to hold, so hand invoke our own out/err streams.
            display = _RolloutDisplay(sys.stdout)
            run_streams = {'out_stream': _LogStream(display),
                           'err_stream': _LogStream(display)}
        watcher = threading.Thread(target=_watch_rollout, args=(stop_event,),
                                   kwargs={'display': display}, daemon=True)

    helm_command = ('{helm_bin} upgrade {project_name} '
                    '--values {helm_values_path} '
                    '--set image.tag={version} '
                    '--version {helm_chart_version}{atomic_flags} {helm_chart}'
                    .format(helm_bin=helm_bin,
                            project_name=config_dict['project_name'],
                            helm_chart=helm_chart,
                            helm_values_path=config_dict['helm_values_path'],
                            version=tag,
                            helm_chart_version=config_dict['helm_chart_version'],
                            atomic_flags=atomic_flags))

    # The helm call below is the sole source of truth for success/failure/
    # rollback. The watcher runs concurrently and is stopped in finally
    # whether helm succeeds or rolls back; its output never influences result.
    try:
        if watcher is not None:
            watcher.start()

        if display is not None:
            # invoke's echo writes straight to stdout, which would race with
            # the display's cursor arithmetic; echo through the display
            # instead (same bold formatting as invoke).
            display.log('\033[1;37m{cmd}\033[0m\n'.format(cmd=helm_command))
        result = ctx.run(helm_command, echo=display is None, warn=True,
                         **run_streams)
    finally:
        stop_event.set()
        if watcher is not None:
            # A poll in flight finishes within kubectl's --request-timeout,
            # so this join is bounded and leaves no child processes behind.
            watcher.join(timeout=15)
        for stream in run_streams.values():
            stream.drain()

    if result.failed:
        if atomic:
            sys.exit('Upgrade of {project_name} to {version} failed.'
                     ' Helm rolled the release back to the previous version.'
                     .format(project_name=config_dict['project_name'],
                             version=tag))
        else:
            sys.exit('Upgrade of {project_name} to {version} failed.'
                     .format(project_name=config_dict['project_name'],
                             version=tag))


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

    os_string = get_helm_os_string()
    if sys.platform == 'win32':
        archive_tool = zipfile
    else:
        archive_tool = tarfile

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
