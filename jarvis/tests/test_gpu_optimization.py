"""Regressão das pausas da revisão e da precisão de carregamento ao vivo."""
from unittest.mock import Mock, patch

import pytest

import gpu_priority
import transcriber


def test_live_loads_same_model_with_int8_float16():
    with patch.object(transcriber, "WhisperModel") as load:
        transcriber.LocalTranscriber()
    load.assert_called_once_with("large-v3-turbo", device="cuda", compute_type="int8_float16")


@pytest.mark.parametrize("method", ["encode", "generate_with_fallback"])
def test_offline_preserves_results_and_uses_short_rest(method):
    result = object()
    model = Mock()
    operation = getattr(model, method)
    operation.return_value = result
    sleep = Mock()
    clock = Mock(side_effect=[10.0, 12.5])
    with patch.object(gpu_priority, "wait_for_live_idle") as idle:
        gpu_priority.pace_offline(model, sleep=sleep, clock=clock)
        assert getattr(model, method)("audio", option=True) is result
    idle.assert_called_once_with(sleep=sleep)
    operation.assert_called_once_with("audio", option=True)
    sleep.assert_called_once_with(0.25)


def test_offline_rest_is_bounded_even_for_long_operations():
    model = Mock()
    sleep = Mock()
    with patch.object(gpu_priority, "wait_for_live_idle"):
        gpu_priority.pace_offline(model, sleep=sleep, clock=Mock(side_effect=[0., 100.]))
        model.encode("audio")
    sleep.assert_called_once_with(1.)


def test_offline_propagates_failure_without_retry_or_extra_sleep():
    model = Mock()
    model.encode.side_effect = RuntimeError("GPU error")
    sleep = Mock()
    with patch.object(gpu_priority, "wait_for_live_idle"):
        gpu_priority.pace_offline(model, sleep=sleep, clock=Mock(return_value=10.0))
        with pytest.raises(RuntimeError, match="GPU error"):
            model.encode("audio")
    sleep.assert_not_called()
