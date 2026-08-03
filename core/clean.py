import asyncio
import os
import shutil
import time
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import logger

from .config import MODE_HTTP, MODE_LOCAL, PluginConfig


class CacheCleaner:
    """
    每天固定时间自动清理插件缓存目录的调度器封装。

    - local 模式：保持原有行为 —— 整目录删除后重建。
    - http 模式：按 mtime 的 TTL 清理 —— 只删除超过 media_http_ttl
      秒的过期文件，保留未过期文件；避免把仍在对外提供服务的
      HTTP 媒体提前删除。清理容忍文件并发消失（竞态）。
    """

    JOBNAME = "CacheCleaner"

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.scheduler = AsyncIOScheduler(timezone=self.cfg.timezone)
        self.scheduler.start()

        self.register_task()

        logger.info(f"{self.JOBNAME} 已启动，任务周期：{self.cfg.clean_cron}")

    def register_task(self):
        try:
            self.trigger = CronTrigger.from_crontab(self.cfg.clean_cron)
            self.scheduler.add_job(
                func=self._clean_plugin_cache,
                trigger=self.trigger,
                name=f"{self.JOBNAME}_scheduler",
                max_instances=1,
            )
        except Exception as e:
            logger.error(f"[{self.JOBNAME}] Cron 格式错误：{e}")

    @property
    def http_mode(self) -> bool:
        """是否处于 http 媒体发送模式（决定清理策略）。"""
        return (self.cfg.media_send_mode or MODE_LOCAL).strip().lower() == MODE_HTTP

    async def _clean_plugin_cache(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            if self.http_mode:
                await self._clean_by_ttl(loop)
            else:
                await self._clean_whole(loop)
        except Exception:
            logger.exception("Error while cleaning cache directory.")

    async def _clean_whole(self, loop: asyncio.AbstractEventLoop) -> None:
        """local 模式：整目录删除并重建（保留原有行为）。"""
        await loop.run_in_executor(None, shutil.rmtree, self.cfg.cache_dir)
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Cache directory cleaned and recreated.")

    async def _clean_by_ttl(self, loop: asyncio.AbstractEventLoop) -> None:
        """http 模式：按 mtime TTL 清理过期文件，保留未过期文件。"""
        ttl = int(self.cfg.media_http_ttl or 0)
        if ttl <= 0:
            logger.error(
                f"[{self.JOBNAME}] http 模式需要正整数 media_http_ttl，跳过本次清理"
            )
            return
        cutoff = time.time() - ttl
        removed = await loop.run_in_executor(
            None, self._remove_expired, self.cfg.cache_dir, cutoff
        )
        logger.info(f"[{self.JOBNAME}] TTL 清理完成，删除 {removed} 项")

    @staticmethod
    def _remove_expired(cache_dir: Path, cutoff: float) -> int:
        """删除 cache_dir 中 mtime 早于 cutoff 的文件（含子目录内文件）。

        - 递归遍历，过期文件/目录删除，未过期保留；
        - 清理空的子目录，但绝不删除 cache 根目录本身；
        - 容忍文件在扫描过程中并发消失（FileNotFoundError / OSError）。
        """
        removed = 0
        try:
            entries = list(os.scandir(cache_dir))
        except FileNotFoundError:
            return 0

        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                stat = entry.stat(follow_symlinks=False)
            except (FileNotFoundError, OSError):
                continue  # 并发消失，容忍

            if is_dir:
                removed += CacheCleaner._remove_expired(Path(entry.path), cutoff)
                # 子目录已空则删除（仅限子目录，不触碰 cache 根）
                try:
                    if not any(os.scandir(entry.path)):
                        os.rmdir(entry.path)
                except (FileNotFoundError, OSError):
                    pass
                continue

            if stat.st_mtime >= cutoff:
                continue  # 未过期，保留
            try:
                os.unlink(entry.path)
                removed += 1
            except (FileNotFoundError, OSError):
                pass  # 并发消失，容忍

        return removed

    async def stop(self):
        self.scheduler.remove_all_jobs()
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.info(f"[{self.JOBNAME}] 已停止")
