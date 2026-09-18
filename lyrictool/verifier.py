from __future__ import annotations

import math
import os
import ctypes
import hashlib
import json
import statistics
import threading
from collections import defaultdict
from pathlib import Path
from typing import Callable, Iterable

from .lrc import affine_lyric_timeline, normalize_text, opening_credit_end, parse_lrc, shift_lyric_timeline, validate_structure
from .models import LyricLine, TranscriptToken, VerificationResult


_MODEL_CACHE: dict[tuple[str, str, str], object] = {}
_MODEL_LOCK = threading.Lock()
_DLL_DIRECTORY_HANDLES: list[object] = []
_DLL_DIRECTORIES: set[str] = set()
_GPU_DLL_NAMES = (
    "cudart64_12.dll",
    "cublasLt64_12.dll",
    "cublas64_12.dll",
    "cudnn64_9.dll",
    "cudnn_ops64_9.dll",
    "cudnn_graph64_9.dll",
    "cudnn_engines_runtime_compiled64_9.dll",
    "cudnn_engines_precompiled64_9.dll",
    "cudnn_heuristic64_9.dll",
    "cudnn_cnn64_9.dll",
    "cudnn_adv64_9.dll",
)


def configure_gpu_runtime(runtime_dir: Path) -> None:
    if os.name != "nt" or not runtime_dir.is_dir() or not hasattr(os, "add_dll_directory"):
        return
    resolved = str(runtime_dir.resolve())
    if resolved in _DLL_DIRECTORIES:
        return
    _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(resolved))
    for name in _GPU_DLL_NAMES:
        path = runtime_dir / name
        if path.is_file():
            try:
                _DLL_DIRECTORY_HANDLES.append(ctypes.WinDLL(str(path.resolve())))
            except OSError:
                # The normal auto-device path will report the CUDA failure and
                # fall back to CPU if a runtime file is incomplete or unusable.
                continue
    _DLL_DIRECTORIES.add(resolved)


def _load_model(model_name: str, model_dir: Path, device: str = "auto"):
    configure_gpu_runtime(model_dir.parent / "GPU运行库")
    cache_key = (model_name, str(model_dir.resolve()), device)
    with _MODEL_LOCK:
        if cache_key in _MODEL_CACHE:
            return _MODEL_CACHE[cache_key]
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("当前程序未包含音频识别组件，请重新安装或使用完整 EXE") from exc
        model_dir.mkdir(parents=True, exist_ok=True)
        if device == "auto":
            try:
                import ctranslate2

                use_cuda = ctranslate2.get_cuda_device_count() > 0
            except (ImportError, RuntimeError, OSError):
                use_cuda = False
            if use_cuda:
                try:
                    model = WhisperModel(model_name, device="cuda", compute_type="float16", download_root=str(model_dir))
                except (RuntimeError, OSError):
                    use_cuda = False
            if not use_cuda:
                model = WhisperModel(model_name, device="cpu", compute_type="int8", download_root=str(model_dir),
                                     cpu_threads=max(2, min(8, os.cpu_count() or 4)))
            _MODEL_CACHE[cache_key] = model
            return model
        model = WhisperModel(model_name, device="cpu", compute_type="int8", download_root=str(model_dir),
                             cpu_threads=max(2, min(8, os.cpu_count() or 4)))
        _MODEL_CACHE[cache_key] = model
        return model


