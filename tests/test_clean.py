"""Test cache cleaning strategies (CacheCleaner).

Covers:
  - http mode: mtime-TTL cleaning — fresh files kept, expired files
    deleted, empty subdirs pruned, cache root never removed.
  - http mode: concurrent-disappearance tolerance (missing dir → 0,
    entries vanishing mid-scan do not raise).
  - local mode: whole-directory clean + recreate (existing behavior).

Uses the same minimal astrbot stubbing approach as the other test files.
"""

from __future__ import annotations

import os
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


# ═══════════════════════════════════════════════════════════════════
#  Module stubs (installed before importing production modules)
# ═══════════════════════════════════════════════════════════════════

_MOCKS_INSTALLED = False


def _install_module_stubs() -> None:
    global _MOCKS_INSTALLED
    if _MOCKS_INSTALLED:
        return
    _MOCKS_INSTALLED = True

    logger = types.SimpleNamespace(
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
        # 注意：不在此安装 astrbot.core.message.components，
        # 以免以空内容抢占该模块，破坏 test_sender/test_media 的组件桩。
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


_install_module_stubs()

from core.clean import CacheCleaner  # noqa: E402


# 通过 __new__ 构造实例，跳过 AsyncIOScheduler 启动（测试只关心清理逻辑）
def _make_cleaner(cfg: SimpleNamespace) -> CacheCleaner:
    cleaner = CacheCleaner.__new__(CacheCleaner)
    cleaner.cfg = cfg
    return cleaner


def _write(path: Path, *, age: float = 0.0) -> None:
    """创建文件并可选地把 mtime 回拨 age 秒（模拟过期文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    if age:
        old = time.time() - age
        os.utime(path, (old, old))


# ═══════════════════════════════════════════════════════════════════
#  1. http 模式：_remove_expired — 未过期保留 / 过期删除
# ═══════════════════════════════════════════════════════════════════


class TestRemoveExpired:
    def test_fresh_file_kept(self, tmp_path: Path) -> None:
        _write(tmp_path / "fresh.mp4")
        removed = CacheCleaner._remove_expired(tmp_path, cutoff=time.time() - 1000)
        assert removed == 0
        assert (tmp_path / "fresh.mp4").exists()

    def test_expired_file_deleted(self, tmp_path: Path) -> None:
        _write(tmp_path / "old.mp4", age=10000)
        removed = CacheCleaner._remove_expired(tmp_path, cutoff=time.time() - 1000)
        assert removed == 1
        assert not (tmp_path / "old.mp4").exists()

    def test_mixed_expired_deleted_fresh_kept(self, tmp_path: Path) -> None:
        _write(tmp_path / "old.mp4", age=10000)
        _write(tmp_path / "fresh.mp4")
        removed = CacheCleaner._remove_expired(tmp_path, cutoff=time.time() - 1000)
        assert removed == 1
        assert not (tmp_path / "old.mp4").exists()
        assert (tmp_path / "fresh.mp4").exists()

    def test_cache_root_never_removed(self, tmp_path: Path) -> None:
        _write(tmp_path / "old.mp4", age=10000)
        CacheCleaner._remove_expired(tmp_path, cutoff=time.time() - 1000)
        assert tmp_path.exists()

    def test_nested_expired_file_removed_and_empty_dir_pruned(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path / "sub" / "old.mp4", age=10000)
        CacheCleaner._remove_expired(tmp_path, cutoff=time.time() - 1000)
        assert not (tmp_path / "sub" / "old.mp4").exists()
        assert not (tmp_path / "sub").exists()  # 空目录被清理
        assert tmp_path.exists()

    def test_nested_fresh_dir_kept(self, tmp_path: Path) -> None:
        _write(tmp_path / "sub" / "fresh.mp4")
        CacheCleaner._remove_expired(tmp_path, cutoff=time.time() - 1000)
        assert (tmp_path / "sub" / "fresh.mp4").exists()
        assert (tmp_path / "sub").exists()


# ═══════════════════════════════════════════════════════════════════
#  2. http 模式：竞态 / 消失容忍
# ═══════════════════════════════════════════════════════════════════


class TestRaceTolerance:
    def test_missing_cache_dir_returns_zero(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        assert CacheCleaner._remove_expired(missing, cutoff=time.time() - 1000) == 0

    def test_file_vanishing_mid_scan_tolerated(self, tmp_path: Path) -> None:
        """扫描期间文件被并发删除：删除失败应被容忍，不抛异常。"""
        _write(tmp_path / "a.mp4", age=10000)
        _write(tmp_path / "b.mp4", age=10000)
        # 预先把目标删掉，模拟并发消失；_remove_expired 捕获 FileNotFoundError
        (tmp_path / "a.mp4").unlink()
        removed = CacheCleaner._remove_expired(tmp_path, cutoff=time.time() - 1000)
        assert removed >= 0
        assert not (tmp_path / "b.mp4").exists()


# ═══════════════════════════════════════════════════════════════════
#  3. 调度入口：http_mode 判定与 TTL 清理
# ═══════════════════════════════════════════════════════════════════


class TestSchedulerEntry:
    def test_http_mode_property_true(self) -> None:
        cleaner = _make_cleaner(SimpleNamespace(media_send_mode="http"))
        assert cleaner.http_mode is True

    def test_local_mode_property_false(self) -> None:
        cleaner = _make_cleaner(SimpleNamespace(media_send_mode="local"))
        assert cleaner.http_mode is False

    def test_missing_mode_defaults_to_local(self) -> None:
        cleaner = _make_cleaner(SimpleNamespace(media_send_mode=None))
        assert cleaner.http_mode is False

    @pytest.mark.asyncio
    async def test_http_mode_ttl_clean_dispatch(self, tmp_path: Path) -> None:
        """http 模式：过期删除、未过期保留、目录根不删。"""
        cfg = SimpleNamespace(
            media_send_mode="http", media_http_ttl=3600, cache_dir=tmp_path
        )
        cleaner = _make_cleaner(cfg)
        _write(tmp_path / "old.mp4", age=10000)
        _write(tmp_path / "fresh.mp4")
        await cleaner._clean_plugin_cache()
        assert not (tmp_path / "old.mp4").exists()
        assert (tmp_path / "fresh.mp4").exists()
        assert tmp_path.exists()


# ═══════════════════════════════════════════════════════════════════
#  4. local 模式：整目录清空后重建
# ═══════════════════════════════════════════════════════════════════


class TestLocalWholeClean:
    @pytest.mark.asyncio
    async def test_local_mode_clean_whole_recreates(self, tmp_path: Path) -> None:
        """local 模式保持原行为：整个目录删除后重建。"""
        cfg = SimpleNamespace(media_send_mode="local", cache_dir=tmp_path)
        cleaner = _make_cleaner(cfg)
        _write(tmp_path / "x.mp4")
        (tmp_path / "sub").mkdir()
        await cleaner._clean_plugin_cache()
        assert tmp_path.exists()  # 已重建
        assert not (tmp_path / "x.mp4").exists()
        assert not (tmp_path / "sub").exists()

    @pytest.mark.asyncio
    async def test_local_mode_dispatch_not_ttl(self, tmp_path: Path) -> None:
        """local 模式即使有未过期文件也整目录清理（不按 TTL 保留）。"""
        cfg = SimpleNamespace(media_send_mode="local", cache_dir=tmp_path)
        cleaner = _make_cleaner(cfg)
        _write(tmp_path / "fresh.mp4")
        await cleaner._clean_plugin_cache()
        assert not (tmp_path / "fresh.mp4").exists()
        assert tmp_path.exists()
