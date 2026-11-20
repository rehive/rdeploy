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
| `upgrade` | Upgrade a Helm deployment to a new image tag (atomic by default: waits for the rollout and rolls back on failure; use `--no-atomic` to skip, `--timeout` to adjust the wait, default `15m`) |
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
| `upload-secrets` | Upload secrets from an env file to Kubernetes |
| `decode-secret` | Print decoded values of a Kubernetes secret |
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
    helm_chart: rehive-service
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
    helm_chart: rehive-service
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
| `cloud_provider.helm_registry` | OCI Helm registry (GCP Artifact Registry) |
| `helm_values_path` | Path to Helm values file |
| `helm_chart` | Helm chart name or OCI reference |
| `helm_chart_version` | Helm chart version |
| `helm_version` | Helm binary version (for local installs) |
| `use_system_helm` | Use system Helm binary (default: `true`) |

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
