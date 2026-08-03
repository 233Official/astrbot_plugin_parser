"""Test media URI resolution (local + http) and sender HTTP behavior.

Covers:
  - MediaUriResolver: local file:// URI, http URL, Chinese/space percent
    encoding, cache-outside rejection, missing/illegal base URL.
  - MessageSender: HTTP mode produces http(s) media URIs; on MediaUriError
    heavy media emits an explicit failure tip and never falls back to file://.
  - Old-config default compatibility: missing media_* fields normalize to
    local / None / DEFAULT_MEDIA_HTTP_TTL.
  - _conf_schema.json parses and carries correct defaults.

Follows the current upstream test architecture (tests/test_cookie.py):
astrbot modules are stubbed inside a pytest fixture via monkeypatch, and the
modules under test are imported through importlib so the stubs take effect.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest


# ═══════════════════════════════════════════════════════════════════
#  Mock component classes (mirror astrbot.core.message.components)
# ═══════════════════════════════════════════════════════════════════


class MockPlain:
    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return f"Plain({self.text!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, MockPlain):
            return self.text == other.text
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.text)


class MockImage:
    def __init__(self, file: str | None = None, **kwargs) -> None:
        self.file = file
        for k, v in kwargs.items():
            setattr(self, k, v)

    @classmethod
    def fromFileSystem(cls, path, **kwargs):
        file_path = Path(path).resolve(strict=False)
        return cls(file=file_path.as_uri(), path=str(file_path), **kwargs)

    def __repr__(self) -> str:
        return f"Image({self.file!r})"


class MockVideo:
    def __init__(self, file: str | None = None, **kwargs) -> None:
        self.file = file
        for k, v in kwargs.items():
            setattr(self, k, v)

    @classmethod
    def fromFileSystem(cls, path, **kwargs):
        file_path = Path(path).resolve(strict=False)
        return cls(file=file_path.as_uri(), path=str(file_path), **kwargs)


class MockRecord:
    def __init__(self, file: str | None = None, **kwargs) -> None:
        self.file = file
        for k, v in kwargs.items():
            setattr(self, k, v)

    @classmethod
    def fromFileSystem(cls, path, **kwargs):
        file_path = Path(path).resolve(strict=False)
        return cls(file=file_path.as_uri(), path=str(file_path), **kwargs)


class MockFile:
    def __init__(
        self, name: str | None = None, file: str | None = None, **kwargs
    ) -> None:
        self.name = name
        self.file = file
        for k, v in kwargs.items():
            setattr(self, k, v)


class MockNode:
    def __init__(self, uin: str, name: str, content: list) -> None:
        self.uin = uin
        self.name = name
        self.content = content


class MockNodes:
    def __init__(self, nodes: list) -> None:
        self.nodes = nodes


class MockBaseMessageComponent:
    pass


# ═══════════════════════════════════════════════════════════════════
#  Fixture: install astrbot stubs and import production modules
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def media_env(monkeypatch: pytest.MonkeyPatch):
    """Install minimal astrbot stubs, then import core modules fresh."""
    logger = SimpleNamespace(
        debug=lambda *a, **kw: None,
        warning=lambda *a, **kw: None,
        error=lambda *a, **kw: None,
        info=lambda *a, **kw: None,
        exception=lambda *a, **kw: None,
    )

    stubs: list[tuple[str, dict[str, Any]]] = [
        ("astrbot", {"__path__": []}),
        ("astrbot.api", {"logger": logger}),
        ("astrbot.core", {"__path__": []}),
        (
            "astrbot.core.message.components",
            {
                "BaseMessageComponent": MockBaseMessageComponent,
                "File": MockFile,
                "Image": MockImage,
                "Node": MockNode,
                "Nodes": MockNodes,
                "Plain": MockPlain,
                "Record": MockRecord,
                "Video": MockVideo,
            },
        ),
        ("astrbot.core.platform", {"__path__": []}),
        ("astrbot.core.platform.astr_message_event", {"AstrMessageEvent": object}),
        ("astrbot.core.config", {"__path__": []}),
        ("astrbot.core.config.astrbot_config", {"AstrBotConfig": dict}),
        ("astrbot.core.star", {"__path__": []}),
        ("astrbot.core.star.context", {"Context": object}),
        ("astrbot.core.star.star_tools", {"StarTools": object}),
        ("astrbot.core.utils", {"__path__": []}),
        (
            "astrbot.core.utils.astrbot_path",
            {
                "get_astrbot_plugin_path": lambda: ".",
                "get_astrbot_plugin_data_path": lambda: ".",
            },
        ),
    ]

    for mod_name, attrs in stubs:
        mod = types.ModuleType(mod_name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        monkeypatch.setitem(sys.modules, mod_name, mod)

    # core.render 依赖 PIL/aiofiles，测试中 stub 掉
    render_stub = types.ModuleType("core.render")
    render_stub.Renderer = object
    monkeypatch.setitem(sys.modules, "core.render", render_stub)

    for mod_name in ("core.config", "core.data", "core.media", "core.sender"):
        monkeypatch.delitem(sys.modules, mod_name, raising=False)

    return SimpleNamespace(
        config=importlib.import_module("core.config"),
        data=importlib.import_module("core.data"),
        media=importlib.import_module("core.media"),
        sender=importlib.import_module("core.sender"),
    )


ROOT = Path(__file__).resolve().parent.parent


# ═══════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════


def _make_cfg(
    cache_dir: Path,
    *,
    mode: str | None = "http",
    base_url: str | None = "https://media.example.com/astrbot",
    ttl: int = 3600,
) -> MagicMock:
    cfg = MagicMock()
    cfg.media_send_mode = mode
    cfg.media_http_base_url = base_url
    cfg.media_http_ttl = ttl
    cfg.cache_dir = Path(cache_dir)
    return cfg


def _make_result(media_env, url: str = "https://example.com/test"):
    return media_env.data.ParseResult(
        platform=media_env.data.Platform(name="Test", display_name="Test"),
        author=media_env.data.Author(name="Author"),
        title="Test Title",
        text="Test body text",
        url=url,
    )


def _make_http_sender(media_env, cache_dir: Path) -> Any:
    cfg = MagicMock()
    cfg.show_download_fail_tip = True
    cfg.single_heavy_render_card = False
    cfg.forward_threshold = 999
    cfg.audio_to_file = True
    cfg.media_send_mode = "http"
    cfg.media_http_base_url = "https://media.example.com/astrbot"
    cfg.media_http_ttl = 3600
    cfg.cache_dir = Path(cache_dir)
    return media_env.sender.MessageSender(cfg, MagicMock())


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
    def test_local_mode_returns_file_uri(self, media_env, tmp_path: Path) -> None:
        media = tmp_path / "video.mp4"
        resolver = media_env.media.MediaUriResolver(_make_cfg(tmp_path, mode="local"))
        assert resolver.resolve(media).startswith("file://")
        assert "video.mp4" in resolver.resolve(media)

    def test_local_mode_default_when_mode_missing(
        self, media_env, tmp_path: Path
    ) -> None:
        """media_send_mode absent → local 行为不变（旧配置兼容）。"""
        cfg = _make_cfg(tmp_path, mode=None)
        resolver = media_env.media.MediaUriResolver(cfg)
        assert resolver.resolve(tmp_path / "a.mp4").startswith("file://")


# ═══════════════════════════════════════════════════════════════════
#  2. MediaUriResolver — http mode
# ═══════════════════════════════════════════════════════════════════


class TestResolverHttp:
    def test_basic_url(self, media_env, tmp_path: Path) -> None:
        resolver = media_env.media.MediaUriResolver(_make_cfg(tmp_path))
        url = resolver.resolve(tmp_path / "video.mp4")
        assert url == "https://media.example.com/astrbot/video.mp4"

    def test_base_url_without_subpath(self, media_env, tmp_path: Path) -> None:
        resolver = media_env.media.MediaUriResolver(
            _make_cfg(tmp_path, base_url="https://media.example.com")
        )
        assert (
            resolver.resolve(tmp_path / "video.mp4")
            == "https://media.example.com/video.mp4"
        )

    def test_chinese_and_space_percent_encoded(self, media_env, tmp_path: Path) -> None:
        resolver = media_env.media.MediaUriResolver(_make_cfg(tmp_path))
        url = resolver.resolve(tmp_path / "我的 视频.mp4")
        assert " " not in url
        assert "我" not in url
        assert url == (
            "https://media.example.com/astrbot/"
            "%E6%88%91%E7%9A%84%20%E8%A7%86%E9%A2%91.mp4"
        )

    def test_nested_segments_encoded_and_preserved(
        self, media_env, tmp_path: Path
    ) -> None:
        resolver = media_env.media.MediaUriResolver(_make_cfg(tmp_path))
        url = resolver.resolve(tmp_path / "album" / "照片 1.jpg")
        assert url == (
            "https://media.example.com/astrbot/album/%E7%85%A7%E7%89%87%201.jpg"
        )

    def test_base_url_query_fragment_stripped(self, media_env, tmp_path: Path) -> None:
        resolver = media_env.media.MediaUriResolver(
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
    def test_cache_outside_rejected(self, media_env, tmp_path: Path) -> None:
        resolver = media_env.media.MediaUriResolver(_make_cfg(tmp_path / "cache"))
        outside = tmp_path / "outside" / "evil.mp4"
        outside.parent.mkdir()
        with pytest.raises(media_env.media.MediaUriError):
            resolver.resolve(outside)

    def test_base_url_missing(self, media_env, tmp_path: Path) -> None:
        resolver = media_env.media.MediaUriResolver(_make_cfg(tmp_path, base_url=None))
        with pytest.raises(media_env.media.MediaUriError):
            resolver.resolve(tmp_path / "a.mp4")

    def test_base_url_empty(self, media_env, tmp_path: Path) -> None:
        resolver = media_env.media.MediaUriResolver(_make_cfg(tmp_path, base_url="   "))
        with pytest.raises(media_env.media.MediaUriError):
            resolver.resolve(tmp_path / "a.mp4")

    def test_base_url_bad_scheme(self, media_env, tmp_path: Path) -> None:
        resolver = media_env.media.MediaUriResolver(
            _make_cfg(tmp_path, base_url="ftp://media.example.com/a")
        )
        with pytest.raises(media_env.media.MediaUriError):
            resolver.resolve(tmp_path / "a.mp4")

    def test_base_url_no_netloc(self, media_env, tmp_path: Path) -> None:
        resolver = media_env.media.MediaUriResolver(
            _make_cfg(tmp_path, base_url="/astrbot")
        )
        with pytest.raises(media_env.media.MediaUriError):
            resolver.resolve(tmp_path / "a.mp4")

    def test_unknown_mode(self, media_env, tmp_path: Path) -> None:
        resolver = media_env.media.MediaUriResolver(_make_cfg(tmp_path, mode="s3"))
        with pytest.raises(media_env.media.MediaUriError):
            resolver.resolve(tmp_path / "a.mp4")


# ═══════════════════════════════════════════════════════════════════
#  4. MessageSender — HTTP mode behavior
# ═══════════════════════════════════════════════════════════════════


class TestSenderHttpMode:
    @pytest.mark.asyncio
    async def test_video_http_component(self, media_env, tmp_path: Path) -> None:
        """HTTP 模式下 VideoContent 生成 http(s) Video 组件，而非 file://。"""
        sender = _make_http_sender(media_env, tmp_path)
        cont = media_env.data.VideoContent(path_task=tmp_path / "ok.mp4")
        segs = await sender._build_segments(
            _make_result(media_env), _basic_plan(heavy=[cont])
        )
        assert len(segs) == 1
        assert isinstance(segs[0], MockVideo)
        assert segs[0].file == "https://media.example.com/astrbot/ok.mp4"
        assert "file://" not in segs[0].file

    @pytest.mark.asyncio
    async def test_video_uri_error_no_file_uri(self, media_env, tmp_path: Path) -> None:
        """URI 映射失败（cache 外）→ 明确失败提示，绝不回退 file://。"""
        sender = _make_http_sender(media_env, tmp_path)
        result = _make_result(media_env)
        cont = media_env.data.VideoContent(path_task=Path("/outside/cache/evil.mp4"))
        segs = await sender._build_segments(result, _basic_plan(heavy=[cont]))
        assert not any(isinstance(s, MockVideo) for s in segs)
        assert not any("file://" in str(getattr(s, "file", "")) for s in segs)
        assert any(isinstance(s, MockPlain) and "发送失败" in s.text for s in segs)

    @pytest.mark.asyncio
    async def test_light_uri_error_skipped_others_kept(
        self, media_env, tmp_path: Path
    ) -> None:
        """轻媒体 URI 失败 → 跳过该项，保留其他内容。"""
        sender = _make_http_sender(media_env, tmp_path)
        bad = media_env.data.ImageContent(path_task=Path("/outside/cache/bad.jpg"))
        good = media_env.data.ImageContent(path_task=tmp_path / "good.jpg")
        segs = await sender._build_segments(
            _make_result(media_env), _basic_plan(light=[bad, good])
        )
        assert len(segs) == 1
        assert isinstance(segs[0], MockImage)
        assert segs[0].file == "https://media.example.com/astrbot/good.jpg"
        assert "file://" not in segs[0].file


