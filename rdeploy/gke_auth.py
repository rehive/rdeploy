"""
Sign kubectl in to GKE through a dedicated OAuth client.

Under Google Cloud certificate-based access (CBA), gcloud's tokens are bound
to the device certificate, and kubectl can't present it. An organisation can
instead exempt a dedicated OAuth client from CBA and limit it to the
`container.read-only` scope. This module signs in through that client and
points the GKE contexts in your kubeconfig at the resulting credentials.

The exemption narrows one control; it doesn't replace the others. The tokens
can't call any Google Cloud API beyond reading GKE metadata, Kubernetes RBAC
still decides what each person can do in a cluster, and the clusters should
stay reachable only from a private network: a private control-plane endpoint,
reached through a bastion or similar access gateway, with an authorised
network allowlist.

Nothing organisation-specific lives here: the GCP project, the Secret Manager
secret holding the OAuth client and the clusters all come from flags,
environment variables, the user's saved settings or the project's
rdeploy.yaml, and clusters are discovered with `gcloud container clusters list`.
"""
import base64
import hashlib
import http.server
import json
import os
import secrets as py_secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
import webbrowser

import requests
import yaml

CONTAINER_SCOPE = 'https://www.googleapis.com/auth/container.read-only'
# Exactly these scopes. `gcloud auth application-default login` always adds
# `cloud-platform`, which a CBA-exempt client should not be allowed to get.
SCOPES = f'openid email {CONTAINER_SCOPE}'

KUBE_USER = 'rdeploy-kubectl'
EXEC_API_VERSION = 'client.authentication.k8s.io/v1beta1'
EXEC_COMMAND = 'gke-gcloud-auth-plugin'
PLUGIN_CACHE = os.path.join(os.path.expanduser('~'), '.kube', 'gke_gcloud_auth_plugin_cache')

ENV_PROJECT = 'RDEPLOY_KUBECTL_PROJECT'
ENV_SECRET = 'RDEPLOY_KUBECTL_SECRET'
DEFAULT_SECRET = 'kubectl-oauth-client'

SIGN_IN_TIMEOUT = 300  # seconds to wait for the browser sign-in

LOGIN_COMMAND = 'rdeploy kubectl-login'


class KubectlLoginError(Exception):
    pass


# Paths and saved settings
##########################

def config_dir():
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(base, 'rdeploy')


def settings_path():
    return os.path.join(config_dir(), 'kubectl-login.json')


def credentials_path():
    return os.path.join(config_dir(), 'kubectl-credentials.json')


def load_saved_settings():
    try:
        with open(settings_path()) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError:
        raise KubectlLoginError(f'{settings_path()} is not valid JSON. Fix or delete it.')


def save_settings(settings):
    os.makedirs(config_dir(), mode=0o700, exist_ok=True)
    with open(settings_path(), 'w') as f:
        json.dump(settings, f, indent=2, sort_keys=True)
        f.write('\n')


def rdeploy_yaml_project(path='rdeploy.yaml'):
    """
    The GCP project named in the current directory's rdeploy.yaml, or None if
    there is no file or it names none. Raise if its configs name several.
    """
    if not os.path.exists(path):
        return None
    with open(path) as f:
        settings = yaml.safe_load(f) or {}
    projects = set()
    for config in (settings.get('configs') or {}).values():
        config = config or {}
        provider = config.get('cloud_provider') or {}
        project = provider.get('project') or config.get('cloud_project')
        if project:
            projects.add(project)
    if len(projects) > 1:
        raise KubectlLoginError(
            '{} names several GCP projects ({}). Pass --gcp-project to pick one.'
            .format(path, ', '.join(sorted(projects))))
    return projects.pop() if projects else None


def resolve_settings(project=None, secret=None, account=None, clusters=None):
    """
    Merge flags, environment variables and saved settings, in that order of
    precedence. Without any of those, take the project from rdeploy.yaml in
    the current directory and use the default secret name.
    """
    saved = load_saved_settings()
    resolved = {
        'project': (project or os.environ.get(ENV_PROJECT) or saved.get('project')
                    or rdeploy_yaml_project()),
        'secret': secret or os.environ.get(ENV_SECRET) or saved.get('secret') or DEFAULT_SECRET,
        'account': account or saved.get('account'),
        'clusters': list(clusters) if clusters else saved.get('clusters') or [],
    }
    if not resolved['project']:
        raise KubectlLoginError(
            'rdeploy doesn\'t know which GCP project to use yet. Run this from a'
            ' directory with an rdeploy.yaml, or pass --gcp-project (the project'
            ' with your GKE clusters and the OAuth client secret), or set {}.'
            ' It is saved to {} for next time.'.format(ENV_PROJECT, settings_path()))
    return resolved


