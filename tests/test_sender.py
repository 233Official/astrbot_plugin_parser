"""Test sender.py: message sending strategies.

Covers:
  - _build_text_fallback with display_url
  - VideoContent download failure: cover + summary (dedup once per call)
  - show_download_fail_tip controls the tip but NOT the summary/cover
  - _append_video_fail_cover helper
  - Non-VideoContent download failure unaffected

Uses minimal mocking: astrbot modules are stubbed out,
core.render is stubbed to avoid PIL/aiofiles, and core.config imports
are satisfied by the astrbot stubs (no real PluginConfig instantiation).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════
#  Minimal mock classes for astrbot.core.message.components
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
    def __init__(self, file: str) -> None:
        self.file = file

    def __repr__(self) -> str:
        return f"Image({self.file!r})"


class MockVideo:
    def __init__(self, file: str) -> None:
        self.file = file


class MockFile:
    def __init__(self, name: str, file: str) -> None:
        self.name = name
        self.file = file


class MockRecord:
    def __init__(self, file: str) -> None:
        self.file = file


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
#  Session-scoped mock environment installation
# ═══════════════════════════════════════════════════════════════════

_MOCKS_INSTALLED = False


def _install_module_stubs() -> None:
    """Install all required module stubs into sys.modules.

    Must be called before any test imports core.sender (or modules
    that transitively import astrbot packages).
    Safe to call multiple times — only runs once.
    """
    global _MOCKS_INSTALLED
    if _MOCKS_INSTALLED:
        return
    _MOCKS_INSTALLED = True

    logger = types.SimpleNamespace(
        debug=lambda *a, **kw: None,
        warning=lambda *a, **kw: None,
        error=lambda *a, **kw: None,
        info=lambda *a, **kw: None,
    )

    stubs: list[tuple[str, dict[str, Any]]] = [
        ("astrbot", {"__path__": []}),
        ("astrbot.api", {"logger": logger}),
        ("astrbot.core", {"__path__": []}),
        ("astrbot.core.message", {"__path__": []}),
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
        ("astrbot.core.utils.astrbot_path", {"get_astrbot_plugin_path": lambda: "."}),
    ]

    for mod_name, attrs in stubs:
        if mod_name not in sys.modules:
            mod = types.ModuleType(mod_name)
            for k, v in attrs.items():
                setattr(mod, k, v)
            sys.modules[mod_name] = mod

    # core.render is a real file on disk that imports PIL / aiofiles / apilmoji.
    # Stub it out so importing core.sender doesn't trigger those heavy imports.
    if "core.render" not in sys.modules:
        render_stub = types.ModuleType("core.render")
        render_stub.Renderer = object
        sys.modules["core.render"] = render_stub


# ═══════════════════════════════════════════════════════════════════
#  Import production modules (safe after stubs are installed)
# ═══════════════════════════════════════════════════════════════════

_install_module_stubs()

from core.data import (
    Author,
    AudioContent,
    ImageContent,
    ParseResult,
    Platform,
    TextContent,
    VideoContent,
)
from core.exception import DownloadException, SizeLimitException
from core.sender import MessageSender


# ═══════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════


def _make_sender(
    *,
    show_download_fail_tip: bool = True,
    single_heavy_render_card: bool = False,
    forward_threshold: int = 999,
    audio_to_file: bool = False,
) -> MessageSender:
    """Create a MessageSender with a lightweight mock config/renderer."""
    cfg = MagicMock()
    cfg.show_download_fail_tip = show_download_fail_tip
    cfg.single_heavy_render_card = single_heavy_render_card
    cfg.forward_threshold = forward_threshold
    cfg.audio_to_file = audio_to_file
    renderer = MagicMock()
    return MessageSender(cfg, renderer)


def _make_result(
    platform_name: str = "Test",
    author_name: str = "Author",
    title: str | None = "Test Title",
    text: str | None = "Test body text",
    url: str | None = "https://example.com/test",
    extra: dict[str, Any] | None = None,
) -> ParseResult:
    return ParseResult(
        platform=Platform(name=platform_name, display_name=platform_name),
        author=Author(name=author_name) if author_name else None,
        title=title,
        text=text,
        url=url,
        extra=extra or {},
    )


def _make_video_content(
    *,
    get_path_side_effect: type[Exception] | None = None,
    cover_path: Path | None = Path("/fake/cover.jpg"),
) -> VideoContent:
    """Create a VideoContent with mocked async methods.

    Because VideoContent uses ``slots=True``, we patch methods on the
    *class* via ``unittest.mock.patch.object``.  Callers must wrap
    usage in a ``with`` block if they need the patches active.
    """
    cont = VideoContent(path_task=Path("/dummy/path.mp4"))
    cont.cover = cover_path

    # Patch get_path on the class so existing instances pick it up.
    # We use patch.object as a context manager in the caller.
    return cont


def _make_basic_plan(
    heavy: list | None = None,
    light: list | None = None,
    *,
    render_card: bool = False,
    force_merge: bool = False,
) -> dict:
    return {
        "light": light or [],
        "heavy": heavy or [],
        "render_card": render_card,
        "preview_card": render_card and not force_merge,
        "force_merge": force_merge,
    }


# ═══════════════════════════════════════════════════════════════════
#  1. _build_text_fallback
# ═══════════════════════════════════════════════════════════════════


class TestBuildTextFallback:
    """Verify _build_text_fallback output, especially display_url inclusion."""

    def test_includes_display_url(self) -> None:
        result = _make_result(url="https://example.com/video")
        segs = MessageSender._build_text_fallback(result)
        assert len(segs) == 1
        text = segs[0].text
        assert "Test @Author | Test Title" in text
        assert "Test body text" in text
        assert "链接: https://example.com/video" in text

    def test_display_url_last(self) -> None:
        """display_url should be the last line."""
        result = _make_result(url="https://example.com/video")
        segs = MessageSender._build_text_fallback(result)
        lines = segs[0].text.split("\n")
        assert lines[-1] == "链接: https://example.com/video"

    def test_no_url(self) -> None:
        result = _make_result(url=None)
        segs = MessageSender._build_text_fallback(result)
        text = segs[0].text
        assert "链接:" not in text
        assert "Test @Author | Test Title" in text

    def test_uses_extra_info_when_no_text(self) -> None:
        result = _make_result(text=None, extra={"info": "Extra info line"})
        segs = MessageSender._build_text_fallback(result)
        text = segs[0].text
        assert "Extra info line" in text
        assert "Test @Author | Test Title" in text
        assert "链接: https://example.com/test" in text

    def test_empty_result_returns_empty_list(self) -> None:
        """When nothing is available, return empty list."""
        result = ParseResult(
            platform=Platform(name="", display_name=""),
            author=None,
            title=None,
            text=None,
            url=None,
        )
        segs = MessageSender._build_text_fallback(result)
        assert segs == []

    def test_only_url(self) -> None:
        """Even with just url, display_url should appear."""
        result = ParseResult(
            platform=Platform(name="", display_name=""),
            author=None,
            title=None,
            text=None,
            url="https://example.com/x",
        )
        segs = MessageSender._build_text_fallback(result)
        text = segs[0].text
        assert text == "链接: https://example.com/x"

    def test_header_without_author(self) -> None:
        result = ParseResult(
            platform=Platform(name="X", display_name="XPlatform"),
            author=None,
            title="TitleOnly",
            text="Body",
            url="https://x.com/p",
        )
        segs = MessageSender._build_text_fallback(result)
        text = segs[0].text
        assert "XPlatform | TitleOnly" in text
        assert "Body" in text
        assert "链接:" in text


# ═══════════════════════════════════════════════════════════════════
#  2. VideoContent download failure behavior in _build_segments
# ═══════════════════════════════════════════════════════════════════


class TestVideoDownloadFailure:
    """Cover + summary fallback for VideoContent download failures."""

    # ── helpers ────────────────────────────────────────────────

    @pytest.fixture
    def sender(self) -> MessageSender:
        return _make_sender()

    @pytest.fixture
    def result(self) -> ParseResult:
        return _make_result()

    async def _run_build_segments(
        self,
        sender: MessageSender,
        result: ParseResult,
        plan: dict,
    ) -> list:
        """Run _build_segments with VideoContent methods patched.

        We patch ``VideoContent.get_path`` and ``VideoContent.get_cover_path``
        on the class so existing instances pick them up without needing
        ``slots`` workarounds.
        """
        # The plan heavy list contains VideoContent instances that were
        # created before patching.  patch.object on the class affects
        # all existing instances.
        return await sender._build_segments(result, plan)

    # ── success path (no failure) ──────────────────────────────

    @pytest.mark.asyncio
    async def test_video_success_no_fallback(self, sender, result) -> None:
        """When video downloads OK, no fallback is injected."""
        cont = _make_video_content(get_path_side_effect=None)
        plan = _make_basic_plan(heavy=[cont])
        with (
            patch.object(VideoContent, "get_path", return_value=Path("/ok.mp4")),
            patch.object(
                VideoContent, "get_cover_path", return_value=Path("/cover.jpg")
            ),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        # Should have one Video component
        assert len(segs) == 1
        assert isinstance(segs[0], MockVideo)
        assert "ok.mp4" in segs[0].file

    # ── DownloadException ──────────────────────────────────────

    @pytest.mark.asyncio
    async def test_download_exception_adds_cover_and_summary(
        self, sender, result
    ) -> None:
        cont = _make_video_content()
        plan = _make_basic_plan(heavy=[cont])
        with (
            patch.object(VideoContent, "get_path", side_effect=DownloadException()),
            patch.object(
                VideoContent,
                "get_cover_path",
                return_value=Path("/fake/cover.jpg"),
            ),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        # Expected: cover Image + summary Plain + fail tip Plain
        assert len(segs) >= 2
        assert isinstance(segs[0], MockImage)  # cover
        assert "/fake/cover.jpg" in segs[0].file
        assert isinstance(segs[1], MockPlain)  # summary
        assert "链接:" in segs[1].text

    @pytest.mark.asyncio
    async def test_download_exception_summary_once(self, sender, result) -> None:
        """Multiple VideoContent failures in one group → summary only once."""
        v1 = _make_video_content(cover_path=Path("/c1.jpg"))
        v2 = _make_video_content(cover_path=Path("/c2.jpg"))
        plan = _make_basic_plan(heavy=[v1, v2])
        with (
            patch.object(VideoContent, "get_path", side_effect=DownloadException()),
            patch.object(
                VideoContent,
                "get_cover_path",
                side_effect=[Path("/c1.jpg"), Path("/c2.jpg")],
            ),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        # Two covers, one summary, plus fail tips
        cover_count = sum(1 for s in segs if isinstance(s, MockImage))
        plain_count = sum(1 for s in segs if isinstance(s, MockPlain))
        summary_plains = [
            s for s in segs if isinstance(s, MockPlain) and "链接:" in s.text
        ]

        assert cover_count == 2, "should have cover for each failed video"
        assert len(summary_plains) == 1, "summary should appear only once"
        assert "链接: https://example.com/test" in summary_plains[0].text
        fail_tips = [
            s for s in segs if isinstance(s, MockPlain) and "此项媒体下载失败" in s.text
        ]
        assert len(fail_tips) == 2

    @pytest.mark.asyncio
    async def test_download_exception_no_cover(self, sender, result) -> None:
        """When cover is None, no Image is appended but summary still sent."""
        cont = _make_video_content(cover_path=None)
        plan = _make_basic_plan(heavy=[cont])
        with (
            patch.object(VideoContent, "get_path", side_effect=DownloadException()),
            patch.object(VideoContent, "get_cover_path", return_value=None),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        assert not any(isinstance(s, MockImage) for s in segs)
        assert any(isinstance(s, MockPlain) and "链接:" in s.text for s in segs)

    @pytest.mark.asyncio
    async def test_download_exception_cover_get_cover_path_raises(
        self, sender, result
    ) -> None:
        """If get_cover_path itself raises, it's ignored, summary still sent."""
        cont = _make_video_content(cover_path=Path("/ignored.jpg"))
        plan = _make_basic_plan(heavy=[cont])
        with (
            patch.object(VideoContent, "get_path", side_effect=DownloadException()),
            patch.object(
                VideoContent,
                "get_cover_path",
                side_effect=RuntimeError("cover download failed"),
            ),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        assert not any(isinstance(s, MockImage) for s in segs)
        assert any(isinstance(s, MockPlain) and "链接:" in s.text for s in segs)

    # ── show_download_fail_tip independent ─────────────────────

    @pytest.mark.asyncio
    async def test_show_download_fail_tip_false_still_shows_cover_and_summary(
        self, result
    ) -> None:
        """show_download_fail_tip=False → no "失败" tip, but cover+summary persist."""
        sender = _make_sender(show_download_fail_tip=False)
        cont = _make_video_content()
        plan = _make_basic_plan(heavy=[cont])
        with (
            patch.object(VideoContent, "get_path", side_effect=DownloadException()),
            patch.object(
                VideoContent,
                "get_cover_path",
                return_value=Path("/fake/cover.jpg"),
            ),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        # Cover + summary present
        assert any(isinstance(s, MockImage) for s in segs)
        assert any(isinstance(s, MockPlain) and "链接:" in s.text for s in segs)
        # No fail tip
        assert not any(isinstance(s, MockPlain) and "下载失败" in s.text for s in segs)

    # ── SizeLimitException ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_size_limit_adds_cover_and_summary(self, sender, result) -> None:
        cont = _make_video_content()
        plan = _make_basic_plan(heavy=[cont])
        with (
            patch.object(VideoContent, "get_path", side_effect=SizeLimitException()),
            patch.object(
                VideoContent,
                "get_cover_path",
                return_value=Path("/fake/cover.jpg"),
            ),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        # Cover + summary + size-limit text
        assert isinstance(segs[0], MockImage)
        assert any(isinstance(s, MockPlain) and "链接:" in s.text for s in segs)
        assert any(isinstance(s, MockPlain) and "大小限制" in s.text for s in segs)

    @pytest.mark.asyncio
    async def test_size_limit_multiple_videos_summary_once(
        self, sender, result
    ) -> None:
        v1 = _make_video_content(cover_path=Path("/c1.jpg"))
        v2 = _make_video_content(cover_path=Path("/c2.jpg"))
        plan = _make_basic_plan(heavy=[v1, v2])
        with (
            patch.object(VideoContent, "get_path", side_effect=SizeLimitException()),
            patch.object(
                VideoContent,
                "get_cover_path",
                side_effect=[Path("/c1.jpg"), Path("/c2.jpg")],
            ),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        summary_count = sum(
            1 for s in segs if isinstance(s, MockPlain) and "链接:" in s.text
        )
        assert summary_count == 1

    # ── non-VideoContent unaffected ────────────────────────────

    @pytest.mark.asyncio
    async def test_non_video_download_exception_no_fallback(
        self, sender, result
    ) -> None:
        """AudioContent with DownloadException → no cover/summary."""
        cont = AudioContent(path_task=Path("/dummy.mp3"))
        plan = _make_basic_plan(heavy=[cont])
        with patch.object(AudioContent, "get_path", side_effect=DownloadException()):
            segs = await self._run_build_segments(sender, result, plan)

        # Only the fail tip (if show_download_fail_tip is True)
        assert not any(isinstance(s, MockImage) for s in segs)
        assert not any(isinstance(s, MockPlain) and "链接:" in s.text for s in segs)
        assert any(isinstance(s, MockPlain) and "下载失败" in s.text for s in segs)


# ═══════════════════════════════════════════════════════════════════
#  3. _append_video_fail_cover helper
# ═══════════════════════════════════════════════════════════════════


class TestAppendVideoFailCover:
    """Targeted tests for the helper that appends cover Image."""

    @pytest.fixture
    def sender(self) -> MessageSender:
        return _make_sender()

    @pytest.mark.asyncio
    async def test_appends_image_when_cover_exists(self, sender) -> None:
        cont = _make_video_content(cover_path=Path("/real/cover.jpg"))
        segs: list = []
        with patch.object(
            VideoContent, "get_cover_path", return_value=Path("/real/cover.jpg")
        ):
            await sender._append_video_fail_cover(segs, cont)

        assert len(segs) == 1
        assert isinstance(segs[0], MockImage)
        assert "cover.jpg" in segs[0].file

    @pytest.mark.asyncio
    async def test_skips_when_cover_is_none(self, sender) -> None:
        cont = _make_video_content(cover_path=None)
        segs: list = []
        with patch.object(VideoContent, "get_cover_path", return_value=None):
            await sender._append_video_fail_cover(segs, cont)

        assert segs == []

    @pytest.mark.asyncio
    async def test_ignores_exception(self, sender) -> None:
        cont = _make_video_content(cover_path=Path("/ignored.jpg"))
        segs: list = []
        with patch.object(
            VideoContent,
            "get_cover_path",
            side_effect=RuntimeError("boom"),
        ):
            await sender._append_video_fail_cover(segs, cont)

        assert segs == []
