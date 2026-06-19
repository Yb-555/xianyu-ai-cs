"""浏览器接管。

两种模式：
  cdp        —— 接管你已打开的 Chrome（推荐，复用现成登录态）
               启动 Chrome 时加： --remote-debugging-port=9222
  persistent —— Playwright 用独立 user_data_dir 自己开（首次需手动登录一次）
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from loguru import logger
from playwright.async_api import (Browser, BrowserContext, Page,
                                   TimeoutError as PlaywrightTimeoutError,
                                   async_playwright)

from app import config

# 登录页检测选择器列表（按优先级排列）
_LOGIN_SELECTORS = [
    'button:has-text("快速进入")',
    'div:has-text("快速进入")',
    'button:has-text("扫码登录")',
    'div:has-text("手机扫码安全登录")',
    'img[alt*="扫码"]',
    'img[src*="qrcode"]',
]

# 扫码登录页特征选择器（无按钮可点，需用户手机扫码）
_QR_LOGIN_SELECTORS = [
    'div:has-text("手机扫码安全登录")',
    'img[alt*="扫码"]',
    'img[src*="qrcode"]',
    'div:has-text("请使用手机闲鱼扫码")',
    'div:has-text("打开手机闲鱼扫一扫")',
    'canvas[class*="qrcode"]',
]


class BrowserManager:
    def __init__(self) -> None:
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None

    @property
    def context(self) -> BrowserContext | None:
        """已登录的浏览器上下文，供商品采集等功能开新标签复用。"""
        return self._context

    async def start(self) -> Page:
        self._pw = await async_playwright().start()
        mode = config.get("browser.mode", "cdp")
        if mode == "cdp":
            await self._connect_cdp()
        else:
            await self._launch_persistent()
        await self._ensure_workbench_page()
        return self.page  # type: ignore[return-value]

    async def _connect_cdp(self) -> None:
        url = config.get("browser.cdp_url", "http://127.0.0.1:9222")
        logger.info(f"CDP 接管已有 Chrome: {url}")
        self._browser = await self._pw.chromium.connect_over_cdp(url)
        self._context = self._browser.contexts[0] if self._browser.contexts \
            else await self._browser.new_context()

    async def _launch_persistent(self) -> None:
        user_dir = config.get("browser.user_data_dir", "./browser_data")
        headless = config.get("browser.headless", False)
        logger.info(f"Playwright 持久化启动: {user_dir}")
        self._context = await self._pw.chromium.launch_persistent_context(
            user_dir, headless=headless,
        )

    async def _ensure_workbench_page(self) -> None:
        target = config.get("browser.workbench_url")
        # 复用已打开的工作台标签页
        for p in self._context.pages:  # type: ignore[union-attr]
            if "seller.goofish.com" in p.url:
                self.page = p
                logger.info(f"复用已打开的工作台页: {p.url}")
                return
        self.page = await self._context.new_page()  # type: ignore[union-attr]
        await self.page.goto(target)
        logger.info(f"已打开工作台: {target}")

    async def close(self) -> None:
        # CDP 模式不关闭用户的浏览器，只断开
        if self._pw:
            await self._pw.stop()


async def ensure_logged_in(page: Page, timeout: int = 15000) -> bool:
    """确保当前已登录闲鱼卖家工作台。

    检测逻辑：
    1. 等待页面加载完成
    2. 如果页面包含登录相关元素（快速进入、扫码登录等），自动点击「快速进入」
    3. 点击后等待跳转到工作台 URL
    4. 失败时自动截图保存到 logs/ 目录

    Args:
        page: Playwright Page 对象
        timeout: 每次等待的超时时间（毫秒），默认 15000

    Returns:
        True 表示已进入工作台，False 表示自动登录失败
    """
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except PlaywrightTimeoutError:
        logger.warning("[Browser] 页面加载超时，尝试继续处理…")

    current_url = page.url

    # 第一步：检测登录页元素（优先于 URL 判断，因为工作台 URL 也可能展示登录覆盖层）
    login_detected = False
    qr_only = True  # 是否只有扫码登录（没有可点击的按钮）
    for selector in _LOGIN_SELECTORS:
        try:
            el = page.locator(selector).first
            if await el.count() > 0 and await el.is_visible(timeout=3000):
                login_detected = True
                logger.warning(f"[Browser] 检测到登录页（匹配选择器: {selector}，URL: {current_url}）")
                # 如果匹配到的是可点击按钮（快速进入/扫码登录），不是纯扫码页
                if "快速进入" in selector or "扫码登录" in selector:
                    qr_only = False
                break
        except Exception:
            continue

    if login_detected:
        if qr_only:
            # 纯扫码登录页，没有可点击的按钮 → 等待用户扫码
            return await _wait_for_qr_login(page, timeout=60000)
        return await _handle_login_page(page, timeout)

    # 第二步：检查是否已在工作台 IM 页面
    if "seller.goofish.com" in current_url and "/im" in current_url:
        logger.debug(f"[Browser] 当前已在工作台 IM 页面: {current_url}")
        return True

    # 第三步：不在登录页也不在工作台，可能加载中或跳转中，再等一等
    logger.debug(f"[Browser] 未检测到登录页，当前 URL: {current_url}")
    try:
        await page.wait_for_url("**/seller.goofish.com/**", timeout=timeout)
        logger.info(f"[Browser] 已进入工作台: {page.url}")
        return True
    except PlaywrightTimeoutError:
        logger.warning(f"[Browser] 等待工作台超时，当前 URL: {page.url}")
        return False


async def _wait_for_qr_login(page: Page, timeout: int = 60000) -> bool:
    """等待用户手机扫码登录。

    扫码登录页没有可点击的按钮，必须用户用手机闲鱼扫码才能登录。
    该方法会等待页面跳转到工作台，超时后截图记录。
    """
    logger.warning(
        "[Browser] 检测到扫码登录页，请用手机闲鱼扫码登录… "
        "（等待最长 {} 秒）".format(timeout // 1000)
    )

    # 先截一张图方便用户看到二维码位置
    await _capture_failure_screenshot(page, "qr_login_ready")

    poll_interval = 2  # 每 2 秒检查一次
    elapsed = 0
    while elapsed < timeout:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        current_url = page.url
        # 检查是否已跳转到工作台
        if "seller.goofish.com" in current_url and "/im" in current_url:
            logger.info(f"[Browser] 扫码登录成功，已进入工作台: {current_url}")
            return True

        # 检查登录页元素是否已消失（页面变了）
        still_login = False
        for selector in _QR_LOGIN_SELECTORS:
            try:
                el = page.locator(selector).first
                if await el.count() > 0 and await el.is_visible(timeout=1000):
                    still_login = True
                    break
            except Exception:
                continue

        if not still_login and "seller.goofish.com" in current_url:
            logger.info(f"[Browser] 登录页已消失，当前 URL: {current_url}")
            return True

        if elapsed % 10 == 0:  # 每 10 秒提示一次
            logger.info(f"[Browser] 等待扫码登录中…（已等待 {elapsed // 1000} 秒）")

    # 超时
    await _capture_failure_screenshot(page, "qr_login_timeout")
    logger.error(f"[Browser] 扫码登录超时（{timeout // 1000} 秒），请手动刷新页面后重试")
    return False


async def _handle_login_page(page: Page, timeout: int = 15000) -> bool:
    """处理登录页，尝试多种方式恢复登录态。

    策略：
    1. 优先点击「快速进入」按钮
    2. 如果没找到，刷新页面再试（最多 3 次）
    3. 多次失败后截图记录
    """
    logger.warning("[Browser] 检测到登录页面，尝试自动恢复...")

    for attempt in range(3):  # 最多尝试 3 次
        try:
            # 1. 优先点击「快速进入」
            quick_btn = page.locator('button:has-text("快速进入"), div:has-text("快速进入")')
            if await quick_btn.count() > 0:
                await quick_btn.first.click()
                logger.info(f"[Browser] 已点击「快速进入」（第 {attempt + 1} 次）")
                await page.wait_for_url("**/im**", timeout=timeout)
                logger.info("[Browser] 通过「快速进入」成功恢复登录态")
                return True

            # 2. 如果没找到，尝试刷新页面再试一次
            logger.warning(f"[Browser] 未找到「快速进入」按钮（第 {attempt + 1} 次），刷新页面重试…")
            await page.reload()
            await page.wait_for_timeout(2000)

        except PlaywrightTimeoutError:
            logger.warning(f"[Browser] 第 {attempt + 1} 次尝试超时")
            if attempt < 2:
                await page.reload()
                await page.wait_for_timeout(2000)
        except Exception as e:
            logger.warning(f"[Browser] 第 {attempt + 1} 次尝试失败: {e}")
            if attempt < 2:
                await page.reload()
                await page.wait_for_timeout(2000)

    # 3. 多次失败后截图并返回 False
    await _capture_failure_screenshot(page, "auto_login_failed")
    logger.error("[Browser] 多次尝试恢复登录态失败，请手动登录后重启 Worker")
    return False


async def _capture_failure_screenshot(page: Page, tag: str) -> None:
    """失败时截图保存到 logs/ 目录。"""
    try:
        logs_dir = Path(config.get("server.log_dir", "./logs"))
        logs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = logs_dir / f"{tag}_{ts}.png"
        await page.screenshot(path=str(path), full_page=True)
        logger.info(f"[Browser] 已保存截图: {path}")
    except Exception as e:
        logger.warning(f"[Browser] 截图失败: {e}")
