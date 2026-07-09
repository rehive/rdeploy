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
