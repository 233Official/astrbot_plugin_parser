"""Test media URI resolution (local + http) and sender HTTP behavior.

Covers:
  - MediaUriResolver: local file:// URI, http URL, Chinese/space percent
    encoding, cache-outside rejection, missing/illegal base URL.
  - MessageSender: HTTP mode produces http(s) media URIs; on MediaUriError
    heavy media falls back to text/display_url and never emits file://.
  - Old-config default compatibility: missing media_* fields normalize to
    local / None / DEFAULT_MEDIA_HTTP_TTL.
  - _conf_schema.json parses and carries correct defaults.

Uses the same minimal astrbot stubbing approach as tests/test_sender.py:
this module reuses test_sender's installed stubs and mock component classes
(importing test_sender triggers its stub installer), so core.sender binds a
single consistent set of mock classes regardless of test collection order.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

# 复用 test_sender 的桩环境与组件 mock 类（先导入以触发其桩安装）。
from test_sender import MockImage, MockPlain, MockVideo  # noqa: E402

from core.config import (  # noqa: E402
    DEFAULT_MEDIA_HTTP_TTL,
    MODE_HTTP,
    MODE_LOCAL,
    PluginConfig,
)
from core.data import (  # noqa: E402
    Author,
    ImageContent,
    ParseResult,
    Platform,
    VideoContent,
)
from core.media import MediaUriError, MediaUriResolver  # noqa: E402
from core.sender import MessageSender  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ═══════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════


def _make_cfg(
    cache_dir: Path,
    *,
    mode: str | None = MODE_HTTP,
    base_url: str | None = "https://media.example.com/astrbot",
    ttl: int = 3600,
) -> MagicMock:
    cfg = MagicMock()
    cfg.media_send_mode = mode
    cfg.media_http_base_url = base_url
    cfg.media_http_ttl = ttl
    cfg.cache_dir = Path(cache_dir)
    return cfg


def _make_result(url: str = "https://example.com/test") -> ParseResult:
    return ParseResult(
        platform=Platform(name="Test", display_name="Test"),
        author=Author(name="Author"),
        title="Test Title",
        text="Test body text",
        url=url,
    )


def _make_http_sender(cache_dir: Path) -> MessageSender:
    cfg = MagicMock()
    cfg.show_download_fail_tip = True
    cfg.single_heavy_render_card = False
    cfg.forward_threshold = 999
    cfg.audio_to_file = True
    cfg.media_send_mode = MODE_HTTP
    cfg.media_http_base_url = "https://media.example.com/astrbot"
    cfg.media_http_ttl = 3600
    cfg.cache_dir = Path(cache_dir)
    return MessageSender(cfg, MagicMock())


def _basic_plan(light: list | None = None, heavy: list | None = None) -> dict:
    return {
        "light": light or [],
        "heavy": heavy or [],
        "render_card": False,
        "preview_card": False,
        "force_merge": False,
    }


# ═══════════════════════════════════════════════════════════════════
#  1. MediaUriResolver — local mode
# ═══════════════════════════════════════════════════════════════════


class TestResolverLocal:
    def test_local_mode_returns_file_uri(self, tmp_path: Path) -> None:
        media = tmp_path / "video.mp4"
        resolver = MediaUriResolver(_make_cfg(tmp_path, mode=MODE_LOCAL))
        assert resolver.resolve(media).startswith("file://")
        assert "video.mp4" in resolver.resolve(media)

    def test_local_mode_default_when_mode_missing(self, tmp_path: Path) -> None:
        """media_send_mode absent → local 行为不变（旧配置兼容）。"""
        cfg = _make_cfg(tmp_path, mode=None)
        resolver = MediaUriResolver(cfg)
        assert resolver.resolve(tmp_path / "a.mp4").startswith("file://")


# ═══════════════════════════════════════════════════════════════════
#  2. MediaUriResolver — http mode
# ═══════════════════════════════════════════════════════════════════


class TestResolverHttp:
    def test_basic_url(self, tmp_path: Path) -> None:
        resolver = MediaUriResolver(_make_cfg(tmp_path))
        url = resolver.resolve(tmp_path / "video.mp4")
        assert url == "https://media.example.com/astrbot/video.mp4"

    def test_base_url_without_subpath(self, tmp_path: Path) -> None:
        resolver = MediaUriResolver(
            _make_cfg(tmp_path, base_url="https://media.example.com")
        )
        assert (
            resolver.resolve(tmp_path / "video.mp4")
            == "https://media.example.com/video.mp4"
        )

    def test_chinese_and_space_percent_encoded(self, tmp_path: Path) -> None:
        resolver = MediaUriResolver(_make_cfg(tmp_path))
        url = resolver.resolve(tmp_path / "我的 视频.mp4")
        assert " " not in url
        assert "我" not in url
        assert url == (
            "https://media.example.com/astrbot/"
            "%E6%88%91%E7%9A%84%20%E8%A7%86%E9%A2%91.mp4"
        )

    def test_nested_segments_encoded_and_preserved(self, tmp_path: Path) -> None:
        resolver = MediaUriResolver(_make_cfg(tmp_path))
        url = resolver.resolve(tmp_path / "album" / "照片 1.jpg")
        assert url == (
            "https://media.example.com/astrbot/album/%E7%85%A7%E7%89%87%201.jpg"
        )

    def test_base_url_query_fragment_stripped(self, tmp_path: Path) -> None:
        resolver = MediaUriResolver(
            _make_cfg(
                tmp_path,
                base_url="https://media.example.com/astrbot?token=secret#frag",
            )
        )
        url = resolver.resolve(tmp_path / "video.mp4")
        assert "?" not in url and "#" not in url
        assert "token" not in url
        assert url == "https://media.example.com/astrbot/video.mp4"


# ═══════════════════════════════════════════════════════════════════
#  3. MediaUriResolver — rejections
# ═══════════════════════════════════════════════════════════════════


class TestResolverRejections:
    def test_cache_outside_rejected(self, tmp_path: Path) -> None:
        resolver = MediaUriResolver(_make_cfg(tmp_path / "cache"))
        outside = tmp_path / "outside" / "evil.mp4"
        outside.parent.mkdir()
        with pytest.raises(MediaUriError):
            resolver.resolve(outside)

    def test_base_url_missing(self, tmp_path: Path) -> None:
        resolver = MediaUriResolver(_make_cfg(tmp_path, base_url=None))
        with pytest.raises(MediaUriError):
            resolver.resolve(tmp_path / "a.mp4")

    def test_base_url_empty(self, tmp_path: Path) -> None:
        resolver = MediaUriResolver(_make_cfg(tmp_path, base_url="   "))
        with pytest.raises(MediaUriError):
            resolver.resolve(tmp_path / "a.mp4")

    def test_base_url_bad_scheme(self, tmp_path: Path) -> None:
        resolver = MediaUriResolver(
            _make_cfg(tmp_path, base_url="ftp://media.example.com/a")
        )
        with pytest.raises(MediaUriError):
            resolver.resolve(tmp_path / "a.mp4")

    def test_base_url_no_netloc(self, tmp_path: Path) -> None:
        resolver = MediaUriResolver(_make_cfg(tmp_path, base_url="/astrbot"))
        with pytest.raises(MediaUriError):
            resolver.resolve(tmp_path / "a.mp4")

    def test_unknown_mode(self, tmp_path: Path) -> None:
        resolver = MediaUriResolver(_make_cfg(tmp_path, mode="s3"))
        with pytest.raises(MediaUriError):
            resolver.resolve(tmp_path / "a.mp4")


# ═══════════════════════════════════════════════════════════════════
#  4. MessageSender — HTTP mode behavior
# ═══════════════════════════════════════════════════════════════════


class TestSenderHttpMode:
    @pytest.mark.asyncio
    async def test_video_http_component(self, tmp_path: Path) -> None:
        """HTTP 模式下 VideoContent 生成 http(s) Video 组件，而非 file://。"""
        sender = _make_http_sender(tmp_path)
        cont = VideoContent(path_task=tmp_path / "ok.mp4")
        segs = await sender._build_segments(_make_result(), _basic_plan(heavy=[cont]))
        assert len(segs) == 1
        assert isinstance(segs[0], MockVideo)
        assert segs[0].file == "https://media.example.com/astrbot/ok.mp4"
        assert "file://" not in segs[0].file

    @pytest.mark.asyncio
    async def test_video_uri_error_fallback_no_file_uri(self, tmp_path: Path) -> None:
        """URI 映射失败（cache 外）→ 文本/原链接 fallback，绝不回退 file://。"""
        sender = _make_http_sender(tmp_path)
        result = _make_result()
        cont = VideoContent(path_task=Path("/outside/cache/evil.mp4"))
        segs = await sender._build_segments(result, _basic_plan(heavy=[cont]))
        assert not any(isinstance(s, MockVideo) for s in segs)
        assert not any("file://" in str(getattr(s, "file", "")) for s in segs)
        fallback = [s for s in segs if isinstance(s, MockPlain) and "链接:" in s.text]
        assert fallback, "应保留文本/原链接 fallback"
        assert "https://example.com/test" in fallback[0].text

    @pytest.mark.asyncio
    async def test_light_uri_error_skipped_others_kept(self, tmp_path: Path) -> None:
        """轻媒体 URI 失败 → 跳过该项，保留其他内容。"""
        sender = _make_http_sender(tmp_path)
        bad = ImageContent(path_task=Path("/outside/cache/bad.jpg"))
        good = ImageContent(path_task=tmp_path / "good.jpg")
        segs = await sender._build_segments(
            _make_result(), _basic_plan(light=[bad, good])
        )
        assert len(segs) == 1
        assert isinstance(segs[0], MockImage)
        assert segs[0].file == "https://media.example.com/astrbot/good.jpg"
        assert "file://" not in segs[0].file


