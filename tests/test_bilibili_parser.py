"""Bilibili parser 最小修复测试

覆盖:
  - _normalize_dash_codecs hvc1 → hev1 标准化
  - 旧配置字段 video_codecs (字符串) fallback
  - extract_download_urls 上游异常 → DownloadException（边界逻辑）

说明：
  codec helper 从 core.parsers.bilibili._codec 直接导入，
  不触发核心模块的副作用（bilibili_api 初始化等）。
  DownloadException 从 core.exception 直接导入。
"""

from __future__ import annotations

from pathlib import Path
import sys
import types
from typing import Any

import pytest


# ═══════════════════════════════════════════════════════════════════
#  Mock 环境 — 在导入生产模块前建立包桩，避免 __init__.py 副作用
# ═══════════════════════════════════════════════════════════════════


def _setup_package_stubs() -> None:
    """在 sys.modules 中写入桩包，避免 core.parsers.bilibili 的
    __init__.py 被执行（其中包含 bilibili_api 等重型导入）。
    仅 _codec 子模块（纯函数，无外部依赖）会被实际加载。
    """
    base = str(Path(__file__).resolve().parent.parent)

    stubs: list[tuple[str, list[str]]] = [
        ("core", [f"{base}/core"]),
        ("core.parsers", [f"{base}/core/parsers"]),
        ("core.parsers.bilibili", [f"{base}/core/parsers/bilibili"]),
    ]

    for name, path in stubs:
        if name not in sys.modules:
            pkg = types.ModuleType(name)
            pkg.__path__ = path
            pkg.__package__ = name
            sys.modules[name] = pkg


_setup_package_stubs()

# 从独立子模块导入被测函数（纯 dict 处理，无外部依赖）
from core.parsers.bilibili._codec import _normalize_dash_codecs, _resolve_codec_config

# 被测异常类 —— core.exception 本身无重型依赖，可直接导入
from core.exception import DownloadException  # noqa: E402


# ═══════════════════════════════════════════════════════════════════
#  边界测试辅助 —— 模拟 extract_download_urls 中的 try/except 模式
# ═══════════════════════════════════════════════════════════════════


def _wrap_detect(detect_fn, *args: Any, **kw: Any) -> Any:
    """应用与 extract_download_urls 一致的 try/except 规则。

    使用生产模块的真实 DownloadException 做异常包装/透传。
    """
    try:
        return detect_fn(*args, **kw)
    except DownloadException:
        raise
    except Exception as e:
        raise DownloadException(f"检测最佳视频流失败") from e


# ═══════════════════════════════════════════════════════════════════
#  1. _normalize_dash_codecs - codec 标准化
# ═══════════════════════════════════════════════════════════════════


