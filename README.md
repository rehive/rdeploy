# rdeploy

A CLI tool that makes it easy to build and deploy applications to Kubernetes on GCP. It wraps common tools (helm, kubectl, gcloud) into simple commands configured via a project-level YAML file.

## Installation

```bash
pip install rdeploy
```

## Quick Start

1. Create an `rdeploy.yaml` in your project root (see [Configuration](#configuration))
2. Run commands:

```bash
# Set cluster context
rdeploy set-context staging

# Build and push docker image
rdeploy cloudbuild staging v1.2.3

# Deploy to kubernetes
rdeploy upgrade staging v1.2.3

# Create a new release tag
rdeploy git-release patch
```

## Commands

| Command | Description |
|---------|-------------|
| `set-project` | Set the active GCP project |
| `set-cluster` | Set the active Kubernetes cluster |
| `set-context` | Switch cluster and namespace context |
| `kubectl-login` | Sign kubectl in to GKE under certificate-based access (see [kubectl under certificate-based access](#kubectl-under-certificate-based-access)) |
| `activate` | Fetch and set project, cluster, and namespace |
| `install` | Install a Helm chart deployment |
| `upgrade` | Upgrade a Helm deployment to a new image tag (atomic by default: waits for the rollout and rolls back on failure; use `--no-atomic` to skip, `--timeout` to adjust the wait, default `15m`). With `secretManager.enabled`, checks the secrets first (`--no-secret-check` skips it) |
| `helm` | Run arbitrary Helm commands |
| `helm-setup` | Download and configure a local Helm binary |
| `git-release` | Bump version and push a git tag |
| `next-version` | Show what the next version number would be |
| `latest-version` | Show the current latest version from git tags |
| `build` | Build and push a Docker image |
| `cloudbuild` | Build a Docker image via Google Cloud Build |
| `live-image` | Display the currently deployed image and tag |
| `shell` | Exec into a management container |
| `manage` | Run a Django management command in-cluster |
| `create-namespace` | Create a Kubernetes namespace |
| `secret list` | List a service's Secret Manager secrets and their newest version, never values (see [Secrets in Secret Manager](#secrets-in-secret-manager)) |
| `secret set` | Add a new version of a secret, from a hidden prompt or stdin |
| `secret check` | Check every key in `secretManager.keys` has an enabled version |
| `secret restart` | Restart the release's deployments so pods read new values |
| `secret migrate` | Copy values from the namespace's Kubernetes Secrets into Secret Manager |
| `upload-secrets` | Deprecated: upload secrets from an env file to a Kubernetes Secret |
| `decode-secret` | Deprecated: print decoded values of a Kubernetes Secret |
| `upload-static` | Upload static files to a GCS bucket |
| `create-bucket` | Create a private GCS bucket |
| `create-public-bucket` | Create a public GCS bucket |
| `create-volume` | Create a GCE persistent disk |
| `compose` | Wrapper for docker-compose |

## kubectl under certificate-based access

With Google Cloud certificate-based access (CBA), gcloud's tokens only work
alongside your device certificate, and kubectl can't present it.
`rdeploy kubectl-login` signs kubectl in through a separate OAuth client that
your organisation exempts from CBA instead. It asks only for
`openid email https://www.googleapis.com/auth/container.read-only`: enough to
identify you to the clusters, where Kubernetes RBAC decides what you can do,
and nothing else in Google Cloud.

The exemption is narrow and doesn't stand alone. Access to the clusters stays
layered:

- **Network isolation:** the control plane should have only a private
  endpoint, reachable from your private network through a bastion or similar
  access gateway, and limited to authorised networks. A token alone doesn't
  reach the cluster.
- **Least privilege:** the token can identify you and read GKE metadata, but
  can't call any other Google Cloud API or change clusters through the GKE API.
- **Device trust:** the OAuth client is fetched from Secret Manager with your
  gcloud login, which still needs your device certificate.
- **Short sessions:** your organisation sets how long a sign-in lasts (often
  12 hours), so a copied credentials file stops working soon after.
- **Kubernetes RBAC and audit logs:** what you can do inside a cluster, and
  the record of what you did, are unchanged.

It:

1. reads the OAuth client from Secret Manager with your gcloud login (or from
   a local file with `--client-json`),
2. opens your browser to sign in (OAuth loopback with PKCE),
3. saves the credentials to `~/.config/rdeploy/kubectl-credentials.json`,
   readable only by you,
4. finds the project's GKE clusters (`gcloud container clusters list`), adds
   any that are missing from your kubeconfig, and points their contexts at a
   kubectl user that uses those credentials.

### One-time setup

You need gcloud signed in with your work account and set up for your device
certificate, kubectl, and the GKE auth plugin
(`gcloud components install gke-gcloud-auth-plugin`). Then run this from any
project that has an `rdeploy.yaml`:

```bash
rdeploy kubectl-login
```

rdeploy takes the GCP project from that `rdeploy.yaml` and reads the OAuth
client from the Secret Manager secret `kubectl-oauth-client` in it. If your
`rdeploy.yaml` names several projects, you're elsewhere, or your organisation
uses another secret name, say so:

```bash
rdeploy kubectl-login --gcp-project my-project --secret my-secret
```

rdeploy saves the project and secret to `~/.config/rdeploy/kubectl-login.json`,
so later runs work from any directory. You can set `RDEPLOY_KUBECTL_PROJECT`
and `RDEPLOY_KUBECTL_SECRET` instead. Pass `--account you@example.com` if
gcloud's active account isn't the one to sign in with, and `--cluster NAME`
(repeatable) to configure only some clusters.

### When the session expires

Sessions last as long as your organisation allows, often 12 hours. When
kubectl fails with an authentication error such as `invalid_grant` or
`reauth related error`, sign in again:

```bash
rdeploy kubectl-login
```

If gcloud's own login has expired too, rdeploy signs gcloud in first: gcloud
asks for your password in the terminal, or opens the browser for
`gcloud auth login`.

To check whether the saved session is still valid (exits non-zero if not):

```bash
rdeploy kubectl-login --status
```

### Troubleshooting

- **`Unauthorized` right after signing in:** delete
  `~/.kube/gke_gcloud_auth_plugin_cache` and retry.
- **`access_not_configured` or "Access blocked" in the browser:** the OAuth
  client only allows the scopes above, so use `rdeploy kubectl-login` rather
  than `gcloud auth application-default login`, which always asks for more.
  If it still fails, your admin hasn't allowed the app for your account.
- **Back to the gcloud user:** `gcloud container clusters get-credentials`
  restores the default user on that cluster's context.

### For admins

The command expects a Desktop app OAuth client, exempted from CBA in your
access levels, limited to the scopes above, and stored as JSON (the file the
Cloud console downloads) in a Secret Manager secret your developers can read,
named `kubectl-oauth-client` in the project your `rdeploy.yaml` files name
unless you tell developers otherwise. Keep the session length short in the
app's session control, keep the control plane on a private endpoint behind
your access gateway, and leave CBA enforced for everything else.
rdeploy doesn't contain any organisation's client ID, project or cluster names.

## Secrets in Secret Manager

The `rehive-service` chart (1.4.0+) loads a service's secrets from Google
Secret Manager instead of a Kubernetes Secret. Each key under
`secretManager.keys` in the values file is one regional secret named
`<environment>--<service>--<KEY>`, which the pods read at startup through
Workload Identity:

```yaml
# etc/helm/staging/values.yaml
secretManager:
  enabled: true
  environment: staging
  keys: [DJANGO_SECRET, POSTGRES_PASSWORD]
```

`rdeploy secret` manages those secrets. Each command takes the rdeploy config
and reads everything else from the config's `helm_values_path`, the way the
chart does: `secretManager.environment`, `service` (default: `project_name`),
`prefix`, `project` (default: `rehive-services`) and `location` (default:
`europe-west4`).

```bash
rdeploy secret list staging              # names and newest version, never values
rdeploy secret set staging DJANGO_SECRET # value from a hidden prompt
rdeploy secret set staging DJANGO_SECRET --generate   # new random value
<command that prints the value> | rdeploy secret set staging SENTRY_DSN_KEY
rdeploy secret check staging             # every key has an enabled version
rdeploy secret restart staging           # pods read the new values
```

`rdeploy secret <action>` is short for invoke's `rdeploy secret.<action>`;
`rdeploy --help secret.set` shows each command's options.

`set` creates the secret if it's missing (labelled `env`, `service` and
`managed-by=rdeploy`) and adds a version. Values never go on the command line,
to the terminal or to disk: rdeploy passes them to gcloud on stdin. Pods read
values once, at startup, so run `restart` after `set`. `restart` restarts the
deployments labelled `app.kubernetes.io/instance=<project_name>` and waits
for each rollout.

`upgrade` runs the same check as `secret check` when the values file has
`secretManager.enabled: true`, and stops before helm if a key has no enabled
version. Otherwise new pods would wait in `ContainerCreating` until the atomic
upgrade times out. `--no-secret-check` skips it, for example if you can add
versions but not list them.

### Moving from Kubernetes Secrets

`rdeploy secret migrate` copies each key's value from the namespace's
Kubernetes Secrets into Secret Manager, then runs `secret check`. By default a
key comes from the field of the same name in the Secret named after the
release (`project_name`); `--map` points a key elsewhere:

```bash
rdeploy kubectl-login
rdeploy secret migrate staging --dry-run \
    --map POSTGRES_PASSWORD=postgres/postgres-password \
    --map RABBITMQ_PASSWORD=rabbitmq/rabbitmq-password
```

Drop `--dry-run` to copy. Values go from kubectl to gcloud in memory and are
never printed. Keys whose Secret or field doesn't exist are skipped. It stops
straight away if kubectl can't read the namespace, for example once the
`rdeploy kubectl-login` session has expired. It's safe to rerun: each run adds
a new version with the same value.

### Certificate-based access

Regional secrets are served from a regional endpoint, and under
certificate-based access from its mTLS form. rdeploy sets
`CLOUDSDK_API_ENDPOINT_OVERRIDES_SECRETMANAGER` to
`https://secretmanager.<location>.rep.mtls.googleapis.com/` when
`GOOGLE_API_USE_CLIENT_CERTIFICATE=true` or gcloud's
`context_aware/use_client_certificate` is on, and to
`https://secretmanager.<location>.rep.googleapis.com/` otherwise. Without
this, gcloud fails with `ACCESS_TOKEN_TYPE_UNSUPPORTED` or "Project ID ...
does not match the expected format". An override already in your environment
wins.

## Configuration

rdeploy is configured via an `rdeploy.yaml` file in your project root. The file supports versioned schemas (v1, v2, v3).

### Version 3 (recommended)

```yaml
version: '3'
configs:
  staging:
    project_name: my-service
    namespace: staging
    kube_context: gke_my-project_us-central1_cluster-1
    docker_image: gcr.io/my-project/my-service:latest
    cloud_provider:
      name: gcp
      project: my-project
      kube_cluster: cluster-1
      region: us-central1
      helm_registry: us-central1-docker.pkg.dev
    helm_values_path: ./etc/helm/staging/values.yaml
    helm_chart: helm-charts/rehive-service
    helm_chart_version: 0.2.0
    helm_version: 3.14.0
    use_system_helm: true

  production:
    project_name: my-service
    namespace: production
    kube_context: gke_my-project_us-central1_cluster-1
    docker_image: gcr.io/my-project/my-service:latest
    cloud_provider:
      name: gcp
      project: my-project
      kube_cluster: cluster-1
      region: us-central1
      helm_registry: us-central1-docker.pkg.dev
    helm_values_path: ./etc/helm/production/values.yaml
    helm_chart: helm-charts/rehive-service
    helm_chart_version: 0.2.0
    helm_version: 3.14.0
    use_system_helm: true
```

### Version 2

```yaml
version: '2'
configs:
  staging:
    project_name: my-service
    namespace: staging
    docker_image: gcr.io/my-project/my-service:latest
    cloud_provider:
      name: gcp
      project: my-project
      kube_cluster: cluster-1
      region: us-central1
      helm_registry: us-central1-docker.pkg.dev
    helm_values_path: ./etc/helm/staging/values.yaml
    helm_chart: rehive/rehive-service
    helm_chart_version: 0.1.38
    helm_version: 3.14.0
    use_system_helm: true
```

### Configuration Reference

| Field | Description |
|-------|-------------|
| `version` | Config schema version (`1`, `2`, or `3`) |
| `project_name` | Name of the Helm release and Kubernetes deployment |
| `namespace` | Kubernetes namespace |
| `kube_context` | (v3) Direct kubectl context name |
| `docker_image` | Docker image path |
| `cloud_provider.name` | Cloud provider (currently `gcp`) |
| `cloud_provider.project` | GCP project ID |
| `cloud_provider.kube_cluster` | Kubernetes cluster name |
| `cloud_provider.region` | GCP region |
| `cloud_provider.zone` | GCP zone (alternative to region) |
| `cloud_provider.helm_registry` | Artifact Registry host for Helm charts, e.g. `us-central1-docker.pkg.dev`. When set, the chart is downloaded with gcloud (see below) |
| `helm_values_path` | Path to Helm values file |
| `helm_chart` | Helm chart name; `REPOSITORY/CHART` when `helm_registry` is set |
| `helm_chart_version` | Helm chart version |
| `helm_version` | Helm binary version (for local installs) |
| `use_system_helm` | Use system Helm binary (default: `true`) |

#### Charts in Artifact Registry

With `helm_registry` set, `install` and `upgrade` download the chart with
gcloud instead of logging Helm in to the registry: they resolve
`helm_chart_version` to a digest, download the chart, check it against the
digest, and pass Helm the local archive. This also works when the
organization requires certificate-based access, where Artifact Registry
rejects Helm's own login. You need read access to the repository
(`roles/artifactregistry.reader`).

If gcloud's login has expired, rdeploy asks before running `gcloud auth
login`. Without a terminal, such as in a script, it stops and tells you to
run it.

## Versioning

rdeploy uses semantic versioning for releases. The `git-release` command automates tagging:

```bash
# Patch release: 1.2.3 → 1.2.4
rdeploy git-release patch

# Minor release: 1.2.3 → 1.3.0
rdeploy git-release minor

# Major release: 1.2.3 → 2.0.0
rdeploy git-release major

# Pre-release: 1.2.3 → 1.2.4-rc.1
rdeploy git-release pre-patch

# Skip confirmation prompt
rdeploy git-release patch --force
```

## Development

```bash
# Clone and install in development mode
git clone https://github.com/rehive/rdeploy.git
cd rdeploy
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run tests
pytest

# Run with coverage
coverage run -m pytest
coverage report
```

## Requirements

- Python 3.9+
- kubectl configured with cluster access (1.22 or later for `kubectl-login`)
- gcloud CLI configured and authenticated
- Helm 3.x

## Publishing to PyPI

```bash
rm -rf dist
python -m build
twine upload dist/*
```
