import os
import tempfile

import pytest
import yaml
from unittest.mock import patch, MagicMock
from invoke import Context, Config

from rdeploy.tasks import (
    next_version,
    latest_version,
    latest_prerelease,
    set_project,
    set_cluster,
    set_context,
    create_namespace,
    git_release,
    upgrade,
)
from rdeploy.exceptions import ReleaseError


@pytest.fixture
def mock_ctx():
    """Create a mock invoke Context."""
    ctx = MagicMock(spec=Context)
    ctx.run = MagicMock()
    return ctx


@pytest.fixture
def sample_settings_v3(tmp_path):
    """Create a sample v3 rdeploy.yaml config."""
    settings = {
        "version": "3",
        "configs": {
            "staging": {
                "namespace": "staging-ns",
                "project_name": "my-project",
                "kube_context": "gke_my-project_us-central1_cluster-1",
                "helm_chart": "rehive/service",
                "helm_chart_version": "0.1.0",
                "helm_values_path": "etc/helm/staging.yaml",
                "docker_image": "gcr.io/my-project/my-service:latest",
                "cloud_provider": {
                    "name": "gcp",
                    "project": "my-project",
                    "kube_cluster": "cluster-1",
                    "region": "us-central1",
                    "helm_registry": None,
                },
            }
        },
    }
    config_file = tmp_path / "rdeploy.yaml"
    config_file.write_text(yaml.dump(settings))
    return str(config_file)


@pytest.fixture
def sample_settings_v2(tmp_path):
    """Create a sample v2 rdeploy.yaml config."""
    settings = {
        "version": "2",
        "configs": {
            "staging": {
                "namespace": "staging-ns",
                "project_name": "my-project",
                "helm_chart": "rehive/service",
                "helm_chart_version": "0.1.0",
                "helm_values_path": "etc/helm/staging.yaml",
                "docker_image": "gcr.io/my-project/my-service:latest",
                "cloud_provider": {
                    "name": "gcp",
                    "project": "my-project",
                    "kube_cluster": "cluster-1",
                    "region": "us-central1",
                    "helm_registry": None,
                },
            }
        },
    }
    config_file = tmp_path / "rdeploy.yaml"
    config_file.write_text(yaml.dump(settings))
    return str(config_file)


@pytest.fixture
def sample_settings_v1(tmp_path):
    """Create a sample v1 (legacy) rdeploy.yaml config."""
    settings = {
        "version": "1",
        "configs": {
            "staging": {
                "namespace": "staging-ns",
                "project_name": "my-project",
                "cloud_project": "my-gcp-project",
                "cluster": "cluster-1",
                "cloud_zone": "europe-west1-c",
                "helm_chart": "rehive/service",
                "helm_chart_version": "0.1.0",
                "helm_values_path": "etc/helm/staging.yaml",
                "docker_image": "gcr.io/my-project/my-service:latest",
            }
        },
    }
    config_file = tmp_path / "rdeploy.yaml"
    config_file.write_text(yaml.dump(settings))
    return str(config_file)


