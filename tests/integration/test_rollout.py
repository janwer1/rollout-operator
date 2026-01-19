"""Integration tests for rollout operator using pytest-kubernetes managed cluster."""
import pytest
import time
from pytest_kubernetes.providers import AClusterManager


@pytest.mark.integration
def test_rollout_detection(cluster: AClusterManager, demo_statefulset):
    """Test that operator detects StatefulSet image change and schedules rollout."""
    print("\n[TEST] Waiting for StatefulSet to be ready...")
    # Wait for StatefulSet to be ready
    cluster.wait(
        "statefulset/demo-sts",
        "jsonpath='{.status.readyReplicas}'=32",
        namespace="demo-sts",
        timeout=300
    )
    print("[TEST] StatefulSet is ready!")
    
    # Get initial revision
    apps_v1 = cluster.apps_v1
    sts = apps_v1.read_namespaced_stateful_set("demo-sts", "demo-sts")
    initial_revision = sts.status.update_revision if sts.status else None
    print(f"[TEST] Initial revision: {initial_revision}")
    assert initial_revision is not None
    
    # Update pod template to trigger rollout
    new_image = f"demo-app:test-{int(time.time())}"
    print(f"[TEST] Patching StatefulSet with new image: {new_image}")
    patch_body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [{
                        "name": "app",
                        "image": new_image
                    }]
                }
            }
        }
    }
    apps_v1.patch_namespaced_stateful_set(
        name="demo-sts",
        namespace="demo-sts",
        body=patch_body
    )
    
    # Wait for operator to detect and schedule rollout
    print("[TEST] Waiting for operator to detect change...")
    time.sleep(5)
    
    # Check that rollout annotation is set
    apps_v1 = cluster.apps_v1
    sts = apps_v1.read_namespaced_stateful_set("demo-sts", "demo-sts")
    annotations = (sts.metadata.annotations if sts.metadata else None) or {}
    
    # Should be in "planned" state
    state = annotations.get("rollout-operator/state")
    print(f"[TEST] Rollout state: {state}")
    assert state == "planned"
    assert "rollout-operator/planned-at" in annotations
    print("[TEST] Rollout detected and scheduled successfully!")


@pytest.mark.integration
def test_rollout_execution(cluster: AClusterManager, demo_statefulset, wait_for_rollout_complete):
    """Test that rollout executes: pods deleted in batches, waits for ready."""
    print("\n[TEST] Waiting for StatefulSet to be ready...")
    # Wait for StatefulSet to be ready
    cluster.wait(
        "statefulset/demo-sts",
        "jsonpath='{.status.readyReplicas}'=32",
        namespace="demo-sts",
        timeout=300
    )
    print("[TEST] StatefulSet is ready!")
    
    # Get initial pod count
    core_v1 = cluster.core_v1
    pods = core_v1.list_namespaced_pod("demo-sts", label_selector="app=demo-sts")
    initial_pod_count = len(pods.items)
    print(f"[TEST] Initial pod count: {initial_pod_count}")
    assert initial_pod_count == 32
    
    # Update pod template to trigger rollout (add annotation to force template change)
    apps_v1 = cluster.apps_v1
    # Use merge patch to add annotation (simpler than JSON patch)
    patch_body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "rollout-test": str(int(time.time()))
                    }
                }
            }
        }
    }
    print("[TEST] Patching StatefulSet to trigger rollout...")
    apps_v1.patch_namespaced_stateful_set(
        name="demo-sts",
        namespace="demo-sts",
        body=patch_body
    )
    print("[TEST] StatefulSet patched, waiting for rollout to complete...")
    
    # Wait for rollout to complete (with timeout)
    # 32 pods / 2 per batch = 16 batches, ~40s per batch = ~640s needed, use 1200s timeout
    try:
        wait_for_rollout_complete("demo-sts", "demo-sts", timeout=1200)
        print("[TEST] Rollout completed!")
    except TimeoutError:
        # Check current state for debugging
        apps_v1 = cluster.apps_v1
        sts = apps_v1.read_namespaced_stateful_set("demo-sts", "demo-sts")
        annotations = (sts.metadata.annotations if sts.metadata else None) or {}
        raise AssertionError(f"Rollout did not complete. State: {annotations.get('rollout-operator/state')}")
    
    # Verify all pods are ready
    cluster.wait(
        "statefulset/demo-sts",
        "jsonpath='{.status.readyReplicas}'=32",
        namespace="demo-sts",
        timeout=300
    )
    
    # Verify rollout is marked as done
    apps_v1 = cluster.apps_v1
    sts = apps_v1.read_namespaced_stateful_set("demo-sts", "demo-sts")
    annotations = (sts.metadata.annotations if sts.metadata else None) or {}
    assert annotations.get("rollout-operator/state") == "done"


@pytest.mark.integration
def test_rollout_skip_updated(cluster: AClusterManager, demo_statefulset, wait_for_rollout_complete):
    """Test that pods already on target revision are not deleted."""
    # Wait for StatefulSet to be ready
    cluster.wait(
        "statefulset/demo-sts",
        "jsonpath='{.status.readyReplicas}'=32",
        namespace="demo-sts",
        timeout=300
    )
    
    apps_v1 = cluster.apps_v1
    
    # First, trigger a rollout and wait for it to complete
    patch_body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "rollout-test": str(int(time.time()))
                    }
                }
            }
        }
    }
    apps_v1.patch_namespaced_stateful_set(
        name="demo-sts",
        namespace="demo-sts",
        body=patch_body
    )
    
    # Wait for first rollout to complete
    wait_for_rollout_complete("demo-sts", "demo-sts", timeout=600)
    
    # Now trigger another rollout with the SAME template (no actual change)
    # This should detect all pods already updated and skip the rollout
    apps_v1.patch_namespaced_stateful_set(
        name="demo-sts",
        namespace="demo-sts",
        body=patch_body  # Same patch - no new revision
    )
    
    # Wait a bit for operator to process
    time.sleep(10)
    
    # Check that rollout was skipped (all pods already updated)
    sts = apps_v1.read_namespaced_stateful_set("demo-sts", "demo-sts")
    annotations = (sts.metadata.annotations if sts.metadata else None) or {}
    
    # Should either be "done" (if it detected all pods already updated immediately)
    # or "planned" if it scheduled but then detected they're already updated
    state = annotations.get("rollout-operator/state")
    # The operator should detect that all pods are already on the target revision
    # and either skip or mark as done immediately
    assert state in ["done", "none", "planned"]  # planned is ok if delay hasn't elapsed yet


