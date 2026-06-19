"""Worker 进程管理：后台界面用它启停自动回复 Worker。

以子进程方式运行 `python -m scripts.run_worker`（日志 logs/worker.log）。
启动前会自动确保「带调试端口的 Edge」已就绪（没有就拉起来），
所以后台点一下「上线运行」即可，不用手动开 Edge。
"""
from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from loguru import logger

from app import config
from app.db import database as db

ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = ROOT / "logs"

DEBUG_PORT = 9222

# 健康检查 / 自动重启参数
HEARTBEAT_TIMEOUT = 90        # 超过这么多秒没心跳，判为不健康
RESTART_COOLDOWN = 120        # 两次自动重启最小间隔，避免狂重启
MAX_CONSECUTIVE_RESTARTS = 5  # 连续重启这么多次仍不健康就暂停，等人工

_last_restart_at = 0.0
_restart_count = 0
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
EDGE_PROFILE = str(ROOT / "edge_debug")

_proc: subprocess.Popen | None = None


# ---------- 调试 Edge 就绪 ----------

def _port_ready() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=2)
        return True
    except Exception:
        return False


def _edge_path() -> str | None:
    p = config.get("browser.edge_path")
    if p and Path(p).exists():
        return p
    for c in EDGE_CANDIDATES:
        if Path(c).exists():
            return c
    return None


def ensure_browser() -> tuple[bool, str]:
    """确保 9222 调试 Edge 在线；不在就（杀掉现有 Edge 后）拉起来。"""
    if _port_ready():
        return True, "调试 Edge 已在线"
    edge = _edge_path()
    if not edge:
        return False, "找不到 Edge，请在 config.yml 配 browser.edge_path"
    # 调试端口只对「首个 Edge 进程」生效，必须先杀掉现有 Edge
    subprocess.run(["taskkill", "/F", "/IM", "msedge.exe"],
                   capture_output=True)
    time.sleep(1.5)
    subprocess.Popen([edge, f"--remote-debugging-port={DEBUG_PORT}",
                      f"--user-data-dir={EDGE_PROFILE}"])
    for _ in range(24):           # 最多等 ~12 秒
        if _port_ready():
            return True, "已自动拉起调试 Edge"
        time.sleep(0.5)
    return False, "拉起 Edge 后 9222 仍未就绪"


def force_restart_browser() -> tuple[bool, str]:
    """强制重启 Edge（会关闭所有 Edge 窗口，谨慎使用）。"""
    edge = _edge_path()
    if not edge:
        return False, "找不到 Edge"
    subprocess.run(["taskkill", "/F", "/IM", "msedge.exe"], capture_output=True)
    time.sleep(1.5)
    subprocess.Popen([edge, f"--remote-debugging-port={DEBUG_PORT}",
                      f"--user-data-dir={EDGE_PROFILE}"])
    for _ in range(24):
        if _port_ready():
            return True, "已强制重启调试 Edge"
        time.sleep(0.5)
    return False, "强制重启后 9222 仍未就绪"


# ---------- Worker 进程 ----------

def _intended() -> bool:
    """用户是否点过「上线运行」（持久化在 settings，跨后端重启仍有效）。"""
    return db.get_setting("worker_intended", "0") == "1"


def health() -> dict:
    """综合存活状态：进程在不在 + 心跳新不新 + 是否应在运行。"""
    running = _proc is not None and _proc.poll() is None
    age = db.get_worker_heartbeat_age()
    return {
        "running": running,
        "pid": _proc.pid if running else None,
        "browser_ready": _port_ready(),
        "intended": _intended(),
        "heartbeat_age": round(age, 1) if age is not None else None,
        "healthy": age is not None and age < HEARTBEAT_TIMEOUT,
        "restart_count": _restart_count,
        "auto_restart_paused": _restart_count >= MAX_CONSECUTIVE_RESTARTS,
    }


# 兼容旧名字（routes 仍调 status()）
def status() -> dict:
    return health()


def _kill_stray_workers() -> None:
    """杀掉所有正在跑的 run_worker 进程。

    关键：保证全局只有一个 Worker。否则多个 Worker 都 hook 同一个页面的 WebSocket，
    会对同一条买家消息各自回复一次 → 重复发消息（跨进程的 msg_id 去重有竞态挡不住）。
    按端口杀后端不会带走它的 worker 子进程（孤儿），靠这里统一清。
    """
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*scripts.run_worker*' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
            capture_output=True, timeout=15,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"清理多余 worker 失败: {e}")


def _terminate_proc() -> None:
    global _proc
    if _proc is not None and _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _proc.kill()
    _proc = None
    _kill_stray_workers()   # 顺带清掉任何孤儿 worker


def _spawn() -> dict:
    """真正拉起 Worker 子进程（不重置重启计数，供自动重启复用）。"""
    global _proc
    _kill_stray_workers()   # 启动前先杀光现存 worker，确保唯一
    ok, msg = ensure_browser()
    if not ok:
        return {"running": False, "browser_ready": False, "error": msg}
    LOG_DIR.mkdir(exist_ok=True)
    logf = open(LOG_DIR / "worker.log", "a", encoding="utf-8")
    _proc = subprocess.Popen(
        [sys.executable, "-m", "scripts.run_worker"],
        cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT,
    )
    db.set_setting("worker_intended", "1")
    time.sleep(1.0)               # 给它一点时间，poll 一下是否秒退
    if _proc.poll() is not None:
        return {"running": False, "browser_ready": _port_ready(),
                "error": "Worker 启动后立即退出，详见 logs/worker.log"}
    return {**health(), "msg": f"已启动（{msg}）"}


def start() -> dict:
    """用户点「上线运行」：标记意图并拉起，重置自动重启计数。"""
    global _restart_count, _last_restart_at
    if _proc is not None and _proc.poll() is None:
        db.set_setting("worker_intended", "1")
        return {**health(), "msg": "已在运行"}
    _restart_count = 0
    _last_restart_at = 0.0
    return _spawn()


def stop() -> dict:
    """用户点「关闭」：清除意图，自动重启不再触发。"""
    global _restart_count
    _terminate_proc()
    db.set_setting("worker_intended", "0")
    _restart_count = 0
    return {"running": False, "msg": "已停止"}


def shutdown_proc() -> None:
    """后端退出时调用：结束受管子进程，但保留 intended，
    这样后端重启后健康检查会自动把 Worker 拉回来。"""
    _terminate_proc()


def restart_if_dead() -> dict:
    """健康检查 + 自动重启。仅当用户点过上线(intended)且确实不健康时才重启。"""
    global _last_restart_at, _restart_count
    if not _intended():
        return {"action": "skip", "reason": "worker 未开启"}
    h = health()
    if h["healthy"]:
        _restart_count = 0            # 恢复健康，清零计数
        return {"action": "ok"}
    if _restart_count >= MAX_CONSECUTIVE_RESTARTS:
        return {"action": "paused",
                "reason": f"连续重启 {_restart_count} 次仍不健康，已暂停自动重启，请人工检查"}
    now = time.time()
    if now - _last_restart_at < RESTART_COOLDOWN:
        return {"action": "cooldown",
                "wait": round(RESTART_COOLDOWN - (now - _last_restart_at))}
    _last_restart_at = now
    _restart_count += 1
    logger.warning(f"Worker 不健康（心跳 {h['heartbeat_age']}s 前），自动重启第 {_restart_count} 次…")
    _terminate_proc()
    time.sleep(2)
    return {"action": "restarted", "count": _restart_count, "result": _spawn()}