class TestNextVersion:
    def test_patch_bump(self, mock_ctx):
        mock_ctx.run.return_value = MagicMock(stdout="v1.2.3\nv1.2.2\nv1.2.1\n")
        with patch('rdeploy.tasks.get_settings', return_value={}):
            result = next_version(mock_ctx, bump='patch')
        assert result == '1.2.4'

    def test_minor_bump(self, mock_ctx):
        mock_ctx.run.return_value = MagicMock(stdout="v1.2.3\nv1.2.2\n")
        with patch('rdeploy.tasks.get_settings', return_value={}):
            result = next_version(mock_ctx, bump='minor')
        assert result == '1.3.0'

    def test_major_bump(self, mock_ctx):
        mock_ctx.run.return_value = MagicMock(stdout="v1.2.3\nv1.2.2\n")
        with patch('rdeploy.tasks.get_settings', return_value={}):
            result = next_version(mock_ctx, bump='major')
        assert result == '2.0.0'

    def test_build_bump(self, mock_ctx):
        mock_ctx.run.return_value = MagicMock(stdout="v1.2.3\n")
        with patch('rdeploy.tasks.get_settings', return_value={}):
            result = next_version(mock_ctx, bump='build')
        assert result == '1.2.3+build.1'

    def test_patch_bump_from_zero(self, mock_ctx):
        # No tags exist - should start from 0.0.0
        mock_ctx.run.return_value = MagicMock(stdout="\n")
        result = next_version(mock_ctx, bump='patch')
        assert result == '0.0.1'

    def test_pre_patch_bump(self, mock_ctx):
        # First call: git fetch, second: git tag (for latest_version)
        # Then again for pre-release check
        mock_ctx.run.side_effect = [
            MagicMock(),  # git fetch for latest_version
            MagicMock(stdout="v1.2.3\n"),  # git tag for latest_version
            MagicMock(),  # git fetch for latest_prerelease
            MagicMock(stdout="\n"),  # git tag for latest_prerelease (no match)
        ]
        result = next_version(mock_ctx, bump='pre-patch')
        assert result == '1.2.4-rc.1'

    def test_pre_patch_bump_with_existing_prerelease(self, mock_ctx):
        mock_ctx.run.side_effect = [
            MagicMock(),  # git fetch for latest_version
            MagicMock(stdout="v1.2.3\n"),  # git tag for latest_version
            MagicMock(),  # git fetch for latest_prerelease
            MagicMock(stdout="v1.2.4-rc.1\n"),  # existing pre-release
        ]
        result = next_version(mock_ctx, bump='pre-patch')
        assert result == '1.2.4-rc.2'


class TestLatestVersion:
    def test_finds_latest_tag(self, mock_ctx):
        mock_ctx.run.side_effect = [
            MagicMock(),  # git fetch --tags
            MagicMock(stdout="v2.1.0\nv2.0.0\nv1.9.0\n"),  # git tag --sort
        ]
        result = latest_version(mock_ctx)
        assert result == '2.1.0'

    def test_strips_v_prefix(self, mock_ctx):
        mock_ctx.run.side_effect = [
            MagicMock(),
            MagicMock(stdout="v1.0.0\n"),
        ]
        result = latest_version(mock_ctx)
        assert result == '1.0.0'

    def test_handles_no_v_prefix(self, mock_ctx):
        mock_ctx.run.side_effect = [
            MagicMock(),
            MagicMock(stdout="1.0.0\n"),
        ]
        result = latest_version(mock_ctx)
        assert result == '1.0.0'

    def test_raises_on_no_tags(self, mock_ctx):
        mock_ctx.run.side_effect = [
            MagicMock(),
            MagicMock(stdout="\n"),
        ]
        with pytest.raises(ReleaseError, match="No valid semver tags"):
            latest_version(mock_ctx)

    def test_ignores_non_semver_tags(self, mock_ctx):
        mock_ctx.run.side_effect = [
            MagicMock(),
            MagicMock(stdout="release-candidate\nfoo-bar\nv1.5.0\n"),
        ]
        result = latest_version(mock_ctx)
        assert result == '1.5.0'


class TestSetProject:
    def test_v2_gcp(self, mock_ctx, sample_settings_v2):
        with patch('rdeploy.tasks.get_settings', return_value=yaml.safe_load(open(sample_settings_v2))):
            set_project(mock_ctx, 'staging')
        mock_ctx.run.assert_called_once()
        call_args = mock_ctx.run.call_args[0][0]
        assert 'gcloud config set project my-project' in call_args

    def test_v1_legacy(self, mock_ctx, sample_settings_v1):
        with patch('rdeploy.tasks.get_settings', return_value=yaml.safe_load(open(sample_settings_v1))):
            set_project(mock_ctx, 'staging')
        mock_ctx.run.assert_called_once()
        call_args = mock_ctx.run.call_args[0][0]
        assert 'gcloud config set project my-gcp-project' in call_args


