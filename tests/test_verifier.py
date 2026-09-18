from types import SimpleNamespace

from lyrictool.lrc import parse_lrc
from lyrictool.models import TranscriptToken
from lyrictool.verifier import compare_transcript, transcribe_audio, verify_audio, verify_audio_candidates


def _lrc():
    return "\n".join(
        f"[00:{second:02d}.000]第{index}句测试歌词内容"
        for index, second in enumerate((2, 12, 22, 32, 42, 52, 62, 72), 1)
    )


def test_stable_offset_generates_correction():
    tokens = [
        TranscriptToken(f"第{index}句测试歌词内容", second * 1_000 + 2_200, second * 1_000 + 4_000)
        for index, second in enumerate((2, 12, 22, 32, 42, 52, 62, 72), 1)
    ]
    result = compare_transcript(_lrc(), tokens, "".join(token.text for token in tokens), audio_duration=80, tolerance_seconds=1.5)
    assert result.status == "已自动校正"
    assert result.corrected_lrc_text
    assert 1_500 <= result.median_offset_ms <= 3_000


def test_auto_correction_does_not_move_opening_credits():
    text = "[00:00.000]作词：甲\n[00:01.000]编曲：乙\n" + _lrc()
    tokens = [
        TranscriptToken(f"第{index}句测试歌词内容", second * 1_000 + 2_200, second * 1_000 + 4_000)
        for index, second in enumerate((2, 12, 22, 32, 42, 52, 62, 72), 1)
    ]
    result = compare_transcript(text, tokens, "".join(token.text for token in tokens), audio_duration=80)
    assert result.status == "已自动校正"
    assert [line.timestamp_ms for line in parse_lrc(result.corrected_lrc_text).lines[:2]] == [0, 1000]


def test_wrong_content_is_not_accepted():
    tokens = [TranscriptToken("完全不同的内容", index * 10_000, index * 10_000 + 2_000) for index in range(8)]
    result = compare_transcript(_lrc(), tokens, "完全不同的内容", audio_duration=80, tolerance_seconds=1.5)
    assert not result.passed
    assert result.corrected_lrc_text is None


def test_recognition_language_modes_reach_whisper(monkeypatch, tmp_path):
    received_languages = []

    class FakeModel:
        def transcribe(self, _path, **kwargs):
            received_languages.append(kwargs["language"])
            assert kwargs["vad_filter"] is False
            return [], SimpleNamespace(language=kwargs["language"] or "auto")

    monkeypatch.setattr("lyrictool.verifier._load_model", lambda *_args: FakeModel())
    for mode, expected in (("auto", None), ("zh", "zh"), ("yue", "yue"), ("en", "en")):
        audio = tmp_path / f"{mode}.flac"
        audio.write_bytes(b"audio")
        transcribe_audio(audio, "small", tmp_path / "models", "歌词", mode)

    assert received_languages == [None, "zh", "yue", "en"]


def test_empty_audio_is_not_reported_as_bad_lyrics():
    result = compare_transcript(_lrc(), [], "", audio_duration=80)
    assert result.status == "无法复核"
    assert not result.passed


def test_gpu_runtime_failure_switches_cached_auto_model_to_cpu(monkeypatch, tmp_path):
    from lyrictool import verifier

    class FakeModel:
        def __init__(self, device):
            self.model = SimpleNamespace(device=device)

        def transcribe(self, _path, **_kwargs):
            if self.model.device == "cuda":
                raise RuntimeError("missing CUDA library")
            return [], SimpleNamespace(language="zh")

    models = {"auto": FakeModel("cuda"), "cpu": FakeModel("cpu")}
    monkeypatch.setattr(verifier, "_load_model", lambda _name, _dir, device="auto": models[device])
    audio = tmp_path / "test.flac"
    audio.write_bytes(b"audio")
    messages = []
    transcribe_audio(audio, "small", tmp_path / "models", language_mode="zh", progress=messages.append)
    key = ("small", str((tmp_path / "models").resolve()), "auto")
    try:
        assert verifier._MODEL_CACHE[key] is models["cpu"]
        assert any("回退 CPU" in message for message in messages)
    finally:
        verifier._MODEL_CACHE.pop(key, None)


