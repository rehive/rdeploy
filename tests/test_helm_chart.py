import hashlib
import json
import os
from unittest.mock import MagicMock, patch

import pytest
from invoke import Context

from rdeploy import helm_chart
from rdeploy.helm_chart import (
    CHART_LAYER_MEDIA_TYPE,
    ChartFetchError,
    chart_layer_digest,
    fetch_chart,
    registry_location,
    split_chart,
)
from rdeploy.gke_auth import GcloudLoginError
from rdeploy.tasks import install, resolve_helm_chart, upgrade

CHART = b'fake chart archive'
CHART_DIGEST = 'sha256:' + hashlib.sha256(CHART).hexdigest()
MANIFEST = json.dumps({
    'schemaVersion': 2,
    'config': {'mediaType': 'application/vnd.cncf.helm.config.v1+json',
               'digest': 'sha256:' + 'c' * 64, 'size': 10},
    'layers': [{'mediaType': CHART_LAYER_MEDIA_TYPE,
                'digest': CHART_DIGEST, 'size': len(CHART)}],
}).encode()
MANIFEST_DIGEST = 'sha256:' + hashlib.sha256(MANIFEST).hexdigest()


class FakeGcloud:
    """Stands in for gcloud: resolves the tag and serves the stored files."""

    def __init__(self, files=None, digest=MANIFEST_DIGEST, tags=('1.2.0',), fail=False):
        self.files = files if files is not None else {
            'rehive-service/manifests/' + MANIFEST_DIGEST: MANIFEST,
            CHART_DIGEST: CHART,
        }
        self.digest = digest
        self.tags = list(tags)
        self.fail = fail
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if self.fail:
            return MagicMock(returncode=1, stdout='')
        if cmd[1:5] == ['artifacts', 'docker', 'images', 'list']:
            images = [
                {'version': 'sha256:' + 'a' * 64, 'tags': ['1.2.0-rc1', '1.1.0']},
                {'version': self.digest, 'tags': self.tags},
            ]
            return MagicMock(returncode=0, stdout=json.dumps(images))
        if cmd[1:4] == ['artifacts', 'files', 'download']:
            name = cmd[4]
            flags = dict(arg[2:].split('=', 1) for arg in cmd[5:] if '=' in arg)
            if name not in self.files:
                return MagicMock(returncode=1, stdout='')
            with open(os.path.join(flags['destination'], flags['local-filename']), 'wb') as f:
                f.write(self.files[name])
            return MagicMock(returncode=0, stdout='')
        raise AssertionError('unexpected gcloud call: {}'.format(cmd))


def fetch(tmp_path, gcloud):
    with patch('rdeploy.helm_chart.subprocess.run', side_effect=gcloud):
        return fetch_chart('europe-west4-docker.pkg.dev', 'my-project',
                           'my-charts/rehive-service', '1.2.0',
                           destination=str(tmp_path))


class TestParsing:
    @pytest.mark.parametrize('registry', [
        'europe-west4-docker.pkg.dev',
        'https://europe-west4-docker.pkg.dev',
        'europe-west4-docker.pkg.dev/',
    ])
    def test_registry_location(self, registry):
        assert registry_location(registry) == 'europe-west4'

    def test_multi_region_location(self):
        assert registry_location('us-docker.pkg.dev') == 'us'

    @pytest.mark.parametrize('registry', ['gcr.io', 'ghcr.io', 'europe-west4-npm.pkg.dev'])
    def test_rejects_other_registries(self, registry):
        with pytest.raises(ChartFetchError):
            registry_location(registry)

    def test_split_chart(self):
        assert split_chart('my-charts/rehive-service') == ('my-charts', 'rehive-service')

    def test_split_chart_needs_repository(self):
        with pytest.raises(ChartFetchError):
            split_chart('rehive-service')

    def test_chart_layer_digest(self, tmp_path):
        path = tmp_path / 'manifest.json'
        path.write_bytes(MANIFEST)
        assert chart_layer_digest(str(path)) == CHART_DIGEST

    def test_manifest_without_chart_layer(self, tmp_path):
        path = tmp_path / 'manifest.json'
        path.write_text(json.dumps({'layers': [{'mediaType': 'other', 'digest': 'x'}]}))
        with pytest.raises(ChartFetchError, match='found 0'):
            chart_layer_digest(str(path))


@pytest.fixture
def gcloud_signed_in():
    with patch('rdeploy.helm_chart.ensure_gcloud_login') as ensure:
        yield ensure