class TestSetContext:
    def test_v3_with_kube_context(self, mock_ctx, sample_settings_v3):
        with patch('rdeploy.tasks.get_settings', return_value=yaml.safe_load(open(sample_settings_v3))):
            set_context(mock_ctx, 'staging')
        assert mock_ctx.run.call_count == 2
        first_call = mock_ctx.run.call_args_list[0][0][0]
        assert 'kubectl config use-context gke_my-project_us-central1_cluster-1' in first_call

    def test_v2_gcp(self, mock_ctx, sample_settings_v2):
        with patch('rdeploy.tasks.get_settings', return_value=yaml.safe_load(open(sample_settings_v2))):
            set_context(mock_ctx, 'staging')
        assert mock_ctx.run.call_count == 2
        first_call = mock_ctx.run.call_args_list[0][0][0]
        assert 'kubectl config use-context' in first_call
        assert 'gcp_my-project_cluster-1_us-central1' in first_call

    def test_v1_legacy(self, mock_ctx, sample_settings_v1):
        with patch('rdeploy.tasks.get_settings', return_value=yaml.safe_load(open(sample_settings_v1))):
            set_context(mock_ctx, 'staging')
        assert mock_ctx.run.call_count == 2
        first_call = mock_ctx.run.call_args_list[0][0][0]
        assert 'gcp_my-gcp-project_cluster-1_europe-west1-c' in first_call

    def test_v2_non_gcp_provider_exits(self, mock_ctx, sample_settings_v2):
        settings = yaml.safe_load(open(sample_settings_v2))
        settings['configs']['staging']['cloud_provider']['name'] = 'azure'
        with patch('rdeploy.tasks.get_settings', return_value=settings):
            with pytest.raises(SystemExit, match="Unsupported cloud provider: azure"):
                set_context(mock_ctx, 'staging')


class TestSetCluster:
    def test_v2_non_gcp_provider_exits(self, mock_ctx, sample_settings_v2):
        settings = yaml.safe_load(open(sample_settings_v2))
        settings['configs']['staging']['cloud_provider']['name'] = 'azure'
        with patch('rdeploy.tasks.get_settings', return_value=settings):
            with pytest.raises(SystemExit, match="Unsupported cloud provider: azure"):
                set_cluster(mock_ctx, 'staging')

    def test_v2_missing_zone_and_region_exits(self, mock_ctx, sample_settings_v2):
        settings = yaml.safe_load(open(sample_settings_v2))
        del settings['configs']['staging']['cloud_provider']['region']
        with patch('rdeploy.tasks.get_settings', return_value=settings):
            with pytest.raises(SystemExit, match="'zone' or 'region'"):
                set_cluster(mock_ctx, 'staging')


class TestCreateNamespace:
    def test_creates_namespace(self, mock_ctx, sample_settings_v3):
        with patch('rdeploy.tasks.get_settings', return_value=yaml.safe_load(open(sample_settings_v3))):
            with patch('rdeploy.tasks.set_context'):
                create_namespace(mock_ctx, 'staging')
        mock_ctx.run.assert_called_once()
        call_args = mock_ctx.run.call_args[0][0]
        assert 'kubectl create namespace staging-ns' in call_args


