import json
import os
import stat
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import MagicMock, patch

import pytest
import yaml

from rdeploy import gke_auth
from rdeploy.gke_auth import KubectlLoginError

CLIENT = {
    'client_id': 'test-client.apps.googleusercontent.com',
    'client_secret': 'test-secret',
    'auth_uri': 'https://accounts.example.test/o/oauth2/auth',
    'token_uri': 'https://oauth2.example.test/token',
}


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    """Keep saved settings and credentials inside tmp_path."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.delenv(gke_auth.ENV_PROJECT, raising=False)
    monkeypatch.delenv(gke_auth.ENV_SECRET, raising=False)
    return tmp_path / 'config'


def completed(stdout='', returncode=0, stderr=''):
    return MagicMock(stdout=stdout, returncode=returncode, stderr=stderr)


# Settings
##########

class TestResolveSettings:
    @pytest.fixture(autouse=True)
    def in_tmp_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

    def test_missing_project(self):
        with pytest.raises(KubectlLoginError, match='--gcp-project'):
            gke_auth.resolve_settings()

    def test_defaults_from_rdeploy_yaml(self, tmp_path):
        (tmp_path / 'rdeploy.yaml').write_text(yaml.safe_dump({'version': '3', 'configs': {
            'staging': {'cloud_provider': {'name': 'gcp', 'project': 'yaml-p'}},
            'production': {'cloud_provider': {'name': 'gcp', 'project': 'yaml-p'}},
        }}))
        resolved = gke_auth.resolve_settings()
        assert resolved['project'] == 'yaml-p'
        assert resolved['secret'] == 'kubectl-oauth-client'

    def test_v1_rdeploy_yaml(self, tmp_path):
        (tmp_path / 'rdeploy.yaml').write_text(yaml.safe_dump(
            {'configs': {'staging': {'cloud_project': 'old-p'}}}))
        assert gke_auth.resolve_settings()['project'] == 'old-p'

    def test_rdeploy_yaml_with_several_projects(self, tmp_path):
        (tmp_path / 'rdeploy.yaml').write_text(yaml.safe_dump({'version': '3', 'configs': {
            'a': {'cloud_provider': {'project': 'p1'}},
            'b': {'cloud_provider': {'project': 'p2'}},
        }}))
        with pytest.raises(KubectlLoginError, match='several GCP projects'):
            gke_auth.resolve_settings()
        assert gke_auth.resolve_settings(project='p2')['project'] == 'p2'

    def test_saved_wins_over_rdeploy_yaml(self, tmp_path):
        (tmp_path / 'rdeploy.yaml').write_text(yaml.safe_dump(
            {'configs': {'staging': {'cloud_provider': {'project': 'yaml-p'}}}}))
        gke_auth.save_settings({'project': 'saved-p'})
        assert gke_auth.resolve_settings()['project'] == 'saved-p'

    def test_flags_win_over_env_and_saved(self, monkeypatch):
        gke_auth.save_settings({'project': 'saved-p', 'secret': 'saved-s'})
        monkeypatch.setenv(gke_auth.ENV_PROJECT, 'env-p')
        resolved = gke_auth.resolve_settings(project='flag-p')
        assert resolved['project'] == 'flag-p'
        assert resolved['secret'] == 'saved-s'

    def test_env_wins_over_saved(self, monkeypatch):
        gke_auth.save_settings({'project': 'saved-p', 'secret': 'saved-s'})
        monkeypatch.setenv(gke_auth.ENV_SECRET, 'env-s')
        assert gke_auth.resolve_settings()['secret'] == 'env-s'

    def test_saved_settings_reused(self):
        gke_auth.save_settings({'project': 'p', 'secret': 's', 'account': 'a@example.test',
                                'clusters': ['one']})
        assert gke_auth.resolve_settings() == {
            'project': 'p', 'secret': 's', 'account': 'a@example.test', 'clusters': ['one']}

    def test_invalid_saved_settings(self, config_home):
        os.makedirs(gke_auth.config_dir())
        with open(gke_auth.settings_path(), 'w') as f:
            f.write('{nope')
        with pytest.raises(KubectlLoginError, match='not valid JSON'):
            gke_auth.resolve_settings()


# OAuth client
##############

class TestLoadClient:
    def test_from_file(self, tmp_path):
        path = tmp_path / 'client.json'
        path.write_text(json.dumps({'installed': CLIENT}))
        assert gke_auth.load_client('p', 's', client_json=str(path)) == CLIENT

    def test_rejects_web_client(self, tmp_path):
        path = tmp_path / 'client.json'
        path.write_text(json.dumps({'web': CLIENT}))
        with pytest.raises(KubectlLoginError, match='Desktop app'):
            gke_auth.load_client('p', 's', client_json=str(path))

    @patch('rdeploy.gke_auth.subprocess.run')
    def test_from_secret_manager(self, mock_run):
        mock_run.return_value = completed(json.dumps({'installed': CLIENT}))
        assert gke_auth.load_client('my-project', 'my-secret', 'me@example.test') == CLIENT
        assert mock_run.call_args[0][0] == [
            'gcloud', 'secrets', 'versions', 'access', 'latest',
            '--secret=my-secret', '--project=my-project', '--account=me@example.test']

    @patch('rdeploy.gke_auth.subprocess.run')
    def test_secret_manager_error(self, mock_run):
        mock_run.return_value = completed(returncode=1)
        with pytest.raises(KubectlLoginError, match='secret s in project p'):
            gke_auth.load_client('p', 's')

    @patch('rdeploy.gke_auth.subprocess.run')
    def test_gcloud_can_prompt_for_reauth(self, mock_run):
        # gcloud only prompts for a password when stdin and stderr are the
        # terminal, so neither may be captured.
        mock_run.return_value = completed(json.dumps({'installed': CLIENT}))
        gke_auth.load_client('p', 's')
        kwargs = mock_run.call_args[1]
        assert kwargs['stdout'] == subprocess.PIPE
        assert 'stderr' not in kwargs and 'stdin' not in kwargs
        assert 'capture_output' not in kwargs


# Sign-in
#########

class TestSignIn:
    def test_authorization_url_requests_exact_scopes(self):
        url = gke_auth.authorization_url(CLIENT, 'http://127.0.0.1:1234', 'st', 'ch', 'me@example.test')
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert query['scope'] == ['openid email https://www.googleapis.com/auth/container.read-only']
        assert 'cloud-platform' not in url
        assert query['code_challenge_method'] == ['S256']
        assert query['access_type'] == ['offline']
        assert query['login_hint'] == ['me@example.test']

    def test_pkce_challenge_matches_verifier(self):
        import base64
        import hashlib
        verifier, challenge = gke_auth.pkce_pair()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        assert challenge == expected

    @patch('rdeploy.gke_auth.requests.post')
    def test_loopback_flow(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {
            'scope': 'openid https://www.googleapis.com/auth/userinfo.email '
                     'https://www.googleapis.com/auth/container.read-only',
            'refresh_token': 'refresh-1'})

        def browser(url):
            # Play the part of Google redirecting back to the loopback server.
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect = query['redirect_uri'][0]

            def visit():
                # A stray request without a code must not end the sign-in.
                with pytest.raises(urllib.error.HTTPError):
                    urllib.request.urlopen(redirect + '/favicon.ico')
                urllib.request.urlopen(redirect + '/?' + urllib.parse.urlencode(
                    {'code': 'the-code', 'state': query['state'][0]})).read()
            threading.Thread(target=visit, daemon=True).start()

        token = gke_auth.sign_in(CLIENT, 'me@example.test', open_browser=browser)
        assert token['refresh_token'] == 'refresh-1'
        data = mock_post.call_args[1]['data']
        assert data['code'] == 'the-code'
        assert data['grant_type'] == 'authorization_code'
        assert data['code_verifier']

    def test_state_mismatch(self):
        def browser(url):
            redirect = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)['redirect_uri'][0]
            threading.Thread(target=lambda: urllib.request.urlopen(
                redirect + '/?code=x&state=forged').read(), daemon=True).start()

        with pytest.raises(KubectlLoginError, match='didn\'t match'):
            gke_auth.sign_in(CLIENT, open_browser=browser)

    def test_admin_blocked(self):
        def browser(url):
            redirect = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)['redirect_uri'][0]
            threading.Thread(target=lambda: urllib.request.urlopen(
                redirect + '/?error=access_not_configured').read(), daemon=True).start()

        with pytest.raises(KubectlLoginError, match='access_not_configured'):
            gke_auth.sign_in(CLIENT, open_browser=browser)

    @patch('rdeploy.gke_auth.requests.post')
    def test_missing_container_scope(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {
            'scope': 'openid email', 'refresh_token': 'r'})
        with pytest.raises(KubectlLoginError, match='Kubernetes Engine scope'):
            gke_auth.exchange_code(CLIENT, 'c', 'http://127.0.0.1:1', 'v')


class TestWriteCredentials:
    def test_authorized_user_file_is_private(self, tmp_path):
        path = str(tmp_path / 'nested' / 'creds.json')
        gke_auth.write_credentials(CLIENT, 'refresh-1', path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        with open(path) as f:
            assert json.load(f) == {
                'type': 'authorized_user',
                'client_id': CLIENT['client_id'],
                'client_secret': CLIENT['client_secret'],
                'refresh_token': 'refresh-1',
            }

    def test_tightens_existing_file(self, tmp_path):
        path = tmp_path / 'creds.json'
        path.write_text('{}')
        os.chmod(path, 0o644)
        gke_auth.write_credentials(CLIENT, 'r', str(path))
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# Session status
################

class TestSessionStatus:
    def test_not_signed_in(self):
        assert gke_auth.session_status() == (False, 'Not signed in.')

    @patch('rdeploy.gke_auth.requests.post')
    def test_valid(self, mock_post):
        gke_auth.write_credentials(CLIENT, 'r')
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {
            'scope': gke_auth.CONTAINER_SCOPE})
        assert gke_auth.session_status()[0] is True

    @patch('rdeploy.gke_auth.requests.post')
    def test_expired(self, mock_post, capsys):
        gke_auth.write_credentials(CLIENT, 'r')
        mock_post.return_value = MagicMock(status_code=400, json=lambda: {
            'error': 'invalid_grant', 'error_description': 'reauth related error (invalid_rapt)'})
        assert gke_auth.status() is False
        out = capsys.readouterr().out
        assert 'expired' in out
        assert '`rdeploy kubectl-login`' in out


# kubeconfig
############

def kube_view(contexts):
    return json.dumps({'contexts': [
        {'name': name, 'context': {'cluster': cluster}} for name, cluster in contexts.items()]})


class FakeTools:
    """Stands in for gcloud and kubectl, recording what they were asked to do."""

    def __init__(self, clusters, contexts, current='other'):
        self.clusters = clusters
        self.contexts = dict(contexts)
        self.current = current
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[:4] == ['gcloud', 'container', 'clusters', 'list']:
            return completed(json.dumps([{'name': n, 'location': l} for n, l in self.clusters]))
        if cmd[:4] == ['gcloud', 'container', 'clusters', 'get-credentials']:
            name = cmd[4]
            location = next(a for a in cmd if a.startswith('--location=')).split('=', 1)[1]
            project = next(a for a in cmd if a.startswith('--project=')).split('=', 1)[1]
            entry = f'gke_{project}_{location}_{name}'
            self.contexts[entry] = entry
            self.current = entry
            return completed()
        if cmd[:3] == ['kubectl', 'config', 'view']:
            return completed(kube_view(self.contexts))
        if cmd[:3] == ['kubectl', 'config', 'current-context']:
            return completed(self.current + '\n')
        if cmd[:3] == ['kubectl', 'config', 'use-context']:
            self.current = cmd[3]
            return completed()
        return completed()

    def kubectl(self, verb):
        return [c for c in self.calls if c[:3] == ['kubectl', 'config', verb]]


class TestConfigureKubeconfig:
    @pytest.fixture(autouse=True)
    def plugin_cache(self, tmp_path, monkeypatch):
        cache = tmp_path / 'gke_gcloud_auth_plugin_cache'
        cache.write_text('{}')
        monkeypatch.setattr(gke_auth, 'PLUGIN_CACHE', str(cache))
        return cache

    def test_points_existing_contexts_at_the_new_user(self, plugin_cache):
        tools = FakeTools(
            clusters=[('alpha', 'region-1'), ('beta', 'region-1')],
            contexts={
                'gke_proj_region-1_alpha': 'gke_proj_region-1_alpha',
                # renamed by `rdeploy set-cluster`; still the same cluster entry
                'gcp_proj_beta_region-1': 'gke_proj_region-1_beta',
                'unrelated': 'gke_other_region-1_gamma',
            })
        with patch('rdeploy.gke_auth.subprocess.run', tools):
            changed = gke_auth.configure_kubeconfig('proj', '/creds.json')

        assert changed == ['gcp_proj_beta_region-1', 'gke_proj_region-1_alpha']
        assert not any(c[:4] == ['gcloud', 'container', 'clusters', 'get-credentials'] for c in tools.calls)
        assert tools.kubectl('set-credentials') == [[
            'kubectl', 'config', 'set-credentials', 'rdeploy-kubectl',
            '--exec-api-version=client.authentication.k8s.io/v1beta1',
            '--exec-command=gke-gcloud-auth-plugin',
            '--exec-arg=--use_application_default_credentials',
            '--exec-env=GOOGLE_APPLICATION_CREDENTIALS=/creds.json',
        ]]
        assert sorted(c[3] for c in tools.kubectl('set-context')) == changed
        assert all(c[4] == '--user=rdeploy-kubectl' for c in tools.kubectl('set-context'))
        assert not plugin_cache.exists()

    def test_adds_missing_clusters_and_keeps_current_context(self):
        tools = FakeTools(clusters=[('alpha', 'region-1')], contexts={'unrelated': 'x'},
                          current='unrelated')
        with patch('rdeploy.gke_auth.subprocess.run', tools):
            changed = gke_auth.configure_kubeconfig('proj', '/creds.json', account='me@example.test')

        get_creds = [c for c in tools.calls if c[:4] == ['gcloud', 'container', 'clusters', 'get-credentials']]
        assert get_creds == [['gcloud', 'container', 'clusters', 'get-credentials', 'alpha',
                              '--location=region-1', '--project=proj', '--account=me@example.test']]
        assert changed == ['gke_proj_region-1_alpha']
        assert tools.current == 'unrelated'

    def test_cluster_filter(self):
        tools = FakeTools(clusters=[('alpha', 'r'), ('beta', 'r')],
                          contexts={'gke_proj_r_alpha': 'gke_proj_r_alpha', 'gke_proj_r_beta': 'gke_proj_r_beta'})
        with patch('rdeploy.gke_auth.subprocess.run', tools):
            assert gke_auth.configure_kubeconfig('proj', '/c', only_clusters=['beta']) == ['gke_proj_r_beta']

    def test_unknown_cluster(self):
        tools = FakeTools(clusters=[('alpha', 'r')], contexts={})
        with patch('rdeploy.gke_auth.subprocess.run', tools):
            with pytest.raises(KubectlLoginError, match='No such cluster'):
                gke_auth.configure_kubeconfig('proj', '/c', only_clusters=['nope'])


# login()
#########

class TestLogin:
    @pytest.fixture(autouse=True)
    def in_tmp_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

    @pytest.fixture(autouse=True)
    def gcloud_signed_in(self):
        with patch('rdeploy.gke_auth.ensure_gcloud_login') as ensure:
            yield ensure

    def test_checks_gcloud_login_first(self, gcloud_signed_in):
        with patch('rdeploy.gke_auth.check_tools'), \
                patch('rdeploy.gke_auth.load_client', return_value=CLIENT), \
                patch('rdeploy.gke_auth.sign_in', return_value={'refresh_token': 'r'}), \
                patch('rdeploy.gke_auth.configure_kubeconfig', return_value=['ctx']):
            gke_auth.login(project='p', secret='s', account='me@example.test')
        gcloud_signed_in.assert_called_once_with('me@example.test')

    def test_no_gcloud_needed_with_local_client_and_no_kubeconfig(self, gcloud_signed_in, tmp_path):
        path = tmp_path / 'client.json'
        path.write_text(json.dumps({'installed': CLIENT}))
        with patch('rdeploy.gke_auth.check_tools'), \
                patch('rdeploy.gke_auth.sign_in', return_value={'refresh_token': 'r'}):
            gke_auth.login(project='p', secret='s', client_json=str(path), skip_kubeconfig=True)
        gcloud_signed_in.assert_not_called()

    @patch('rdeploy.gke_auth.configure_kubeconfig', return_value=['ctx'])
    @patch('rdeploy.gke_auth.sign_in', return_value={'refresh_token': 'r'})
    @patch('rdeploy.gke_auth.load_client', return_value=CLIENT)
    @patch('rdeploy.gke_auth.check_tools')
    def test_saves_settings_for_next_run(self, _tools, mock_load, mock_sign_in, mock_kube):
        gke_auth.login(project='proj', secret='sec', account='me@example.test')
        assert gke_auth.load_saved_settings() == {
            'project': 'proj', 'secret': 'sec', 'account': 'me@example.test'}
        mock_kube.assert_called_once_with('proj', gke_auth.credentials_path(), 'me@example.test', [])

        # The next run needs no flags.
        gke_auth.login()
        assert mock_load.call_args[0][:2] == ('proj', 'sec')

    @patch('rdeploy.gke_auth.sign_in', side_effect=KubectlLoginError('nope'))
    @patch('rdeploy.gke_auth.load_client', return_value=CLIENT)
    @patch('rdeploy.gke_auth.check_tools')
    def test_failed_sign_in_saves_nothing(self, *_):
        with pytest.raises(KubectlLoginError):
            gke_auth.login(project='typo', secret='sec')
        assert gke_auth.load_saved_settings() == {}

    def test_run_exits_cleanly(self):
        def fail():
            raise KubectlLoginError('explained')
        with pytest.raises(SystemExit, match='explained'):
            gke_auth.run(fail)


# gcloud login
##############

class TestEnsureGcloudLogin:
    TOKEN = ['gcloud', 'auth', 'print-access-token', '--account=me@example.test']

    @patch('rdeploy.gke_auth.subprocess.run')
    def test_signed_in(self, mock_run):
        mock_run.return_value = completed('token')
        gke_auth.ensure_gcloud_login('me@example.test')
        assert mock_run.call_count == 1
        assert mock_run.call_args[0][0] == self.TOKEN
        assert mock_run.call_args[1]['stdin'] == subprocess.DEVNULL

    @patch('rdeploy.gke_auth.subprocess.run')
    def test_reauth_prompts_in_the_terminal(self, mock_run):
        mock_run.side_effect = [
            completed(returncode=1, stderr='Reauthentication failed. cannot prompt during non-interactive execution.'),
            completed('token'),
        ]
        gke_auth.ensure_gcloud_login('me@example.test')
        retry = mock_run.call_args_list[1]
        assert retry[0][0] == self.TOKEN
        assert 'stderr' not in retry[1] and 'stdin' not in retry[1]

    @patch('rdeploy.gke_auth.subprocess.run')
    def test_expired_login_runs_gcloud_auth_login(self, mock_run, capsys):
        mock_run.side_effect = [
            completed(returncode=1, stderr='There was a problem refreshing your current auth tokens. Please run: gcloud auth login'),
            completed(),
        ]
        gke_auth.ensure_gcloud_login('me@example.test')
        assert mock_run.call_args_list[1][0][0] == ['gcloud', 'auth', 'login', 'me@example.test']
        assert 'sign in again' in capsys.readouterr().out

    @patch('rdeploy.gke_auth.subprocess.run')
    def test_failed_reauth_falls_back_to_login(self, mock_run):
        mock_run.side_effect = [
            completed(returncode=1, stderr='reauth related error (invalid_rapt)'),
            completed(returncode=1),
            completed(),
        ]
        gke_auth.ensure_gcloud_login()
        assert mock_run.call_args_list[0][0][0] == ['gcloud', 'auth', 'print-access-token']
        assert mock_run.call_args_list[2][0][0] == ['gcloud', 'auth', 'login']

    @patch('rdeploy.gke_auth.subprocess.run')
    def test_login_fails(self, mock_run):
        mock_run.side_effect = [completed(returncode=1, stderr='no credentialed accounts'), completed(returncode=1)]
        with pytest.raises(KubectlLoginError, match='gcloud sign-in failed'):
            gke_auth.ensure_gcloud_login()