@pytest.mark.usefixtures('gcloud_signed_in')
class TestFetchChart:
    def test_downloads_and_verifies_chart(self, tmp_path):
        gcloud = FakeGcloud()
        path = fetch(tmp_path, gcloud)
        assert path == str(tmp_path / 'rehive-service-1.2.0.tgz')
        with open(path, 'rb') as f:
            assert f.read() == CHART

    def test_gcloud_calls(self, tmp_path):
        gcloud = FakeGcloud()
        fetch(tmp_path, gcloud)
        listing, manifest, chart = gcloud.calls
        assert listing[5] == 'europe-west4-docker.pkg.dev/my-project/my-charts/rehive-service'
        assert '--include-tags' in listing
        assert manifest[4] == 'rehive-service/manifests/' + MANIFEST_DIGEST
        assert chart[4] == CHART_DIGEST
        for call in (manifest, chart):
            assert '--project=my-project' in call
            assert '--location=europe-west4' in call
            assert '--repository=my-charts' in call

    def test_never_asks_gcloud_for_a_token(self, tmp_path):
        gcloud = FakeGcloud()
        fetch(tmp_path, gcloud)
        assert not any('print-access-token' in call for call in gcloud.calls)

    def test_gcloud_prompts_reach_the_terminal(self, tmp_path):
        with patch('rdeploy.helm_chart.subprocess.run', side_effect=FakeGcloud()) as run:
            fetch_chart('europe-west4-docker.pkg.dev', 'my-project',
                        'my-charts/rehive-service', '1.2.0', destination=str(tmp_path))
        for call in run.call_args_list:
            assert 'stderr' not in call.kwargs
            assert 'stdin' not in call.kwargs

    def test_downloads_are_quiet(self, tmp_path):
        gcloud = FakeGcloud()
        fetch(tmp_path, gcloud)
        for call in gcloud.calls[1:]:
            assert '--no-user-output-enabled' in call

    def test_checks_gcloud_login_first_and_asks(self, tmp_path, gcloud_signed_in):
        gcloud = FakeGcloud()
        gcloud_signed_in.side_effect = lambda **kwargs: gcloud.calls.append('login check')
        fetch(tmp_path, gcloud)
        gcloud_signed_in.assert_called_once_with(ask=True)
        assert gcloud.calls[0] == 'login check'

    def test_declined_login_stops_before_downloading(self, tmp_path, gcloud_signed_in):
        gcloud = FakeGcloud()
        gcloud_signed_in.side_effect = GcloudLoginError('Run `gcloud auth login`, then run this command again.')
        with pytest.raises(ChartFetchError, match='gcloud auth login'):
            fetch(tmp_path, gcloud)
        assert gcloud.calls == []

    def test_rejects_tampered_chart(self, tmp_path):
        gcloud = FakeGcloud()
        gcloud.files[CHART_DIGEST] = b'something else'
        with pytest.raises(ChartFetchError, match='Chart digest mismatch'):
            fetch(tmp_path, gcloud)

    def test_rejects_tampered_manifest(self, tmp_path):
        gcloud = FakeGcloud()
        gcloud.files['rehive-service/manifests/' + MANIFEST_DIGEST] = MANIFEST + b' '
        with pytest.raises(ChartFetchError, match='Manifest digest mismatch'):
            fetch(tmp_path, gcloud)

    def test_never_queries_container_analysis(self, tmp_path):
        # `images describe` needs containeranalysis.occurrences.list.
        gcloud = FakeGcloud()
        fetch(tmp_path, gcloud)
        assert not any(call[1:5] == ['artifacts', 'docker', 'images', 'describe']
                       for call in gcloud.calls)
        assert not any('--show-occurrences' in call for call in gcloud.calls)

    def test_matches_tag_exactly(self, tmp_path):
        gcloud = FakeGcloud(tags=['1.2.0-rc2', '1.2.0'])
        assert fetch(tmp_path, gcloud).endswith('rehive-service-1.2.0.tgz')

    def test_accepts_comma_separated_tags(self, tmp_path):
        gcloud = FakeGcloud()
        gcloud.tags = 'latest, 1.2.0'
        assert fetch(tmp_path, gcloud).endswith('rehive-service-1.2.0.tgz')

    def test_unresolved_tag(self, tmp_path):
        with pytest.raises(ChartFetchError, match='digest'):
            fetch(tmp_path, FakeGcloud(digest=''))

    def test_missing_tag(self, tmp_path):
        with pytest.raises(ChartFetchError, match='1.2.0 to a digest'):
            fetch(tmp_path, FakeGcloud(tags=['1.2.0-rc1']))

    def test_gcloud_failure(self, tmp_path):
        with pytest.raises(ChartFetchError, match='gcloud failed'):
            fetch(tmp_path, FakeGcloud(fail=True))

    def test_temporary_destination_is_cleaned_up(self):
        registered = []
        with patch('rdeploy.helm_chart.subprocess.run', side_effect=FakeGcloud()), \
                patch('rdeploy.helm_chart.atexit.register',
                      side_effect=lambda *args: registered.append(args)):
            path = fetch_chart('europe-west4-docker.pkg.dev', 'my-project',
                               'my-charts/rehive-service', '1.2.0')
        cleanup, directory, _ = registered[0]
        assert os.path.dirname(path) == directory
        cleanup(directory, True)
        assert not os.path.exists(directory)


