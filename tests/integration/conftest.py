"""Integration test fixtures using pytest-kubernetes for cluster management."""
import os
import subprocess
import time
import pytest
from pathlib import Path
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from pytest_kubernetes.providers import AClusterManager


# Generic image names for testing
OPERATOR_IMAGE = "rollout-operator:test"
DEMO_IMAGE = "demo-app:test"

@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """
    Expose test outcome on the test item so fixtures can react in teardown.

    Pattern:
      if request.node.rep_call.failed: ...
    """
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


def _debug_dump_operator(k8s: AClusterManager, namespace: str = "demo-sts") -> None:
    """Best-effort debug dump of operator state (events + pod logs). Never raises."""

    def safe_kubectl(args: list[str]) -> str:
        try:
            return k8s.kubectl(args, as_dict=False)
        except Exception as e:  # noqa: BLE001 - best-effort debug helper
            return f"[debug-dump] failed: kubectl {' '.join(args)}: {e}"

    print("\n==================== DEBUG DUMP (rollout-operator) ====================")
    print(safe_kubectl(["get", "deploy", "rollout-operator", "-n", namespace, "-o", "wide"]))
    print(safe_kubectl(["get", "pods", "-n", namespace, "-l", "app=rollout-operator", "-o", "wide"]))
    print(safe_kubectl(["describe", "deploy", "rollout-operator", "-n", namespace]))
    print("\n--- events (newest last) ---")
    print(safe_kubectl(["get", "events", "-n", namespace, "--sort-by=.lastTimestamp"]))

    pods = safe_kubectl(["get", "pods", "-n", namespace, "-l", "app=rollout-operator", "-o", "name"])
    pod_names = [line.strip() for line in pods.splitlines() if line.strip().startswith("pod/")]
    if not pod_names:
        print("[debug-dump] no operator pods found for logs")
        return

    for pod in pod_names[:2]:
        print(f"\n--- logs: {pod} (tail 400) ---")
        print(safe_kubectl(["logs", pod, "-n", namespace, "--tail=400"]))


def _build_image(image_name: str, dockerfile_path: Path, context_path: Path) -> None:
    """Build Docker image using subprocess to avoid credential store issues."""
    result = subprocess.run(
        ["docker", "images", "-q", image_name],
        capture_output=True, text=True
    )
    if result.stdout.strip():
        return  # Image already exists
    
    subprocess.run(
        ["docker", "build", "-t", image_name, "-f", str(dockerfile_path), str(context_path)],
        check=True
    )


