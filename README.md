# Rollout Operator

This repository contains a **Kopf-based Kubernetes operator** that manages **rollouts for a single StatefulSet** by driving an `OnDelete` StatefulSet update.

StatefulSets are great for stable identities and ordering, but Kubernetes’ default rollout behavior is usually **conservative and sequential**. This operator exists for the case where you want a **controlled, fast rollout** for a StatefulSet by using:

- `updateStrategy.type=OnDelete` (pods only update when you delete them)
- `podManagementPolicy: Parallel` (the controller can create pods in parallel)
- An **upper/lower split** rollout plan (two ranges) so you can roll one range while the other keeps serving traffic

In short: **it automatically deletes the right pods in the right order**, waits for readiness, and gives you knobs for delays and batching.

## How it works

Kubernetes updates the StatefulSet’s *template revision* when you change the pod template, but with `updateStrategy.type=OnDelete` it **won’t restart existing pods automatically**. This operator watches a single target StatefulSet, detects when `updateRevision != currentRevision`, and then drives the rollout by **deleting only pods that are still on the old revision** (the controller recreates them on the new revision).

If `RO_ENABLE_HALF_SPLIT=true`, the operator plans the rollout in two ranges (upper first, then lower). For `N` replicas with ordinals `0..N-1`:

- **lower**: `0..(N//2 - 1)`
- **upper**: `(N//2)..(N - 1)`

It deletes pods in batches up to `RO_MAX_UNAVAILABLE`, waits for readiness after each batch, and (optionally) waits `RO_DELAY_SECONDS` before starting. For faster rollouts, use `podManagementPolicy: Parallel`.

## Features

- Watches exactly one StatefulSet (configured via environment variables)
- Only acts on StatefulSets with `updateStrategy.type=OnDelete` (recommended with `podManagementPolicy: Parallel`)
- Requires `apps.kubernetes.io/pod-index` label on pods (StatefulSet pod index)
- Detects pod template changes via Kubernetes native revision tracking (updateRevision vs currentRevision)
- Plans a rollout with a configurable delay and logs a countdown
- Splits rollout into upper and lower halves (by ordinal) if enabled
- Deletes pods in batches up to max_unavailable, waiting for Ready after each batch

## Environment Variables

All environment variables are prefixed with `RO_`:

- `RO_TARGET_NAMESPACE` (required) - Namespace of the target StatefulSet
- `RO_TARGET_STATEFUL_SET` (required) - Name of the target StatefulSet
- `RO_DELAY_SECONDS` (default: 600) - Delay before starting rollout
- `RO_ENABLE_HALF_SPLIT` (default: true) - Split rollout into upper/lower halves
- `RO_MAX_UNAVAILABLE` (default: 2) - Maximum pods unavailable during rollout
- `RO_COUNTDOWN_LOG_INTERVAL` (default: 60) - Log countdown every N seconds

## Testing

### Integration Tests with pytest-kubernetes

