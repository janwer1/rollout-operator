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
- Docker Hub account (logged in via `docker login`)
- `kind` installed (`brew install kind` or see [kind docs](https://kind.sigs.k8s.io/))
- `kubectl` installed

**Quick Start:**

1. **Set your Docker Hub username** (optional, will try to auto-detect):

   ```bash
   export DOCKER_USER=your-dockerhub-username
   ```

2. **Run the full test suite**:

   ```bash
   ./run-tests.sh
   ```

   This will:
   - Build and push operator and demo images to Docker Hub
   - Create a kind cluster
   - Deploy the operator
   - Deploy a demo StatefulSet with 32 replicas
   - Wait for everything to be ready

3. **Test a rollout**:

   ```bash
   ./test-rollout.sh
   ```

   Or manually trigger a rollout:

   ```bash
   kubectl patch sts demo-sts -n demo-sts -p '{"spec":{"template":{"metadata":{"annotations":{"test":"'$(date +%s)'"}}}}}'
   ```

4. **Debug the operator**:

   ```bash
   ./debug-operator.sh
   ```

5. **Watch operator logs**:

   ```bash
   kubectl logs -f deployment/rollout-operator -n demo-sts
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

1. **Build and push images**:

   ```bash
   ./build-images.sh
   ```

2. **Create kind cluster** (if not using run-tests.sh):

   ```bash
   kind create cluster --name rollout-operator-test
   ```

3. **Load images into kind**:

   ```bash
   kind load docker-image $DOCKER_USER/rollout-operator:test --name rollout-operator-test
   kind load docker-image $DOCKER_USER/demo-app:test --name rollout-operator-test
   ```

4. **Deploy operator**:

   ```bash
   # Update image name in operator.yml first, then:
   kubectl apply -f operator.yml
   ```

5. **Deploy demo StatefulSet**:

   ```bash
   # Update image name in demo/sts.yml first, then:
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

## Files

- `rollout_operator.py` - Main operator code
- `operator.yml` - Kubernetes manifests for the operator
- `demo/sts.yml` - Demo StatefulSet for testing
- `demo/app.py` - Simple HTTP server for demo pods
- `run-tests.sh` - Full test suite with kind
- `test-local.sh` - Local testing script
- `test-rollout.sh` - Test rollout functionality
- `debug-operator.sh` - Debug information script
- `build-images.sh` - Build and push Docker images

## Recent Fixes

- Fixed RoleBinding namespace bug (was pointing to wrong namespace)
- Improved template hash detection with fallback computation
- Added support for local kubeconfig (for development)
- Fixed unused variable warnings
