"""Unit tests for helper functions in rollout_operator.py."""
import os
import pytest
from unittest.mock import patch
from pydantic import ValidationError
from kubernetes.client import V1Pod, V1ObjectMeta, V1StatefulSet, V1StatefulSetStatus

from rollout_operator import (
    split_halves,
    batch_ordinals,
    get_ordinal,
    pod_needs_update,
    is_target_sts,
    set_annotations_patch,
    needs_rollout,
    RolloutSettings,
    POD_INDEX_LABEL,
    ROLL_ANNOTATION_STATE,
    ROLL_ANNOTATION_REVISION,
    ROLL_ANNOTATION_PLANNED_AT,
)


@pytest.mark.unit
class TestSplitHalves:
    """Test split_halves function."""
    
    def test_even_replicas(self):
        ordinals = list(range(10))
        lower, upper = split_halves(ordinals, 10)
        assert lower == [0, 1, 2, 3, 4]
        assert upper == [5, 6, 7, 8, 9]
    
    def test_odd_replicas(self):
        ordinals = list(range(9))
        lower, upper = split_halves(ordinals, 9)
        assert lower == [0, 1, 2, 3]
        assert upper == [4, 5, 6, 7, 8]
    
    def test_single_replica(self):
        ordinals = [0]
        lower, upper = split_halves(ordinals, 1)
        assert lower == []
        assert upper == [0]
    
    def test_two_replicas(self):
        ordinals = [0, 1]
        lower, upper = split_halves(ordinals, 2)
        assert lower == [0]
        assert upper == [1]
    
    def test_zero_replicas(self):
        ordinals = []
        lower, upper = split_halves(ordinals, 0)
        assert lower == []
        assert upper == []
    
    def test_filtered_ordinals(self):
        # Test with non-contiguous ordinals
        ordinals = [0, 2, 5, 7, 9]
        lower, upper = split_halves(ordinals, 10)
        assert lower == [0, 2]
        assert upper == [5, 7, 9]


@pytest.mark.unit
class TestBatchOrdinals:
    """Test batch_ordinals function."""
    
    def test_batch_size_one(self):
        ordinals = [0, 1, 2, 3, 4]
        batches = batch_ordinals(ordinals, 1)
        assert batches == [[0], [1], [2], [3], [4]]
    
    def test_batch_size_two(self):
        ordinals = [0, 1, 2, 3, 4]
        batches = batch_ordinals(ordinals, 2)
        assert batches == [[0, 1], [2, 3], [4]]
    
    def test_batch_size_three(self):
        ordinals = [0, 1, 2, 3, 4, 5]
        batches = batch_ordinals(ordinals, 3)
        assert batches == [[0, 1, 2], [3, 4, 5]]
    
    def test_empty_list(self):
        ordinals = []
        batches = batch_ordinals(ordinals, 2)
        assert batches == []
    
    def test_single_item(self):
        ordinals = [5]
        batches = batch_ordinals(ordinals, 2)
        assert batches == [[5]]
    
    def test_unsorted_ordinals(self):
        ordinals = [5, 1, 3, 2, 4, 0]
        batches = batch_ordinals(ordinals, 2)
        assert batches == [[0, 1], [2, 3], [4, 5]]  # Should be sorted