# Prerequisites
###############

def check_tools(need_kubeconfig=True):
    tools = ['gcloud']
    if need_kubeconfig:
        tools += ['kubectl', EXEC_COMMAND]
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        hint = ''
        if EXEC_COMMAND in missing:
            hint = f' Install the GKE plugin with `gcloud components install {EXEC_COMMAND}`.'
        raise KubectlLoginError('Not found on PATH: {}.{}'.format(', '.join(missing), hint))


def gcloud_account():
    """The active gcloud account, used as a sign-in hint. Empty if unknown."""
    result = subprocess.run(['gcloud', 'config', 'get-value', 'account'],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ''


# OAuth client
##############

def _installed_client(data, source):
    client = data.get('installed') if isinstance(data, dict) else None
    if not client or not all(client.get(k) for k in ('client_id', 'client_secret', 'auth_uri', 'token_uri')):
        raise KubectlLoginError(
            f'{source} isn\'t a Desktop app OAuth client file (expected an "installed" client).')
    return client


def run_gcloud(cmd):
    """
    Run a gcloud command, capturing only its output. Its prompts and errors go
    to the terminal, so gcloud can ask for a password when the login needs
    reauthentication; with stderr captured it refuses to prompt.
    """
    return subprocess.run(cmd, stdout=subprocess.PIPE, text=True)


def load_client(project, secret, account=None, client_json=None):
    """Read the OAuth client from a local file, or from Secret Manager with the user's gcloud login."""
    if client_json:
        with open(client_json) as f:
            return _installed_client(json.load(f), client_json)

    cmd = ['gcloud', 'secrets', 'versions', 'access', 'latest',
           f'--secret={secret}', f'--project={project}']
    if account:
        cmd.append(f'--account={account}')
    result = run_gcloud(cmd)
    if result.returncode != 0:
        raise KubectlLoginError(
            'Couldn\'t read the OAuth client from Secret Manager'
            f' (secret {secret} in project {project}); gcloud\'s error is above.'
            ' Check that gcloud is signed in with your work account and uses your'
            ' device certificate (`gcloud auth list`).')
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise KubectlLoginError(f'Secret {secret} doesn\'t contain JSON.')
    return _installed_client(data, f'Secret {secret}')


# Sign-in
#########

def pkce_pair():
    verifier = py_secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return verifier, challenge


def authorization_url(client, redirect_uri, state, challenge, login_hint=None):
    params = {
        'client_id': client['client_id'],
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': SCOPES,
        'access_type': 'offline',
        'prompt': 'consent',
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    }
    if login_hint:
        params['login_hint'] = login_hint
    return client['auth_uri'] + '?' + urllib.parse.urlencode(params)


def wait_for_redirect(server, timeout=SIGN_IN_TIMEOUT):
    """Serve the loopback redirect until it brings a code or an error."""
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if 'code' in query or 'error' in query:
                result.update({k: v[0] for k, v in query.items()})
                message = b'Signed in. You can close this tab and return to the terminal.'
                if 'error' in query:
                    message = b'Sign-in failed. Return to the terminal for details.'
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write(message)
            else:
                # e.g. the browser asking for /favicon.ico
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    server.RequestHandlerClass = Handler
    server.timeout = 5
    deadline = time.monotonic() + timeout
    while not result:
        if time.monotonic() > deadline:
            raise KubectlLoginError('Timed out waiting for the browser sign-in.')
        server.handle_request()
    return result


def exchange_code(client, code, redirect_uri, verifier):
    response = requests.post(client['token_uri'], data={
        'code': code,
        'client_id': client['client_id'],
        'client_secret': client['client_secret'],
        'redirect_uri': redirect_uri,
        'grant_type': 'authorization_code',
        'code_verifier': verifier,
    }, timeout=30)
    if response.status_code != 200:
        raise KubectlLoginError(f'Token exchange failed: {response.text.strip()}')
    token = response.json()
    granted = set(token.get('scope', '').split())
    if CONTAINER_SCOPE not in granted:
        raise KubectlLoginError(
            'Sign-in didn\'t grant the Kubernetes Engine scope (got: {}). Tick every'
            ' box on the consent screen and try again.'.format(' '.join(sorted(granted)) or 'nothing'))
    if not token.get('refresh_token'):
        raise KubectlLoginError('Sign-in didn\'t return a refresh token. Try again.')
    return token


def sign_in(client, login_hint=None, open_browser=webbrowser.open):
    """Run an OAuth loopback + PKCE sign-in and return the token response."""
    verifier, challenge = pkce_pair()
    state = py_secrets.token_urlsafe(16)
    server = http.server.HTTPServer(('127.0.0.1', 0), http.server.BaseHTTPRequestHandler)
    try:
        redirect_uri = f'http://127.0.0.1:{server.server_port}'
        url = authorization_url(client, redirect_uri, state, challenge, login_hint)
        print('Opening your browser to sign in. If it doesn\'t open, visit:\n' + url, flush=True)
        open_browser(url)
        result = wait_for_redirect(server)
    finally:
        server.server_close()

    if 'error' in result:
        hint = ''
        if result['error'] in ('access_denied', 'access_not_configured', 'admin_policy_enforced'):
            hint = (' Your organisation\'s admin may not have allowed this app for your'
                    ' account yet.')
        raise KubectlLoginError(f'Sign-in failed: {result["error"]}.{hint}')
    if result.get('state') != state:
        raise KubectlLoginError('Sign-in failed: the response didn\'t match this sign-in. Try again.')
    return exchange_code(client, result['code'], redirect_uri, verifier)


def write_credentials(client, refresh_token, path=None):
    """Write an `authorized_user` credentials file readable only by the user."""
    path = path or credentials_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({
            'type': 'authorized_user',
            'client_id': client['client_id'],
            'client_secret': client['client_secret'],
            'refresh_token': refresh_token,
        }, f)
    # O_CREAT's mode only applies to new files.
    os.chmod(path, 0o600)
    return path


# Session status
################

TOKEN_URI = 'https://oauth2.googleapis.com/token'


def session_status(path=None):
    """
    Return (ok, message) for the saved credentials, by trying to refresh them.
    Google refuses the refresh once the session length set by the admin is up.
    """
    path = path or credentials_path()
    try:
        with open(path) as f:
            creds = json.load(f)
    except FileNotFoundError:
        return False, 'Not signed in.'
    except ValueError:
        return False, f'{path} is not valid JSON.'

    try:
        response = requests.post(TOKEN_URI, data={
            'client_id': creds.get('client_id'),
            'client_secret': creds.get('client_secret'),
            'refresh_token': creds.get('refresh_token'),
            'grant_type': 'refresh_token',
        }, timeout=30)
    except requests.RequestException as e:
        return False, f'Couldn\'t reach Google to check the session: {e}'

    if response.status_code == 200:
        scopes = set(response.json().get('scope', '').split())
        if CONTAINER_SCOPE not in scopes:
            return False, 'Signed in, but without the Kubernetes Engine scope.'
        return True, 'Signed in; the session is valid.'
    try:
        error = response.json().get('error_description') or response.json().get('error')
    except ValueError:
        error = response.text.strip()
    return False, f'The session has expired or was revoked ({error}).'


# kubeconfig
############

def list_clusters(project, account=None):
    """[(name, location)] of the project's GKE clusters."""
    cmd = ['gcloud', 'container', 'clusters', 'list', f'--project={project}',
           '--format=json(name,location)']
    if account:
        cmd.append(f'--account={account}')
    result = run_gcloud(cmd)
    if result.returncode != 0:
        raise KubectlLoginError(
            f'Couldn\'t list the GKE clusters in {project}; gcloud\'s error is above.')
    return [(c['name'], c['location']) for c in json.loads(result.stdout or '[]')]


def gke_cluster_entry(project, location, name):
    """The kubeconfig cluster entry name `gcloud container clusters get-credentials` writes."""
    return f'gke_{project}_{location}_{name}'


def kubectl_config(*args):
    result = subprocess.run(['kubectl', 'config', *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise KubectlLoginError(
            'kubectl config {} failed: {}'.format(args[0], result.stderr.strip()))
    return result.stdout


def kubeconfig_contexts():
    """{context name: cluster entry name} from the merged kubeconfig."""
    view = json.loads(kubectl_config('view', '-o', 'json') or '{}')
    return {c['name']: (c.get('context') or {}).get('cluster')
            for c in view.get('contexts') or []}


def current_context():
    result = subprocess.run(['kubectl', 'config', 'current-context'], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ''


def get_credentials(project, location, name, account=None):
    cmd = ['gcloud', 'container', 'clusters', 'get-credentials', name,
           f'--location={location}', f'--project={project}']
    if account:
        cmd.append(f'--account={account}')
    print('Adding {} to your kubeconfig'.format(name), flush=True)
    result = run_gcloud(cmd)
    if result.returncode != 0:
        raise KubectlLoginError(
            f'Couldn\'t add cluster {name} to your kubeconfig; gcloud\'s error is above.')


def configure_kubeconfig(project, creds_path, account=None, only_clusters=()):
    """
    Make sure the kubeconfig has a context for each of the project's clusters
    and point those contexts at a user that authenticates with `creds_path`.
    Return the context names it changed.
    """
    clusters = list_clusters(project, account)
    if only_clusters:
        unknown = set(only_clusters) - {name for name, _ in clusters}
        if unknown:
            raise KubectlLoginError('No such cluster in {}: {}'.format(project, ', '.join(sorted(unknown))))
        clusters = [c for c in clusters if c[0] in only_clusters]
    if not clusters:
        raise KubectlLoginError(f'No GKE clusters found in {project}.')

    contexts = kubeconfig_contexts()
    entries = {gke_cluster_entry(project, location, name): (name, location)
               for name, location in clusters}

    missing = [entry for entry in entries if entry not in contexts.values()]
    if missing:
        # get-credentials switches the current context; put it back afterwards.
        previous = current_context()
        for entry in missing:
            name, location = entries[entry]
            get_credentials(project, location, name, account)
        if previous:
            kubectl_config('use-context', previous)
        contexts = kubeconfig_contexts()

    # Only the flags kubectl has had since 1.22: no --exec-interactive-mode,
    # --exec-provide-cluster-info and the like.
    kubectl_config(
        'set-credentials', KUBE_USER,
        f'--exec-api-version={EXEC_API_VERSION}',
        f'--exec-command={EXEC_COMMAND}',
        '--exec-arg=--use_application_default_credentials',
        f'--exec-env=GOOGLE_APPLICATION_CREDENTIALS={creds_path}',
    )
    changed = sorted(ctx for ctx, cluster in contexts.items() if cluster in entries)
    for ctx in changed:
        kubectl_config('set-context', ctx, f'--user={KUBE_USER}')

    # The plugin caches the last token it got; drop it so the new user takes effect.
    if os.path.exists(PLUGIN_CACHE):
        os.remove(PLUGIN_CACHE)
    return changed


# Entry point
#############

def login(project=None, secret=None, account=None, clusters=None, client_json=None,
          skip_kubeconfig=False, open_browser=webbrowser.open):
    check_tools(need_kubeconfig=not skip_kubeconfig)
    settings = resolve_settings(project, secret, account, clusters)

    client = load_client(settings['project'], settings['secret'], settings['account'], client_json)
    token = sign_in(client, settings['account'] or gcloud_account(), open_browser)
    path = write_credentials(client, token['refresh_token'])
    print(f'Saved credentials to {path}')

    # Save after a successful sign-in, so a typo isn't remembered.
    save_settings({k: v for k, v in settings.items() if v})

    if not skip_kubeconfig:
        changed = configure_kubeconfig(settings['project'], path, settings['account'],
                                       settings['clusters'])
        print('kubectl contexts now using these credentials:\n  ' + '\n  '.join(changed))

    print('\nThe session lasts as long as your organisation allows (often 12 hours).'
          f' When kubectl reports an authentication error, run `{LOGIN_COMMAND}` again.')


def status():
    ok, message = session_status()
    print(message)
    if not ok:
        print(f'Run `{LOGIN_COMMAND}` to sign in again.')
    return ok


def run(fn, *args, **kwargs):
    """Run `fn`, turning KubectlLoginError into a clean exit."""
    try:
        return fn(*args, **kwargs)
    except KubectlLoginError as e:
        sys.exit(str(e))
