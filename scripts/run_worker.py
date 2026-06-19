"""启动 Playwright Worker：接管 Edge → 监听 IM → 自动回复。

前提：Edge 已用 --remote-debugging-port=9222 启动并登录工作台。
用法：.venv\\Scripts\\python.exe -m scripts.run_worker
"""
from __future__ import annotations

import asyncio
import socket
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from loguru import logger

from app.db import database as db
from app.worker.browser import BrowserManager, ensure_logged_in
from app.worker.im_listener import IMListener

HEARTBEAT_INTERVAL = 30  # 秒
_SINGLETON_PORT = 49231  # 单例锁端口：绑不上说明已有 Worker 在跑
_lock_sock: socket.socket | None = None


def _acquire_singleton_lock() -> bool:
    """绑定本地端口作为互斥锁，保证全局只有一个 Worker 真正 hook WebSocket。
    任何多余被拉起的 Worker 绑不上端口会立即退出，从根上杜绝重复回消息。"""
    global _lock_sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", _SINGLETON_PORT))
        s.listen(1)
    except OSError:
        s.close()
        return False
    _lock_sock = s  # 持有到进程退出
    return True


async def main() -> None:
    bm = BrowserManager()
    await bm.start()

    # 自动处理登录页（检测到「快速进入」等按钮自动点击）
    login_ok = await ensure_logged_in(bm.page)
    if not login_ok:
        logger.error("无法自动进入工作台，请手动登录后重启 Worker")
        # 不退出，给用户手动操作的机会

    listener = IMListener(bm)
    await listener.start()
    db.set_worker_heartbeat()  # 启动即写一次，避免刚起来就被误判为死
    logger.info("Worker 运行中，等待买家消息… (Ctrl+C 退出)")
    last_hb = time.time()
    while True:
        if time.time() - last_hb >= HEARTBEAT_INTERVAL:
            db.set_worker_heartbeat()
            last_hb = time.time()
        await asyncio.sleep(1)


if __name__ == "__main__":
    if not _acquire_singleton_lock():
        logger.warning("已有 Worker 在运行（单例锁被占用），本进程退出，避免重复回复")
        sys.exit(0)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("已退出")