class TestUpgrade:
    # _watch_rollout is patched in every test so the progress watcher thread
    # never shells out to a real kubectl (and whatever cluster the local
    # kubeconfig points at) during unit tests.

    def test_atomic_by_default(self, mock_ctx, sample_settings_v3):
        mock_ctx.run.return_value = MagicMock(failed=False)
        with patch('rdeploy.tasks.get_settings', return_value=yaml.safe_load(open(sample_settings_v3))):
            with patch('rdeploy.tasks.set_context'):
                with patch('rdeploy.tasks._watch_rollout'):
                    upgrade(mock_ctx, 'staging', tag='1.2.3')
        call_args = mock_ctx.run.call_args[0][0]
        assert 'helm upgrade my-project' in call_args
        assert '--set image.tag=1.2.3' in call_args
        assert '--atomic --timeout 15m' in call_args

    def test_custom_timeout(self, mock_ctx, sample_settings_v3):
        mock_ctx.run.return_value = MagicMock(failed=False)
        with patch('rdeploy.tasks.get_settings', return_value=yaml.safe_load(open(sample_settings_v3))):
            with patch('rdeploy.tasks.set_context'):
                with patch('rdeploy.tasks._watch_rollout'):
                    upgrade(mock_ctx, 'staging', tag='1.2.3', timeout='15m')
        call_args = mock_ctx.run.call_args[0][0]
        assert '--atomic --timeout 15m' in call_args

    def test_no_atomic_opt_out(self, mock_ctx, sample_settings_v3):
        mock_ctx.run.return_value = MagicMock(failed=False)
        with patch('rdeploy.tasks.get_settings', return_value=yaml.safe_load(open(sample_settings_v3))):
            with patch('rdeploy.tasks.set_context'):
                with patch('rdeploy.tasks._watch_rollout'):
                    upgrade(mock_ctx, 'staging', tag='1.2.3', atomic=False)
        call_args = mock_ctx.run.call_args[0][0]
        assert '--atomic' not in call_args
        assert '--timeout' not in call_args

    def test_failed_upgrade_exits_with_rollback_message(self, mock_ctx, sample_settings_v3):
        mock_ctx.run.return_value = MagicMock(failed=True)
        with patch('rdeploy.tasks.get_settings', return_value=yaml.safe_load(open(sample_settings_v3))):
            with patch('rdeploy.tasks.set_context'):
                with patch('rdeploy.tasks._watch_rollout'):
                    with pytest.raises(SystemExit, match='rolled the release back'):
                        upgrade(mock_ctx, 'staging', tag='1.2.3')

    def test_failed_non_atomic_upgrade_exits(self, mock_ctx, sample_settings_v3):
        mock_ctx.run.return_value = MagicMock(failed=True)
        with patch('rdeploy.tasks.get_settings', return_value=yaml.safe_load(open(sample_settings_v3))):
            with patch('rdeploy.tasks.set_context'):
                with patch('rdeploy.tasks._watch_rollout'):
                    with pytest.raises(SystemExit, match='failed') as exc_info:
                        upgrade(mock_ctx, 'staging', tag='1.2.3', atomic=False)
        assert 'rolled the release back' not in str(exc_info.value)

    def test_progress_watcher_runs_for_atomic_upgrade(self, mock_ctx, sample_settings_v3):
        mock_ctx.run.return_value = MagicMock(failed=False)
        with patch('rdeploy.tasks.get_settings', return_value=yaml.safe_load(open(sample_settings_v3))):
            with patch('rdeploy.tasks.set_context'):
                with patch('rdeploy.tasks._watch_rollout') as mock_watch:
                    upgrade(mock_ctx, 'staging', tag='1.2.3')
        # The watcher thread is joined in upgrade()'s finally, so by the time
        # upgrade returns the (mocked) watcher must have been invoked.
        assert mock_watch.called

    def test_no_progress_opt_out(self, mock_ctx, sample_settings_v3):
        mock_ctx.run.return_value = MagicMock(failed=False)
        with patch('rdeploy.tasks.get_settings', return_value=yaml.safe_load(open(sample_settings_v3))):
            with patch('rdeploy.tasks.set_context'):
                with patch('rdeploy.tasks._watch_rollout') as mock_watch:
                    upgrade(mock_ctx, 'staging', tag='1.2.3', progress=False)
        assert not mock_watch.called

    def test_no_atomic_skips_progress_watcher(self, mock_ctx, sample_settings_v3):
        mock_ctx.run.return_value = MagicMock(failed=False)
        with patch('rdeploy.tasks.get_settings', return_value=yaml.safe_load(open(sample_settings_v3))):
            with patch('rdeploy.tasks.set_context'):
                with patch('rdeploy.tasks._watch_rollout') as mock_watch:
                    upgrade(mock_ctx, 'staging', tag='1.2.3', atomic=False)
        assert not mock_watch.called

    def test_watcher_stopped_even_when_upgrade_fails(self, mock_ctx, sample_settings_v3):
        mock_ctx.run.return_value = MagicMock(failed=True)
        with patch('rdeploy.tasks.get_settings', return_value=yaml.safe_load(open(sample_settings_v3))):
            with patch('rdeploy.tasks.set_context'):
                with patch('rdeploy.tasks._watch_rollout') as mock_watch:
                    with pytest.raises(SystemExit):
                        upgrade(mock_ctx, 'staging', tag='1.2.3')
        # The stop event handed to the watcher must be set by the finally
        # block even on the failure path.
        stop_event = mock_watch.call_args[0][0]
        assert stop_event.is_set()


