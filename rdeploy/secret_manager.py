"""
`rdeploy secret ...`: a service's secrets in Google Secret Manager.

The rehive-service chart (1.4.0+) mounts one regional secret per key in
`secretManager.keys`, named `<environment>--<service>--<KEY>`, with the GKE
Secret Manager add-on. These tasks read the same settings from the config's
`helm_values_path`, so they always name the secrets the pods load.

Secret values never go on a command line, to the terminal, to a log or to
disk: they are held in memory and passed to gcloud on stdin.
"""
import base64
import getpass
import json
import os
import re
import secrets as py_secrets
import shlex
import subprocess
import sys

import yaml
from invoke import task

from rdeploy.utils import get_settings, kubectl_target_flags

KEY_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

ACTIONS = ('list', 'set', 'check', 'restart', 'migrate')


class SecretManagerError(Exception):
    pass


# Settings
##########
class SecretSettings:
    """Where a config's secrets live, resolved the way the chart does."""

    def __init__(self, config, config_dict, values):
        sm = values.get('secretManager') or {}
        self.config = config
        self.config_dict = config_dict
        self.enabled = bool(sm.get('enabled'))
        self.keys = list(sm.get('keys') or [])
        # The chart has its own defaults for these; rdeploy falls back to
        # the config's cloud_provider instead, so set them in the values
        # file when the two differ. A zone isn't a secret location.
        provider = config_dict.get('cloud_provider') or {}
        self.project = sm.get('project') or provider.get('project')
        self.location = sm.get('location') or provider.get('region')
        # The chart's fullname: the release name, which is project_name.
        self.service = (sm.get('service')
                        or str(config_dict['project_name'])[:63].rstrip('-'))
        self.environment = sm.get('environment') or ''
        if sm.get('prefix'):
            self.prefix = sm['prefix']
        elif self.environment:
            self.prefix = '{env}--{service}--'.format(
                env=self.environment, service=self.service)
        else:
            self.prefix = None
        self.release = config_dict['project_name']

    def name_of(self, key):
        return '{prefix}{key}'.format(prefix=self.prefix, key=key)

    def labels(self):
        return {'env': self.environment or self.config,
                'service': self.service,
                'managed-by': 'rdeploy'}


def read_values(config_dict):
    """The config's Helm values, or {} when it has no values file."""
    path = config_dict.get('helm_values_path')
    if not path or not os.path.exists(path):
        return {}
    with open(path) as stream:
        return yaml.safe_load(stream) or {}


def load_settings(config):
    settings_dict = get_settings()
    try:
        config_dict = settings_dict['configs'][config]
    except KeyError:
        sys.exit("No config '{config}' in rdeploy.yaml.".format(config=config))
    path = config_dict.get('helm_values_path')
    if not path or not os.path.exists(path):
        sys.exit("Can't read the Helm values for '{config}' (helm_values_path:"
                 " {path}).".format(config=config, path=path))
    smset = SecretSettings(config, config_dict, read_values(config_dict))
    if not smset.keys:
        sys.exit('No secretManager.keys in {path}.'.format(path=path))
    if smset.prefix is None:
        sys.exit('Set secretManager.environment (e.g. {config}) in {path}.'
                 .format(config=config, path=path))
    if not smset.project:
        sys.exit('Set secretManager.project in {path} (or cloud_provider.project'
                 ' in rdeploy.yaml).'.format(path=path))
    if not smset.location:
        sys.exit('Set secretManager.location in {path} (or cloud_provider.region'
                 ' in rdeploy.yaml).'.format(path=path))
    for key in smset.keys:
        if not KEY_PATTERN.match(str(key)):
            sys.exit("secretManager.keys: '{key}' is not a valid environment"
                     " variable name.".format(key=key))
    return settings_dict, config_dict, smset