The integration tests use [pytest-kubernetes](https://pypi.org/project/pytest-kubernetes/) to automatically manage local Kubernetes clusters. The tests will automatically use the first available provider:

- **k3d** (if installed) - Recommended for faster cluster creation
- **kind** (if installed) - Good alternative
- **minikube** (if installed) - Works but slower

#### Prerequisites

- Docker installed and running
- `kubectl` installed
- At least one of: `k3d`, `kind`, or `minikube` installed
  - For k3d: `brew install k3d` or see [k3d docs](https://k3d.io/)
  - For kind: `brew install kind` or see [kind docs](https://kind.sigs.k8s.io/)
  - For minikube: `brew install minikube` or see [minikube docs](https://minikube.sigs.k8s.io/)

#### Running Integration Tests

```bash
# Run all integration tests (pytest-kubernetes will create/delete cluster automatically)
pytest tests/integration/ -v

# Run with a specific provider
pytest tests/integration/ -v --k8s-provider=k3d
pytest tests/integration/ -v --k8s-provider=kind
pytest tests/integration/ -v --k8s-provider=minikube

# Run only unit tests (no cluster needed)
pytest tests/unit/ -v
```

The pytest-kubernetes plugin will:

- Automatically create a Kubernetes cluster before tests
- Build and load Docker images into the cluster
- Set up the operator and demo StatefulSet
- Clean up the cluster after tests complete

### Manual Testing with Kind

**Prerequisites:**

- Docker installed and running
- `kind` installed (`brew install kind` or see [kind docs](https://kind.sigs.k8s.io/))
- `kubectl` installed

**Quick Start:**

1. **Build local images**:

   ```bash
   docker build -t rollout-operator:test .
   docker build -t demo-app:test -f demo/Dockerfile demo
   ```

2. **Create kind cluster**:

   ```bash
   kind create cluster --name rollout-operator-test
   ```

3. **Load images into kind** (no push needed):

   ```bash
   kind load docker-image rollout-operator:test --name rollout-operator-test
   kind load docker-image demo-app:test --name rollout-operator-test
   ```

4. **Deploy operator + demo**:

   ```bash
   kubectl apply -f operator.yml
   kubectl apply -f demo/sts.yml
   ```

5. **Watch it**:

   ```bash
   kubectl get pods -n demo-sts -w
   kubectl logs -f deployment/rollout-operator -n demo-sts
   ```

6. **Trigger a rollout**:

   ```bash
   kubectl patch sts demo-sts -n demo-sts -p '{"spec":{"template":{"metadata":{"annotations":{"test":"'$(date +%s)'"}}}}}'
   ```

**Using the test scripts:**

The `run-tests.sh` script automates the above steps. It builds images locally and loads them into kind (no Docker Hub push needed unless you set `PUSH_IMAGES=true`):

```bash
./scripts/run-tests.sh
```

### Manual Testing with OrbStack (or any existing kube context)

If you already have a local cluster running (e.g. OrbStack), you can use **local Docker images** and apply the manifests directly.

1. **Make sure you’re on the right context**:

   ```bash
   kubectl config current-context
   ```

2. **Build local images** (matching the tags used in `operator.yml` and `demo/sts.yml`):

   ```bash
   docker build -t rollout-operator:test .
   docker build -t demo-app:test -f demo/Dockerfile demo
   ```

3. **Deploy operator + demo**:

   ```bash
   kubectl apply -f operator.yml
   kubectl apply -f demo/sts.yml
   ```

4. **Watch it**:

   ```bash
   kubectl get pods -n demo-sts -w
   kubectl logs -f deployment/rollout-operator -n demo-sts
   ```

5. **Trigger a rollout**:

   ```bash
   kubectl patch sts demo-sts -n demo-sts -p '{"spec":{"template":{"metadata":{"annotations":{"test":"'$(date +%s)'"}}}}}'
   ```

### Local Testing (for Development)

For local development and debugging, you can run the operator locally:

```bash
./test-local.sh
```

This will:

- Use your local kubeconfig (not in-cluster config)
- Run the operator in the foreground
- Use shorter delays for faster testing

Make sure you have:

- A Kubernetes cluster accessible via kubectl
- The target namespace and StatefulSet deployed
- Environment variables set (or defaults will be used)

### Manual Testing Steps

1. **Build local images**:

   ```bash
   docker build -t rollout-operator:test .
   docker build -t demo-app:test -f demo/Dockerfile demo
   ```

2. **Create kind cluster** (if not using run-tests.sh):

   ```bash
   kind create cluster --name rollout-operator-test
   ```

3. **Load images into kind**:

   ```bash
   kind load docker-image rollout-operator:test --name rollout-operator-test
   kind load docker-image demo-app:test --name rollout-operator-test
   ```

4. **Deploy operator**:

   ```bash
   kubectl apply -f operator.yml
   ```

5. **Deploy demo StatefulSet**:

   ```bash
   kubectl apply -f demo/sts.yml
   ```

## Troubleshooting

### Operator not starting

- Check operator logs: `kubectl logs deployment/rollout-operator -n demo-sts`
- Verify RBAC: `kubectl get role,rolebinding -n demo-sts`
- Check environment variables: `kubectl get deployment rollout-operator -n demo-sts -o yaml | grep -A 20 env`

### Rollout not triggering

- Verify StatefulSet has `updateStrategy.type=OnDelete`
- Check if pods have `apps.kubernetes.io/pod-index` label: `kubectl get pods -n demo-sts --show-labels`
- Check operator logs for errors
- Verify template hash detection: `kubectl get sts demo-sts -n demo-sts -o yaml | grep -A 5 annotations`

### Pods not becoming ready

- Check pod status: `kubectl get pods -n demo-sts`
- Check pod logs: `kubectl logs <pod-name> -n demo-sts`
- Verify readiness probes are working
- Check if STARTUP_DELAY_SECONDS is too long
