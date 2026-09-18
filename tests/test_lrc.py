from lyrictool.lrc import affine_lyric_timeline, normalize_text, parse_lrc, shift_lrc, shift_lyric_timeline, validate_structure


VALID = """[ar:测试歌手]
[ti:测试歌曲]
[00:01.000]第一句歌词
[00:05.200]第二句歌词
[00:10.300]第三句歌词
[00:15.400]第四句歌词
[00:20.500]第五句歌词
"""


def test_valid_lrc_passes_structure_check():
    report = validate_structure(parse_lrc(VALID), audio_duration=30.0)
    assert report.valid
    assert report.timed_line_count == 5


def test_out_of_order_is_rejected():
    text = VALID.replace("[00:10.300]", "[00:00.300]")
    report = validate_structure(parse_lrc(text), audio_duration=30.0)
    assert not report.valid
    assert any("倒序" in issue for issue in report.issues)


def test_online_duration_mismatch_is_rejected():
    report = validate_structure(parse_lrc(VALID), audio_duration=30.0, candidate_duration=80.0)
    assert not report.valid
    assert any("时长" in issue for issue in report.issues)


def test_shift_rewrites_explicit_timestamps():
    shifted = parse_lrc(shift_lrc(VALID, 1_500))
    assert shifted.lines[0].timestamp_ms == 2_500
    assert shifted.offset_ms == 0


def test_shift_clamps_timestamps_at_zero():
    shifted = parse_lrc(shift_lrc("[00:00.050]开头\n[00:01.000]下一句\n", -100))
    assert [line.timestamp_ms for line in shifted.lines] == [0, 900]


def test_timeline_shift_keeps_opening_credits_and_body_order():
    text = "[00:00.000]作词：甲\n[00:01.000]编曲：乙\n[00:25.000]第一句正文\n[00:28.000]第二句正文\n"
    delayed = parse_lrc(shift_lyric_timeline(text, 10_000))
    assert [line.timestamp_ms for line in delayed.lines] == [0, 1_000, 35_000, 38_000]
    early = parse_lrc(shift_lyric_timeline(text, -30_000))
    assert [line.timestamp_ms for line in early.lines] == [0, 1_000, 1_100, 4_100]


def test_affine_timeline_shift_keeps_credits():
    text = "[00:00.000]作词：甲\n[00:01.000]编曲：乙\n[00:25.000]第一句正文\n[00:28.000]第二句正文\n"
    shifted = parse_lrc(affine_lyric_timeline(text, 1.01, 2_000))
    assert [line.timestamp_ms for line in shifted.lines] == [0, 1_000, 27_250, 30_280]


def test_late_opening_credit_stays_when_song_body_moves():
    text = "[00:00.000]歌名 - 乐队\n[00:07.000]词：甲\n[00:14.000]曲：乙\n[00:22.000]编曲：乐队\n[00:29.000]第一句歌词\n[00:33.000]第二句歌词\n"
    shifted = parse_lrc(shift_lyric_timeline(text, -20_000))
    assert [line.timestamp_ms for line in shifted.lines] == [0, 7000, 14000, 22000, 22100, 26100]


def test_traditional_and_simplified_text_compare_equally():
    assert normalize_text("風到這裡就是黏") == normalize_text("风到这里就是黏")