@pytest.fixture
def cluster(k8s: AClusterManager, request):
    """
    Set up operator and demo app on a Kubernetes cluster managed by pytest-kubernetes.
    
    pytest-kubernetes automatically creates a cluster using the first available provider:
    - k3d (if available)
    - kind (if available)
    - minikube (if available)
    
    The cluster is automatically created before tests and deleted after tests.
    You can override the provider using pytest-kubernetes's configuration options.
    
    To use a specific provider, you can use pytest-kubernetes's k8s_manager fixture:
    - For k3d: pytest --k8s-provider=k3d
    - For kind: pytest --k8s-provider=kind
    - For minikube: pytest --k8s-provider=minikube
    """
    project_root = Path(__file__).parent.parent.parent
    always = os.environ.get("RO_TEST_DEBUG") == "1"

    try:
        # Ensure cluster is created and ready (pytest-kubernetes doesn't auto-create)
        if not k8s.ready(timeout=5):
            print(f"[cluster] Creating cluster '{k8s.cluster_name}'...")
            k8s.create()
            print(f"[cluster] Cluster '{k8s.cluster_name}' is ready")
        else:
            print(f"[cluster] Cluster '{k8s.cluster_name}' already exists and is ready")

        # Build Docker images
        _build_image(OPERATOR_IMAGE, project_root / "Dockerfile", project_root)
        _build_image(DEMO_IMAGE, project_root / "demo" / "Dockerfile", project_root / "demo")

        # Load images into the cluster (pytest-kubernetes handles this for all providers)
        print(f"[cluster] Loading images into cluster...")
        k8s.load_image(OPERATOR_IMAGE)
        k8s.load_image(DEMO_IMAGE)
        print(f"[cluster] Images loaded successfully")

        # Set KUBECONFIG environment variable for kubernetes client
        os.environ["KUBECONFIG"] = str(k8s.kubeconfig)

        # Load kubeconfig for kubernetes client
        config.load_kube_config(config_file=str(k8s.kubeconfig))
        core_v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()
        rbac_v1 = client.RbacAuthorizationV1Api()

        # Create namespace
        try:
            core_v1.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name="demo-sts")))
        except ApiException as e:
            if e.status != 409:  # Already exists
                raise

        # Create ServiceAccount
        try:
            core_v1.create_namespaced_service_account(
                namespace="demo-sts",
                body=client.V1ServiceAccount(metadata=client.V1ObjectMeta(name="rollout-operator")),
            )
        except ApiException as e:
            if e.status != 409:
                raise

        # Create ClusterRole
        try:
            rbac_v1.create_cluster_role(
                client.V1ClusterRole(
                    metadata=client.V1ObjectMeta(name="rollout-operator"),
                    rules=[
                        client.V1PolicyRule(
                            api_groups=["apps"],
                            resources=["statefulsets"],
                            verbs=["get", "list", "watch", "update", "patch"],
                        ),
                        client.V1PolicyRule(
                            api_groups=[""],
                            resources=["pods", "events"],
                            verbs=["get", "list", "watch", "delete", "create", "patch"],
                        ),
                        client.V1PolicyRule(
                            api_groups=["apiextensions.k8s.io"],
                            resources=["customresourcedefinitions"],
                            verbs=["get", "list", "watch"],
                        ),
                    ],
                )
            )
        except ApiException as e:
            if e.status != 409:
                raise

        # Create ClusterRoleBinding
        try:
            rbac_v1.create_cluster_role_binding(
                client.V1ClusterRoleBinding(
                    metadata=client.V1ObjectMeta(name="rollout-operator"),
                    role_ref=client.V1RoleRef(
                        api_group="rbac.authorization.k8s.io",
                        kind="ClusterRole",
                        name="rollout-operator",
                    ),
                    subjects=[
                        client.RbacV1Subject(
                            kind="ServiceAccount",
                            name="rollout-operator",
                            namespace="demo-sts",
                        )
                    ],
                )
            )
        except ApiException as e:
            if e.status != 409:
                raise

        # Create operator Deployment
        try:
            apps_v1.create_namespaced_deployment(
                namespace="demo-sts",
                body=client.V1Deployment(
                    metadata=client.V1ObjectMeta(name="rollout-operator"),
                    spec=client.V1DeploymentSpec(
                        replicas=1,
                        selector=client.V1LabelSelector(match_labels={"app": "rollout-operator"}),
                        template=client.V1PodTemplateSpec(
                            metadata=client.V1ObjectMeta(labels={"app": "rollout-operator"}),
                            spec=client.V1PodSpec(
                                service_account_name="rollout-operator",
                                containers=[
                                    client.V1Container(
                                        name="operator",
                                        image=OPERATOR_IMAGE,
                                        image_pull_policy="Never",
                                        env=[
                                            client.V1EnvVar(name="RO_TARGET_NAMESPACE", value="demo-sts"),
                                            client.V1EnvVar(name="RO_TARGET_STATEFUL_SET", value="demo-sts"),
                                            client.V1EnvVar(name="RO_DELAY_SECONDS", value="10"),
                                            client.V1EnvVar(name="RO_ENABLE_HALF_SPLIT", value="true"),
                                            client.V1EnvVar(name="RO_MAX_UNAVAILABLE", value="2"),
                                            client.V1EnvVar(name="RO_COUNTDOWN_LOG_INTERVAL", value="60"),
                                            client.V1EnvVar(name="RO_JSON_LOGS", value="false"),
                                        ],
                                    )
                                ],
                            ),
                        ),
                    ),
                ),
            )
        except ApiException as e:
            if e.status != 409:
                raise

        # Wait for operator to be ready using kubernetes client
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                deployment = apps_v1.read_namespaced_deployment("rollout-operator", "demo-sts")
                if deployment.status.conditions:
                    for condition in deployment.status.conditions:
                        if condition.type == "Available" and condition.status == "True":
                            break
                    else:
                        time.sleep(2)
                        continue
                    break
            except ApiException:
                pass
            time.sleep(2)
        else:
            raise RuntimeError("Operator deployment did not become available within timeout")

        # Extend k8s object with kubernetes client APIs for convenience
        k8s.core_v1 = core_v1
        k8s.apps_v1 = apps_v1
        k8s.rbac_v1 = rbac_v1

        yield k8s

    except Exception:
        # On setup failure, also dump whatever we can (may be partial).
        if always:
            _debug_dump_operator(k8s, namespace="demo-sts")
        raise

    finally:
        # Dump operator logs/events on test failure (or when explicitly enabled).
        rep_call = getattr(request.node, "rep_call", None)
        failed = bool(rep_call and rep_call.failed)
        if always or failed:
            _debug_dump_operator(k8s, namespace="demo-sts")