# gcloud
########
def _uses_client_certificate():
    if os.environ.get('GOOGLE_API_USE_CLIENT_CERTIFICATE', '').lower() == 'true':
        return True
    try:
        result = subprocess.run(
            ['gcloud', 'config', 'get', 'context_aware/use_client_certificate'],
            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError:
        return False
    return result.stdout.strip().lower() == 'true'


def gcloud_env(location):
    """
    The environment for gcloud secrets commands.

    Regional secrets live behind a regional endpoint; some gcloud versions
    send --location requests to the global one and fail with "Project ID ...
    does not match the expected format". Under certificate-based access the
    request has to go to the mTLS form of that endpoint, or Google refuses it
    with ACCESS_TOKEN_TYPE_UNSUPPORTED. An override already in the
    environment wins.
    """
    env = dict(os.environ)
    if not env.get('CLOUDSDK_API_ENDPOINT_OVERRIDES_SECRETMANAGER'):
        host = 'rep.mtls' if _uses_client_certificate() else 'rep'
        env['CLOUDSDK_API_ENDPOINT_OVERRIDES_SECRETMANAGER'] = (
            'https://secretmanager.{location}.{host}.googleapis.com/'
            .format(location=location, host=host))
    return env


class Gcloud:
    """Runs `gcloud secrets` for one project and location."""

    def __init__(self, project, location):
        self.scope = ['--project={0}'.format(project),
                      '--location={0}'.format(location)]
        self.env = gcloud_env(location)

    def run(self, args, data=None):
        """
        Run gcloud. `data` (bytes) goes to stdin and nowhere else; stdout is
        only parsed by the caller, never echoed.
        """
        try:
            return subprocess.run(
                ['gcloud', 'secrets'] + args + self.scope + ['--quiet'],
                input=data if data is not None else b'',
                capture_output=True, env=self.env)
        except OSError as err:
            raise SecretManagerError("can't run gcloud: {err}".format(err=err))

    @staticmethod
    def error(result):
        return result.stderr.decode(errors='replace').strip()

    @staticmethod
    def not_found(result):
        err = result.stderr.decode(errors='replace')
        return 'NOT_FOUND' in err or 'not found' in err.lower()

    def latest_version(self, name):
        """
        The newest version of a secret, which `versions/latest` (what the
        pods mount) resolves to, as {'version', 'state', 'created'}; None
        when the secret or any version is missing.
        """
        result = self.run(['versions', 'list', name, '--limit=1',
                           '--sort-by=~createTime', '--format=json'])
        if result.returncode:
            if self.not_found(result):
                return None
            raise SecretManagerError('{name}: {err}'.format(
                name=name, err=self.error(result)))
        versions = json.loads(result.stdout or b'[]')
        if not versions:
            return None
        version = versions[0]
        return {'version': version.get('name', '').rsplit('/', 1)[-1],
                'state': version.get('state', ''),
                'created': _short_time(version.get('createTime', ''))}

    def secret_names(self, prefix):
        result = self.run(['list', '--filter=name:{0}'.format(prefix),
                           '--format=value(name.basename())'])
        if result.returncode:
            raise SecretManagerError(self.error(result))
        names = result.stdout.decode().split()
        return sorted(n for n in names if n.startswith(prefix))

    def add_version(self, name, labels, data):
        """Add `data` as a new version, creating the secret if it's missing.
        Returns True when the secret was created."""
        created = False
        result = self.run(['versions', 'add', name, '--data-file=-'], data=data)
        if result.returncode and self.not_found(result):
            create = self.run(['create', name, '--labels={0}'.format(
                ','.join('{0}={1}'.format(k, v) for k, v in labels.items()))])
            if create.returncode:
                raise SecretManagerError("can't create {name}: {err}".format(
                    name=name, err=self.error(create)))
            created = True
            result = self.run(['versions', 'add', name, '--data-file=-'],
                              data=data)
        if result.returncode:
            raise SecretManagerError("can't add a version to {name}: {err}"
                                     .format(name=name, err=self.error(result)))
        return created


def _short_time(timestamp):
    """'2026-11-25T11:57:54.914113Z' -> '2026-11-25 11:57 UTC'."""
    match = re.match(r'^(\d{4}-\d\d-\d\d)T(\d\d:\d\d)', timestamp)
    return '{0} {1} UTC'.format(*match.groups()) if match else timestamp


def _gcloud(smset):
    return Gcloud(smset.project, smset.location)


def _put(gcloud, smset, key, data):
    name = smset.name_of(key)
    if gcloud.add_version(name, smset.labels(), data):
        print('created {name}'.format(name=name))
    print('set     {name}'.format(name=name))


# check, also used by `rdeploy upgrade`
#######################################
def check_keys(smset, gcloud=None):
    """Print each key's state; True when every key's newest version is
    enabled."""
    gcloud = gcloud or _gcloud(smset)
    ok = True
    for key in smset.keys:
        name = smset.name_of(key)
        try:
            latest = gcloud.latest_version(name)
        except SecretManagerError as err:
            print('ERROR    {err}'.format(err=err))
            ok = False
            continue
        if latest is None:
            print('MISSING  {name}'.format(name=name))
            ok = False
        elif latest['state'] != 'ENABLED':
            print('{state:<8} {name} (version {version})'.format(
                state=latest['state'] or 'UNKNOWN', name=name,
                version=latest['version']))
            ok = False
        else:
            print('ok       {name} (version {version})'.format(
                name=name, version=latest['version']))
    return ok


def check_before_release(config, config_dict):
    """For `rdeploy upgrade`: when the values turn Secret Manager on, stop
    before helm if a key has no enabled version."""
    values = read_values(config_dict)
    if not (values.get('secretManager') or {}).get('enabled'):
        return
    _, _, smset = load_settings(config)
    print('Checking Secret Manager secrets ({config}):'.format(config=config))
    if not check_keys(smset):
        sys.exit("Pods can't start until every key above has an enabled"
                 " version. Add one with `rdeploy secret set {config} <KEY>`,"
                 " or skip this check with --no-secret-check."
                 .format(config=config))


# Tasks
#######
@task(name='list', help={'config': 'rdeploy config, e.g. staging'})
def list_secrets(ctx, config):
    """
    List the service's secrets and their newest version, never values

    Shows every key in secretManager.keys, and any other secret named with
    the service's prefix.
    """
    _, _, smset = load_settings(config)
    gcloud = _gcloud(smset)
    try:
        names = set(gcloud.secret_names(smset.prefix))
    except SecretManagerError as err:
        sys.exit("Can't list secrets: {err}".format(err=err))
    names.update(smset.name_of(k) for k in smset.keys)
    loaded = {smset.name_of(k) for k in smset.keys}
    width = max(len(n) for n in names)
    for name in sorted(names):
        try:
            latest = gcloud.latest_version(name)
        except SecretManagerError as err:
            sys.exit("Can't read {name}: {err}".format(name=name, err=err))
        if latest is None:
            state = 'missing'
        else:
            state = 'version {version:<4} {state:<9} {created}'.format(**latest)
        note = '' if name in loaded else '  (not in secretManager.keys)'
        print('{name:<{width}}  {state}{note}'.format(
            name=name, width=width, state=state, note=note))


def _read_value(key, config):
    if sys.stdin.isatty():
        value = getpass.getpass('{key} for {config} (hidden): '.format(
            key=key, config=config))
        again = getpass.getpass('again: ')
        if value != again:
            sys.exit("Values don't match.")
    else:
        # A trailing newline (from echo or a file) is dropped, as the
        # chart's with-secrets loader would drop it anyway.
        value = sys.stdin.read().rstrip('\r\n')
    if not value:
        sys.exit('Empty value.')
    return value.encode()


@task(name='set', help={
    'config': 'rdeploy config, e.g. staging',
    'key': 'Environment variable name, e.g. DJANGO_SECRET',
    'generate': 'Set a new random value (64 URL-safe characters)',
})
def set_secret(ctx, config, key, generate=False):
    """
    Add a new version of a secret

    Reads the value from a hidden prompt, or from stdin when it isn't a
    terminal, never from the command line. Creates the secret
    <environment>--<service>--<KEY> if it doesn't exist. Pods read values at
    startup: run `rdeploy secret restart` afterwards.
    """
    _, _, smset = load_settings(config)
    if not KEY_PATTERN.match(key):
        sys.exit("'{key}' is not a valid environment variable name.".format(key=key))
    if key not in smset.keys:
        print("note: {key} isn't in secretManager.keys in {path}, so pods"
              " won't load it.".format(key=key,
                                       path=smset.config_dict['helm_values_path']),
              file=sys.stderr)
    data = (py_secrets.token_urlsafe(48).encode() if generate
            else _read_value(key, config))
    try:
        _put(_gcloud(smset), smset, key, data)
    except SecretManagerError as err:
        sys.exit(str(err))
    print('Restart to apply: rdeploy secret restart {config}'.format(config=config))


@task(name='check', help={'config': 'rdeploy config, e.g. staging'})
def check(ctx, config):
    """
    Check every key in secretManager.keys has an enabled version

    Exits nonzero otherwise: pods would stay in ContainerCreating.
    """
    _, _, smset = load_settings(config)
    if not check_keys(smset):
        sys.exit("Pods can't start until every key above has an enabled"
                 " version (rdeploy secret set {config} <KEY>).".format(config=config))


@task(name='restart', help={
    'config': 'rdeploy config, e.g. staging',
    'timeout': 'How long to wait for each rollout (default 5m)',
})
def restart(ctx, config, timeout='5m'):
    """
    Restart the release's deployments so pods read new secret values
    """
    settings_dict, config_dict, smset = load_settings(config)
    target = kubectl_target_flags(settings_dict, config_dict)
    selector = 'app.kubernetes.io/instance={0}'.format(smset.release)
    result = ctx.run('kubectl {target} get deployments -l {selector} -o name'
                     .format(target=target, selector=shlex.quote(selector)),
                     hide='stdout', echo=True, warn=True)
    if result.failed:
        sys.exit("Can't list deployments in {ns}; is `rdeploy kubectl-login`"
                 " still valid?".format(ns=config_dict['namespace']))
    deployments = result.stdout.split()
    if not deployments:
        sys.exit('No deployments with {selector} in {ns}.'.format(
            selector=selector, ns=config_dict['namespace']))
    names = ' '.join(shlex.quote(d) for d in deployments)
    ctx.run('kubectl {target} rollout restart {names}'
            .format(target=target, names=names), echo=True)
    for deployment in deployments:
        ctx.run('kubectl {target} rollout status {name} --timeout={timeout}'
                .format(target=target, name=shlex.quote(deployment),
                        timeout=shlex.quote(timeout)), echo=True)


def parse_mapping(keys, release, overrides):
    """
    {KEY: (Kubernetes Secret, field)}: each key from the Secret named after
    the release by default, overridden by `KEY=secret/field` entries.
    """
    mapping = {key: (release, key) for key in keys}
    for entry in overrides or []:
        key, sep, source = entry.partition('=')
        secret, slash, field = source.partition('/')
        if not (sep and slash and secret and field and KEY_PATTERN.match(key)):
            sys.exit("--map takes KEY=secret/field, not '{entry}'.".format(entry=entry))
        mapping[key] = (secret, field)
    return mapping


def _kubectl(settings_dict, config_dict, args):
    """kubectl with output captured in memory (Secret data stays there)."""
    cmd = ['kubectl'] + shlex.split(kubectl_target_flags(settings_dict, config_dict)) + args
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)
    except OSError as err:
        sys.exit("Can't run kubectl: {err}".format(err=err))