@pytest.mark.unit
class TestGetOrdinal:
    """Test get_ordinal function."""
    
    def test_label_present(self):
        pod = V1Pod(
            metadata=V1ObjectMeta(
                name="web-5",
                labels={POD_INDEX_LABEL: "13"}
            )
        )
        assert get_ordinal(pod, "web") == 13
    
    def test_label_missing_fallback_to_name(self):
        pod = V1Pod(
            metadata=V1ObjectMeta(
                name="web-5",
                labels={}
            )
        )
        assert get_ordinal(pod, "web") == 5
    
    def test_label_invalid_fallback_to_name(self):
        pod = V1Pod(
            metadata=V1ObjectMeta(
                name="web-5",
                labels={POD_INDEX_LABEL: "invalid"}
            )
        )
        # When label is invalid, it should return None (doesn't fallback to name)
        assert get_ordinal(pod, "web") is None
    
    def test_name_parsing_invalid(self):
        pod = V1Pod(
            metadata=V1ObjectMeta(
                name="web-invalid",
                labels={}
            )
        )
        assert get_ordinal(pod, "web") is None
    
    def test_wrong_prefix(self):
        pod = V1Pod(
            metadata=V1ObjectMeta(
                name="other-5",
                labels={}
            )
        )
        assert get_ordinal(pod, "web") is None
    
    def test_no_metadata(self):
        pod = V1Pod()
        assert get_ordinal(pod, "web") is None


@pytest.mark.unit
class TestPodNeedsUpdate:
    """Test pod_needs_update function."""
    
    def test_matching_revision(self):
        pod = V1Pod(
            metadata=V1ObjectMeta(
                labels={"controller-revision-hash": "rev-123"}
            )
        )
        assert pod_needs_update(pod, "rev-123") is False
    
    def test_different_revision(self):
        pod = V1Pod(
            metadata=V1ObjectMeta(
                labels={"controller-revision-hash": "rev-123"}
            )
        )
        assert pod_needs_update(pod, "rev-456") is True
    
    def test_missing_label(self):
        pod = V1Pod(
            metadata=V1ObjectMeta(
                labels={}
            )
        )
        assert pod_needs_update(pod, "rev-123") is True
    
    def test_no_metadata(self):
        pod = V1Pod()
        assert pod_needs_update(pod, "rev-123") is True
    
    def test_no_labels(self):
        pod = V1Pod(
            metadata=V1ObjectMeta()
        )
        assert pod_needs_update(pod, "rev-123") is True


@pytest.mark.unit
class TestIsTargetSts:
    """Test is_target_sts function."""
    
    @patch('rollout_operator.settings')
    def test_match(self, mock_settings):
        mock_settings.target_namespace = "demo"
        mock_settings.target_stateful_set = "demo-sts"
        assert is_target_sts("demo", "demo-sts") is True
    
    @patch('rollout_operator.settings')
    def test_namespace_mismatch(self, mock_settings):
        mock_settings.target_namespace = "demo"
        mock_settings.target_stateful_set = "demo-sts"
        assert is_target_sts("other", "demo-sts") is False
    
    @patch('rollout_operator.settings')
    def test_name_mismatch(self, mock_settings):
        mock_settings.target_namespace = "demo"
        mock_settings.target_stateful_set = "demo-sts"
        assert is_target_sts("demo", "other-sts") is False
    
    @patch('rollout_operator.settings')
    def test_both_mismatch(self, mock_settings):
        mock_settings.target_namespace = "demo"
        mock_settings.target_stateful_set = "demo-sts"
        assert is_target_sts("other", "other-sts") is False


@pytest.mark.unit
class TestSetAnnotationsPatch:
    """Test set_annotations_patch function."""
    
    def test_all_params(self):
        patch = set_annotations_patch(
            state="rolling",
            last_revision="rev-123",
            planned_at="1234567890"
        )
        assert patch["metadata"]["annotations"][ROLL_ANNOTATION_STATE] == "rolling"
        assert patch["metadata"]["annotations"][ROLL_ANNOTATION_REVISION] == "rev-123"
        assert patch["metadata"]["annotations"][ROLL_ANNOTATION_PLANNED_AT] == "1234567890"
    
    def test_partial_params(self):
        patch = set_annotations_patch(state="planned")
        assert patch["metadata"]["annotations"][ROLL_ANNOTATION_STATE] == "planned"
        assert ROLL_ANNOTATION_REVISION not in patch["metadata"]["annotations"]
        assert ROLL_ANNOTATION_PLANNED_AT not in patch["metadata"]["annotations"]
    
    def test_empty(self):
        patch = set_annotations_patch()
        assert patch["metadata"]["annotations"] == {}
    
    def test_none_values(self):
        patch = set_annotations_patch(state="rolling", last_revision=None, planned_at=None)
        assert patch["metadata"]["annotations"][ROLL_ANNOTATION_STATE] == "rolling"
        assert ROLL_ANNOTATION_REVISION not in patch["metadata"]["annotations"]
        assert ROLL_ANNOTATION_PLANNED_AT not in patch["metadata"]["annotations"]