@pytest.fixture
def demo_statefulset(cluster: AClusterManager):
    """Deploy demo StatefulSet, cleanup after test."""
    # Use kubernetes client APIs from cluster fixture
    core_v1 = cluster.core_v1
    apps_v1 = cluster.apps_v1
    
    # Create headless service
    try:
        core_v1.create_namespaced_service(
            namespace="demo-sts",
            body=client.V1Service(
                metadata=client.V1ObjectMeta(name="demo-sts"),
                spec=client.V1ServiceSpec(
                    ports=[client.V1ServicePort(port=8080, name="http")],
                    cluster_ip="None"
                )
            )
        )
    except ApiException as e:
        if e.status != 409:
            raise
    
    # Create StatefulSet
    try:
        apps_v1.create_namespaced_stateful_set(
            namespace="demo-sts",
            body=client.V1StatefulSet(
                metadata=client.V1ObjectMeta(name="demo-sts"),
                spec=client.V1StatefulSetSpec(
                    service_name="demo-sts",
                    replicas=32,
                    pod_management_policy="Parallel",
                    update_strategy=client.V1StatefulSetUpdateStrategy(type="OnDelete"),
                    selector=client.V1LabelSelector(match_labels={"app": "demo-sts"}),
                    template=client.V1PodTemplateSpec(
                        metadata=client.V1ObjectMeta(labels={"app": "demo-sts"}),
                        spec=client.V1PodSpec(
                            termination_grace_period_seconds=1,
                            containers=[client.V1Container(
                                name="app",
                                image=DEMO_IMAGE,
                                image_pull_policy="Never",
                                env=[
                                    client.V1EnvVar(name="PORT", value="8080"),
                                    client.V1EnvVar(name="STARTUP_DELAY_SECONDS", value="2"),
                                ],
                                ports=[client.V1ContainerPort(name="http", container_port=8080)],
                                liveness_probe=client.V1Probe(
                                    http_get=client.V1HTTPGetAction(path="/livez", port="http"),
                                    initial_delay_seconds=5,
                                    period_seconds=10,
                                    failure_threshold=3
                                ),
                                readiness_probe=client.V1Probe(
                                    http_get=client.V1HTTPGetAction(path="/readyz", port="http"),
                                    initial_delay_seconds=5,
                                    period_seconds=5,
                                    failure_threshold=3
                                )
                            )]
                        )
                    )
                )
            )
        )
    except ApiException as e:
        if e.status != 409:
            raise
    
    # Wait for StatefulSet to be ready using kubernetes client
    deadline = time.time() + 300
    while time.time() < deadline:
        try:
            sts = apps_v1.read_namespaced_stateful_set("demo-sts", "demo-sts")
            ready = sts.status.ready_replicas or 0
            replicas = sts.status.replicas or 0
            if ready == 32:
                # Ensure all pods have the apps.kubernetes.io/pod-index label
                # (StatefulSet controller adds this in K8s 1.27+, but we add it manually for older versions)
                pods = core_v1.list_namespaced_pod("demo-sts", label_selector="app=demo-sts")
                for pod in pods.items:
                    if pod.metadata and pod.metadata.labels:
                        # Extract ordinal from pod name (format: demo-sts-<ordinal>)
                        pod_name = pod.metadata.name
                        if pod_name.startswith("demo-sts-"):
                            try:
                                ordinal = int(pod_name.split("-")[-1])
                                if "apps.kubernetes.io/pod-index" not in pod.metadata.labels:
                                    # Add the label manually
                                    patch = {
                                        "metadata": {
                                            "labels": {
                                                "apps.kubernetes.io/pod-index": str(ordinal)
                                            }
                                        }
                                    }
                                    core_v1.patch_namespaced_pod(
                                        name=pod_name,
                                        namespace="demo-sts",
                                        body=patch
                                    )
                            except (ValueError, IndexError):
                                pass
                break
            # Log progress every 30 seconds
            if int(time.time()) % 30 == 0:
                print(f"StatefulSet status: {ready}/{replicas} pods ready")
        except ApiException as e:
            if int(time.time()) % 30 == 0:
                print(f"Error checking StatefulSet: {e}")
        time.sleep(2)
    else:
        # Get final status for debugging
        try:
            sts = apps_v1.read_namespaced_stateful_set("demo-sts", "demo-sts")
            pods = core_v1.list_namespaced_pod("demo-sts", label_selector="app=demo-sts")
            print(f"Final StatefulSet status: ready={sts.status.ready_replicas}, replicas={sts.status.replicas}")
            print(f"Pods: {len(pods.items)} total")
            for pod in pods.items[:5]:  # Show first 5 pods
                print(f"  Pod {pod.metadata.name}: phase={pod.status.phase}, ready={pod.status.conditions}")
        except Exception as e:
            print(f"Error getting final status: {e}")
        raise RuntimeError("StatefulSet did not become ready within timeout")
    
    yield
    
    # Cleanup
    try:
        apps_v1.delete_namespaced_stateful_set("demo-sts", "demo-sts", propagation_policy="Background")
        core_v1.delete_namespaced_service("demo-sts", "demo-sts")
    except ApiException:
        pass


