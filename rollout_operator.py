"""
rollout_operator.py

Kopf operator for controlled OnDelete rollouts of a single StatefulSet.

Features:
- Watches exactly one StatefulSet (configured via env).
- Only acts on StatefulSets with updateStrategy.type=OnDelete.
- Requires apps.kubernetes.io/pod-index label on pods (StatefulSet pod index). [web:13][web:99]
- Detects pod template changes via a template hash.
- Plans a rollout with a configurable delay and logs a countdown.
- Splits rollout into upper and lower halves (by ordinal) if enabled.
- Deletes pods in batches up to max_unavailable, waiting for Ready after each batch.

Env vars (all prefixed with ROLL_OP_):
- ROLL_OP_TARGET_NAMESPACE          (required)
- ROLL_OP_TARGET_STATEFUL_SET       (required)
- ROLL_OP_ROLLOUT_DELAY_SECONDS     (default: 600)
- ROLL_OP_ENABLE_HALF_SPLIT         (default: true)
- ROLL_OP_MAX_UNAVAILABLE           (default: 2)
- ROLL_OP_COUNTDOWN_LOG_INTERVAL    (default: 60)
"""

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

import kopf
import kubernetes
from kubernetes.client import AppsV1Api, CoreV1Api, V1Pod, V1StatefulSet
from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings


# =========================
# Settings
# =========================

class RolloutSettings(BaseSettings):
    target_namespace: Optional[str] = None
    target_stateful_set: Optional[str] = None
    rollout_delay_seconds: int = 600
    enable_half_split: bool = True
    max_unavailable: int = 2
    countdown_log_interval: int = 60

    class Config:
        env_prefix = "ROLL_OP_"

    @field_validator("target_namespace", "target_stateful_set")
    @classmethod
    def must_not_be_empty(cls, v: Optional[str], field):
        if not v or not v.strip():
            raise ValueError(f"{field.name} must be set via environment")
        return v.strip()


settings = RolloutSettings()


# =========================
# Constants / Annotations
# =========================

ROLL_ANNOTATION_STATE = "rollout-operator/state"
ROLL_ANNOTATION_HASH = "rollout-operator/last-template-hash"
ROLL_ANNOTATION_PLANNED_AT = "rollout-operator/planned-at"

ROLL_STATE_NONE = "none"
ROLL_STATE_PLANNED = "planned"
ROLL_STATE_ROLLING = "rolling"
ROLL_STATE_DONE = "done"

POD_INDEX_LABEL = "apps.kubernetes.io/pod-index"


# =========================
# Helper functions
# =========================

def get_template_hash(sts: V1StatefulSet) -> Optional[str]:
    """Try to obtain a hash that uniquely identifies the pod template."""
    template = sts.spec.template if sts.spec else None
    if not template:
        return None
    ann: Dict[str, str] = (template.metadata.annotations if template.metadata else None) or {}
    return ann.get("controller-revision-hash") or ann.get("pod-template-hash")


def extract_annotations(sts: V1StatefulSet) -> Dict[str, str]:
    """Extract annotations from StatefulSet metadata."""
    return (sts.metadata.annotations if sts.metadata else None) or {}