def test_unreliable_drift_slope_is_not_presented_as_precise():
    tokens = [
        TranscriptToken(f"第{index}句测试歌词内容", second * 1_500, second * 1_500 + 2_000)
        for index, second in enumerate((2, 12, 22, 32, 42, 52, 62, 72), 1)
    ]
    result = compare_transcript(_lrc(), tokens, "".join(token.text for token in tokens), audio_duration=120, tolerance_seconds=1.5)
    assert "线性漂移估计不稳定（未用于校正）" in result.details
    assert not any("线性漂移系数" in detail for detail in result.details)


def test_large_stable_offset_is_blocked_as_probable_repeated_section():
    tokens = [
        TranscriptToken(f"第{index}句测试歌词内容", second * 1_000 + 56_000, second * 1_000 + 58_000)
        for index, second in enumerate((2, 12, 22, 32, 42, 52, 62, 72), 1)
    ]
    result = compare_transcript(_lrc(), tokens, "".join(token.text for token in tokens), audio_duration=140, tolerance_seconds=1.5)
    assert result.status == "疑似异常"
    assert result.corrected_lrc_text is None
    assert "过大的整体偏移" in result.summary


def test_chinese_homophones_contribute_to_fuzzy_match():
    lyrics = "\n".join(
        f"[00:{index * 8:02d}.000]爱我的话给我回答第{index}句"
        for index in range(1, 9)
    )
    tokens = [
        TranscriptToken(f"碍我的话给我回答第{index}句", index * 8_000, index * 8_000 + 2_000)
        for index in range(1, 9)
    ]
    result = compare_transcript(lyrics, tokens, "".join(token.text for token in tokens), audio_duration=75)
    assert result.content_similarity > 0.85
    assert any("读音模糊命中率" in detail for detail in result.details)


def test_wrong_auto_detected_language_retries_chinese_and_selects_best(monkeypatch, tmp_path):
    transcript = [
        TranscriptToken(f"第{index}句测试歌词内容", second * 1_000, second * 1_000 + 2_000)
        for index, second in enumerate((2, 12, 22, 32, 42, 52, 62, 72), 1)
    ]
    calls = []

    def fake_transcribe(*_args, **kwargs):
        language = _args[4]
        calls.append(language)
        if language == "auto":
            return [TranscriptToken("нерезультат", 1_000, 2_000)], "нерезультат", "ru"
        return transcript, "".join(token.text for token in transcript), "zh"

    monkeypatch.setattr("lyrictool.verifier.transcribe_audio", fake_transcribe)
    best, results = verify_audio_candidates(
        tmp_path / "测试.flac", [_lrc()], model_name="large-v3", model_dir=tmp_path,
        audio_duration=80, tolerance_seconds=1.5,
    )
    assert calls == ["auto", "zh"]
    assert best == 0 and results[0].status == "通过"
    assert "模型识别语言：zh" in results[0].details


def test_single_candidate_verification_retries_wrong_language(monkeypatch, tmp_path):
    transcript = [
        TranscriptToken(f"第{index}句测试歌词内容", second * 1_000, second * 1_000 + 2_000)
        for index, second in enumerate((2, 12, 22, 32, 42, 52, 62, 72), 1)
    ]
    calls = []

    def fake_transcribe(*_args, **_kwargs):
        language = _args[4]
        calls.append(language)
        if language == "auto":
            return [TranscriptToken("нерезультат", 1_000, 2_000)], "нерезультат", "ru"
        return transcript, "".join(token.text for token in transcript), "zh"

    monkeypatch.setattr("lyrictool.verifier.transcribe_audio", fake_transcribe)
    result = verify_audio(
        tmp_path / "测试.flac", _lrc(), model_name="large-v3", model_dir=tmp_path,
        audio_duration=80, tolerance_seconds=1.5,
    )
    assert calls == ["auto", "zh"]
    assert result.status == "通过"
    assert "模型识别语言：zh" in result.details