@pytest.mark.unit
class TestNeedsRollout:
    """Test needs_rollout function."""
    
    def test_all_updated(self):
        sts = V1StatefulSet(
            status=V1StatefulSetStatus(
                replicas=5,
                updated_replicas=5
            )
        )
        assert needs_rollout(sts) is False
    
    def test_some_updated(self):
        sts = V1StatefulSet(
            status=V1StatefulSetStatus(
                replicas=5,
                updated_replicas=3
            )
        )
        assert needs_rollout(sts) is True
    
    def test_none_updated(self):
        sts = V1StatefulSet(
            status=V1StatefulSetStatus(
                replicas=5,
                updated_replicas=0
            )
        )
        assert needs_rollout(sts) is True
    
    def test_no_status(self):
        sts = V1StatefulSet()
        assert needs_rollout(sts) is False
    
    def test_zero_replicas(self):
        sts = V1StatefulSet(
            status=V1StatefulSetStatus(
                replicas=0,
                updated_replicas=0
            )
        )
        assert needs_rollout(sts) is False
    
    def test_none_replicas(self):
        # Kubernetes client doesn't allow None for replicas, so we test with 0
        sts = V1StatefulSet(
            status=V1StatefulSetStatus(
                replicas=0,
                updated_replicas=0
            )
        )
        assert needs_rollout(sts) is False


@pytest.mark.unit
class TestRolloutSettings:
    """Test RolloutSettings validation."""
    
    def test_valid_config(self):
        with patch.dict(os.environ, {
            "RO_TARGET_NAMESPACE": "demo",
            "RO_TARGET_STATEFUL_SET": "demo-sts"
        }):
            settings = RolloutSettings()
            assert settings.target_namespace == "demo"
            assert settings.target_stateful_set == "demo-sts"
            assert settings.delay_seconds == 600
            assert settings.enable_half_split is True
            assert settings.max_unavailable == 2
    
    def test_missing_required_namespace(self):
        with patch.dict(os.environ, {
            "RO_TARGET_STATEFUL_SET": "demo-sts"
        }, clear=True):
            with pytest.raises(ValidationError):
                RolloutSettings()
    
    def test_missing_required_statefulset(self):
        with patch.dict(os.environ, {
            "RO_TARGET_NAMESPACE": "demo"
        }, clear=True):
            with pytest.raises(ValidationError):
                RolloutSettings()
    
    def test_empty_string_namespace(self):
        with patch.dict(os.environ, {
            "RO_TARGET_NAMESPACE": "   ",
            "RO_TARGET_STATEFUL_SET": "demo-sts"
        }):
            with pytest.raises(ValidationError):
                RolloutSettings()
    
    def test_custom_values(self):
        with patch.dict(os.environ, {
            "RO_TARGET_NAMESPACE": "demo",
            "RO_TARGET_STATEFUL_SET": "demo-sts",
            "RO_DELAY_SECONDS": "300",
            "RO_ENABLE_HALF_SPLIT": "false",
            "RO_MAX_UNAVAILABLE": "5",
            "RO_COUNTDOWN_LOG_INTERVAL": "30",
            "RO_JSON_LOGS": "true",
            "RO_POD_TERMINATION_GRACE_PERIOD": "60"
        }):
            settings = RolloutSettings()
            assert settings.delay_seconds == 300
            assert settings.enable_half_split is False
            assert settings.max_unavailable == 5
            assert settings.countdown_log_interval == 30
            assert settings.json_logs is True
            assert settings.pod_termination_grace_period == 60

