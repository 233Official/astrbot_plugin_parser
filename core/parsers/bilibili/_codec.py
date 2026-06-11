"""Bilibili 解析器辅助函数 —— 独立可导入，不含外部依赖。"""

from __future__ import annotations

import re as _re
from typing import List, Union


_HVC1_RE = _re.compile(r"\bhvc1(?=\.)")


def _normalize_dash_codecs(download_url_data: dict) -> dict:
    """将 dash video codecs 中的 hvc1.* 原地替换为 hev1.*，使
    bilibili-api-python 的 VideoDownloadURLDataDetecter 能识别为 HEVC。

    保留原始信息中的 profile/tier/level 部分不变。
    """
    data = download_url_data.get("video_info", download_url_data)
    dash = data.get("dash")
    if not dash:
        return download_url_data
    for video_entry in dash.get("video", []):
        codecs = video_entry.get("codecs", "")
        if "hvc1" in codecs:
            video_entry["codecs"] = _HVC1_RE.sub("hev1", codecs)
    return download_url_data


def _resolve_codec_config(raw_data: dict) -> List[str]:
    """解析 codec 配置，支持新字段 video_codec_list 和旧字段 video_codecs。

    语义：
    - video_codec_list（列表）优先
    - 回退到 video_codecs（字符串或列表）
    - 最终默认 ["AVC"]
    """
    codec_config: Union[str, List[str]] = (
        raw_data.get("video_codec_list") or raw_data.get("video_codecs") or ["AVC"]
    )
    if isinstance(codec_config, str):
        codec_config = [codec_config]
    return codec_config