@pytest.fixture
def wait_for_rollout_complete(cluster: AClusterManager):
    """Helper to wait for rollout to complete."""
    def _wait(namespace: str, name: str, timeout: int = 1200):
        apps_v1 = cluster.apps_v1
        core_v1 = cluster.core_v1
        
        def _ensure_pod_labels():
            """Ensure all pods have pod-index label (for older K8s versions)."""
            try:
                pods = core_v1.list_namespaced_pod(namespace, label_selector="app=demo-sts")
                for pod in pods.items:
                    if pod.metadata and pod.metadata.labels:
                        pod_name = pod.metadata.name
                        if pod_name.startswith(f"{name}-"):
                            try:
                                ordinal = int(pod_name.split("-")[-1])
                                if "apps.kubernetes.io/pod-index" not in pod.metadata.labels:
                                    patch = {
                                        "metadata": {
                                            "labels": {
                                                "apps.kubernetes.io/pod-index": str(ordinal)
                                            }
                                        }
                                    }
                                    core_v1.patch_namespaced_pod(
                                        name=pod_name,
                                        namespace=namespace,
                                        body=patch
                                    )
                            except (ValueError, IndexError, ApiException):
                                pass
            except Exception:
                pass  # Best effort
        
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                # Ensure pod labels are set (for older K8s versions)
                _ensure_pod_labels()
                
                sts = apps_v1.read_namespaced_stateful_set(name, namespace)
                annotations = sts.metadata.annotations or {}
                state = annotations.get("rollout-operator/state")
                if state == "done":
                    return True
                # Log progress every 30 seconds
                elapsed = int(time.time() - (deadline - timeout))
                if elapsed % 30 == 0:
                    sts_status = sts.status
                    ready = sts_status.ready_replicas if sts_status else 0
                    replicas = sts_status.replicas if sts_status else 0
                    updated = sts_status.updated_replicas if sts_status else 0
                    print(f"[ROLLOUT] State: {state}, Ready: {ready}/{replicas}, Updated: {updated}/{replicas}, Elapsed: {elapsed}s")
            except ApiException:
                pass
            time.sleep(5)
        raise TimeoutError(f"Rollout did not complete within {timeout}s")
    return _wait