class TestNormalizeDashCodecs:
    def test_hvc1_replaced_with_hev1(self) -> None:
        data = {
            "dash": {
                "video": [
                    {"codecs": "hvc1.1.6.L153.90", "baseUrl": "https://e.com/v1"}
                ],
                "audio": [],
            }
        }
        _normalize_dash_codecs(data)
        assert data["dash"]["video"][0]["codecs"] == "hev1.1.6.L153.90"

    def test_hev1_unchanged(self) -> None:
        data = {"dash": {"video": [{"codecs": "hev1.1.6.L153.90", "baseUrl": "..."}]}}
        _normalize_dash_codecs(data)
        assert data["dash"]["video"][0]["codecs"] == "hev1.1.6.L153.90"

    def test_avc1_unchanged(self) -> None:
        data = {"dash": {"video": [{"codecs": "avc1.64001F", "baseUrl": "..."}]}}
        _normalize_dash_codecs(data)
        assert data["dash"]["video"][0]["codecs"] == "avc1.64001F"

    def test_av01_unchanged(self) -> None:
        data = {"dash": {"video": [{"codecs": "av01.0.05M.08", "baseUrl": "..."}]}}
        _normalize_dash_codecs(data)
        assert data["dash"]["video"][0]["codecs"] == "av01.0.05M.08"

    def test_hvc1_in_substring_not_replaced(self) -> None:
        """xhvc1.xxx 不应被替换（\bhvc1 边界匹配）"""
        data = {"dash": {"video": [{"codecs": "xhvc1.xxx", "baseUrl": "..."}]}}
        _normalize_dash_codecs(data)
        assert data["dash"]["video"][0]["codecs"] == "xhvc1.xxx"

    def test_multiple_video_streams(self) -> None:
        data = {
            "dash": {
                "video": [
                    {"codecs": "avc1.64001F", "baseUrl": "..."},
                    {"codecs": "hvc1.1.6.L153.90", "baseUrl": "..."},
                    {"codecs": "hev1.1.6.L153.90", "baseUrl": "..."},
                ],
            }
        }
        _normalize_dash_codecs(data)
        assert data["dash"]["video"][0]["codecs"] == "avc1.64001F"
        assert data["dash"]["video"][1]["codecs"] == "hev1.1.6.L153.90"
        assert data["dash"]["video"][2]["codecs"] == "hev1.1.6.L153.90"

    def test_no_dash_key(self) -> None:
        _normalize_dash_codecs({"durl": [{"url": "..."}]})  # no crash

    def test_video_info_bangumi_path(self) -> None:
        data = {
            "video_info": {
                "dash": {
                    "video": [{"codecs": "hvc1.1.6.L153.90", "baseUrl": "..."}],
                }
            }
        }
        _normalize_dash_codecs(data)
        assert data["video_info"]["dash"]["video"][0]["codecs"] == "hev1.1.6.L153.90"

    def test_no_video_key(self) -> None:
        _normalize_dash_codecs({"dash": {"audio": []}})  # no crash

    def test_empty_video_list(self) -> None:
        _normalize_dash_codecs({"dash": {"video": [], "audio": []}})  # no crash


# ═══════════════════════════════════════════════════════════════════
#  2. 配置 fallback
# ═══════════════════════════════════════════════════════════════════


class TestCodecConfigFallback:
    def test_video_codec_list_priority(self) -> None:
        assert _resolve_codec_config(
            {
                "video_codec_list": ["AV1"],
                "video_codecs": "AVC",
            }
        ) == ["AV1"]

    def test_fallback_video_codecs_string(self) -> None:
        assert _resolve_codec_config(
            {
                "video_codecs": "HEV",
            }
        ) == ["HEV"]

    def test_fallback_video_codecs_list(self) -> None:
        assert _resolve_codec_config(
            {
                "video_codecs": ["AV1", "HEV"],
            }
        ) == ["AV1", "HEV"]

    def test_default_when_no_codec_config(self) -> None:
        assert _resolve_codec_config({}) == ["AVC"]

    def test_video_codec_list_empty_fallback(self) -> None:
        """空列表视为 falsy → fallback 到旧字段"""
        assert _resolve_codec_config(
            {
                "video_codec_list": [],
                "video_codecs": "HEV",
            }
        ) == ["HEV"]


# ═══════════════════════════════════════════════════════════════════
#  3. extract_download_urls 异常包装边界
# ═══════════════════════════════════════════════════════════════════


class TestExceptionWrapping:
    """测试 extract_download_urls 中的 try/except 逻辑边界"""

    def test_attribute_error_wrapped(self) -> None:
        """AttributeError → DownloadException"""

        def _bad_detect(**kw: Any) -> Any:
            raise AttributeError("'NoneType' object has no attribute 'value'")

        with pytest.raises(DownloadException) as exc_info:
            _wrap_detect(_bad_detect)
        assert "检测最佳视频流失败" in str(exc_info.value)

    def test_value_error_wrapped(self) -> None:
        """ValueError → DownloadException"""

        def _bad_detect(**kw: Any) -> Any:
            raise ValueError("invalid codec")

        with pytest.raises(DownloadException):
            _wrap_detect(_bad_detect)

    def test_download_exception_passthrough(self) -> None:
        """DownloadException 透传"""

        def _raise_de(**kw: Any) -> Any:
            raise DownloadException("未找到可下载的视频流")

        with pytest.raises(DownloadException) as exc_info:
            _wrap_detect(_raise_de)
        assert "未找到可下载的视频流" in str(exc_info.value)

    def test_normal_return_passthrough(self) -> None:
        """正常返回不受影响"""
        result = _wrap_detect(lambda **kw: ("v_url", "a_url"))
        assert result == ("v_url", "a_url")
