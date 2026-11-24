"""
Download Helm charts from Google Artifact Registry through gcloud.

Helm logs in to an OCI registry with a bearer token. When the organization
enforces certificate-based access, Artifact Registry rejects that token,
because Helm cannot present the device certificate. gcloud can, so rdeploy
downloads the chart with gcloud and hands Helm the local archive.

A chart in Artifact Registry is an OCI artifact: the tag points to a manifest,
and the manifest names one layer, the chart's .tgz. Both are checked against
their sha256 digests before Helm sees them.
"""
import atexit
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

from rdeploy.gke_auth import GcloudLoginError, ensure_gcloud_login

CHART_LAYER_MEDIA_TYPE = 'application/vnd.cncf.helm.chart.content.v1.tar+gzip'
REGISTRY_HOST = re.compile(r'^(?:https?://)?(?P<location>[a-z0-9-]+)-docker\.pkg\.dev/?$')


class ChartFetchError(Exception):
    pass


def registry_location(helm_registry):
    """`europe-west4-docker.pkg.dev` -> `europe-west4`."""
    match = REGISTRY_HOST.match(helm_registry.strip())
    if not match:
        raise ChartFetchError(
            'helm_registry {registry!r} is not an Artifact Registry Docker host'
            ' (LOCATION-docker.pkg.dev).'.format(registry=helm_registry))
    return match.group('location')


def split_chart(helm_chart):
    """`my-repo/my-chart` -> (`my-repo`, `my-chart`)."""
    repository, _, package = helm_chart.strip('/').partition('/')
    if not repository or not package:
        raise ChartFetchError(
            'helm_chart {chart!r} must be REPOSITORY/CHART when helm_registry'
            ' is set.'.format(chart=helm_chart))
    return repository, package


def run_gcloud(cmd):
    """
    Run gcloud, capturing only its output. Prompts and errors go to the
    terminal, so gcloud can ask for reauthentication.
    """
    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise ChartFetchError('gcloud failed: {cmd}'.format(cmd=' '.join(cmd)))
    return result.stdout


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 16), b''):
            digest.update(block)
    return 'sha256:' + digest.hexdigest()


def resolve_digest(host, project, repository, package, chart_version):
    """
    Return the manifest digest the chart's version tag points to.

    This lists the package's versions rather than running `gcloud artifacts
    docker images describe`, which always queries Container Analysis as well
    and so needs `containeranalysis.occurrences.list`, a permission deployers
    may not have. Listing only needs read access to Artifact Registry.
    """
    package_path = '{host}/{project}/{repository}/{package}'.format(
        host=host, project=project, repository=repository, package=package)
    output = run_gcloud([
        'gcloud', 'artifacts', 'docker', 'images', 'list', package_path,
        '--include-tags',
        '--format=json(version,tags)',
    ])
    try:
        images = json.loads(output or '[]')
    except ValueError:
        raise ChartFetchError('Could not read the versions of {package}.'.format(
            package=package_path))
    for image in images:
        tags = image.get('tags') or []
        if isinstance(tags, str):
            tags = [tag.strip() for tag in tags.split(',')]
        if chart_version in tags:
            digest = image.get('version') or ''
            if digest.startswith('sha256:'):
                return digest
    raise ChartFetchError('Could not resolve {package}:{version} to a digest.'.format(
        package=package_path, version=chart_version))


def download_file(project, location, repository, name, destination, filename):
    run_gcloud([
        'gcloud', 'artifacts', 'files', 'download', name,
        '--project={0}'.format(project),
        '--location={0}'.format(location),
        '--repository={0}'.format(repository),
        '--destination={0}'.format(destination),
        '--local-filename={0}'.format(filename),
        '--allow-overwrite',
        # No progress boxes; gcloud's errors still print.
        '--no-user-output-enabled',
    ])
    return os.path.join(destination, filename)


def verified(path, digest, what):
    actual = sha256_file(path)
    if actual != digest:
        raise ChartFetchError(
            '{what} digest mismatch: expected {expected}, got {actual}.'
            .format(what=what, expected=digest, actual=actual))
    return path


def chart_layer_digest(manifest_path):
    with open(manifest_path) as f:
        manifest = json.load(f)
    layers = [layer for layer in manifest.get('layers', [])
              if layer.get('mediaType') == CHART_LAYER_MEDIA_TYPE]
    if len(layers) != 1:
        raise ChartFetchError(
            'Expected one Helm chart layer in the manifest, found {count}.'
            .format(count=len(layers)))
    return layers[0]['digest']


def fetch_chart(helm_registry, project, helm_chart, chart_version, destination=None):
    """
    Download `helm_chart` at `chart_version` from Artifact Registry with
    gcloud and return the path of the verified chart archive.

    Without `destination`, the archive goes to a temporary directory that is
    removed when rdeploy exits.
    """
    location = registry_location(helm_registry)
    host = '{location}-docker.pkg.dev'.format(location=location)
    repository, package = split_chart(helm_chart)

    if destination is None:
        destination = tempfile.mkdtemp(prefix='rdeploy-chart-')
        atexit.register(shutil.rmtree, destination, True)

    # Before anything else, so an expired login is caught while nothing has
    # happened yet, and the user is asked before a browser opens.
    try:
        ensure_gcloud_login(ask=True)
    except GcloudLoginError as err:
        raise ChartFetchError(str(err))

    print('Downloading chart {chart} {version} with gcloud'.format(
        chart=helm_chart, version=chart_version), flush=True)

    manifest_digest = resolve_digest(host, project, repository, package, chart_version)
    manifest = verified(
        download_file(project, location, repository,
                      '{package}/manifests/{digest}'.format(package=package, digest=manifest_digest),
                      destination, 'manifest.json'),
        manifest_digest, 'Manifest')

    layer_digest = chart_layer_digest(manifest)
    filename = '{name}-{version}.tgz'.format(name=package.rsplit('/', 1)[-1],
                                             version=chart_version)
    return verified(
        download_file(project, location, repository, layer_digest, destination, filename),
        layer_digest, 'Chart')
