#!/usr/bin/env python3
"""
Rollout TUI - Terminal UI for monitoring rollout progress.

Reads StatefulSet annotations and pod status from Kubernetes API
to display a live dashboard of rollout progress.
"""

import argparse
import json
import sys
import time
from typing import Dict, List, Optional, Tuple

import kubernetes
from kubernetes.client import AppsV1Api, CoreV1Api, V1Pod, V1StatefulSet
from rich.align import Align
from rich import box
from rich.console import Console, Group
from rich.columns import Columns
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Annotation keys (matching rollout_operator.py)
ROLL_ANNOTATION_STATE = "rollout-operator/state"
ROLL_ANNOTATION_REVISION = "rollout-operator/last-revision"
ROLL_ANNOTATION_PLANNED_AT = "rollout-operator/planned-at"
ROLL_ANNOTATION_STARTED_AT = "rollout-operator/started-at"
ROLL_ANNOTATION_UPDATED_AT = "rollout-operator/updated-at"
ROLL_ANNOTATION_REPLICAS = "rollout-operator/replicas"
ROLL_ANNOTATION_MAX_UNAVAILABLE = "rollout-operator/max-unavailable"
ROLL_ANNOTATION_HALF_SPLIT = "rollout-operator/half-split"
ROLL_ANNOTATION_TARGET_REVISION = "rollout-operator/target-revision"
ROLL_ANNOTATION_RANGE_NAME = "rollout-operator/range-name"
ROLL_ANNOTATION_RANGE_INDEX = "rollout-operator/range-index"
ROLL_ANNOTATION_RANGE_TOTAL = "rollout-operator/range-total"
ROLL_ANNOTATION_BATCH_INDEX = "rollout-operator/batch-index"
ROLL_ANNOTATION_BATCH_TOTAL = "rollout-operator/batch-total"
ROLL_ANNOTATION_CURRENT_BATCH_ORDINALS = "rollout-operator/current-batch-ordinals"
ROLL_ANNOTATION_PODS_TO_UPDATE = "rollout-operator/pods-to-update"
ROLL_ANNOTATION_PODS_DELETED = "rollout-operator/pods-deleted"

POD_INDEX_LABEL = "apps.kubernetes.io/pod-index"


def get_ordinal(pod: V1Pod, sts_name: str) -> Optional[int]:
    """Determine the ordinal of a pod."""
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


def pod_needs_update(pod: V1Pod, target_revision: str) -> bool:
    """Check if a pod is on an old revision."""
    labels: Dict[str, str] = (pod.metadata.labels if pod.metadata else None) or {}
    pod_revision = labels.get("controller-revision-hash")
    return pod_revision != target_revision


def is_pod_ready(pod: V1Pod) -> bool:
    """Check if a pod is Ready."""
    conditions: List = (pod.status.conditions if pod.status else None) or []
    return any(c.type == "Ready" and c.status == "True" for c in conditions)


def current_pods(core: CoreV1Api, sts: V1StatefulSet) -> List[V1Pod]:
    """List pods belonging to the StatefulSet."""
    selector: Dict[str, str] = (sts.spec.selector.match_labels if sts.spec and sts.spec.selector else None) or {}
    label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])
    pods = core.list_namespaced_pod(
        namespace=sts.metadata.namespace if sts.metadata else "default",
        label_selector=label_selector,
    ).items
    return pods


def parse_annotation(ann: Dict[str, str], key: str, default: Optional[str] = None) -> Optional[str]:
    """Parse annotation value."""
    return ann.get(key, default)


