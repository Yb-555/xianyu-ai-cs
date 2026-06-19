"""自动评价：订单完成后延迟一段时间，自动给买家好评。

⚠️ 这是 sensitive 操作，易触发滑块。默认关闭（safety.auto_rate_enabled）。
   评价的真实入口（评价页 URL / 按钮选择器 / 是否走 mtop）需调研后填写。
"""
from __future__ import annotations

import asyncio

from loguru import logger
from playwright.async_api import Page

from app import config

# ---- 调研后填写 ----
SEL_RATE_BTN = "[class*=rate]"          # TODO: 评价按钮
SEL_GOOD_RATE = "[class*=good]"         # TODO: 好评选项
SEL_RATE_TEXT = "textarea"               # TODO: 评价内容输入
SEL_RATE_SUBMIT = "[class*=submit]"     # TODO: 提交按钮
DEFAULT_RATE_TEXT = "好买家，交易愉快，欢迎下次光临～"
# --------------------


async def rate_order(page: Page, order_url: str, text: str = DEFAULT_RATE_TEXT) -> bool:
    if not config.get("safety.auto_rate_enabled", False):
        logger.info("自动评价未启用，跳过")
        return False
    delay = config.get("safety.auto_rate_delay_sec", 1800)
    logger.info(f"将在 {delay}s 后评价订单: {order_url}")
    await asyncio.sleep(delay)
    try:
        await page.goto(order_url)
        await page.locator(SEL_RATE_BTN).first.click()
        await page.locator(SEL_GOOD_RATE).first.click()
        await page.locator(SEL_RATE_TEXT).first.fill(text)
        await page.locator(SEL_RATE_SUBMIT).first.click()
        logger.info("评价已提交")
        return True
    except Exception as e:
        logger.error(f"评价失败（可能出现滑块，已停止）: {e}")
        return False
