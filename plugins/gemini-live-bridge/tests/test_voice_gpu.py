"""GPU routing and independent speech priority without loading ML models."""
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import gpu_priority
import local_voice
import pytest


@pytest.mark.parametrize('config, expected', [({}, ('cpu', 'int8')),
    ({'stt_device': 'cpu'}, ('cpu', 'int8')), ({'stt_device': 'cuda'}, ('cuda', 'int8_float16'))])
def test_recognition_precision_matches_device(config, expected):
    assert local_voice.recognition_device(config) == expected


@pytest.mark.parametrize('device', ['auto', 'cuda:999', '', None])
def test_invalid_recognition_device_is_not_silently_ignored(device):
    with pytest.raises(ValueError):
        local_voice.recognition_device({'stt_device': device})


def worker_module():
    spec = importlib.util.spec_from_file_location('voice_gpu_worker_test',
                                                Path(local_voice.__file__).with_name('local_voice_worker.py'))
    module = importlib.util.module_from_spec(spec)
    original = sys.path[:]
    try:
        with patch.object(sys, 'argv', ['worker', 'stt']):
            spec.loader.exec_module(module)
    finally:
        sys.path[:] = original
    return module


@pytest.mark.parametrize('device, precision', [('cuda', 'int8_float16'), ('cpu', 'int8')])
def test_worker_loads_requested_device_once_and_reports_actual_device(device, precision):
    module = worker_module()
    model = Mock(model=SimpleNamespace(device=device))
    load = Mock(return_value=model)
    packages = {'transcriber': SimpleNamespace(WhisperModel=load),
                'faster_whisper': SimpleNamespace(WhisperModel=load)}
    with patch.dict(sys.modules, packages), patch.object(local_voice, 'settings',
            return_value={'stt_device': device, 'stt_model': 'small'}):
        speech = module.Speech('stt')
        for _ in range(2):
            assert speech.run({'op': 'prepare'}) == {'ok': True, 'engine': 'stt', 'device': device}
    load.assert_called_once_with('small', device=device, compute_type=precision, cpu_threads=2, num_workers=1)


def test_cuda_failure_is_reported_without_silent_cpu_fallback():
    module = worker_module()
    load = Mock(side_effect=RuntimeError('CUDA unavailable'))
    with patch.dict(sys.modules, {'transcriber': SimpleNamespace(WhisperModel=load)}), \
            patch.object(local_voice, 'settings', return_value={'stt_device': 'cuda'}), \
            pytest.raises(RuntimeError, match='CUDA unavailable'):
        module.Speech('stt').load()
    assert load.call_count == 1


@pytest.mark.parametrize('kind', ['stt', 'tts'])
def test_both_workers_claim_interactive_priority(kind):
    module = worker_module()
    speech = Mock(kind=kind)
    speech.run.return_value = {'ok': True}
    output = io.StringIO()
    with patch.object(module, 'Speech', return_value=speech), \
            patch.object(sys, 'argv', ['worker', kind]), \
            patch.object(sys, 'stdin', io.StringIO('{"op":"prepare"}\n')), \
            patch.object(sys, 'stdout', output), patch.object(gpu_priority, 'voice_priority') as priority:
        module.main()
    priority.assert_called_once_with(kind=kind)
    assert json.loads(output.getvalue()) == {'ok': True}


@pytest.mark.parametrize('outer, inner', [('stt', 'tts'), ('tts', 'stt')])
def test_one_worker_finishing_never_clears_the_others_priority(tmp_path, monkeypatch, outer, inner):
    for name in ('BUSY_FILE', 'STT_BUSY_FILE', 'TTS_BUSY_FILE'):
        monkeypatch.setattr(gpu_priority, name, tmp_path / name)
    with gpu_priority.voice_priority(outer):
        with gpu_priority.voice_priority(inner):
            gpu_priority.mark_idle()
            assert gpu_priority.busy_until() > 0
        gpu_priority.mark_idle()
        assert gpu_priority.busy_until() > 0
        sleep = Mock(side_effect=lambda _: gpu_priority.mark_idle(
            gpu_priority.STT_BUSY_FILE if outer == 'stt' else gpu_priority.TTS_BUSY_FILE))
        gpu_priority.wait_for_live_idle(sleep=sleep)
        sleep.assert_called_once()
    assert gpu_priority.busy_until() == 0