def parse_int_annotation(ann: Dict[str, str], key: str, default: Optional[int] = None) -> Optional[int]:
    """Parse integer annotation value."""
    val = ann.get(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def parse_bool_annotation(ann: Dict[str, str], key: str, default: Optional[bool] = None) -> Optional[bool]:
    """Parse boolean annotation value."""
    val = ann.get(key)
    if val is None:
        return default
    return val.lower() == "true"


def parse_list_annotation(ann: Dict[str, str], key: str, default: Optional[List] = None) -> Optional[List]:
    """Parse JSON list annotation value."""
    val = ann.get(key)
    if val is None:
        return default
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return default


def format_timestamp(ts_str: Optional[str]) -> str:
    """Format unix timestamp to human-readable."""
    if not ts_str:
        return "N/A"
    try:
        ts = int(ts_str)
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (ValueError, TypeError):
        return ts_str


def format_duration(start_ts_str: Optional[str], end_ts_str: Optional[str] = None) -> str:
    """Format duration between timestamps."""
    if not start_ts_str:
        return "N/A"
    try:
        start_ts = int(start_ts_str)
        if end_ts_str:
            end_ts = int(end_ts_str)
        else:
            end_ts = int(time.time())
        duration = end_ts - start_ts
        if duration < 60:
            return f"{duration}s"
        elif duration < 3600:
            return f"{duration // 60}m {duration % 60}s"
        else:
            hours = duration // 3600
            minutes = (duration % 3600) // 60
            return f"{hours}h {minutes}m"
    except (ValueError, TypeError):
        return "N/A"


def get_pod_status(pods: List[V1Pod], sts_name: str, target_revision: str, replicas: int) -> Dict[int, Dict[str, any]]:
    """Get status for each pod ordinal."""
    status: Dict[int, Dict[str, any]] = {}
    
    for pod in pods:
        ordinal = get_ordinal(pod, sts_name)
        if ordinal is None or ordinal >= replicas:
            continue
        
        # Skip pods being deleted (old pods)
        if pod.metadata and pod.metadata.deletion_timestamp:
            continue
        
        labels: Dict[str, str] = (pod.metadata.labels if pod.metadata else None) or {}
        pod_revision = labels.get("controller-revision-hash", "")
        # If we can't determine the target revision or the pod revision, treat revision as unknown
        # rather than defaulting everything to OLD.
        if not target_revision or not pod_revision:
            needs_update: Optional[bool] = None
        else:
            needs_update = (pod_revision != target_revision)
        ready = is_pod_ready(pod)
        
        status[ordinal] = {
            "revision": pod_revision,
            "needs_update": needs_update,
            "ready": ready,
            "name": pod.metadata.name if pod.metadata else f"ordinal-{ordinal}",
        }
    
    return status


def compute_derived_status(
    sts: V1StatefulSet,
    pods: List[V1Pod],
    pod_status: Dict[int, Dict[str, any]],
    annotations: Dict[str, str],
) -> Dict[str, any]:
    """
    Compute derived status from Kubernetes state and operator annotations.
    
    Returns a dict with:
    - replicas_expected, replicas_observed
    - target_revision (from sts.status.updateRevision)
    - pods_new_ready, pods_new_not_ready, pods_old_ready, pods_old_not_ready, pods_missing
    - work_remaining, idle
    - operator_state, ui_state
    """
    status = sts.status
    spec = sts.spec
    
    replicas_expected = spec.replicas if spec else 0
    target_revision = status.update_revision if status else None
    
    # Count pods by state
    pods_new_ready = 0
    pods_new_not_ready = 0
    pods_old_ready = 0
    pods_old_not_ready = 0
    pods_missing = 0
    replicas_observed = 0
    
    for ordinal in range(replicas_expected):
        info = pod_status.get(ordinal)
        if not info:
            pods_missing += 1
            continue
        
        replicas_observed += 1
        needs_update = info.get("needs_update")
        ready = info.get("ready", False)
        
        if needs_update is None:
            # Unknown revision state - treat as missing for work calculation
            pods_missing += 1
        elif needs_update:
            # Old revision
            if ready:
                pods_old_ready += 1
            else:
                pods_old_not_ready += 1
        else:
            # New revision
            if ready:
                pods_new_ready += 1
            else:
                pods_new_not_ready += 1
    
    # Work remaining: pods that need update OR are not ready on new revision OR missing
    work_remaining = pods_old_ready + pods_old_not_ready + pods_new_not_ready + pods_missing
    # Idle: all pods are NEW+Ready (nothing to do)
    idle = (work_remaining == 0) and (pods_new_ready == replicas_expected)
    
    # Operator state from annotations
    operator_state = parse_annotation(annotations, ROLL_ANNOTATION_STATE, "unknown")
    
    # Determine UI state
    if idle:
        ui_state = "Idle"
    elif operator_state == "rolling" or (parse_list_annotation(annotations, ROLL_ANNOTATION_CURRENT_BATCH_ORDINALS, [])):
        ui_state = "InProgress"
    elif operator_state == "planned":
        ui_state = "Scheduled"
    elif work_remaining > 0:
        # K8s indicates work but operator not active
        ui_state = "NeedsAction"
    else:
        ui_state = "Idle"
    
    return {
        "replicas_expected": replicas_expected,
        "replicas_observed": replicas_observed,
        "target_revision": target_revision,
        "pods_new_ready": pods_new_ready,
        "pods_new_not_ready": pods_new_not_ready,
        "pods_old_ready": pods_old_ready,
        "pods_old_not_ready": pods_old_not_ready,
        "pods_missing": pods_missing,
        "work_remaining": work_remaining,
        "idle": idle,
        "operator_state": operator_state,
        "ui_state": ui_state,
    }


def render_dashboard(
    sts: V1StatefulSet,
    pods: List[V1Pod],
    console: Console,
) -> Panel:
    """Render the rollout dashboard."""
    ann = (sts.metadata.annotations if sts.metadata else None) or {}
    status = sts.status
    
    # Parse annotations
    state = parse_annotation(ann, ROLL_ANNOTATION_STATE, "none")
    # Source of truth for revision comparisons: StatefulSet status.updateRevision.
    # Annotations can drift (e.g., if operator restarts or revision changes mid-rollout),
    # so we only use annotation as a fallback for display.
    status_update_revision = status.update_revision if status else None
    annotation_target_revision = parse_annotation(ann, ROLL_ANNOTATION_TARGET_REVISION)
    effective_target_revision = status_update_revision or annotation_target_revision
    started_at = parse_annotation(ann, ROLL_ANNOTATION_STARTED_AT)
    updated_at = parse_annotation(ann, ROLL_ANNOTATION_UPDATED_AT)
    replicas = parse_int_annotation(ann, ROLL_ANNOTATION_REPLICAS) or (sts.spec.replicas if sts.spec else 0)
    max_unavailable = parse_int_annotation(ann, ROLL_ANNOTATION_MAX_UNAVAILABLE)
    half_split = parse_bool_annotation(ann, ROLL_ANNOTATION_HALF_SPLIT)
    range_name = parse_annotation(ann, ROLL_ANNOTATION_RANGE_NAME)
    range_index = parse_int_annotation(ann, ROLL_ANNOTATION_RANGE_INDEX)
    range_total = parse_int_annotation(ann, ROLL_ANNOTATION_RANGE_TOTAL)
    batch_index = parse_int_annotation(ann, ROLL_ANNOTATION_BATCH_INDEX)
    batch_total = parse_int_annotation(ann, ROLL_ANNOTATION_BATCH_TOTAL)
    current_batch_ordinals = parse_list_annotation(ann, ROLL_ANNOTATION_CURRENT_BATCH_ORDINALS, [])
    pods_to_update = parse_int_annotation(ann, ROLL_ANNOTATION_PODS_TO_UPDATE)
    pods_deleted = parse_int_annotation(ann, ROLL_ANNOTATION_PODS_DELETED)

    # Batch context used across sections/tiles
    in_batch = set(current_batch_ordinals or [])
    
    # Get pod status (compare against effective target revision)
    pod_status = get_pod_status(pods, sts.metadata.name if sts.metadata else "", effective_target_revision or "", replicas)
    
    # Compute derived status
    derived = compute_derived_status(sts, pods, pod_status, ann)
    
    def split_ranges(replica_count: int) -> Tuple[List[int], List[int]]:
        mid = replica_count // 2
        lower = list(range(0, mid))
        upper = list(range(mid, replica_count))
        return lower, upper

    def section_counts(ordinals: List[int]) -> Dict[str, int]:
        counts = {
            "new": 0,
            "old": 0,
            "unk": 0,
            "ready": 0,
            "not_ready": 0,
            "missing": 0,
            "in_batch": 0,
        }
        for o in ordinals:
            if o in in_batch:
                counts["in_batch"] += 1
            info = pod_status.get(o)
            if not info:
                counts["missing"] += 1
                continue
            if info["ready"]:
                counts["ready"] += 1
            else:
                counts["not_ready"] += 1
            if info["needs_update"] is None:
                counts["unk"] += 1
            elif info["needs_update"]:
                counts["old"] += 1
            else:
                counts["new"] += 1
        return counts

    def section_is_done(ordinals: List[int]) -> bool:
        # Smart: done only if every ordinal has a pod observed and it is NEW+Ready.
        for o in ordinals:
            info = pod_status.get(o)
            if not info:
                return False
            if info["needs_update"] is None:
                return False
            if info["needs_update"]:
                return False
            if not info["ready"]:
                return False
        return True

    def section_status(section_range_name: str, ordinals: List[int]) -> str:
        # Uses both annotations and live pod state (smart).
        if section_is_done(ordinals):
            return "Done"
        if state == "rolling" and range_name == section_range_name:
            return "Active"
        if state in ("planned", "rolling"):
            return "Queued"
        return "Idle"

    def status_style(status_str: str) -> str:
        if status_str == "Active":
            return "bold black on bright_yellow"
        if status_str == "Done":
            return "bold white on green"
        if status_str == "Queued":
            return "bold white on blue"
        return "dim"

    def pod_tile(ordinal: int) -> Panel:
        info = pod_status.get(ordinal)
        if not info:
            # Missing pod (compact boxed tile)
            token = Text(" ❌ ", style="bold dim")
            body = Align.center(token, vertical="middle")
            return Panel(
                body,
                title=f"{ordinal:02d}",
                width=8,
                height=3,
                box=box.ROUNDED,
                border_style="dim",
            )

        needs_update = info["needs_update"]
        ready = info["ready"]

        # Single emoji combining rollout state + readiness
        # Priority: in_batch > NEW+Ready > OLD+Ready > OLD+NotReady > unknown
        if ordinal in in_batch:
            if ready:
                emoji = "🚧"  # Currently rolling out and ready
            else:
                emoji = "🚧"  # Currently rolling out but not ready yet
            border_style = "bright_yellow"
        elif needs_update is None:
            emoji = "❓"  # Revision state unknown
            border_style = "dim"
        elif not needs_update and ready:
            emoji = "✅"  # NEW + Ready (ideal state)
            border_style = "green"
        elif needs_update and ready:
            emoji = "🔄"  # OLD + Ready (needs update but ready)
            border_style = "yellow"
        else:
            emoji = "⏳"  # OLD + NotReady (needs update and not ready)
            border_style = "red"

        # No background color - just emoji, border color indicates status
        token = Text(f" {emoji} ", style="bold", justify="center")
        body = Align.center(token, vertical="middle")
        return Panel(
            body,
            title=f"{ordinal:02d}",
            width=8,
            height=3,
            box=box.ROUNDED,
            border_style=border_style,
        )

    def section_panel(title: str, range_key: str, ordinals: List[int]) -> Panel:
        status_str = section_status(range_key, ordinals)
        counts = section_counts(ordinals)
        # Up-to-date & Ready progress for this section
        new_ready_in_section = counts['new']  # new means up-to-date & ready (we count separately)
        total_in_section = len(ordinals)

        header = Text()
        header.append(f"{title}  ", style="bold")
        header.append(f" {status_str} ", style=status_style(status_str))
        header.append("  ")
        # Show up-to-date progress prominently
        header.append(f"Up-to-date {new_ready_in_section}/{total_in_section}  ", style="bold" if new_ready_in_section == total_in_section else "")
        
        # Show only useful labels (avoid redundancy)
        detail_parts = []
        if counts['old'] > 0:
            detail_parts.append(f"Needs update: {counts['old']}")
        if counts['not_ready'] > 0:
            detail_parts.append(f"Not ready: {counts['not_ready']}")
        if counts['unk'] > 0:
            detail_parts.append(f"Unknown revision: {counts['unk']}")
        if counts['missing'] > 0:
            detail_parts.append(f"Missing: {counts['missing']}")
        
        if detail_parts:
            header.append("  ", style="dim")
            header.append("  ".join(detail_parts), style="dim")

        tiles = Columns([pod_tile(o) for o in ordinals], equal=True, expand=True, column_first=False, padding=(0, 0))
        body = Group(header, Text(""), tiles)

        # Panel border echoes status
        if status_str == "Active":
            border_style = "bright_yellow"
        elif status_str == "Done":
            border_style = "green"
        elif status_str == "Queued":
            border_style = "blue"
        else:
            border_style = "dim"

        return Panel(body, border_style=border_style)

    # Build state-aware header
    sts_name = sts.metadata.name if sts.metadata else "unknown"
    namespace = sts.metadata.namespace if sts.metadata else "unknown"
    ui_state = derived["ui_state"]
    
    header_table = Table.grid(padding=(0, 2))
    header_table.add_column(style="bold cyan")
    header_table.add_column()
    
    header_table.add_row("StatefulSet:", f"{namespace}/{sts_name}")
    
    # State-specific header rendering
    if ui_state == "Idle":
        # A) Idle (nothing to do)
        header_table.add_row("Status:", Text("Nothing to do", style="bold green"))
        header_table.add_row("Summary:", f"All pods up-to-date & ready ({derived['pods_new_ready']}/{derived['replicas_expected']})")
        if updated_at:
            header_table.add_row("Last Update:", format_timestamp(updated_at))
        if derived["target_revision"]:
            rev_short = derived["target_revision"][:12] + "..." if len(derived["target_revision"]) > 12 else derived["target_revision"]
            header_table.add_row("Current Revision:", rev_short)
        # Hide: pods_deleted, batch info, target revision (shown as "Current revision")
        
    elif ui_state == "Scheduled":
        # B) Scheduled (operator planned)
        header_table.add_row("Status:", Text("Scheduled", style="bold blue"))
        summary_parts = []
        if derived["pods_old_ready"] + derived["pods_old_not_ready"] > 0:
            summary_parts.append(f"{derived['pods_old_ready'] + derived['pods_old_not_ready']} pods need update")
        if derived["pods_old_ready"] > 0:
            summary_parts.append(f"{derived['pods_old_ready']} ready on old revision")
        header_table.add_row("Summary:", ", ".join(summary_parts) if summary_parts else "No pods need update")
        
        # Countdown
        planned_at_str = parse_annotation(ann, ROLL_ANNOTATION_PLANNED_AT)
        if planned_at_str:
            try:
                planned_ts = int(planned_at_str)
                # Try to get delay_seconds from env or use default
                # For now, just show planned timestamp
                header_table.add_row("Planned At:", format_timestamp(planned_at_str))
            except (ValueError, TypeError):
                pass
        
        # Planned rollout params
        if max_unavailable is not None:
            header_table.add_row("Max Unavailable:", str(max_unavailable))
        if half_split is not None:
            header_table.add_row("Half Split:", "enabled" if half_split else "disabled")
        if derived["target_revision"]:
            rev_short = derived["target_revision"][:12] + "..." if len(derived["target_revision"]) > 12 else derived["target_revision"]
            header_table.add_row("Desired Revision:", rev_short)
        
    elif ui_state == "InProgress":
        # C) InProgress (operator rolling)
        header_table.add_row("Status:", Text("Rolling", style="bold yellow"))
        remaining = derived["work_remaining"]
        new_ready = derived["pods_new_ready"]
        total = derived["replicas_expected"]
        header_table.add_row("Summary:", f"Progress: Up-to-date {new_ready}/{total}, Remaining {remaining}")
        
        # Active batch info
        if range_name and range_index and range_total:
            header_table.add_row("Active Range:", f"{range_name} ({range_index}/{range_total})")
        if batch_index and batch_total:
            header_table.add_row("Active Batch:", f"{batch_index}/{batch_total}")
        if current_batch_ordinals:
            ordinals_str = ",".join(str(o) for o in current_batch_ordinals[:10])
            if len(current_batch_ordinals) > 10:
                ordinals_str += f"... (+{len(current_batch_ordinals) - 10} more)"
            header_table.add_row("Current Batch Ordinals:", ordinals_str)
        if started_at:
            header_table.add_row("Started:", format_timestamp(started_at))
        if updated_at:
            header_table.add_row("Last Update:", format_timestamp(updated_at))
        if derived["target_revision"]:
            rev_short = derived["target_revision"][:12] + "..." if len(derived["target_revision"]) > 12 else derived["target_revision"]
            header_table.add_row("Desired Revision:", rev_short)
        # Hide: pods_deleted (replaced with computed progress)
        
    elif ui_state == "NeedsAction":
        # D) NeedsAction (drift: K8s indicates work but operator idle)
        header_table.add_row("Status:", Text("Update detected, operator idle", style="bold red"))
        summary_parts = []
        if derived["pods_old_ready"] + derived["pods_old_not_ready"] > 0:
            summary_parts.append(f"{derived['pods_old_ready'] + derived['pods_old_not_ready']} pods on old revision")
        if derived["pods_new_not_ready"] > 0:
            summary_parts.append(f"{derived['pods_new_not_ready']} new pods not ready")
        if derived["pods_missing"] > 0:
            summary_parts.append(f"{derived['pods_missing']} pods missing")
        header_table.add_row("Summary:", ", ".join(summary_parts) if summary_parts else "Work detected")
        header_table.add_row("Hint:", "StatefulSet has pods not on updateRevision; operator annotations indicate no active rollout.")
        if derived["target_revision"]:
            rev_short = derived["target_revision"][:12] + "..." if len(derived["target_revision"]) > 12 else derived["target_revision"]
            header_table.add_row("Desired Revision:", rev_short)
    
    # Common fields (always show if available)
    if replicas:
        header_table.add_row("Replicas:", str(replicas))
    
    # Build section(s)
    sections: List[Panel] = []
    if half_split:
        lower, upper = split_ranges(replicas)
        # Operator processes upper first, then lower (to match operator UX)
        sections.append(section_panel("Upper", "upper_range", upper))
        sections.append(section_panel("Lower", "lower_range", lower))
    else:
        sections.append(section_panel("All pods", "all", list(range(replicas))))

    # Legend as single line at bottom
    legend = Text()
    legend.append("Legend: ", style="bold")
    legend.append("✅ Up-to-date (Ready)  ", style="bold")
    legend.append("🚧 Updating  ", style="bold")
    legend.append("🔄 Needs update (Ready)  ", style="bold")
    legend.append("⏳ Not ready  ", style="bold")
    legend.append("❓ Unknown  ", style="bold")
    legend.append("❌ Missing pod", style="bold")
    
    # Combine everything using Rich's Group for proper rendering
    content_parts: List = [header_table, Text("")]
    for section in sections:
        content_parts.extend([section, Text("")])
    content_parts.extend([legend])
    
    content = Group(*content_parts)
    
    return Panel(content, title="Rollout Progress", border_style="blue")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Monitor rollout progress using Kubernetes watch API")
    parser.add_argument("--namespace", required=True, help="Namespace of the StatefulSet")
    parser.add_argument("--sts", required=True, help="Name of the StatefulSet")
    args = parser.parse_args()
    
    # Load kubeconfig
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        try:
            kubernetes.config.load_kube_config()
        except Exception as e:
            print(f"Failed to load Kubernetes config: {e}", file=sys.stderr)
            sys.exit(1)
    
    apps_api = AppsV1Api()
    core_api = CoreV1Api()
    console = Console()
    
    def get_data() -> Tuple[V1StatefulSet, List[V1Pod]]:
        """Fetch current StatefulSet and pods."""
        try:
            sts = apps_api.read_namespaced_stateful_set(
                name=args.sts,
                namespace=args.namespace,
            )
            pods = current_pods(core_api, sts)
            return sts, pods
        except kubernetes.client.exceptions.ApiException as e:
            console.print(f"[red]Error fetching data: {e}[/red]")
            raise
    
    # Watch-based: update immediately on changes
    from kubernetes import watch
    import queue
    import threading
    
    def watch_updates(live):
        """Watch for StatefulSet and Pod changes."""
        w = watch.Watch()
        sts_stream = None
        pod_stream = None
        
        try:
            # Initial fetch
            sts, pods = get_data()
            dashboard = render_dashboard(sts, pods, console)
            live.update(dashboard)
            
            # Watch StatefulSet
            sts_stream = w.stream(
                apps_api.list_namespaced_stateful_set,
                namespace=args.namespace,
                field_selector=f"metadata.name={args.sts}",
            )
            
            # Watch Pods (using label selector from StatefulSet)
            selector = sts.spec.selector.match_labels if sts.spec and sts.spec.selector else {}
            label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])
            pod_stream = w.stream(
                core_api.list_namespaced_pod,
                namespace=args.namespace,
                label_selector=label_selector,
            )
            
            # Process events from both streams
            event_queue = queue.Queue()
            
            def stream_worker(stream, stream_name):
                """Worker to process watch stream."""
                try:
                    for event in stream:
                        event_queue.put((stream_name, event))
                except Exception as e:
                    event_queue.put(("error", e))
            
            # Start watch threads
            sts_thread = threading.Thread(target=stream_worker, args=(sts_stream, "sts"), daemon=True)
            pod_thread = threading.Thread(target=stream_worker, args=(pod_stream, "pod"), daemon=True)
            sts_thread.start()
            pod_thread.start()
            
            # Process events
            while True:
                try:
                    stream_name, event = event_queue.get(timeout=1.0)
                    if stream_name == "error":
                        raise event
                    
                    # Refresh data on any change
                    sts, pods = get_data()
                    dashboard = render_dashboard(sts, pods, console)
                    live.update(dashboard)
                except queue.Empty:
                    # Periodic refresh to catch any missed updates
                    sts, pods = get_data()
                    dashboard = render_dashboard(sts, pods, console)
                    live.update(dashboard)
        except Exception as e:
            live.update(Panel(f"[red]Error: {e}[/red]", title="Error"))
            time.sleep(5)  # Wait before retrying
        finally:
            if sts_stream:
                w.stop()
            if pod_stream:
                w.stop()
    
    try:
        with Live(console=console, refresh_per_second=10, screen=True) as live:
            while True:
                watch_updates(live)
    except KeyboardInterrupt:
        console.print("\n[yellow]Exiting...[/yellow]")
        sys.exit(0)


if __name__ == "__main__":
    main()