# ═══════════════════════════════════════════════════════════════════
#  5. 旧配置默认兼容（直接实例化 PluginConfig）
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
    def _instantiate(self, media_env, config_data: dict[str, Any], tmp_path: Path):
        return media_env.config.PluginConfig(config_data, _make_context())

    def test_old_config_missing_media_fields_defaults(
        self, media_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_mod = media_env.config
        monkeypatch.setattr(
            config_mod, "get_astrbot_plugin_data_path", lambda: str(tmp_path)
        )
        cfg = self._instantiate(media_env, _make_old_style_config(), tmp_path)
        assert cfg.media_send_mode == config_mod.MODE_LOCAL
        assert cfg.media_http_base_url is None
        assert cfg.media_http_ttl == config_mod.DEFAULT_MEDIA_HTTP_TTL

    def test_invalid_values_normalized(
        self, media_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_mod = media_env.config
        monkeypatch.setattr(
            config_mod, "get_astrbot_plugin_data_path", lambda: str(tmp_path)
        )
        data = _make_old_style_config()
        data["media_send_mode"] = "HTTP"
        data["media_http_base_url"] = "  https://media.example.com/astrbot  "
        data["media_http_ttl"] = "abc"  # 非法 → 回退默认
        cfg = self._instantiate(media_env, data, tmp_path)
        assert cfg.media_send_mode == config_mod.MODE_HTTP
        assert cfg.media_http_base_url == "https://media.example.com/astrbot"
        assert cfg.media_http_ttl == config_mod.DEFAULT_MEDIA_HTTP_TTL

    def test_invalid_mode_falls_back_to_local(
        self, media_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_mod = media_env.config
        monkeypatch.setattr(
            config_mod, "get_astrbot_plugin_data_path", lambda: str(tmp_path)
        )
        data = _make_old_style_config()
        data["media_send_mode"] = "s3"
        cfg = self._instantiate(media_env, data, tmp_path)
        assert cfg.media_send_mode == config_mod.MODE_LOCAL


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
        assert ttl["default"] == 3600
        assert ttl["slider"]["min"] > 0
