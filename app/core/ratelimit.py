"""频率与安全控制：拟人延迟、每日上限、营业时段。"""
from __future__ import annotations

import asyncio
import datetime as dt
import random
import time

from loguru import logger

from app import config
from app.db import database as db


def in_business_hours() -> bool:
    start, end = config.get("safety.business_hours", [0, 24])
    return start <= dt.datetime.now().hour < end


def _today_count(source_filter: str) -> int:
    start = time.mktime(dt.date.today().timetuple())
    rows = db.query(
        f"SELECT COUNT(*) c FROM reply_logs WHERE sent=1 AND created_at>=? {source_filter}",
        (start,),
    )
    return rows[0]["c"] if rows else 0


def can_reply() -> tuple[bool, str]:
    if not in_business_hours():
        return False, "非营业时段"
    limit = config.get("safety.daily_reply_limit", 500)
    if _today_count("") >= limit:
        return False, "达到每日回复上限"
    return True, ""


def compute_reply_delay(message: str = "", reply: str = "") -> float:
    """计算回复延迟（秒）：基础思考时间 + 随机扰动 + 轻微长度影响。

    比"按字数"更像真人：主要由 base + jitter 决定，消息/回复长度只是轻微加成。
    所有参数走 config 的 reply_delay.* 可调（见 config.yml）。
    """
    def g(key: str, default: float) -> float:
        return float(config.get(f"reply_delay.{key}", default))

    base = random.uniform(g("base_min", 4.5), g("base_max", 9.5))
    length_factor = min(len(message or "") / g("msg_len_div", 90.0), g("msg_len_cap", 2.0))
    reply_factor = min(len(reply or "") / g("reply_len_div", 130.0), g("reply_len_cap", 1.8))
    jitter = random.uniform(g("jitter_min", -2.8), g("jitter_max", 3.2))

    delay = base + length_factor + reply_factor + jitter
    delay = round(max(g("min_delay", 2.5), min(delay, g("max_delay", 16.0))), 1)
    logger.debug(f"[Delay] msg_len={len(message or '')} reply_len={len(reply or '')} "
                 f"base={base:.1f} jitter={jitter:.1f} -> {delay}s")
    return delay


async def human_delay(message: str = "", reply: str = "") -> None:
    delay = compute_reply_delay(message, reply)
    logger.info(f"[Reply] 准备发送，延迟 {delay}s")
    await asyncio.sleep(delay)
