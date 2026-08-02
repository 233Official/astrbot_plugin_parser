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
    SendGroup,
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

        ``Path.exists`` is also patched to return ``True`` so that the
        ``_append_video_fail_cover`` coverage check passes for tests that
        expect a cover ``Image`` to be appended.
        """
        # The plan heavy list contains VideoContent instances that were
        # created before patching.  patch.object on the class affects
        # all existing instances.
        with patch.object(Path, "exists", return_value=True):
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

        # Expected: cover Image + reason Plain + summary Plain + fail tip Plain
        assert len(segs) >= 3
        assert isinstance(segs[0], MockImage)  # cover
        assert "/fake/cover.jpg" in segs[0].file
        assert isinstance(segs[1], MockPlain)  # failure reason
        assert "视频下载失败" in segs[1].text
        assert isinstance(segs[2], MockPlain)  # summary
        assert "链接:" in segs[2].text

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
        """show_download_fail_tip=False → no "此项媒体下载失败" tip, but cover+reason+summary persist."""
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

        # Cover + reason + summary present
        assert any(isinstance(s, MockImage) for s in segs)
        assert any(isinstance(s, MockPlain) and "视频下载失败" in s.text for s in segs)
        assert any(isinstance(s, MockPlain) and "链接:" in s.text for s in segs)
        # No fail tip (the generic "此项媒体下载失败" is controlled by show_download_fail_tip)
        assert not any(
            isinstance(s, MockPlain) and "此项媒体下载失败" in s.text for s in segs
        )

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
        assert any(
            isinstance(s, MockPlain) and "此项媒体下载失败" in s.text for s in segs
        )

    # ── Failure reason text ──────────────────────────────────

    @pytest.mark.asyncio
    async def test_download_exception_shows_custom_reason(self, sender, result) -> None:
        """DownloadException with custom message shows reason in output."""
        cont = _make_video_content()
        plan = _make_basic_plan(heavy=[cont])
        with (
            patch.object(
                VideoContent,
                "get_path",
                side_effect=DownloadException("网络超时"),
            ),
            patch.object(
                VideoContent,
                "get_cover_path",
                return_value=Path("/fake/cover.jpg"),
            ),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        reasons = [
            s for s in segs if isinstance(s, MockPlain) and "视频下载失败" in s.text
        ]
        assert len(reasons) == 1
        assert "网络超时" in reasons[0].text

    @pytest.mark.asyncio
    async def test_size_limit_exception_shows_reason(self, sender, result) -> None:
        """SizeLimitException shows its reason in output."""
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

        reasons = [
            s for s in segs if isinstance(s, MockPlain) and "视频超过大小限制" in s.text
        ]
        assert len(reasons) == 1
        assert "媒体大小超过配置限制" in reasons[0].text

    @pytest.mark.asyncio
    async def test_size_limit_exception_shows_actual_and_limit(
        self, sender, result
    ) -> None:
        """SizeLimitException with sizes shows actual media size and configured limit."""
        cont = _make_video_content()
        plan = _make_basic_plan(heavy=[cont])
        with (
            patch.object(
                VideoContent,
                "get_path",
                side_effect=SizeLimitException(92.3, 90),
            ),
            patch.object(
                VideoContent,
                "get_cover_path",
                return_value=Path("/fake/cover.jpg"),
            ),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        reasons = [
            s for s in segs if isinstance(s, MockPlain) and "视频超过大小限制" in s.text
        ]
        assert len(reasons) == 1
        assert "92.30 MB" in reasons[0].text
        assert "90 MB" in reasons[0].text

    @pytest.mark.asyncio
    async def test_download_exception_empty_reason_fallback(
        self, sender, result
    ) -> None:
        """When exception message is empty, use generic fallback."""
        cont = _make_video_content()
        plan = _make_basic_plan(heavy=[cont])

        # Create DownloadException without calling __init__ → message is unset, args=()
        exc = DownloadException.__new__(DownloadException)
        with (
            patch.object(VideoContent, "get_path", side_effect=exc),
            patch.object(
                VideoContent,
                "get_cover_path",
                return_value=Path("/fake/cover.jpg"),
            ),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        reasons = [
            s for s in segs if isinstance(s, MockPlain) and "视频下载失败" in s.text
        ]
        assert len(reasons) == 1
        # Falls back to "未知错误"
        assert "未知错误" in reasons[0].text

    @pytest.mark.asyncio
    async def test_reason_before_summary(self, sender, result) -> None:
        """Failure reason appears before the text fallback summary."""
        cont = _make_video_content()
        plan = _make_basic_plan(heavy=[cont])
        with (
            patch.object(
                VideoContent,
                "get_path",
                side_effect=DownloadException("网络超时"),
            ),
            patch.object(
                VideoContent,
                "get_cover_path",
                return_value=Path("/fake/cover.jpg"),
            ),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        # Order: cover → reason → summary → tip
        assert isinstance(segs[0], MockImage)  # cover
        assert isinstance(segs[1], MockPlain)
        assert "视频下载失败" in segs[1].text  # reason
        assert segs[1].text.endswith("\n\n")
        assert isinstance(segs[2], MockPlain)
        assert "链接:" in segs[2].text  # summary

    @pytest.mark.asyncio
    async def test_show_download_fail_tip_false_still_shows_reason(
        self, result
    ) -> None:
        """show_download_fail_tip=False → reason still shown, but no tip."""
        sender = _make_sender(show_download_fail_tip=False)
        cont = _make_video_content()
        plan = _make_basic_plan(heavy=[cont])
        with (
            patch.object(
                VideoContent,
                "get_path",
                side_effect=DownloadException("权限不足"),
            ),
            patch.object(
                VideoContent,
                "get_cover_path",
                return_value=Path("/fake/cover.jpg"),
            ),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        # Reason still present
        assert any(isinstance(s, MockPlain) and "视频下载失败" in s.text for s in segs)
        # Tip absent
        assert not any(
            isinstance(s, MockPlain) and "此项媒体下载失败" in s.text for s in segs
        )

    @pytest.mark.asyncio
    async def test_sanitize_file_uri_in_reason(self, sender, result) -> None:
        """file:// URI in exception message is sanitized (replaced with [file])."""
        cont = _make_video_content()
        plan = _make_basic_plan(heavy=[cont])
        with (
            patch.object(
                VideoContent,
                "get_path",
                side_effect=DownloadException("下载失败 file:///tmp/video.mp4"),
            ),
            patch.object(
                VideoContent,
                "get_cover_path",
                return_value=Path("/fake/cover.jpg"),
            ),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        reasons = [
            s for s in segs if isinstance(s, MockPlain) and "视频下载失败" in s.text
        ]
        assert len(reasons) == 1
        assert "file://" not in reasons[0].text
        assert "[file]" in reasons[0].text

    @pytest.mark.asyncio
    async def test_sanitize_http_url_in_reason(self, sender, result) -> None:
        """HTTP download URL in exception message is sanitized."""
        cont = _make_video_content()
        plan = _make_basic_plan(heavy=[cont])
        with (
            patch.object(
                VideoContent,
                "get_path",
                side_effect=DownloadException(
                    "下载失败 https://example.com/video.m4s?token=secret"
                ),
            ),
            patch.object(
                VideoContent,
                "get_cover_path",
                return_value=Path("/fake/cover.jpg"),
            ),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        reasons = [
            s for s in segs if isinstance(s, MockPlain) and "视频下载失败" in s.text
        ]
        assert len(reasons) == 1
        assert "https://" not in reasons[0].text
        assert "token=secret" not in reasons[0].text
        assert "[url]" in reasons[0].text

    @pytest.mark.asyncio
    async def test_sanitize_secret_fields_in_reason(self, sender, result) -> None:
        """Credential-like fields in exception message are sanitized."""
        cont = _make_video_content()
        plan = _make_basic_plan(heavy=[cont])
        with (
            patch.object(
                VideoContent,
                "get_path",
                side_effect=DownloadException(
                    "鉴权失败 SESSDATA=abcdef bili_jct=123456"
                ),
            ),
            patch.object(
                VideoContent,
                "get_cover_path",
                return_value=Path("/fake/cover.jpg"),
            ),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        reasons = [
            s for s in segs if isinstance(s, MockPlain) and "视频下载失败" in s.text
        ]
        assert len(reasons) == 1
        assert "abcdef" not in reasons[0].text
        assert "123456" not in reasons[0].text
        assert "SESSDATA=[redacted]" in reasons[0].text
        assert "bili_jct=[redacted]" in reasons[0].text

    @pytest.mark.asyncio
    async def test_sanitize_token_variants_in_reason(self, sender, result) -> None:
        """access_token / refresh_token fields in exception message are sanitized."""
        cont = _make_video_content()
        plan = _make_basic_plan(heavy=[cont])
        with (
            patch.object(
                VideoContent,
                "get_path",
                side_effect=DownloadException(
                    "鉴权失败 access_token=abc123 refresh_token=xyz789"
                ),
            ),
            patch.object(
                VideoContent,
                "get_cover_path",
                return_value=Path("/fake/cover.jpg"),
            ),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        reasons = [
            s for s in segs if isinstance(s, MockPlain) and "视频下载失败" in s.text
        ]
        assert len(reasons) == 1
        assert "abc123" not in reasons[0].text
        assert "xyz789" not in reasons[0].text
        assert "access_token=[redacted]" in reasons[0].text
        assert "refresh_token=[redacted]" in reasons[0].text

    @pytest.mark.asyncio
    async def test_sanitize_truncate_long_reason(self, sender, result) -> None:
        """Very long exception message is truncated."""
        long_msg = "x" * 300
        cont = _make_video_content()
        plan = _make_basic_plan(heavy=[cont])
        with (
            patch.object(
                VideoContent, "get_path", side_effect=DownloadException(long_msg)
            ),
            patch.object(
                VideoContent,
                "get_cover_path",
                return_value=Path("/fake/cover.jpg"),
            ),
        ):
            segs = await self._run_build_segments(sender, result, plan)

        reasons = [
            s for s in segs if isinstance(s, MockPlain) and "视频下载失败" in s.text
        ]
        assert len(reasons) == 1
        # Prefix "视频下载失败：" (9 chars) + truncated 197 + "..." = ~209
        assert len(reasons[0].text) < 250
        assert reasons[0].text.rstrip().endswith("...")


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
        with (
            patch.object(
                VideoContent, "get_cover_path", return_value=Path("/real/cover.jpg")
            ),
            patch.object(Path, "exists", return_value=True),
        ):
            await sender._append_video_fail_cover(segs, cont)

        assert len(segs) == 1
        assert isinstance(segs[0], MockImage)
        assert "cover.jpg" in segs[0].file

    @pytest.mark.asyncio
    async def test_skips_when_cover_path_not_exist(self, sender) -> None:
        """Cover path returns a valid Path but file doesn't exist on disk → no Image appended."""
        cont = _make_video_content(cover_path=Path("/fake/missing.jpg"))
        segs: list = []
        with patch.object(
            VideoContent, "get_cover_path", return_value=Path("/fake/missing.jpg")
        ):
            await sender._append_video_fail_cover(segs, cont)

        assert segs == []

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


# ═══════════════════════════════════════════════════════════════════
#  4. _send_group fallback to plain text on send failure
# ═══════════════════════════════════════════════════════════════════


class TestSendGroupFallback:
    """_send_group retry: when event.send() fails, extract Plain from raw_segs and retry."""

    @pytest.fixture
    def sender(self) -> MessageSender:
        return _make_sender()

    @pytest.fixture
    def event(self) -> MagicMock:
        event = MagicMock()
        event.get_self_id.return_value = "test_bot"
        event.chain_result.side_effect = lambda x: x
        return event

    @pytest.fixture
    def group(self) -> SendGroup:
        return SendGroup(contents=[])

    async def _run_with_segs(
        self,
        sender: MessageSender,
        event: MagicMock,
        group: SendGroup,
        segs: list,
        merged_segs: list | None = None,
        plan: dict | None = None,
    ) -> bool:
        """Run _send_group with internals mocked."""
        if merged_segs is None:
            merged_segs = segs
        if plan is None:
            plan = _make_basic_plan()
        with (
            patch.object(sender, "_build_send_plan", return_value=plan),
            patch.object(sender, "_send_preview_card", return_value=None),
            patch.object(sender, "_build_segments", return_value=segs),
            patch.object(sender, "_merge_segments_if_needed", return_value=merged_segs),
        ):
            return await sender._send_group(event, _make_result(), group)

    # ── success path ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_send_success_no_fallback(self, sender, event, group) -> None:
        """When event.send succeeds, return True without fallback."""
        event.send = AsyncMock(return_value=None)
        segs = [MockPlain("hello")]
        result = await self._run_with_segs(sender, event, group, segs)
        assert result is True
        event.send.assert_awaited_once()

    # ── send fails → plain text retry succeeds ──────────────────

    @pytest.mark.asyncio
    async def test_send_with_image_fails_fallback_to_plain(
        self, sender, event, group
    ) -> None:
        """send(Image+Plain) fails → retry with Plain only → returns True."""
        event.send = AsyncMock(side_effect=[Exception("send failed"), None])
        segs = [MockImage("file:///img.jpg"), MockPlain("fallback text")]
        result = await self._run_with_segs(sender, event, group, segs)
        assert result is True
        assert event.send.await_count == 2
        second_args = event.send.await_args_list[1][0][0]
        assert len(second_args) == 1
        assert isinstance(second_args[0], MockPlain)
        assert second_args[0].text == "fallback text"

    # ── plain text retry also fails ─────────────────────────────

    @pytest.mark.asyncio
    async def test_send_fallback_plain_also_fails(self, sender, event, group) -> None:
        """Both send and plain text retry fail → return False."""
        event.send = AsyncMock(side_effect=Exception("always fail"))
        segs = [MockImage("file:///img.jpg"), MockPlain("fallback text")]
        result = await self._run_with_segs(sender, event, group, segs)
        assert result is False
        assert event.send.await_count == 2

    # ── no Plain in raw_segs → no retry ─────────────────────────

    @pytest.mark.asyncio
    async def test_no_plain_in_raw_segs_no_retry(self, sender, event, group) -> None:
        """No Plain segments → return False without attempting retry."""
        event.send = AsyncMock(side_effect=Exception("send failed"))
        segs = [MockImage("file:///img.jpg")]
        result = await self._run_with_segs(sender, event, group, segs)
        assert result is False
        event.send.assert_awaited_once()  # only the first attempt

    # ── Plain extracted from raw_segs (pre-merge), not merged ───

    @pytest.mark.asyncio
    async def test_plain_from_raw_segs_not_merged(self, sender, event, group) -> None:
        """Plain is extracted from raw_segs (pre-merge), not from merged Nodes."""
        event.send = AsyncMock(side_effect=[Exception("send failed"), None])
        raw = [MockImage("file:///img.jpg"), MockPlain("important text")]
        # Merged wraps everything in Nodes — no top-level Plain
        merged = [MockNodes([MockNode("bot", "parser", [s]) for s in raw])]
        result = await self._run_with_segs(sender, event, group, raw, merged)
        assert result is True
        second_args = event.send.await_args_list[1][0][0]
        assert len(second_args) == 1
        assert isinstance(second_args[0], MockPlain)
        assert second_args[0].text == "important text"

    # ── empty/whitespace-only Plain excluded ────────────────────

    @pytest.mark.asyncio
    async def test_empty_plain_excluded_from_fallback(
        self, sender, event, group
    ) -> None:
        """Whitespace-only Plain is excluded; non-empty Plain is used."""
        event.send = AsyncMock(side_effect=[Exception("send failed"), None])
        segs = [MockImage("file:///img.jpg"), MockPlain("   "), MockPlain("valid")]
        result = await self._run_with_segs(sender, event, group, segs)
        assert result is True
        second_args = event.send.await_args_list[1][0][0]
        assert len(second_args) == 1
        assert second_args[0].text == "valid"

    @pytest.mark.asyncio
    async def test_all_plain_empty_no_retry(self, sender, event, group) -> None:
        """Only whitespace-only Plain segments → no retry, return False."""
        event.send = AsyncMock(side_effect=Exception("send failed"))
        segs = [MockImage("file:///img.jpg"), MockPlain("")]
        result = await self._run_with_segs(sender, event, group, segs)
        assert result is False
        event.send.assert_awaited_once()