class TestWatchRollout:
    """Unit tests for the _watch_rollout polling loop.

    subprocess.run is always mocked -- these tests must never touch a real
    kubectl or cluster. The stop_event is either a real Event driven by the
    subprocess.run side effects or a MagicMock that makes waits instant.
    """

    TABLE_V1 = ('NAME         READY   UP-TO-DATE   AVAILABLE   AGE\n'
                'my-project   1/3     3            1           5d\n')
    TABLE_V2 = ('NAME         READY   UP-TO-DATE   AVAILABLE   AGE\n'
                'my-project   3/3     3            3           5d\n')
    # Same as TABLE_V1 except the AGE column ticked over.
    TABLE_V1_AGED = ('NAME         READY   UP-TO-DATE   AVAILABLE   AGE\n'
                     'my-project   1/3     3            1           5d1h\n')

    @staticmethod
    def _instant_event(is_set_results):
        """A stop-event stand-in whose waits return instantly and whose
        is_set() runs through is_set_results then stays True."""
        event = MagicMock()
        event.is_set.side_effect = list(is_set_results) + [True] * 10
        event.wait.return_value = False
        return event

    def test_prints_table_with_prefix(self, capsys):
        from rdeploy.tasks import _watch_rollout
        event = self._instant_event([False])
        result = MagicMock(returncode=0, stdout=self.TABLE_V1, stderr='')
        with patch('rdeploy.tasks.subprocess.run', return_value=result):
            _watch_rollout(event)
        out = capsys.readouterr().out
        assert '[rollout] NAME' in out
        assert '[rollout] my-project   1/3' in out

    def test_unchanged_table_not_reprinted(self, capsys):
        from rdeploy.tasks import _watch_rollout
        event = self._instant_event([False, False])
        result = MagicMock(returncode=0, stdout=self.TABLE_V1, stderr='')
        with patch('rdeploy.tasks.subprocess.run', return_value=result):
            _watch_rollout(event)
        out = capsys.readouterr().out
        assert out.count('my-project') == 1

    def test_age_only_change_not_reprinted(self, capsys):
        from rdeploy.tasks import _watch_rollout
        event = self._instant_event([False, False])
        results = [MagicMock(returncode=0, stdout=self.TABLE_V1, stderr=''),
                   MagicMock(returncode=0, stdout=self.TABLE_V1_AGED, stderr='')]
        with patch('rdeploy.tasks.subprocess.run', side_effect=results):
            _watch_rollout(event)
        out = capsys.readouterr().out
        assert out.count('my-project') == 1

    def test_changed_row_reprinted_without_header(self, capsys):
        from rdeploy.tasks import _watch_rollout
        event = self._instant_event([False, False])
        results = [MagicMock(returncode=0, stdout=self.TABLE_V1, stderr=''),
                   MagicMock(returncode=0, stdout=self.TABLE_V2, stderr='')]
        with patch('rdeploy.tasks.subprocess.run', side_effect=results):
            _watch_rollout(event)
        out = capsys.readouterr().out
        assert '1/3' in out
        assert '3/3' in out
        # The header is a one-time baseline; changes print only the row.
        assert out.count('NAME') == 1

    def test_only_changed_rows_reprinted(self, capsys):
        from rdeploy.tasks import _watch_rollout
        table_v1 = ('NAME             READY   UP-TO-DATE   AVAILABLE   AGE\n'
                    'scheduler        1/1     1            1           2y\n'
                    'worker-general   1/1     1            1           2y\n')
        table_v2 = ('NAME             READY   UP-TO-DATE   AVAILABLE   AGE\n'
                    'scheduler        1/1     1            1           2y\n'
                    'worker-general   2/1     1            2           2y\n')
        event = self._instant_event([False, False])
        results = [MagicMock(returncode=0, stdout=table_v1, stderr=''),
                   MagicMock(returncode=0, stdout=table_v2, stderr='')]
        with patch('rdeploy.tasks.subprocess.run', side_effect=results):
            _watch_rollout(event)
        out = capsys.readouterr().out
        # scheduler never changed: baseline only. worker-general changed once.
        assert out.count('scheduler') == 1
        assert out.count('worker-general') == 2
        assert '2/1' in out

    def test_deployment_created_mid_upgrade_appears(self, capsys):
        from rdeploy.tasks import _watch_rollout
        table_v1 = ('NAME        READY   UP-TO-DATE   AVAILABLE   AGE\n'
                    'scheduler   1/1     1            1           2y\n')
        table_v2 = ('NAME         READY   UP-TO-DATE   AVAILABLE   AGE\n'
                    'new-worker   0/1     1            0           2s\n'
                    'scheduler    1/1     1            1           2y\n')
        event = self._instant_event([False, False])
        results = [MagicMock(returncode=0, stdout=table_v1, stderr=''),
                   MagicMock(returncode=0, stdout=table_v2, stderr='')]
        with patch('rdeploy.tasks.subprocess.run', side_effect=results):
            _watch_rollout(event)
        out = capsys.readouterr().out
        assert 'new-worker' in out

    def test_gives_up_after_repeated_kubectl_failures(self, capsys):
        from rdeploy.tasks import _watch_rollout
        event = self._instant_event([False] * 10)
        result = MagicMock(returncode=1, stdout='',
                           stderr='error: current-context is not set\n')
        with patch('rdeploy.tasks.subprocess.run',
                   return_value=result) as mock_run:
            _watch_rollout(event)
        out = capsys.readouterr().out
        assert 'progress display unavailable' in out
        assert 'current-context is not set' in out
        assert mock_run.call_count == 3

    def test_transient_failure_recovers(self, capsys):
        from rdeploy.tasks import _watch_rollout
        event = self._instant_event([False, False, False])
        results = [MagicMock(returncode=1, stdout='', stderr='boom\n'),
                   MagicMock(returncode=0, stdout=self.TABLE_V1, stderr=''),
                   MagicMock(returncode=1, stdout='', stderr='boom\n')]
        with patch('rdeploy.tasks.subprocess.run', side_effect=results):
            _watch_rollout(event)
        out = capsys.readouterr().out
        assert 'my-project' in out
        assert 'progress display unavailable' not in out

    def test_missing_kubectl_reports_unavailable(self, capsys):
        from rdeploy.tasks import _watch_rollout
        event = self._instant_event([False])
        with patch('rdeploy.tasks.subprocess.run',
                   side_effect=FileNotFoundError('kubectl')):
            _watch_rollout(event)  # must not raise
        out = capsys.readouterr().out
        assert 'progress display unavailable' in out

    def test_preset_stop_event_polls_nothing(self):
        import threading
        from rdeploy.tasks import _watch_rollout
        event = threading.Event()
        event.set()
        with patch('rdeploy.tasks.subprocess.run') as mock_run:
            _watch_rollout(event)
        assert not mock_run.called


class TestGitRelease:
    def test_creates_and_pushes_tag(self, mock_ctx):
        with patch('rdeploy.tasks.confirm'):
            with patch('rdeploy.tasks.next_version', return_value='1.3.0'):
                git_release(mock_ctx, version_bump='minor')
        assert mock_ctx.run.call_count == 2
        tag_call = mock_ctx.run.call_args_list[0][0][0]
        push_call = mock_ctx.run.call_args_list[1][0][0]
        assert "git tag 'v1.3.0'" in tag_call
        assert "git push origin v1.3.0" in push_call

    def test_force_skips_confirm(self, mock_ctx):
        with patch('rdeploy.tasks.next_version', return_value='2.0.0'):
            git_release(mock_ctx, version_bump='major', force=True)
        assert mock_ctx.run.call_count == 2

    def test_pre_release_exits(self, mock_ctx):
        with patch('rdeploy.tasks.confirm'):
            with pytest.raises(SystemExit):
                git_release(mock_ctx, version_bump='pre')