def set_annotations_patch(
    state: Optional[str] = None,
    last_hash: Optional[str] = None,
    planned_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a JSON patch to update StatefulSet annotations."""
    patch: Dict[str, Any] = {"metadata": {"annotations": {}}}
    ann = patch["metadata"]["annotations"]
    if state is not None:
        ann[ROLL_ANNOTATION_STATE] = state
    if last_hash is not None:
        ann[ROLL_ANNOTATION_HASH] = last_hash
    if planned_at is not None:
        ann[ROLL_ANNOTATION_PLANNED_AT] = planned_at
    return patch


def get_ordinal(pod: V1Pod, sts_name: str) -> Optional[int]:
    """
    Determine the ordinal of a pod:
    - Prefer the apps.kubernetes.io/pod-index label. [web:13][web:99]
    - Fallback to parsing from the pod name "<sts-name>-<ordinal>".
    """
    labels: Dict[str, str] = (pod.metadata.labels if pod.metadata else None) or {}
    if POD_INDEX_LABEL in labels:
        try:
            return int(labels[POD_INDEX_LABEL])
        except ValueError:
            return None

    if not pod.metadata:
        return None
    prefix = f"{sts_name}-"
    if not pod.metadata.name.startswith(prefix):
        return None
    suffix = pod.metadata.name[len(prefix):]
    try:
        return int(suffix)
    except ValueError:
        return None


def current_pods(core: CoreV1Api, sts: V1StatefulSet) -> List[V1Pod]:
    """List pods belonging to the StatefulSet using its selector."""
    selector: Dict[str, str] = (sts.spec.selector.match_labels if sts.spec and sts.spec.selector else None) or {}
    label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])
    pods = core.list_namespaced_pod(
        namespace=sts.metadata.namespace if sts.metadata else "default",
        label_selector=label_selector,
    ).items
    return pods


def split_halves(ordinals: List[int], replicas: int) -> Tuple[List[int], List[int]]:
    """Split ordinals into lower and upper halves by replicas/2."""
    mid = replicas // 2
    lower = [o for o in ordinals if o < mid]
    upper = [o for o in ordinals if o >= mid]
    return lower, upper


def batch_ordinals(ordinals: List[int], batch_size: int) -> List[List[int]]:
    """Split ordinals into batches of batch_size."""
    batches: List[List[int]] = []
    sorted_o = sorted(ordinals)
    for i in range(0, len(sorted_o), batch_size):
        batches.append(sorted_o[i : i + batch_size])
    return batches


async def wait_pods_ready(
    core: CoreV1Api,
    ns: str,
    sts_name: str,
    ordinals: List[int],
    timeout: int = 1800,
    poll: int = 10,
):
    """Wait until all pods with these ordinals are Running & Ready."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pods = core.list_namespaced_pod(namespace=ns).items
        ready_ordinals: List[int] = []
        for p in pods:
            if not p.metadata or not p.metadata.name.startswith(f"{sts_name}-"):
                continue
            ord_ = get_ordinal(p, sts_name)
            if ord_ is None or ord_ not in ordinals:
                continue
            conditions: List[Any] = (p.status.conditions if p.status else None) or []
            if any(c.type == "Ready" and c.status == "True" for c in conditions):
                ready_ordinals.append(ord_)

        if set(ready_ordinals) == set(ordinals):
            return

        await asyncio.sleep(poll)

    raise kopf.TemporaryError(
        f"Timeout waiting for pods {ordinals} to become Ready",
        delay=poll,
    )


@kopf.on.startup()  # type: ignore
def configure():
    """Configure Kubernetes client and Kopf operator at startup."""
    try:
        _ = settings
    except ValidationError as e:
        raise

    kubernetes.config.load_incluster_config()

    apps = AppsV1Api()
    try:
        sts = apps.read_namespaced_stateful_set(
            name=settings.target_stateful_set,
            namespace=settings.target_namespace,
        )
    except kubernetes.client.exceptions.ApiException as exc:
        raise



@kopf.on.update("apps", "v1", "statefulsets")
async def on_sts_change(spec, meta, status, diff, patch, logger, **kwargs):
    """Monitor StatefulSet for template changes and orchestrate rollout."""
    name = meta["name"]
    namespace = meta["namespace"]

    if namespace != settings.target_namespace:
        return
    if name != settings.target_stateful_set:
        return

    sts_api = AppsV1Api()
    core_api = CoreV1Api()
    sts: V1StatefulSet = sts_api.read_namespaced_stateful_set(
        name=name,
        namespace=namespace,
    )

    strategy = sts.spec.update_strategy if sts.spec else None
    if not strategy or strategy.type != "OnDelete":
        logger.info(
            f"[{namespace}/{name}] Skipping: updateStrategy.type="
            f"{strategy.type if strategy else 'None'} (requires OnDelete)."
        )
        return

    template_hash = get_template_hash(sts)
    if not template_hash:
        logger.info(f"[{namespace}/{name}] No template hash found; skipping.")
        return

    ann = extract_annotations(sts)
    last_hash = ann.get(ROLL_ANNOTATION_HASH)
    state = ann.get(ROLL_ANNOTATION_STATE, ROLL_STATE_NONE)
    planned_at_str = ann.get(ROLL_ANNOTATION_PLANNED_AT)

    if template_hash == last_hash and state in [ROLL_STATE_DONE, ROLL_STATE_NONE]:
        return

    pods = current_pods(core_api, sts)
    if not pods:
        logger.info(
            f"[{namespace}/{name}] No pods found; cannot verify pod index label yet."
        )
        return

    missing_index = [
        p.metadata.name
        for p in pods
        if not p.metadata or POD_INDEX_LABEL not in ((p.metadata.labels if p.metadata else None) or {})
    ]
    if missing_index:
        logger.error(
            f"[{namespace}/{name}] Skipping rollout: pods missing {POD_INDEX_LABEL} label: "
            f"{missing_index}. StatefulSet controller / cluster may be too old for this operator."
        )
        return

    now = int(time.time())

    if state in [ROLL_STATE_NONE, ROLL_STATE_DONE]:
        planned_at = now
        patch.update(
            set_annotations_patch(
                state=ROLL_STATE_PLANNED,
                last_hash=template_hash,
                planned_at=str(planned_at),
            )
        )
        logger.info(
            f"[{namespace}/{name}] New template detected (hash {template_hash}). "
            f"Planning rollout in {settings.rollout_delay_seconds}s."
        )
        return

    if state == ROLL_STATE_PLANNED:
        if not planned_at_str:
            planned_at_str = str(now)
            patch.update(set_annotations_patch(planned_at=planned_at_str))

        planned_at = int(planned_at_str)
        waited = now - planned_at
        remaining = settings.rollout_delay_seconds - waited
        if remaining > 0:
            if waited % settings.countdown_log_interval < 5:
                logger.info(
                    f"[{namespace}/{name}] Rollout planned; starting in {remaining}s "
                    f"(waited {waited}s so far)."
                )
            return

        patch.update(set_annotations_patch(state=ROLL_STATE_ROLLING))
        logger.info(f"[{namespace}/{name}] Rollout delay elapsed. Starting rollout...")
        return

    if state == ROLL_STATE_ROLLING:
        replicas = (sts.spec.replicas if sts.spec else None) or 0
        ordinals = list(range(replicas))

        logger.info(
            f"[{namespace}/{name}] Rolling update (OnDelete) for hash {template_hash}. "
            f"Replicas={replicas}, max_unavailable={settings.max_unavailable}, "
            f"half_split={settings.enable_half_split}."
        )

        if settings.enable_half_split and replicas > 1:
            lower, upper = split_halves(ordinals, replicas)
            phases = [upper, lower]
            phase_names = ["upper", "lower"]
        else:
            phases = [ordinals]
            phase_names = ["all"]

        for phase_name, phase_ordinals in zip(phase_names, phases):
            if not phase_ordinals:
                continue

            logger.info(
                f"[{namespace}/{name}] Starting {phase_name} phase for ordinals {phase_ordinals}."
            )

            batches = batch_ordinals(phase_ordinals, settings.max_unavailable)
            for batch in batches:
                pods = current_pods(core_api, sts)
                to_delete: List[V1Pod] = []
                for p in pods:
                    ord_ = get_ordinal(p, name)
                    if ord_ in batch:
                        to_delete.append(p)

                if not to_delete:
                    logger.info(
                        f"[{namespace}/{name}] No pods to delete for batch {batch}."
                    )
                    continue

                logger.info(
                    f"[{namespace}/{name}] Deleting batch {batch}: "
                    f"{[p.metadata.name for p in to_delete if p.metadata]}."
                )
                for p in to_delete:
                    if p.metadata:
                        core_api.delete_namespaced_pod(
                            name=p.metadata.name,
                            namespace=namespace,
                            grace_period_seconds=30,
                        )

                await wait_pods_ready(core_api, namespace, name, batch)
                logger.info(
                    f"[{namespace}/{name}] Batch {batch} Ready."
                )

        patch.update(set_annotations_patch(state=ROLL_STATE_DONE, last_hash=template_hash))
        logger.info(
            f"[{namespace}/{name}] Rollout completed for hash {template_hash}."
        )
