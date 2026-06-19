"""后台 REST API：话术学习、规则管理、日志、AI 试聊、身份管理、客户记忆。"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app import config
from app.core import (item_collector, memory, persona, persona_gen,
                      pricing, rules, worker_manager)
from app.db import database as db

router = APIRouter(prefix="/api")


# ---------- Worker 启停 ----------
@router.get("/worker")
def worker_status():
    return worker_manager.status()


@router.post("/worker/start")
def worker_start():
    return worker_manager.start()


@router.post("/worker/stop")
def worker_stop():
    return worker_manager.stop()


@router.post("/worker/force-restart-browser")
def worker_force_restart_browser():
    """强制重启 Edge（会关闭所有 Edge 窗口，谨慎使用）。
    仅当 ensure_browser 提示需要手动关闭 Edge 时使用。
    """
    ok, msg = worker_manager.force_restart_browser()
    return {"ok": ok, "msg": msg}


# ---------- 默认人设 ----------
class PersonaIn(BaseModel):
    persona: str | None = None


@router.get("/persona")
def get_persona():
    return {
        "persona": persona.get_persona(),
        "preview_system_prompt": persona.build_system_prompt(),
        "persona_routes": [
            {
                "name": persona.route_label(r),
                "item_ids": r.get("item_ids") or r.get("item_id") or [],
                "url_patterns": r.get("url_patterns") or r.get("urls") or [],
                "keywords": r.get("keywords") or [],
            }
            for r in persona.get_persona_routes()
        ],
    }


@router.post("/persona")
def save_persona(body: PersonaIn):
    if body.persona is not None:
        db.set_setting("persona", body.persona)
    return {"ok": True, "preview_system_prompt": persona.build_system_prompt()}


# ---------- 概览 / 设置 ----------
@router.get("/stats")
def stats():
    def c(sql, p=()):
        r = db.query(sql, p)
        return r[0]["c"] if r else 0
    return {
        "personas": c("SELECT COUNT(*) c FROM personas WHERE enabled=1"),
        "customers": c("SELECT COUNT(*) c FROM customer_memory"),
        "replies_total": c("SELECT COUNT(*) c FROM reply_logs WHERE sent=1"),
        "replies_today": c(
            "SELECT COUNT(*) c FROM reply_logs WHERE sent=1 AND created_at>="
            "strftime('%s','now','start of day','utc')"),
    }


@router.get("/config")
def get_config():
    """返回非敏感配置供界面展示。"""
    return {
        "ai_enabled": config.get("ai.enabled"),
        "ai_model": config.get("ai.model"),
        "ai_base_url": config.get("ai.base_url"),
        "reply_delay": [config.get("reply_delay.min_delay", 2.5),
                        config.get("reply_delay.max_delay", 16.0)],
        "reply_delay_base": [config.get("reply_delay.base_min", 4.5),
                             config.get("reply_delay.base_max", 9.5)],
        "daily_reply_limit": config.get("safety.daily_reply_limit"),
        "business_hours": config.get("safety.business_hours"),
        "reply_to_active_only": config.get("safety.reply_to_active_only"),
        "route_fallback": config.get("safety.route_fallback"),
        "persona_routes_enabled": config.get("persona_routes.enabled", True),
        "persona_routes_count": len(persona.get_persona_routes()),
        "fallback_reply": config.get("fallback_reply"),
    }


# ---------- AI 试聊（不发送，只看 AI 会怎么回） ----------
class ChatIn(BaseModel):
    text: str
    item_id: str = ""
    link_url: str = ""


@router.post("/preview-reply")
def preview_reply(body: ChatIn):
    reply, source = rules.decide_reply(body.text, body.item_id, link_url=body.link_url)
    route = persona.resolve_route(body.text, body.item_id, body.link_url)
    return {
        "reply": reply,
        "source": source,
        "persona_route": persona.route_label(route),
    }


# ---------- 日志 ----------
@router.get("/logs")
def logs(limit: int = 100):
    return db.query(
        "SELECT * FROM reply_logs ORDER BY id DESC LIMIT ?", (limit,))


# ---------- 身份管理（按商品切换 AI 身份 + 目标设定） ----------
class SlotIn(BaseModel):
    key: str
    label: str = ""
    desc: str = ""
    optional: bool = False


class PersonaRow(BaseModel):
    id: int | None = None
    name: str = ""
    item_ids: list[str] = []
    url_patterns: list[str] = []
    keywords: list[str] = []
    persona_text: str = ""
    goal_type: str = "none"        # collect / answer / none
    goal_text: str = ""
    slots: list[SlotIn] = []
    priority: int = 0
    enabled: bool = True


@router.get("/personas")
def list_personas():
    return db.list_personas()


@router.post("/personas")
def save_persona_row(body: PersonaRow):
    data: dict[str, Any] = body.model_dump()
    data["slots"] = [s for s in data.get("slots", []) if s.get("key")]
    pid = db.upsert_persona(data)
    return {"id": pid, "ok": True}


@router.delete("/personas/{pid}")
def delete_persona(pid: int):
    db.delete_persona(pid)
    return {"ok": True}


# ---------- 报价策略 ----------
class PricingIn(BaseModel):
    category: str
    keywords: list[str] = []
    price_range: str = ""
    tier: str = "low_price"
    base_prompt: str = ""
    low_budget_prompt: str = ""
    upselling_prompt: str = ""
    clarification_questions: str = ""
    enabled: bool = True


@router.get("/pricing")
def list_pricing():
    return db.list_pricing()


@router.post("/pricing")
def save_pricing(body: PricingIn):
    if not body.category.strip():
        return {"error": "类目名必填"}
    db.upsert_pricing(body.model_dump())
    return {"ok": True}


@router.delete("/pricing/{category}")
def delete_pricing(category: str):
    db.delete_pricing(category)
    return {"ok": True}


class TestPricingIn(BaseModel):
    category: str
    message: str = ""


@router.post("/test_pricing_prompt")
def test_pricing_prompt(body: TestPricingIn):
    """测试当前会使用什么报价 Prompt（后台调试用）。

    传入产品类目和模拟客户消息，返回匹配的报价策略和完整 Prompt。
    """
    prompt = pricing.get_pricing_prompt(body.category, body.message)
    strategy = db.get_pricing(body.category)
    return {
        "category": body.category,
        "strategy": strategy,
        "pricing_prompt": prompt,
        "is_low_budget": any(w in body.message for w in ["低", "少", "便宜", "预算有限", "有限", "没多少", "不多", "穷", "划算"]),
    }


# ---------- 商品信息采集 ----------
class CollectIn(BaseModel):
    item_id: str = ""
    text: str = ""        # 可直接传商品链接/消息，自动解析 item_id


@router.post("/collect-item")
async def collect_item(body: CollectIn):
    """手动采集商品信息（复用正在运行的调试 Edge 的登录态）。"""
    iid = body.item_id.strip() or (item_collector.extract_item_id(body.text) or "")
    if not iid:
        return {"error": "未能从输入中解析出 item_id，请直接填 item_id 或贴商品链接"}
    result = await item_collector.collect_standalone(
        iid, config.get("browser.cdp_url", "http://127.0.0.1:9222"))
    db.upsert_collected_item(result)
    return result


@router.get("/collected-items")
def list_collected_items(limit: int = 100):
    return db.list_collected_items(limit)


@router.get("/collected-items/{item_id}")
def get_collected_item(item_id: str):
    return db.get_collected_item(item_id) or {"error": "未采集过该商品"}


@router.post("/personas/generate")
async def generate_persona(body: CollectIn):
    """贴商品链接/ID → 自动采集商品信息 → AI 生成身份草稿（人设/目标/槽位）。
    返回 {item, draft}，前端预填「身份管理」表单，人工确认后保存。"""
    iid = body.item_id.strip() or (item_collector.extract_item_id(body.text) or "")
    if not iid:
        return {"error": "未能解析出 item_id，请直接填 item_id 或贴商品链接"}
    item = await item_collector.collect_standalone(
        iid, config.get("browser.cdp_url", "http://127.0.0.1:9222"))
    db.upsert_collected_item(item)
    if not item.get("title"):
        return {"item": item, "error": item.get("error") or "未抓到商品标题，无法生成身份"}
    draft = await asyncio.to_thread(persona_gen.generate, item)
    return {"item": item, "draft": draft}


@router.post("/personas/from-item/{item_id}")
async def persona_from_item(item_id: str):
    """对已采集过的商品，用 AI 生成身份草稿（不重新打开浏览器）。"""
    it = db.get_collected_item(item_id)
    if not it:
        return {"error": "未采集过该商品，请先采集/生成"}
    draft = await asyncio.to_thread(persona_gen.generate, it)
    return {"item": it, "draft": draft}


# ---------- 客户记忆管理 ----------
@router.get("/memory")
def list_memory(limit: int = 200):
    return db.list_memories(limit)


@router.get("/memory/{chat_id:path}")
def get_memory(chat_id: str):
    mem = db.get_memory(chat_id) or {"chat_id": chat_id, "slots": {}}
    msgs = db.query(
        "SELECT role,content,created_at FROM messages WHERE chat_id=? ORDER BY id ASC LIMIT 200",
        (chat_id,))
    return {"memory": mem, "messages": msgs}


class MemoryEdit(BaseModel):
    summary: str | None = None
    slots: dict[str, Any] | None = None
    doc: str | None = None
    goal_done: int | None = None


@router.put("/memory/{chat_id:path}")
def edit_memory(chat_id: str, body: MemoryEdit):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if fields:
        db.upsert_memory(chat_id, **fields)
    return {"ok": True, "memory": db.get_memory(chat_id)}


@router.delete("/memory/{chat_id:path}")
def clear_memory(chat_id: str):
    db.delete_memory(chat_id)
    return {"ok": True}


class PauseIn(BaseModel):
    paused: bool


@router.post("/memory/{chat_id:path}/pause")
def pause_memory(chat_id: str, body: PauseIn):
    """人工接管：暂停/恢复某客户的 AI 自动回复。"""
    db.set_pause(chat_id, body.paused)
    return {"ok": True, "ai_paused": int(body.paused)}


# ---------- 多轮试聊（选身份/商品，模拟客户与 AI 连续对话） ----------
class TestChatIn(BaseModel):
    session_id: str = "default"
    text: str
    persona_id: int | None = None
    item_id: str = ""
    link_url: str = ""


def _test_key(session_id: str) -> str:
    return f"test:{session_id or 'default'}"


@router.get("/test-chat/{session_id}")
def test_chat_history(session_id: str):
    key = _test_key(session_id)
    msgs = db.query(
        "SELECT role,content,created_at FROM messages WHERE chat_id=? ORDER BY id ASC",
        (key,))
    return {"messages": msgs, "memory": db.get_memory(key)}


@router.delete("/test-chat/{session_id}")
def test_chat_reset(session_id: str):
    key = _test_key(session_id)
    db.execute("DELETE FROM messages WHERE chat_id=?", (key,))
    db.delete_memory(key)
    return {"ok": True}


@router.post("/test-chat")
def test_chat(body: TestChatIn):
    key = _test_key(body.session_id)
    mem = memory.get(key)
    # 身份：UI 显式指定 persona_id 则用它；否则会话级解析（首次命中后锁定沿用）
    if body.persona_id:
        persona_obj = persona.get_persona_by_id(body.persona_id)
    else:
        persona_obj = persona.resolve_for_conversation(
            mem, text=body.text, item_id=body.item_id, link_url=body.link_url)

    history = db.query(
        "SELECT role,content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT 12",
        (key,))[::-1]
    db.add_message(key, "buyer", body.text, body.item_id)
    reply, source = rules.decide_reply(
        body.text, item_id=body.item_id, history=history,
        link_url=body.link_url, memory=mem, persona_obj=persona_obj)

    seller_role = "ai" if source == "ai" else "seller"
    db.add_message(key, seller_role, reply, body.item_id)

    turn_history = history + [
        {"role": "buyer", "content": body.text},
        {"role": seller_role, "content": reply},
    ]
    try:
        mem = memory.update_after_turn(
            key, persona_obj, turn_history, buyer_nick="(试聊)", item_id=body.item_id)
    except Exception:
        from loguru import logger
        logger.exception("update_after_turn 失败")
        mem = memory.get(key)

    return {
        "reply": reply,
        "source": source,
        "persona": persona.route_label(persona_obj),
        "goal_type": (persona_obj or {}).get("goal_type", "none"),
        "memory": mem,
    }
