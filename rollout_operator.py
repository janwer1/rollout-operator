"""
rollout_operator.py

Kopf operator for controlled OnDelete rollouts of a single StatefulSet.

Features:
- Watches exactly one StatefulSet (configured via env).
- Only acts on StatefulSets with updateStrategy.type=OnDelete.
- Requires apps.kubernetes.io/pod-index label on pods (StatefulSet pod index).
- Detects pod template changes via Kubernetes native revision tracking (updateRevision vs currentRevision).
- Only deletes pods that are not yet on the target revision (avoids unnecessary restarts).
- Plans a rollout with a configurable delay and logs a countdown.
- Splits rollout into upper and lower halves (by ordinal) if enabled.
- Deletes pods in batches up to max_unavailable, waiting for Ready after each batch.

Env vars (all prefixed with RO_):
- RO_TARGET_NAMESPACE          (required)
- RO_TARGET_STATEFUL_SET       (required)
- RO_DELAY_SECONDS             (default: 600)
- RO_ENABLE_HALF_SPLIT         (default: true)
- RO_MAX_UNAVAILABLE           (default: 2)
- RO_COUNTDOWN_LOG_INTERVAL    (default: 60)
- RO_JSON_LOGS                 (default: false)
- RO_POD_TERMINATION_GRACE_PERIOD (default: 30)
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import kopf
import kubernetes
from kubernetes.client import AppsV1Api, CoreV1Api, V1Pod, V1StatefulSet
from pydantic import ValidationError, ValidationInfo, field_validator, ConfigDict
from pydantic_settings import BaseSettings

# Standard Python logger for verbose logs (doesn't create K8s events)
_verbose_logger = logging.getLogger(__name__)


# Filter to suppress noisy log messages
class LogMessageFilter(logging.Filter):
    """Filter out log messages matching specified patterns."""
    def __init__(self, patterns: list):
        super().__init__()
        self.patterns = patterns
    
    def filter(self, record):
        msg = record.getMessage()
        for pattern in self.patterns:
            if all(p in msg for p in pattern):
                return False
        return True


# Suppress noisy Kopf internal messages
logging.getLogger("kopf.objects").addFilter(LogMessageFilter([
    ["Timer", "succeeded"],  # Timer success messages
]))


# =========================
# Structured Logging
# =========================

def log_structured(logger, level: str, event: str, message: str, json_logs: bool = False, create_event: bool = False, **kwargs):
    """
    Log with structured data, optionally as JSON.
    
    Args:
        logger: Logger to use (Kopf logger creates events, standard logger doesn't)
        level: Log level (info, debug, etc.)
        event: Event type name
        message: Log message
        json_logs: Whether to output JSON format
        create_event: If True, use logger (creates K8s event), else use standard logger (no event)
    """
    data = {
        "event": event,
        "message": message,
        **kwargs
    }
    if json_logs:
        log_msg = json.dumps(data)
    else:
        # Human-readable format with emojis
        log_msg = message
    
    # Use standard logger for verbose logs (no events), Kopf logger for important milestones (creates events)
    target_logger = logger if create_event else _verbose_logger
    getattr(target_logger, level)(log_msg)


# =========================
# Settings
# =========================

class RolloutSettings(BaseSettings):
    target_namespace: Optional[str] = None
    target_stateful_set: Optional[str] = None
    delay_seconds: int = 600
    enable_half_split: bool = True
    max_unavailable: int = 2
    countdown_log_interval: int = 60
    json_logs: bool = False
    pod_termination_grace_period: int = 30

    model_config = ConfigDict(env_prefix="RO_")

    @field_validator("target_namespace", "target_stateful_set")
    @classmethod
    def must_not_be_empty(cls, v: Optional[str], info: ValidationInfo):
        if not v or not v.strip():
            raise ValueError(f"{info.field_name} must be set via environment")
        return v.strip()


settings = RolloutSettings()


# =========================
# Dataclasses
# =========================

@dataclass
class RolloutContext:
    """Context for a single rollout operation."""
    namespace: str
    name: str
    target_revision: str
    replicas: int
    settings: RolloutSettings
    logger: Any  # Kopf logger


@dataclass
class BatchResult:
    """Result of processing a single batch."""
    batch_num: int
    total_batches: int
    ordinals: List[int]
    deleted_count: int
    skipped_count: int


@dataclass
class RolloutState:
    """Parsed rollout state from annotations."""
    state: str
    last_revision: Optional[str]
    planned_at: Optional[datetime]


# =========================
# Constants / Annotations
# =========================

ROLL_ANNOTATION_STATE = "rollout-operator/state"
ROLL_ANNOTATION_REVISION = "rollout-operator/last-revision"
ROLL_ANNOTATION_PLANNED_AT = "rollout-operator/planned-at"

ROLL_STATE_NONE = "none"
ROLL_STATE_PLANNED = "planned"
ROLL_STATE_ROLLING = "rolling"
ROLL_STATE_DONE = "done"

POD_INDEX_LABEL = "apps.kubernetes.io/pod-index"


# =========================
# Helper functions
# =========================

def get_sts_revisions(sts: V1StatefulSet) -> Tuple[Optional[str], Optional[str]]:
    """
    Get current and update revision hashes from StatefulSet status.
    
    Returns:
        Tuple of (current_revision, update_revision)
        - current_revision: The revision currently running on most pods
        - update_revision: The revision for the latest template (target)
    """
    status = sts.status
    if not status:
        return None, None
    return status.current_revision, status.update_revision


def needs_rollout(sts: V1StatefulSet) -> bool:
    """
    Check if StatefulSet has pods that need updating.
    
    For OnDelete strategy, currentRevision never updates, so we use updatedReplicas.
    A rollout is needed when not all replicas are on the update revision.
    """
    status = sts.status
    if not status:
        return False
    replicas = status.replicas or 0
    updated = status.updated_replicas or 0
    # Rollout needed if not all replicas are on the update revision
    return updated < replicas


def pod_needs_update(pod: V1Pod, target_revision: str) -> bool:
    """
    Check if a pod is on an old revision and needs updating.
    
    Args:
        pod: The pod to check
        target_revision: The target revision (updateRevision from StatefulSet)
    
    Returns:
        True if pod needs to be deleted/recreated, False if already on target revision
    """
    labels = (pod.metadata.labels if pod.metadata else None) or {}
    pod_revision = labels.get("controller-revision-hash")
    return pod_revision != target_revision




def extract_annotations(sts: V1StatefulSet) -> Dict[str, str]:
    """Extract annotations from StatefulSet metadata."""
    return (sts.metadata.annotations if sts.metadata else None) or {}


def set_annotations_patch(
    state: Optional[str] = None,
    last_revision: Optional[str] = None,
    planned_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a JSON patch to update StatefulSet annotations."""
    patch: Dict[str, Any] = {"metadata": {"annotations": {}}}
    ann = patch["metadata"]["annotations"]
    if state is not None:
        ann[ROLL_ANNOTATION_STATE] = state
    if last_revision is not None:
        ann[ROLL_ANNOTATION_REVISION] = last_revision
    if planned_at is not None:
        ann[ROLL_ANNOTATION_PLANNED_AT] = planned_at
    return patch


def get_ordinal(pod: V1Pod, sts_name: str) -> Optional[int]:
    """
    Determine the ordinal of a pod:
    - Prefer the apps.kubernetes.io/pod-index label.
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


def is_target_sts(namespace: str, name: str) -> bool:
    """Check if this StatefulSet matches the target configuration."""
    return namespace == settings.target_namespace and name == settings.target_stateful_set


async def wait_pods_ready(
    core: CoreV1Api,
    ns: str,
    sts_name: str,
    ordinals: List[int],
    timeout: int = 1800,
    poll: int = 5,
    logger: Optional[Callable[[str], None]] = None,
):
    """
    Wait until all pods with these ordinals are Running & Ready.
    In StatefulSet, deleting a pod immediately creates a new one with the same name.
    We need to wait for the new pods (not being deleted) to become Ready.
    """
    deadline = time.time() + timeout
    start_time = time.time()
    last_log_time = 0
    
    log = logger if logger else (lambda msg: None)
    
    while time.time() < deadline:
        pods = core.list_namespaced_pod(namespace=ns).items
        found_pods: Dict[int, V1Pod] = {}
        
        for p in pods:
            if not p.metadata or not p.metadata.name.startswith(f"{sts_name}-"):
                continue
            ord_ = get_ordinal(p, sts_name)
            if ord_ is None or ord_ not in ordinals:
                continue
            
            # Skip pods that are being deleted (these are the old pods we just deleted)
            if p.metadata.deletion_timestamp:
                continue
            
            # This is a pod that's not being deleted - it's the new one
            found_pods[ord_] = p
        
        # Check if we have new pods for all ordinals
        if len(found_pods) == len(ordinals):
            # All new pods exist - check if they're ready
            ready_ordinals: List[int] = []
            not_ready_pods: List[str] = []
            
            for ord_, p in found_pods.items():
                conditions: List[Any] = (p.status.conditions if p.status else None) or []
                ready = any(c.type == "Ready" and c.status == "True" for c in conditions)
                if ready:
                    ready_ordinals.append(ord_)
                else:
                    phase = p.status.phase if p.status else "Unknown"
                    # Check readiness probe status if available
                    ready_condition = next((c for c in conditions if c.type == "Ready"), None)
                    ready_reason = ready_condition.reason if ready_condition else "Unknown"
                    not_ready_pods.append(f"{p.metadata.name}({phase}/{ready_reason})")

            if set(ready_ordinals) == set(ordinals):
                elapsed = int(time.time() - start_time)
                log(f"  ✅ All {len(ordinals)} pods ready after {elapsed}s")
                return
            
            # Log progress every 10 seconds
            now = time.time()
            if now - last_log_time >= 10:
                log(f"  ⏳ {len(ready_ordinals)}/{len(ordinals)} pods ready. Waiting for: {', '.join(not_ready_pods[:3])}{'...' if len(not_ready_pods) > 3 else ''}")
                last_log_time = now
        else:
            # Not all pods created yet
            now = time.time()
            if now - last_log_time >= 10:
                missing = set(ordinals) - set(found_pods.keys())
                log(f"  ⏳ Waiting for {len(missing)} pods to be created (missing ordinals: {sorted(missing)[:5]}{'...' if len(missing) > 5 else ''})")
                last_log_time = now

        await asyncio.sleep(poll)

    elapsed = int(time.time() - start_time)
    raise kopf.TemporaryError(
        f"Timeout after {elapsed}s waiting for pods {ordinals} to become Ready",
        delay=poll,
    )


# =========================
# RolloutExecutor
# =========================

class RolloutExecutor:
    """Executes rollout operations for a StatefulSet."""
    
    def __init__(self, ctx: RolloutContext, core_api: CoreV1Api, sts_api: AppsV1Api):
        self.ctx = ctx
        self.core = core_api
        self.sts_api = sts_api
    
    def compute_batches(self, ordinals: List[int]) -> List[List[int]]:
        """Compute batch plan using half-split or max_unavailable strategy."""
        if self.ctx.settings.enable_half_split and self.ctx.replicas > 1:
            lower_range, upper_range = split_halves(ordinals, self.ctx.replicas)
            ranges = [upper_range, lower_range]
        else:
            ranges = [ordinals]
        
        all_batches: List[List[int]] = []
        for range_ordinals in ranges:
            batches = batch_ordinals(range_ordinals, self.ctx.settings.max_unavailable)
            all_batches.extend(batches)
        return all_batches
    
    async def delete_batch(self, batch: List[int], target_revision: str) -> BatchResult:
        """Delete pods in a batch, skipping already-updated ones."""
        pods = current_pods(self.core, self._get_sts())
        to_delete: List[V1Pod] = []
        already_updated: List[str] = []
        
        for p in pods:
            ord_ = get_ordinal(p, self.ctx.name)
            if ord_ in batch:
                if pod_needs_update(p, target_revision):
                    to_delete.append(p)
                else:
                    already_updated.append(p.metadata.name if p.metadata else f"ordinal-{ord_}")
        
        deleted_count = 0
        for p in to_delete:
            if p.metadata:
                self.core.delete_namespaced_pod(
                    name=p.metadata.name,
                    namespace=self.ctx.namespace,
                    grace_period_seconds=self.ctx.settings.pod_termination_grace_period,
                )
                deleted_count += 1
        
        return BatchResult(
            batch_num=0,  # Will be set by caller
            total_batches=0,  # Will be set by caller
            ordinals=batch,
            deleted_count=deleted_count,
            skipped_count=len(already_updated),
        )
    
    async def wait_batch_ready(self, ordinals: List[int]) -> None:
        """Wait for new pods to become ready."""
        await wait_pods_ready(
            self.core,
            self.ctx.namespace,
            self.ctx.name,
            ordinals,
            logger=lambda msg: self.ctx.logger.info(f"[{self.ctx.namespace}/{self.ctx.name}]     {msg}")
        )
    
    def check_for_new_revision(self) -> Optional[str]:
        """Re-read StatefulSet to detect mid-rollout revision changes."""
        sts = self.sts_api.read_namespaced_stateful_set(
            name=self.ctx.name,
            namespace=self.ctx.namespace,
        )
        _, update_revision = get_sts_revisions(sts)
        return update_revision
    
    def _get_sts(self) -> V1StatefulSet:
        """Get current StatefulSet."""
        return self.sts_api.read_namespaced_stateful_set(
            name=self.ctx.name,
            namespace=self.ctx.namespace,
        )


async def _execute_rollout(executor: RolloutExecutor, original_target_revision: str, patch: Dict[str, Any]) -> None:
    """Execute the full rollout process."""
    ctx = executor.ctx
    core_api = executor.core
    sts_api = executor.sts_api
    
    ordinals = list(range(ctx.replicas))
    
    # Determine ranges (half-split or single)
    if ctx.settings.enable_half_split and ctx.replicas > 1:
        lower_range, upper_range = split_halves(ordinals, ctx.replicas)
        ranges = [upper_range, lower_range]
        range_names = ["upper_range", "lower_range"]
        log_structured(
            ctx.logger, "info",
            "rollout_split",
            f"[{ctx.namespace}/{ctx.name}] 📊 Split rollout: upper_range={len(upper_range)} pods (ordinals {upper_range[:5]}{'...' if len(upper_range) > 5 else ''}), "
            f"lower_range={len(lower_range)} pods (ordinals {lower_range[:5]}{'...' if len(lower_range) > 5 else ''})",
            json_logs=ctx.settings.json_logs,
            namespace=ctx.namespace,
            statefulset=ctx.name,
            upper_range_ordinals=upper_range,
            lower_range_ordinals=lower_range,
            upper_range_count=len(upper_range),
            lower_range_count=len(lower_range),
        )
    else:
        ranges = [ordinals]
        range_names = ["all"]
        log_structured(
            ctx.logger, "info",
            "rollout_single_range",
            f"[{ctx.namespace}/{ctx.name}] 📊 Single range rollout: {ctx.replicas} pods",
            json_logs=ctx.settings.json_logs,
            namespace=ctx.namespace,
            statefulset=ctx.name,
            total_replicas=ctx.replicas,
        )

    total_ranges = len(ranges)
    for range_idx, (range_name, range_ordinals) in enumerate(zip(range_names, ranges), 1):
        if not range_ordinals:
            continue

        log_structured(
            ctx.logger, "info",
            "rollout_range_started",
            f"[{ctx.namespace}/{ctx.name}] 📦 Range {range_idx}/{total_ranges} ({range_name}): "
            f"Processing {len(range_ordinals)} pods with ordinals {range_ordinals[:10]}{'...' if len(range_ordinals) > 10 else ''}",
            json_logs=ctx.settings.json_logs,
            namespace=ctx.namespace,
            statefulset=ctx.name,
            range_index=range_idx,
            total_ranges=total_ranges,
            range_name=range_name,
            range_ordinals=range_ordinals,
            range_count=len(range_ordinals),
        )

        batches = batch_ordinals(range_ordinals, ctx.settings.max_unavailable)
        total_batches = len(batches)
        log_structured(
            ctx.logger, "info",
            "rollout_batches_created",
            f"[{ctx.namespace}/{ctx.name}]   → Split into {total_batches} batches (max {ctx.settings.max_unavailable} pods per batch)",
            json_logs=ctx.settings.json_logs,
            namespace=ctx.namespace,
            statefulset=ctx.name,
            range_name=range_name,
            total_batches=total_batches,
            max_unavailable=ctx.settings.max_unavailable,
        )

        for batch_idx, batch in enumerate(batches, 1):
            # Check for new revision during rollout
            current_update_revision = executor.check_for_new_revision()
            
            if current_update_revision != original_target_revision:
                log_structured(
                    ctx.logger, "info",
                    "rollout_interrupted",
                    f"[{ctx.namespace}/{ctx.name}] 🔄 New revision detected during rollout! "
                    f"{original_target_revision} → {current_update_revision}. Restarting rollout...",
                    json_logs=ctx.settings.json_logs,
                    create_event=True,  # Major milestone
                    namespace=ctx.namespace,
                    statefulset=ctx.name,
                    old_revision=original_target_revision,
                    new_revision=current_update_revision,
                )
                # Update annotation to track new revision, keep state as ROLLING
                patch.update(set_annotations_patch(last_revision=current_update_revision))
                return  # Exit - next invocation will restart with new revision
            
            # Get pod names before deletion for logging
            sts = executor._get_sts()
            pods = current_pods(core_api, sts)
            pod_names = [
                p.metadata.name for p in pods
                if p.metadata and get_ordinal(p, ctx.name) in batch and pod_needs_update(p, ctx.target_revision)
            ]
            
            # Process batch
            result = await executor.delete_batch(batch, ctx.target_revision)
            
            if result.deleted_count == 0:
                if result.skipped_count > 0:
                    log_structured(
                        ctx.logger, "info",
                        "rollout_batch_skip",
                        f"[{ctx.namespace}/{ctx.name}]   ⏭️  Batch {batch_idx}/{total_batches} (ordinals {batch}): "
                        f"All {result.skipped_count} pods already on target revision, skipping.",
                        json_logs=ctx.settings.json_logs,
                        namespace=ctx.namespace,
                        statefulset=ctx.name,
                        range_name=range_name,
                        batch_index=batch_idx,
                        total_batches=total_batches,
                        batch_ordinals=batch,
                    )
                else:
                    log_structured(
                        ctx.logger, "info",
                        "rollout_batch_skip",
                        f"[{ctx.namespace}/{ctx.name}]   ⏭️  Batch {batch_idx}/{total_batches} (ordinals {batch}): No pods found, skipping.",
                        json_logs=ctx.settings.json_logs,
                        namespace=ctx.namespace,
                        statefulset=ctx.name,
                        range_name=range_name,
                        batch_index=batch_idx,
                        total_batches=total_batches,
                        batch_ordinals=batch,
                    )
                continue

            log_structured(
                ctx.logger, "info",
                "rollout_batch_deleting",
                f"[{ctx.namespace}/{ctx.name}]   🗑️  Batch {batch_idx}/{total_batches} (ordinals {batch}): "
                f"Deleting {result.deleted_count} pods: {', '.join(pod_names[:5])}{'...' if len(pod_names) > 5 else ''}",
                json_logs=ctx.settings.json_logs,
                namespace=ctx.namespace,
                statefulset=ctx.name,
                range_name=range_name,
                batch_index=batch_idx,
                total_batches=total_batches,
                batch_ordinals=batch,
                pod_names=pod_names,
                pod_count=result.deleted_count,
            )

            log_structured(
                ctx.logger, "info",
                "rollout_batch_waiting",
                f"[{ctx.namespace}/{ctx.name}]   ⏳ Waiting for batch {batch_idx}/{total_batches} to become ready...",
                json_logs=ctx.settings.json_logs,
                namespace=ctx.namespace,
                statefulset=ctx.name,
                range_name=range_name,
                batch_index=batch_idx,
                total_batches=total_batches,
                batch_ordinals=batch,
            )
            
            await executor.wait_batch_ready(batch)
            
            # Count completed pods in this range
            completed_in_range = sum(len(b) for b in batches[:batch_idx])
            log_structured(
                ctx.logger, "info",
                "rollout_batch_ready",
                f"[{ctx.namespace}/{ctx.name}]   ✅ Batch {batch_idx}/{total_batches} ready. "
                f"Progress: {completed_in_range}/{len(range_ordinals)} pods in {range_name}",
                json_logs=ctx.settings.json_logs,
                namespace=ctx.namespace,
                statefulset=ctx.name,
                range_name=range_name,
                batch_index=batch_idx,
                total_batches=total_batches,
                completed_in_range=completed_in_range,
                total_in_range=len(range_ordinals),
            )

    # Verify all pods are now on the target revision
    sts = executor._get_sts()
    final_pods = current_pods(core_api, sts)
    pods_still_outdated = [p for p in final_pods if pod_needs_update(p, ctx.target_revision)]
    
    if pods_still_outdated:
        outdated_names = [p.metadata.name for p in pods_still_outdated if p.metadata]
        log_structured(
            ctx.logger, "warning",
            "rollout_incomplete",
            f"[{ctx.namespace}/{ctx.name}] ⚠️  Rollout finished batches but {len(pods_still_outdated)} pods still on old revision: {outdated_names[:5]}{'...' if len(outdated_names) > 5 else ''}",
            json_logs=ctx.settings.json_logs,
            namespace=ctx.namespace,
            statefulset=ctx.name,
            outdated_count=len(pods_still_outdated),
            outdated_pods=outdated_names,
        )
    
    patch.update(set_annotations_patch(state=ROLL_STATE_DONE, last_revision=ctx.target_revision))
    log_structured(
        ctx.logger, "info",
        "rollout_completed",
        f"[{ctx.namespace}/{ctx.name}] 🎉 Rollout completed successfully! All {ctx.replicas} pods updated to revision {ctx.target_revision}.",
        json_logs=ctx.settings.json_logs,
        create_event=True,  # Major milestone - create K8s event
        namespace=ctx.namespace,
        statefulset=ctx.name,
        update_revision=ctx.target_revision,
        total_replicas=ctx.replicas,
    )


@kopf.on.startup()  # type: ignore
async def configure(logger, param=None, **kwargs):
    """Configure Kubernetes client and Kopf operator at startup."""
    try:
        _ = settings
    except ValidationError:
        raise

    # Try in-cluster config first (for production), fallback to local kubeconfig (for development)
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        try:
            kubernetes.config.load_kube_config()
        except Exception as e:
            raise RuntimeError(
                "Failed to load Kubernetes config. "
                "Either run in-cluster or configure kubeconfig."
            ) from e

    apps = AppsV1Api()
    try:
        _ = apps.read_namespaced_stateful_set(
            name=settings.target_stateful_set,
            namespace=settings.target_namespace,
        )
    except kubernetes.client.exceptions.ApiException as e:
        # During tests (and sometimes during bootstrap) the target StatefulSet might not exist yet.
        # Don't fail startup in that case; we'll start reacting once it appears.
        if getattr(e, "status", None) == 404:
            logger.warning(
                f"Target StatefulSet {settings.target_namespace}/{settings.target_stateful_set} not found yet; "
                "operator will continue and start acting once it exists."
            )
        else:
            raise



@kopf.timer("apps", "v1", "statefulsets", interval=10.0)
async def check_rollout_timer(meta, logger, **kwargs):
    """Periodically check if planned rollouts should start."""
    name = meta["name"]
    namespace = meta["namespace"]

    if not is_target_sts(namespace, name):
        return

    sts_api = AppsV1Api()
    try:
        sts: V1StatefulSet = sts_api.read_namespaced_stateful_set(
            name=name,
            namespace=namespace,
        )
    except kubernetes.client.exceptions.ApiException:
        return

    ann = extract_annotations(sts)
    state = ann.get(ROLL_ANNOTATION_STATE)
    planned_at_str = ann.get(ROLL_ANNOTATION_PLANNED_AT)

    if state != ROLL_STATE_PLANNED:
        return

    if not planned_at_str:
        return

    now = int(time.time())
    planned_at = int(planned_at_str)
    waited = now - planned_at
    remaining = settings.delay_seconds - waited

    if remaining > 0:
            # Log countdown every countdown_log_interval seconds
            if waited % settings.countdown_log_interval == 0 or (waited < 5 and waited % 2 == 0):
                log_structured(
                    logger, "info",
                    "rollout_countdown",
                    f"[{namespace}/{name}] ⏳ Rollout countdown: {remaining}s remaining "
                    f"(waited {waited}s / {settings.delay_seconds}s total delay)",
                    json_logs=settings.json_logs,
                    namespace=namespace,
                    statefulset=name,
                    remaining_seconds=remaining,
                    waited_seconds=waited,
                    total_delay_seconds=settings.delay_seconds,
                )
    else:
        # Delay elapsed, trigger rollout by patching the StatefulSet
        log_structured(
            logger, "info",
            "rollout_delay_elapsed",
            f"[{namespace}/{name}] ✅ Rollout delay elapsed. Starting rollout now...",
            json_logs=settings.json_logs,
            create_event=True,  # Major milestone - create K8s event
            namespace=namespace,
            statefulset=name,
        )
        patch = set_annotations_patch(state=ROLL_STATE_ROLLING)
        sts_api.patch_namespaced_stateful_set(
            name=name,
            namespace=namespace,
            body=patch,
        )


@kopf.on.update("apps", "v1", "statefulsets")
async def on_sts_change(spec, meta, status, diff, patch, logger, **kwargs):
    """Monitor StatefulSet for revision changes and orchestrate rollout."""
    name = meta["name"]
    namespace = meta["namespace"]

    if not is_target_sts(namespace, name):
        return

    log_structured(
        logger, "info",
        "statefulset_update_detected",
        f"[{namespace}/{name}] StatefulSet update detected",
        json_logs=settings.json_logs,
        namespace=namespace,
        statefulset=name,
    )

    sts_api = AppsV1Api()
    core_api = CoreV1Api()
    
    # Use Kopf-provided data when possible, but re-read for full StatefulSet object
    # when we need to check strategy or get full pod list
    strategy_type = spec.get("updateStrategy", {}).get("type") if spec else None
    if strategy_type != "OnDelete":
        log_structured(
            logger, "info",
            "skip_wrong_strategy",
            f"[{namespace}/{name}] Skipping: updateStrategy.type={strategy_type} (requires OnDelete).",
            json_logs=settings.json_logs,
            namespace=namespace,
            statefulset=name,
            strategy_type=strategy_type,
        )
        return

    # Get revision from status (Kopf-provided)
    update_revision = status.get("updateRevision") if status else None
    current_revision = status.get("currentRevision") if status else None
    
    if not update_revision:
        log_structured(
            logger, "info",
            "no_update_revision",
            f"[{namespace}/{name}] No update revision found in StatefulSet status; skipping.",
            json_logs=settings.json_logs,
            namespace=namespace,
            statefulset=name,
        )
        return

    # Re-read to get annotations (not available in Kopf meta)
    sts: V1StatefulSet = sts_api.read_namespaced_stateful_set(
        name=name,
        namespace=namespace,
    )
    ann = extract_annotations(sts)
    last_revision = ann.get(ROLL_ANNOTATION_REVISION)
    state = ann.get(ROLL_ANNOTATION_STATE, ROLL_STATE_NONE)
    planned_at_str = ann.get(ROLL_ANNOTATION_PLANNED_AT)

    # If we've already processed this revision and state is done/none, check if rollout still needed
    if update_revision == last_revision and state in [ROLL_STATE_DONE, ROLL_STATE_NONE]:
        if not needs_rollout(sts):  # All pods already updated
            log_structured(
                logger, "info",
                "revision_complete",
                f"[{namespace}/{name}] Revision {update_revision} complete, skipping.",
                json_logs=settings.json_logs,
                namespace=namespace,
                statefulset=name,
                update_revision=update_revision,
            )
        return
    
    # Check if rollout is needed using updatedReplicas
    rollout_needed = needs_rollout(sts)

    pods = current_pods(core_api, sts)
    if not pods:
        log_structured(
            logger, "info",
            "no_pods_found",
            f"[{namespace}/{name}] No pods found; cannot verify pod index label yet.",
            json_logs=settings.json_logs,
            namespace=namespace,
            statefulset=name,
        )
        return

    missing_index = [
        p.metadata.name
        for p in pods
        if not p.metadata or POD_INDEX_LABEL not in ((p.metadata.labels if p.metadata else None) or {})
    ]
    if missing_index:
        log_structured(
            logger, "error",
            "missing_pod_index_label",
            f"[{namespace}/{name}] Skipping rollout: pods missing {POD_INDEX_LABEL} label: "
            f"{missing_index}. StatefulSet controller / cluster may be too old for this operator.",
            json_logs=settings.json_logs,
            namespace=namespace,
            statefulset=name,
            missing_pods=missing_index,
        )
        return

    now = int(time.time())

    if state in [ROLL_STATE_NONE, ROLL_STATE_DONE] and rollout_needed:
        planned_at = now
        patch.update(
            set_annotations_patch(
                state=ROLL_STATE_PLANNED,
                last_revision=update_revision,
                planned_at=str(planned_at),
            )
        )
        log_structured(
            logger, "info",
            "revision_detected",
            f"[{namespace}/{name}] 🔍 New revision detected! Revision: {current_revision} → {update_revision}",
            json_logs=settings.json_logs,
            create_event=True,  # Major milestone - create K8s event
            namespace=namespace,
            statefulset=name,
            current_revision=current_revision,
            update_revision=update_revision,
        )
        log_structured(
            logger, "info",
            "rollout_scheduled",
            f"[{namespace}/{name}] 📅 Rollout scheduled: Will start in {settings.delay_seconds}s "
            f"({settings.delay_seconds // 60} minutes). Countdown will be logged every {settings.countdown_log_interval}s",
            json_logs=settings.json_logs,
            create_event=True,  # Major milestone - create K8s event
            namespace=namespace,
            statefulset=name,
            delay_seconds=settings.delay_seconds,
            countdown_interval=settings.countdown_log_interval,
        )
        return

    if state == ROLL_STATE_PLANNED:
        if not planned_at_str:
            planned_at_str = str(now)
            patch.update(set_annotations_patch(planned_at=planned_at_str))
            log_structured(
                logger, "info",
                "rollout_planned",
                f"[{namespace}/{name}] ⏳ Rollout planned. Timer will check every 10s. "
                f"Starting in {settings.delay_seconds}s...",
                json_logs=settings.json_logs,
                namespace=namespace,
                statefulset=name,
                delay_seconds=settings.delay_seconds,
            )
        # Timer will handle the countdown and starting the rollout
        return

    if state == ROLL_STATE_ROLLING:
        # Use spec from Kopf when available, fallback to re-read
        replicas = spec.get("replicas", 0) if spec else ((sts.spec.replicas if sts.spec else None) or 0)
        ordinals = list(range(replicas))

        # Store the original target revision for this rollout
        original_target_revision = update_revision

        # Count how many pods actually need updating
        pods = current_pods(core_api, sts)
        pods_needing_update = [p for p in pods if pod_needs_update(p, update_revision)]
        
        if not pods_needing_update:
            # All pods already on target revision - mark as done
            log_structured(
                logger, "info",
                "rollout_already_complete",
                f"[{namespace}/{name}] ✅ All pods already on target revision {update_revision}. Marking rollout complete.",
                json_logs=settings.json_logs,
                create_event=True,  # Major milestone - create K8s event
                namespace=namespace,
                statefulset=name,
                update_revision=update_revision,
            )
            patch.update(set_annotations_patch(state=ROLL_STATE_DONE, last_revision=update_revision))
            return

        # Create executor and execute rollout
        ctx = RolloutContext(
            namespace=namespace,
            name=name,
            target_revision=update_revision,
            replicas=replicas,
            settings=settings,
            logger=logger,
        )
        executor = RolloutExecutor(ctx, core_api, sts_api)
        
        log_structured(
            logger, "info",
            "rollout_started",
            f"[{namespace}/{name}] 🚀 Starting rollout to revision {update_revision}. "
            f"Pods to update: {len(pods_needing_update)}/{replicas}, max_unavailable: {settings.max_unavailable}, "
            f"half_split: {settings.enable_half_split}",
            json_logs=settings.json_logs,
            create_event=True,  # Major milestone - create K8s event
                            namespace=namespace,
            statefulset=name,
            update_revision=update_revision,
            pods_to_update=len(pods_needing_update),
            total_replicas=replicas,
            max_unavailable=settings.max_unavailable,
            half_split_enabled=settings.enable_half_split,
        )

        # Execute rollout using executor
        await _execute_rollout(executor, original_target_revision, patch)