# ═══════════════════════════════════════════════════════════════════
#  5. 旧配置默认兼容（直接实例化 PluginConfig，不重构生产代码）
# ═══════════════════════════════════════════════════════════════════


def _make_context() -> SimpleNamespace:
    return SimpleNamespace(get_config=lambda: {"admins_id": [], "timezone": None})


def _make_old_style_config() -> dict[str, Any]:
    """Issue #2 之前的旧配置：不含 media_* 字段。"""
    return {
        "whitelist": [],
        "blacklist": [],
        "arbiter": True,
        "debounce_interval": 300,
        "source_max_size": 90,
        "source_max_minute": 15,
        "audio_to_file": True,
        "single_heavy_render_card": False,
        "forward_threshold": 2,
        "show_download_fail_tip": True,
        "download_timeout": 280,
        "download_retry_times": 2,
        "common_timeout": 15,
        "proxy": "",
        "clean_cron": "30 2 * * *",
        "parsers_template": [
            {
                "__template_key": "bilibili",
                "enable": True,
                "use_proxy": False,
                "cookies": "",
                "video_codec_list": ["AVC"],
                "video_quality": "_720P",
            }
        ],
    }


class TestConfigDefaultCompat:
    def _instantiate(self, config_data: dict[str, Any], tmp_path: Path) -> PluginConfig:
        return PluginConfig(config_data, _make_context())

    def test_old_config_missing_media_fields_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import core.config as config_mod

        monkeypatch.setattr(
            config_mod,
            "StarTools",
            SimpleNamespace(get_data_dir=lambda name: tmp_path),
        )
        cfg = self._instantiate(_make_old_style_config(), tmp_path)
        assert cfg.media_send_mode == MODE_LOCAL
        assert cfg.media_http_base_url is None
        assert cfg.media_http_ttl == DEFAULT_MEDIA_HTTP_TTL

    def test_invalid_values_normalized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import core.config as config_mod

        monkeypatch.setattr(
            config_mod,
            "StarTools",
            SimpleNamespace(get_data_dir=lambda name: tmp_path),
        )
        data = _make_old_style_config()
        data["media_send_mode"] = "HTTP"
        data["media_http_base_url"] = "  https://media.example.com/astrbot  "
        data["media_http_ttl"] = "abc"  # 非法 → 回退默认
        cfg = self._instantiate(data, tmp_path)
        assert cfg.media_send_mode == MODE_HTTP
        assert cfg.media_http_base_url == "https://media.example.com/astrbot"
        assert cfg.media_http_ttl == DEFAULT_MEDIA_HTTP_TTL

    def test_invalid_mode_falls_back_to_local(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import core.config as config_mod

        monkeypatch.setattr(
            config_mod,
            "StarTools",
            SimpleNamespace(get_data_dir=lambda name: tmp_path),
        )
        data = _make_old_style_config()
        data["media_send_mode"] = "s3"
        cfg = self._instantiate(data, tmp_path)
        assert cfg.media_send_mode == MODE_LOCAL


# ═══════════════════════════════════════════════════════════════════
#  6. _conf_schema.json
# ═══════════════════════════════════════════════════════════════════


class TestConfSchema:
    def test_schema_parses_and_media_defaults(self) -> None:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        mode = schema["media_send_mode"]
        assert mode["type"] == "string"
        assert mode["options"] == ["local", "http"]
        assert mode["default"] == "local"

        base = schema["media_http_base_url"]
        assert base["type"] == "string"
        assert base["default"] == ""

        ttl = schema["media_http_ttl"]
        assert ttl["type"] == "int"
        assert ttl["default"] == DEFAULT_MEDIA_HTTP_TTL
        assert ttl["slider"]["min"] > 0
