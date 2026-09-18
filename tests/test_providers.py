from pathlib import Path

from lyrictool.models import TrackRecord
from lyrictool.providers import SearchContext, _candidate, _plausible, search_all


LRC = """[00:01.000]第一句歌词
[00:05.000]第二句歌词
[00:10.000]第三句歌词
[00:15.000]第四句歌词
[00:20.000]第五句歌词
"""


def _track():
    return TrackRecord(Path("江南.flac"), "江南", "林俊杰", "第二天堂", 267.9, "FLAC")


def test_online_live_version_is_rejected_for_studio_track():
    assert _candidate(
        _track(),
        source="test",
        source_id="1",
        title="江南 (Live)",
        artist="林俊杰",
        album="现场",
        duration=267.0,
        lrc_text=LRC,
    ) is None


def test_matching_studio_version_is_kept():
    assert _candidate(
        _track(),
        source="test",
        source_id="2",
        title="江南",
        artist="林俊杰",
        album="第二天堂",
        duration=267.0,
        lrc_text=LRC,
    ) is not None


def test_unrequested_language_version_and_wrong_artist_are_rejected():
    assert _candidate(
        _track(), source="test", source_id="3", title="江南（粤语版）", artist="林俊杰", album="", duration=267, lrc_text=LRC
    ) is None


def test_prefilter_skips_unrelated_songs_before_lyric_download():
    assert _plausible(_track(), "江南", "林俊杰", 267)
    assert not _plausible(_track(), "江南 Live", "林俊杰", 267)
    assert not _plausible(_track(), "江南", "林俊杰", 190)


def test_domestic_results_do_not_wait_for_lrclib(monkeypatch):
    from lyrictool import providers

    calls = []

    class Domestic:
        name = "domestic"

        def search(self, track):
            calls.append("domestic")
            return [_candidate(track, source="domestic", source_id="1", title="江南", artist="林俊杰",
                               album="第二天堂", duration=267, lrc_text=LRC)]

    class Fallback:
        name = "LRCLIB"

        def search(self, _track):
            calls.append("lrclib")
            return []

    monkeypatch.setitem(providers.PROVIDER_FACTORIES, "netease", Domestic)
    monkeypatch.setitem(providers.PROVIDER_FACTORIES, "lrclib", Fallback)
    result = search_all(_track(), ["netease", "lrclib"], SearchContext())
    assert result.candidates and calls == ["domestic"]
    assert _candidate(
        _track(), source="test", source_id="4", title="江南", artist="其他歌手", album="", duration=267, lrc_text=LRC
    ) is None
