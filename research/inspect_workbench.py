"""第 1 步·实地调研脚本。

作用：接管你已登录的 Chrome，观察鱼小铺工作台的：
  1. WebSocket 连接地址与收发的帧（判断是否加密、新消息长什么样）
  2. mtop / HTTP 接口（发送回复、自动评价分别调了哪个 api）
  3. 页面 DOM 结构（消息容器、输入框、发送按钮的选择器）
所有记录写到 research/output/ 下，供后续填 im_listener.py 的选择器。

用法（先用调试端口启动 Chrome，再跑本脚本）：
  1) 完全关闭 Chrome
  2) 启动带调试端口的 Chrome：
     & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" `
         --remote-debugging-port=9222 --user-data-dir="D:\\xianyu\\v2\\chrome_debug"
     然后在这个 Chrome 里登录并打开工作台 IM 页
  3) .venv\\Scripts\\python.exe -m research.inspect_workbench
  4) 在工作台里手动收发几条消息 / 做一次评价，让脚本抓到样本
  5) Ctrl+C 结束，查看 research/output/
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from playwright.async_api import async_playwright

CDP_URL = "http://127.0.0.1:9222"
OUT = Path(__file__).resolve().parent / "output"
OUT.mkdir(exist_ok=True)

ws_log = (OUT / "websocket_frames.jsonl").open("a", encoding="utf-8")
http_log = (OUT / "http_requests.jsonl").open("a", encoding="utf-8")


def _write(fp, obj) -> None:
    fp.write(json.dumps(obj, ensure_ascii=False) + "\n")
    fp.flush()


def _short(s: str, n: int = 2000) -> str:
    return s if len(s) <= n else s[:n] + f"...(+{len(s)-n})"


async def main() -> None:
    async with async_playwright() as pw:
        print(f"连接 CDP: {CDP_URL}")
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        ctx = browser.contexts[0]

        page = None
        for p in ctx.pages:
            if "seller.goofish.com" in p.url:
                page = p
                break
        if page is None:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        print(f"目标页: {page.url}")

        # 1. WebSocket
        def on_ws(ws):
            print(f"[WS open] {ws.url}")
            _write(ws_log, {"event": "open", "url": ws.url, "ts": time.time()})
            ws.on("framesent", lambda p: _write(
                ws_log, {"event": "sent", "url": ws.url, "ts": time.time(),
                         "payload": _short(p if isinstance(p, str) else str(p))}))
            ws.on("framereceived", lambda p: _write(
                ws_log, {"event": "recv", "url": ws.url, "ts": time.time(),
                         "payload": _short(p if isinstance(p, str) else str(p))}))

        page.on("websocket", on_ws)

        # 2. HTTP / mtop（只记录 goofish/taobao 域的 api 调用）
        def on_request(req):
            if any(k in req.url for k in ("mtop", "goofish", "taobao")):
                _write(http_log, {"method": req.method, "url": req.url,
                                  "post": _short(req.post_data or ""), "ts": time.time()})

        page.on("request", on_request)

        print("开始监听。请在工作台里手动收发消息 / 做一次评价。Ctrl+C 结束。")
        print("提示：可在浏览器控制台用 $0 选中消息节点，观察其 class，用于填选择器。")
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\n结束。输出在:", OUT)


if __name__ == "__main__":
    asyncio.run(main())