def transcribe_audio(
    audio_path: Path,
    model_name: str,
    model_dir: Path,
    lyric_hint: str = "",
    language_mode: str = "auto",
    progress: Callable[[str], None] | None = None,
    compute_device: str = "auto",
    checkpoint: Callable[[], None] | None = None,
) -> tuple[list[TranscriptToken], str, str]:
    # Chinese script alone cannot distinguish Mandarin from Cantonese.  In
    # automatic mode let Whisper inspect the audio instead of forcing `zh`.
    language_hint = None if language_mode == "auto" else language_mode
    cache_dir = model_dir.parent / "缓存" / "识别结果"
    cache_dir.mkdir(parents=True, exist_ok=True)
    stat = audio_path.stat()
    cache_key = hashlib.sha256(
        f"v4-beam1-phonetic|{audio_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{model_name}|{language_hint or 'auto'}".encode("utf-8")
    ).hexdigest()
    cache_file = cache_dir / f"{cache_key}.json"
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        tokens = [TranscriptToken(**item) for item in cached["tokens"]]
        if progress:
            progress("已读取本地音频识别缓存")
        return tokens, str(cached["recognized_text"]), str(cached["language"])
    except (OSError, ValueError, TypeError, KeyError):
        pass
    if progress:
        progress(f"正在加载 {model_name} 音频识别模型；首次使用需要下载模型")
    if checkpoint:
        checkpoint()
    model = _load_model(model_name, model_dir, compute_device)
    if progress:
        device = getattr(getattr(model, "model", None), "device", "cpu")
        progress(f"正在使用 {str(device).upper()} 识别人声文字与时间锚点")

    def recognize(active_model):
        segments, info = active_model.transcribe(
            str(audio_path), beam_size=1, best_of=1, word_timestamps=True, vad_filter=False,
            condition_on_previous_text=False, temperature=0.0, language=language_hint,
            no_speech_threshold=0.65, log_prob_threshold=-1.1, compression_ratio_threshold=2.4,
            repetition_penalty=1.1, no_repeat_ngram_size=3, hallucination_silence_threshold=2.0,
        )
        tokens: list[TranscriptToken] = []
        recognized_parts: list[str] = []
        for segment in segments:
            if checkpoint:
                checkpoint()
            if getattr(segment, "no_speech_prob", 0.0) > 0.75 or getattr(segment, "avg_logprob", 0.0) < -1.2:
                continue
            segment_text = (segment.text or "").strip()
            if segment_text:
                recognized_parts.append(segment_text)
            words = getattr(segment, "words", None) or []
            if words:
                for word in words:
                    text = (word.word or "").strip()
                    if text:
                        tokens.append(TranscriptToken(text, round(word.start * 1_000), round(word.end * 1_000)))
            elif segment_text:
                tokens.append(TranscriptToken(segment_text, round(segment.start * 1_000), round(segment.end * 1_000)))
        return tokens, "".join(recognized_parts), getattr(info, "language", "") or ""

    try:
        tokens, recognized_text, language = recognize(model)
    except (RuntimeError, OSError) as exc:
        if compute_device != "auto" or getattr(getattr(model, "model", None), "device", "cpu") != "cuda":
            raise
        if progress:
            progress(f"GPU 推理不可用（{exc}），正在回退 CPU")
        cpu_model = _load_model(model_name, model_dir, "cpu")
        with _MODEL_LOCK:
            _MODEL_CACHE[(model_name, str(model_dir.resolve()), "auto")] = cpu_model
        tokens, recognized_text, language = recognize(cpu_model)
    temporary = cache_file.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {
                "tokens": [
                    {"text": token.text, "start_ms": token.start_ms, "end_ms": token.end_ms}
                    for token in tokens
                ],
                "recognized_text": recognized_text,
                "language": language,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(cache_file)
    return tokens, recognized_text, language


def _phonetic_units(text: str) -> list[str]:
    normalized = normalize_text(text)
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return list(normalized)
    units: list[str] = []
    for character in normalized:
        if "\u3400" <= character <= "\u9fff":
            units.extend(f"zh:{value}" for value in lazy_pinyin(character, style=Style.NORMAL, errors="default"))
        else:
            units.append(f"ch:{character}")
    return units


def _lyrics_character_map(lines: Iterable[LyricLine]) -> tuple[list[str], list[int]]:
    characters: list[str] = []
    line_indexes: list[int] = []
    for index, line in enumerate(lines):
        for character in _phonetic_units(line.text):
            characters.append(character)
            line_indexes.append(index)
    return characters, line_indexes


def _transcript_character_map(tokens: Iterable[TranscriptToken]) -> tuple[list[str], list[int]]:
    characters: list[str] = []
    times: list[int] = []
    for token in tokens:
        units = _phonetic_units(token.text)
        if not units:
            continue
        duration = max(0, token.end_ms - token.start_ms)
        for index, character in enumerate(units):
            fraction = index / max(1, len(units))
            characters.append(character)
            times.append(round(token.start_ms + duration * fraction))
    return characters, times


def _median_absolute_deviation(values: list[float], median: float | None = None) -> float:
    if not values:
        return math.inf
    center = statistics.median(values) if median is None else median
    return float(statistics.median(abs(value - center) for value in values))


def _linear_regression(points: list[tuple[float, float]]) -> tuple[float, float, float]:
    mean_x = statistics.fmean(point[0] for point in points)
    mean_y = statistics.fmean(point[1] for point in points)
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    if denominator <= 0:
        return 1.0, mean_y - mean_x, math.inf
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator
    intercept = mean_y - slope * mean_x
    residuals = [abs(y - (slope * x + intercept)) for x, y in points]
    return slope, intercept, float(statistics.median(residuals))


def compare_transcript(
    lrc_text: str,
    tokens: list[TranscriptToken],
    recognized_text: str,
    *,
    audio_duration: float,
    tolerance_seconds: float = 1.5,
) -> VerificationResult:
    from difflib import SequenceMatcher

    parsed = parse_lrc(lrc_text)
    credit_end = opening_credit_end(parsed)
    lyric_lines = [line for line in parsed.lines if normalize_text(line.text) and line.timestamp_ms > credit_end]
    lyrics_text, lyric_line_indexes = _lyrics_character_map(lyric_lines)
    transcript_text, transcript_times = _transcript_character_map(tokens)
    if not lyrics_text or not transcript_text:
        return VerificationResult(
            "无法复核",
            "音频中未识别到足够的人声文字，无法判断歌词对错；可试听人工核对",
            0.0,
            0,
            0.0,
            recognized_text=recognized_text,
        )

    matcher = SequenceMatcher(None, lyrics_text, transcript_text, autojunk=False)
    matching_blocks = matcher.get_matching_blocks()
    matched_total = sum(block.size for block in matching_blocks)
    sequence_ratio = matcher.ratio()
    content_similarity = matched_total / max(1, min(len(lyrics_text), len(transcript_text)))
    exact_ratio = SequenceMatcher(
        None,
        normalize_text("".join(line.text for line in lyric_lines)),
        normalize_text(recognized_text),
        autojunk=False,
    ).ratio()
    line_times: dict[int, list[int]] = defaultdict(list)
    matched_chars: dict[int, int] = defaultdict(int)
    for block in matching_blocks:
        if block.size < 2:
            continue
        for offset in range(block.size):
            lyric_position = block.a + offset
            transcript_position = block.b + offset
            if lyric_position >= len(lyric_line_indexes) or transcript_position >= len(transcript_times):
                continue
            line_index = lyric_line_indexes[lyric_position]
            line_times[line_index].append(transcript_times[transcript_position])
            matched_chars[line_index] += 1

    anchors: list[tuple[float, float]] = []
    for line_index, times in line_times.items():
        line_length = len(normalize_text(lyric_lines[line_index].text))
        required = max(2, math.ceil(line_length * 0.32))
        if matched_chars[line_index] < required:
            continue
        ordered = sorted(times)
        early_index = min(len(ordered) - 1, max(0, round(len(ordered) * 0.12)))
        anchors.append((float(lyric_lines[line_index].timestamp_ms), float(ordered[early_index])))
    anchors.sort()

    minimum_anchors = max(4, min(8, max(1, len(lyric_lines) // 5)))
    if len(anchors) >= 2 and audio_duration > 0:
        coverage = max(0.0, min(1.0, (anchors[-1][0] - anchors[0][0]) / (audio_duration * 1_000)))
    else:
        coverage = 0.0
    details = [
        f"读音模糊命中率：{content_similarity:.0%}",
        f"原字文本相似度：{exact_ratio:.0%}",
        f"全量读音相似度：{sequence_ratio:.0%}",
        f"有效时间锚点：{len(anchors)} 个",
        f"锚点覆盖音频：{coverage:.0%}",
    ]
    if content_similarity < 0.28 or exact_ratio < 0.16 or len(anchors) < minimum_anchors or coverage < 0.30:
        return VerificationResult(
            "低置信度",
            "歌词内容或时间锚点不足，不能判定时间轴正确，也不会自动修改",
            content_similarity,
            len(anchors),
            coverage,
            recognized_text=recognized_text,
            details=details,
        )

    offsets = [recognized - lyric for lyric, recognized in anchors]
    median_offset = statistics.median(offsets)
    offset_mad = _median_absolute_deviation(offsets, median_offset)
    filter_limit = max(2_500.0, offset_mad * 3.5)
    filtered = [point for point, offset in zip(anchors, offsets) if abs(offset - median_offset) <= filter_limit]
    if len(filtered) >= minimum_anchors:
        anchors = filtered
        offsets = [recognized - lyric for lyric, recognized in anchors]
        median_offset = statistics.median(offsets)
        offset_mad = _median_absolute_deviation(offsets, median_offset)

    slope, intercept, regression_residual = _linear_regression(anchors)
    tolerance_ms = max(500.0, tolerance_seconds * 1_000)
    maximum_auto_shift_ms = max(8_000.0, min(15_000.0, audio_duration * 1_000 * 0.05))
    slope_detail = (
        f"线性漂移系数：{slope:.6f}"
        if 0.90 <= slope <= 1.10
        else "线性漂移估计不稳定（未用于校正）"
    )
    details.extend(
        [
            f"整体偏移中位数：{median_offset / 1_000:+.2f} 秒",
            f"偏移离散度：{offset_mad / 1_000:.2f} 秒",
            slope_detail,
        ]
    )

    if abs(median_offset) <= tolerance_ms and offset_mad <= tolerance_ms:
        return VerificationResult(
            "通过",
            f"内容与时间轴在允许的 ±{tolerance_seconds:.1f} 秒误差内",
            content_similarity,
            len(anchors),
            coverage,
            round(median_offset),
            round(offset_mad),
            slope,
            recognized_text=recognized_text,
            details=details,
        )

    if abs(median_offset) > maximum_auto_shift_ms or abs(intercept) > maximum_auto_shift_ms:
        details.append(f"自动校正上限：±{maximum_auto_shift_ms / 1_000:.1f} 秒")
        return VerificationResult(
            "疑似异常",
            "检测到过大的整体偏移，可能是模型漏掉首段或把重复段落对错位置；已禁止自动修改",
            content_similarity,
            len(anchors),
            coverage,
            round(median_offset),
            round(offset_mad),
            slope,
            recognized_text=recognized_text,
            details=details,
        )

    if abs(median_offset) > tolerance_ms and offset_mad <= max(900.0, tolerance_ms * 0.65):
        correction = round(median_offset)
        corrected = shift_lyric_timeline(lrc_text, correction)
        corrected_structure = validate_structure(parse_lrc(corrected), audio_duration=audio_duration, strict_online=False)
        if not corrected_structure.valid:
            return VerificationResult(
                "疑似异常",
                f"校正结果越过音频边界（{corrected_structure.summary}），已禁止自动修改",
                content_similarity, len(anchors), coverage, correction, round(offset_mad), slope,
                recognized_text=recognized_text, details=details,
            )
        return VerificationResult(
            "已自动校正",
            f"检测到稳定整体偏移 {correction / 1_000:+.2f} 秒，已生成校正预览",
            content_similarity,
            len(anchors),
            coverage,
            correction,
            round(offset_mad),
            slope,
            corrected,
            recognized_text,
            details,
        )

    drift_across_song = abs((slope - 1.0) * audio_duration * 1_000)
    if (
        0.97 <= slope <= 1.03
        and drift_across_song > tolerance_ms
        and regression_residual <= max(900.0, tolerance_ms * 0.65)
    ):
        corrected = affine_lyric_timeline(lrc_text, slope, intercept)
        corrected_structure = validate_structure(parse_lrc(corrected), audio_duration=audio_duration, strict_online=False)
        if not corrected_structure.valid:
            return VerificationResult(
                "疑似异常",
                f"线性校正结果越过音频边界（{corrected_structure.summary}），已禁止自动修改",
                content_similarity, len(anchors), coverage, round(median_offset), round(regression_residual), slope,
                recognized_text=recognized_text, details=details,
            )
        return VerificationResult(
            "已自动校正",
            f"检测到逐渐漂移，已生成线性校正预览（全曲漂移约 {drift_across_song / 1_000:.2f} 秒）",
            content_similarity,
            len(anchors),
            coverage,
            round(median_offset),
            round(regression_residual),
            slope,
            corrected,
            recognized_text,
            details,
        )

    return VerificationResult(
        "疑似异常",
        "歌词与音频存在不稳定的局部差异，未自动修改；请更换候选或人工检查",
        content_similarity,
        len(anchors),
        coverage,
        round(median_offset),
        round(offset_mad),
        slope,
        recognized_text=recognized_text,
        details=details,
    )


def verify_audio(
    audio_path: Path,
    lrc_text: str,
    *,
    model_name: str,
    model_dir: Path,
    audio_duration: float,
    tolerance_seconds: float,
    language_mode: str = "auto",
    progress: Callable[[str], None] | None = None,
    compute_device: str = "auto",
    checkpoint: Callable[[], None] | None = None,
) -> VerificationResult:
    messages: list[str] = []

    def on_progress(message: str) -> None:
        if "CPU" in message or "CUDA" in message:
            messages.append(message)
        if progress:
            progress(message)

    tokens, recognized_text, language = transcribe_audio(
        audio_path,
        model_name,
        model_dir,
        lrc_text,
        language_mode,
        on_progress,
        compute_device,
        checkpoint,
    )
    result = compare_transcript(
        lrc_text,
        tokens,
        recognized_text,
        audio_duration=audio_duration,
        tolerance_seconds=tolerance_seconds,
    )
    if (
        language_mode == "auto"
        and any("\u3400" <= character <= "\u9fff" for character in lrc_text)
        and language not in ("zh", "yue", "en")
    ):
        if progress:
            progress(f"识别语言为 {language or '未知'}，与中文歌词不符；正尝试中文约束重识别")
        zh_tokens, zh_text, zh_language = transcribe_audio(
            audio_path, model_name, model_dir, lrc_text, "zh", on_progress, compute_device, checkpoint,
        )
        zh_result = compare_transcript(
            lrc_text, zh_tokens, zh_text,
            audio_duration=audio_duration, tolerance_seconds=tolerance_seconds,
        )
        if verification_rank(zh_result) > verification_rank(result):
            result, language = zh_result, zh_language
    if language:
        result.details.insert(0, f"模型识别语言：{language}")
    result.details[:0] = messages
    return result


def verification_rank(result: VerificationResult) -> tuple[int, float, float, int, float]:
    status_rank = {
        "通过": 4,
        "已自动校正": 3,
        "疑似异常": 2,
        "低置信度": 1,
        "无法复核": 0,
    }.get(result.status, 0)
    offset_penalty = -abs(result.median_offset_ms or 0)
    return status_rank, result.content_similarity, result.coverage, result.anchor_count, offset_penalty


def verify_audio_candidates(
    audio_path: Path,
    lrc_texts: list[str],
    *,
    model_name: str,
    model_dir: Path,
    audio_duration: float,
    tolerance_seconds: float,
    language_mode: str = "auto",
    progress: Callable[[str], None] | None = None,
    compute_device: str = "auto",
    checkpoint: Callable[[], None] | None = None,
) -> tuple[int, list[VerificationResult]]:
    messages: list[str] = []

    def on_progress(message: str) -> None:
        if "CPU" in message or "CUDA" in message:
            messages.append(message)
        if progress:
            progress(message)

    tokens, recognized_text, language = transcribe_audio(
        audio_path,
        model_name,
        model_dir,
        "",
        language_mode,
        on_progress,
        compute_device,
        checkpoint,
    )
    def compare_all(current_tokens, current_text, current_language):
        compared = []
        for index, lrc_text in enumerate(lrc_texts, 1):
            if checkpoint:
                checkpoint()
            if progress and len(lrc_texts) > 1:
                progress(f"正在比对歌词候选 {index}/{len(lrc_texts)}")
            result = compare_transcript(
                lrc_text, current_tokens, current_text,
                audio_duration=audio_duration, tolerance_seconds=tolerance_seconds,
            )
            if current_language:
                result.details.insert(0, f"模型识别语言：{current_language}")
            result.details[:0] = messages
            compared.append(result)
        return compared

    results = compare_all(tokens, recognized_text, language)
    chinese_lyrics = any(
        any("\u3400" <= character <= "\u9fff" for character in lrc_text)
        for lrc_text in lrc_texts
    )
    if language_mode == "auto" and chinese_lyrics and language not in ("zh", "yue", "en"):
        if progress:
            progress(f"识别语言为 {language or '未知'}，与中文歌词不符；正尝试中文约束重识别")
        zh_tokens, zh_text, zh_language = transcribe_audio(
            audio_path, model_name, model_dir, "", "zh", on_progress, compute_device, checkpoint,
        )
        chinese_results = compare_all(zh_tokens, zh_text, zh_language)
        if max(map(verification_rank, chinese_results)) > max(map(verification_rank, results)):
            results = chinese_results
    best_index = max(range(len(results)), key=lambda index: verification_rank(results[index]))
    return best_index, results