@task(name='migrate', iterable=['map'], help={
    'config': 'rdeploy config, e.g. staging',
    'map': 'Where a key comes from, as KEY=secret/field (repeatable). By'
           ' default KEY comes from the field KEY of the Secret named'
           ' after the release (project_name).',
    'dry-run': 'Only show what would be copied',
})
def migrate(ctx, config, map=None, dry_run=False):
    """
    Copy secret values from the namespace's Kubernetes Secrets

    For the move to Secret Manager. Each value goes from kubectl to gcloud
    in memory, through stdin; it's never printed or written to disk. Keys
    whose Secret or field isn't there are skipped. Safe to rerun: each run
    adds a new version. Runs `rdeploy secret check` at the end.
    """
    settings_dict, config_dict, smset = load_settings(config)
    mapping = parse_mapping(smset.keys, smset.release, map)
    namespace = config_dict['namespace']

    # Fail early when the cluster can't be read (e.g. the kubectl-login
    # session expired), rather than reporting every key as missing.
    listed = _kubectl(settings_dict, config_dict,
                      ['get', 'secrets', '-o', 'name'])
    if listed.returncode:
        sys.exit("Can't read Secrets in {ns}: {err}\nRun `rdeploy kubectl-login`"
                 " and try again.".format(ns=namespace, err=listed.stderr.strip()))
    available = {line.split('/', 1)[-1] for line in listed.stdout.split()}

    data = {}
    for secret in sorted({s for s, _ in mapping.values()} & available):
        result = _kubectl(settings_dict, config_dict,
                          ['get', 'secret', secret, '-o', 'json'])
        if result.returncode:
            sys.exit("Can't read Secret {secret}: {err}".format(
                secret=secret, err=result.stderr.strip()))
        data[secret] = json.loads(result.stdout).get('data') or {}

    plan = []
    for key, (secret, field) in mapping.items():
        source = '{0}/{1}'.format(secret, field)
        if key not in smset.keys:
            print('note    {key} isn\'t in secretManager.keys, so pods won\'t'
                  ' load it'.format(key=key))
        if secret not in data:
            print('skip    {key} (no Secret {secret} in {ns})'.format(
                key=key, secret=secret, ns=namespace))
        elif not data[secret].get(field):
            print('skip    {key} ({source} not set in {ns})'.format(
                key=key, source=source, ns=namespace))
        else:
            plan.append((key, secret, field))
            print('copy    {source} -> {name}'.format(
                source=source, name=smset.name_of(key)))

    if dry_run or not plan:
        return

    gcloud = _gcloud(smset)
    for key, secret, field in plan:
        value = base64.b64decode(data[secret][field])
        try:
            _put(gcloud, smset, key, value)
        except SecretManagerError as err:
            sys.exit(str(err))

    if not check_keys(smset, gcloud):
        sys.exit("Some keys still have no enabled version"
                 " (rdeploy secret set {config} <KEY>).".format(config=config))
    print('Restart to apply: rdeploy secret restart {config}'.format(config=config))
