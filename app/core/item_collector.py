"""商品信息采集：复用 worker 已登录的 Edge（CDP）打开商品页抓取。

为什么不另起 headless chromium：本机只有 Edge、没装 chromium；且闲鱼商品页需要
登录态，无登录的新浏览器多半被登录墙/风控挡住，只能拿到空壳。复用 CDP 接管的
已登录 Edge 最稳。

两种用法：
  - worker 侧：collect_with_context(browser.context, item_id)  在现有上下文开新页
  - API  侧：collect_standalone(item_id, cdp_url)              连到 9222 调试 Edge
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from loguru import logger

# 从消息文本里提取 item_id（靠前的更精确，最后一条是兜底长数字）
ITEM_ID_PATTERNS = [
    re.compile(r"goofish\.com/item/(\d{6,})", re.IGNORECASE),
    re.compile(r"taobao\.com/item[^\d]*(\d{6,})", re.IGNORECASE),
    re.compile(r"/item/(\d{6,})", re.IGNORECASE),
    re.compile(r"item[_-]?id[=:\s]*(\d{6,})", re.IGNORECASE),
    re.compile(r"[?&]id=(\d{6,})"),       # 查询串形式 .../item?...&id=123
    re.compile(r"!!(\d{6,})-"),          # 头像/图片 URL 里的 userId 形式（少见误命中）
    re.compile(r"(\d{9,14})"),            # 兜底：一长串数字
]


def extract_item_id(text: str = "") -> Optional[str]:
    for pattern in ITEM_ID_PATTERNS:
        m = pattern.search(text or "")
        if m:
            return m.group(1)
    return None


def item_url(item_id: str) -> str:
    # 闲鱼商品页是查询串形式（/item/<id>.html 对真实商品并不可用）
    return f"https://www.goofish.com/item?id={item_id}"


def _clean_price(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"[¥￥]?\s*\d+(?:\.\d+)?", text)
    price = (m.group(0).strip() if m else text.strip())[:20]
    if price and price[0] not in "¥￥":
        price = "¥" + price          # 闲鱼主价格元素常只给数字，补回符号
    return price


def _clean_title(doc_title: str) -> str:
    return re.sub(r"\s*[_\-]\s*(闲鱼|Goofish)\s*$", "", (doc_title or "").strip())


# 基于真实页面调研定的选择器（2026-06：闲鱼商品页 og:title 为空，标题取 document.title；
# 价格首个 [class*=price]；卖家 [class*=nick]（避开 [class*=seller] 那是操作按钮）；
# 详情直接取 body 文本喂 AI，避免猜哈希 class）。
_SCRAPE_JS = """() => {
  const pick = sel => { const e=document.querySelector(sel); return e? (e.innerText||e.textContent||'').trim() : ''; };
  const imgs = [...document.querySelectorAll('img')].map(i=>i.src)
    .filter(s => s && s.includes('alicdn') && !s.includes('tps-'));   // 去掉 tps- 图标
  return {
    docTitle: document.title || '',
    price: pick('[class*="price"]'),
    seller: pick('[class*="nick"]'),
    images: [...new Set(imgs)].slice(0, 6),
    bodyText: ((document.body && document.body.innerText) || '').replace(/\\n{2,}/g, '\\n').trim().slice(0, 2000),
  };
}"""


async def _scrape(page, item_id: str, url: str) -> dict[str, Any]:
    data = await page.evaluate(_SCRAPE_JS)
    title = _clean_title(data.get("docTitle", ""))
    body = (data.get("bodyText") or "").strip()
    info: dict[str, Any] = {
        "item_id": item_id,
        "url": url,
        "title": title[:200],
        "price": _clean_price(data.get("price", "")),
        "description": body[:800],
        "seller_nick": (data.get("seller") or "").strip()[:60],
        "images": data.get("images") or [],
        "page_text": body[:2000],   # 不入库，仅供 AI 生成身份用
        "error": "",
        "collected_at": time.time(),
    }
    # 软提示：标题为空或像登录墙，多半是未登录/被风控
    if not info["title"] or info["title"] in ("闲鱼", "Goofish"):
        info["error"] = "页面可能未登录或被风控（标题为空）；请确认 Edge 已登录闲鱼"
    return info


async def collect_with_context(context, item_id: str, *, timeout: int = 20000) -> dict[str, Any]:
    """在给定的（已登录）浏览器上下文里开新标签抓商品页，抓完关掉该标签。"""
    if context is None:
        return {"item_id": item_id, "error": "没有可用的浏览器上下文（worker 未在线）"}
    url = item_url(item_id)
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        # 闲鱼商品页是重 SPA，标题/价格是 JS 渲染的；轮询等它出来（最多 ~6s）
        for _ in range(12):
            await page.wait_for_timeout(500)
            t = await page.title()
            if t and t not in ("闲鱼", "Goofish") and len(t) > 4:
                break
        return await _scrape(page, item_id, url)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"采集 item {item_id} 失败: {e}")
        return {"item_id": item_id, "url": url, "error": str(e), "collected_at": time.time()}
    finally:
        try:
            await page.close()
        except Exception:  # noqa: BLE001
            pass


async def collect_standalone(item_id: str, cdp_url: str) -> dict[str, Any]:
    """API 进程用：连到正在跑的调试 Edge（9222），复用其登录态采集。"""
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(cdp_url)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        return await collect_with_context(ctx, item_id)
    except Exception as e:  # noqa: BLE001
        return {"item_id": item_id,
                "error": f"连不上调试浏览器（{cdp_url}）：{e}。请先在后台「上线运行」启动 Edge。",
                "collected_at": time.time()}
    finally:
        try:
            await pw.stop()
        except Exception:  # noqa: BLE001
            pass