def registry_config(helm_registry='europe-west4-docker.pkg.dev'):
    return {
        'namespace': 'staging-ns',
        'project_name': 'my-service',
        'helm_chart': 'my-charts/rehive-service',
        'helm_chart_version': '1.2.0',
        'helm_values_path': 'etc/helm/staging.yaml',
        'cloud_provider': {'name': 'gcp', 'project': 'my-project',
                           'kube_cluster': 'cluster-1', 'region': 'europe-west4',
                           'helm_registry': helm_registry},
    }


class TestResolveHelmChart:
    def test_repository_chart_keeps_version_flag(self):
        config = registry_config(helm_registry=None)
        config['helm_chart'] = 'rehive/service'
        assert resolve_helm_chart(config) == ('rehive/service', ' --version 1.2.0')

    def test_registry_chart_is_downloaded(self):
        with patch('rdeploy.tasks.fetch_chart', return_value='/tmp/x/rehive-service-1.2.0.tgz') as fetch_mock:
            assert resolve_helm_chart(registry_config()) == ('/tmp/x/rehive-service-1.2.0.tgz', '')
        fetch_mock.assert_called_once_with('europe-west4-docker.pkg.dev', 'my-project',
                                           'my-charts/rehive-service', '1.2.0')

    def test_path_is_quoted(self):
        with patch('rdeploy.tasks.fetch_chart', return_value='/tmp/a b/chart.tgz'):
            assert resolve_helm_chart(registry_config())[0] == "'/tmp/a b/chart.tgz'"

    def test_windows_path_is_double_quoted(self):
        path = r'C:\Users\a b\AppData\Local\Temp\rdeploy-chart-x\chart.tgz'
        with patch('rdeploy.tasks.fetch_chart', return_value=path), \
                patch('rdeploy.tasks.sys.platform', 'win32'):
            assert resolve_helm_chart(registry_config())[0] == '"{}"'.format(path)

    def test_windows_path_without_spaces_is_unquoted(self):
        path = r'C:\Temp\rdeploy-chart-x\chart.tgz'
        with patch('rdeploy.tasks.fetch_chart', return_value=path), \
                patch('rdeploy.tasks.sys.platform', 'win32'):
            assert resolve_helm_chart(registry_config())[0] == path

    def test_download_failure_exits(self):
        with patch('rdeploy.tasks.fetch_chart', side_effect=ChartFetchError('boom')):
            with pytest.raises(SystemExit, match='Could not download the Helm chart: boom'):
                resolve_helm_chart(registry_config())


class TestTasksUseDownloadedChart:
    def settings(self):
        return {'version': '3', 'configs': {'staging': registry_config()}}

    def run_task(self, task, **kwargs):
        ctx = MagicMock(spec=Context)
        ctx.run.return_value = MagicMock(failed=False)
        with patch('rdeploy.tasks.get_settings', return_value=self.settings()), \
                patch('rdeploy.tasks.set_context'), \
                patch('rdeploy.tasks._watch_rollout'), \
                patch('rdeploy.tasks.fetch_chart', return_value='/tmp/x/rehive-service-1.2.0.tgz'):
            task(ctx, 'staging', **kwargs)
        return [call.args[0] for call in ctx.run.call_args_list]

    @pytest.mark.parametrize('task,kwargs', [(upgrade, {'tag': '1.0.0'}), (install, {})])
    def test_helm_gets_local_chart(self, task, kwargs):
        commands = self.run_task(task, **kwargs)
        helm_command = commands[-1]
        assert helm_command.endswith(' /tmp/x/rehive-service-1.2.0.tgz')
        assert '--version' not in helm_command
        assert 'oci://' not in helm_command
        assert not any('registry login' in c or 'print-access-token' in c for c in commands)

    def test_upgrade_flags_intact(self):
        helm_command = self.run_task(upgrade, tag='1.0.0')[-1]
        assert '--set image.tag=1.0.0 --atomic --timeout 15m /tmp/x/' in helm_command
