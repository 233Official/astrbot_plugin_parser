"""媒体 URI 解析器：把本地媒体文件路径解析为发送 URI。

- local 模式：返回现有 ``file://`` URI，默认行为不变。
- http 模式：仅允许 ``resolve()`` 后位于 ``cfg.cache_dir.resolve()``
  内的文件，返回 ``base URL + 相对 cache 路径`` 的 HTTP(S) URL；
  每个路径 segment 均做 percent-encode，并保留 ``/``。

安全约束：
- 拒绝目录遍历 / cache 目录外的路径。
- base URL 仅允许 http/https 且必须包含 netloc。
- 日志与异常信息不得包含 query / fragment 等敏感内容。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

from astrbot.api import logger

from .config import PluginConfig


class MediaUriError(Exception):
    """媒体 URI 解析失败（配置缺失 / 非法、路径越界等）。"""


class MediaUriResolver:
    """媒体 URI 解析器。"""

    MODE_LOCAL = "local"
    MODE_HTTP = "http"

    def __init__(self, cfg: PluginConfig):
        self.cfg = cfg

    @property
    def mode(self) -> str:
        return (self.cfg.media_send_mode or self.MODE_LOCAL).strip().lower() or (
            self.MODE_LOCAL
        )

    def resolve(self, path: Path) -> str:
        """把本地媒体路径解析为发送 URI。

        Raises:
            MediaUriError: 模式非法、base URL 缺失/非法或路径越界。
        """
        mode = self.mode
        if mode == self.MODE_LOCAL:
            return self._to_file_uri(path)
        if mode == self.MODE_HTTP:
            return self._to_http_url(path)
        raise MediaUriError(f"未知的 media_send_mode: {self.cfg.media_send_mode!r}")

    def _to_file_uri(self, path: Path) -> str:
        if not path.is_absolute():
            path = path.resolve()
        return path.as_uri()

    def _to_http_url(self, path: Path) -> str:
        base = self.cfg.media_http_base_url
        if not base or not base.strip():
            raise MediaUriError(
                "media_send_mode=http 但未配置 media_http_base_url，无法生成媒体 URL"
            )
        safe_base = self._validate_base_url(base.strip())

        resolved = path.resolve() if not path.is_absolute() else path.resolve()
        cache = self.cfg.cache_dir.resolve()
        try:
            rel = resolved.relative_to(cache)
        except ValueError:
            raise MediaUriError(
                f"HTTP 模式只允许 cache_dir 内的文件，拒绝越界路径: {resolved}"
            ) from None

        # 每个 segment 独立 percent-encode（segment 内不会再有 "/"），
        # 再以 "/" 连接，保证空 格/中文 等字符被正确编码且路径层级保留。
        encoded = "/".join(quote(part) for part in rel.parts)
        return f"{safe_base.rstrip('/')}/{encoded}"

    @staticmethod
    def _validate_base_url(base: str) -> str:
        parsed = urlparse(base)
        if parsed.scheme not in ("http", "https"):
            raise MediaUriError(
                f"media_http_base_url 仅允许 http/https，实际 scheme={parsed.scheme!r}"
            )
        if not parsed.netloc:
            raise MediaUriError("media_http_base_url 缺少主机名（netloc）")
        # 丢弃 query / fragment，避免敏感内容出现在日志与 URL 中
        safe = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
        logger.debug(f"[media] http 模式 base URL 已校验: {safe}")
        return safe