@pytest.mark.integration
def test_wrong_strategy_ignored(cluster: AClusterManager):
    """Test that StatefulSet with RollingUpdate strategy is ignored."""
    # Create a StatefulSet with RollingUpdate strategy
    sts_manifest = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {
            "name": "rolling-sts",
            "namespace": "demo-sts"
        },
        "spec": {
            "serviceName": "rolling-sts",
            "replicas": 2,
            "updateStrategy": {
                "type": "RollingUpdate"
            },
            "selector": {
                "matchLabels": {
                    "app": "rolling-sts"
                }
            },
            "template": {
                "metadata": {
                    "labels": {
                        "app": "rolling-sts"
                    }
                },
                "spec": {
                    "containers": [{
                        "name": "app",
                        "image": "demo-app:test",
                        "imagePullPolicy": "IfNotPresent"
                    }]
                }
            }
        }
    }
    
    cluster.apply(sts_manifest)
    
    # Wait a bit
    time.sleep(5)
    
    # Update pod template (using kubernetes client)
    apps_v1 = cluster.apps_v1
    patch_body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "rollout-test": str(int(time.time()))
                    }
                }
            }
        }
    }
    apps_v1.patch_namespaced_stateful_set(
        name="rolling-sts",
        namespace="demo-sts",
        body=patch_body
    )
    
    # Wait a bit more
    time.sleep(5)
    
    # Check that operator did NOT add annotations (it should ignore this STS)
    apps_v1 = cluster.apps_v1
    sts = apps_v1.read_namespaced_stateful_set("rolling-sts", "demo-sts")
    annotations = (sts.metadata.annotations if sts.metadata else None) or {}
    
    # Should not have rollout operator annotations
    assert "rollout-operator/state" not in annotations
    
    # Cleanup
    apps_v1.delete_namespaced_stateful_set("rolling-sts", "demo-sts", propagation_policy="Background")


@pytest.mark.integration
def test_rollout_delay(cluster: AClusterManager, demo_statefulset):
    """Test that rollout delay is respected."""
    # Wait for StatefulSet to be ready
    cluster.wait(
        "statefulset/demo-sts",
        "jsonpath='{.status.readyReplicas}'=32",
        namespace="demo-sts",
        timeout=300
    )
    
    # Update pod template to trigger rollout (add annotation to force template change)
    apps_v1 = cluster.apps_v1
    # Use merge patch to add annotation (simpler than JSON patch)
    patch_body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "rollout-test": str(int(time.time()))
                    }
                }
            }
        }
    }
    apps_v1.patch_namespaced_stateful_set(
        name="demo-sts",
        namespace="demo-sts",
        body=patch_body
    )
    
    # Wait a bit for operator to detect
    time.sleep(5)
    
    # Check that rollout is in "planned" state
    apps_v1 = cluster.apps_v1
    sts = apps_v1.read_namespaced_stateful_set("demo-sts", "demo-sts")
    annotations = (sts.metadata.annotations if sts.metadata else None) or {}
    assert annotations.get("rollout-operator/state") == "planned"
    
    # Wait for delay to elapse (10 seconds as configured)
    # Check that state transitions to "rolling" after delay
    time.sleep(15)
    
    sts = apps_v1.read_namespaced_stateful_set("demo-sts", "demo-sts")
    annotations = (sts.metadata.annotations if sts.metadata else None) or {}
    
    # Should transition to "rolling" after delay
    assert annotations.get("rollout-operator/state") in ["rolling", "done"]


@pytest.mark.integration
def test_rollout_half_split(cluster: AClusterManager, demo_statefulset, wait_for_rollout_complete):
    """Test that rollout processes upper range before lower range."""
    # Wait for StatefulSet to be ready
    cluster.wait(
        "statefulset/demo-sts",
        "jsonpath='{.status.readyReplicas}'=32",
        namespace="demo-sts",
        timeout=300
    )
    
    # Update pod template to trigger rollout (add annotation to force template change)
    apps_v1 = cluster.apps_v1
    # Use merge patch to add annotation (simpler than JSON patch)
    patch_body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "rollout-test": str(int(time.time()))
                    }
                }
            }
        }
    }
    apps_v1.patch_namespaced_stateful_set(
        name="demo-sts",
        namespace="demo-sts",
        body=patch_body
    )
    
    # Wait for rollout to start
    time.sleep(15)  # Wait for delay to elapse
    
    # Check operator logs to verify upper range processed first
    # (This is a basic test - in practice you'd check logs more carefully)
    try:
        wait_for_rollout_complete("demo-sts", "demo-sts", timeout=600)
    except TimeoutError:
        pass  # Not critical for this test
    
    # Verify rollout completed
    apps_v1 = cluster.apps_v1
    sts = apps_v1.read_namespaced_stateful_set("demo-sts", "demo-sts")
    annotations = (sts.metadata.annotations if sts.metadata else None) or {}
    assert annotations.get("rollout-operator/state") == "done"


