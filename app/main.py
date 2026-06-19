"""FastAPI 入口。

启动后：
  - 后台管理 API：http://127.0.0.1:8090/api/...
  - 文档：       http://127.0.0.1:8090/docs

Playwright Worker 默认不随 web 自动启动（避免误操作浏览器）。
用 START_WORKER=1 环境变量开启，或单独跑 python -m app.worker_runner。
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app import config
from app.api.routes import router

WEB_DIR = Path(__file__).resolve().parent / "web"

_worker_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core import worker_manager
    if os.getenv("START_WORKER") == "1":
        from app.worker.browser import BrowserManager
        from app.worker.im_listener import IMListener
        bm = BrowserManager()
        await bm.start()
        listener = IMListener(bm)
        await listener.start()
        _worker_state["bm"] = bm
        logger.info("Worker 已随服务启动")

    # 后台健康检查：每 60 秒看 Worker 心跳，掉了就自动重启（仅在用户点过上线时）
    async def _health_loop():
        while True:
            await asyncio.sleep(60)
            try:
                r = worker_manager.restart_if_dead()
                if r.get("action") not in ("ok", "skip"):
                    logger.info(f"[health] {r}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[health] 检查异常: {e}")

    task = asyncio.create_task(_health_loop())
    yield
    task.cancel()
    if _worker_state.get("bm"):
        await _worker_state["bm"].close()
    # 后端退出：结束受管子进程，但保留 intended（重启后自动恢复）
    worker_manager.shutdown_proc()


app = FastAPI(title="闲鱼工作台自动化 v2", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
def health():
    return {"status": "ok", "ai_enabled": config.get("ai.enabled")}


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


# 其余静态资源（如以后拆分 css/js）
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    # 传 app 对象而非 "app.main:app" 字符串：避免 `python -m app.main` + 导入串
    # 触发 uvicorn 再起一个子进程（两个后端 = 两个健康检查循环 = 重复拉起 Worker）。
    uvicorn.run(app,
                host=config.get("server.host", "127.0.0.1"),
                port=config.get("server.port", 8090),
                reload=False)
