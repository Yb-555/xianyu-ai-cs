"""客户记忆模块：每个客户（按会话 chat_id）一份独立记忆，互不串台。

记忆由三部分组成：
  1. messages 表里的原始对话（按 chat_id 隔离，已有）
  2. customer_memory.summary —— AI 维护的「这位客户的需求摘要」
  3. customer_memory.slots   —— 关键信息槽位（收集需求型目标用），如
     {项目是什么, 用途, 样式要求, 客户想法}

收集需求型（goal_type=collect）的身份：每轮买家说完后，用一次独立的
轻量 AI 抽取调用，把对话里能确认的槽位提取成 JSON 并合并进记忆。槽位集齐
后生成一份需求总结文档（doc），并标记 goal_done，AI 即停止追问、礼貌收尾。

答疑型（goal_type=answer）/无目标：只维护一句话摘要，不做槽位抽取。
"""
from __future__ import annotations

import json
import re

from loguru import logger

from app.core import ai_client
from app.db import database as db


def get(chat_id: str) -> dict:
    """读取某客户的记忆（不存在返回空骨架）。"""
    row = db.get_memory(chat_id)
    if row:
        return row
    return {"chat_id": chat_id, "summary": "", "slots": {}, "goal_done": 0, "doc": ""}


def ensure(chat_id: str, *, buyer_id: str = "", buyer_nick: str = "",
           item_id: str = "", item_title: str = "", persona_name: str = "",
           persona_id: int | None = None) -> None:
    """确保该客户有一条记忆，并刷新基础身份信息。"""
    fields: dict = {}
    if buyer_id:
        fields["buyer_id"] = buyer_id
    if buyer_nick:
        fields["buyer_nick"] = buyer_nick
    if item_id:
        fields["item_id"] = item_id
    if item_title:
        fields["item_title"] = item_title
    if persona_name:
        fields["persona_name"] = persona_name
    if persona_id is not None:
        fields["persona_id"] = persona_id
    db.upsert_memory(chat_id, **fields)


# ---------- 摘要渲染 ----------

def render_summary(persona: dict | None, slots: dict) -> str:
    """把已收集的槽位渲染成给 AI 看的「客户已知情况」摘要。"""
    if not slots:
        return ""
    label_map = {}
    for s in (persona or {}).get("slots", []) or []:
        label_map[s.get("key")] = s.get("label", s.get("key"))
    lines = []
    for k, v in slots.items():
        if v:
            lines.append(f"- {label_map.get(k, k)}：{v}")
    return "\n".join(lines)


def _required_slots(persona: dict) -> list[dict]:
    return [s for s in (persona.get("slots") or []) if not s.get("optional")]


def all_required_filled(persona: dict, slots: dict) -> bool:
    req = _required_slots(persona)
    if not req:
        return False
    return all(slots.get(s["key"]) for s in req)


# ---------- 槽位抽取（独立 AI 调用） ----------

def _strip_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return m.group(0) if m else text


def _extract_slots(persona: dict, history: list[dict]) -> dict:
    """让 AI 从对话里抽取每个槽位的值；未提及返回 null。返回 {key: value}。"""
    slot_defs = persona.get("slots") or []
    if not slot_defs:
        return {}
    spec = "\n".join(
        f'- {s["key"]}（{s.get("label", s["key"])}）：{s.get("desc", "")}'
        for s in slot_defs
    )
    convo = "\n".join(
        f'{"买家" if h.get("role") == "buyer" else "卖家"}：{h.get("content", "")}'
        for h in history
    )
    sys = (
        "你是信息抽取器。从下面的买家与卖家对话中，判断每个信息点是否已被买家明确表达。"
        "买家一句话里可能同时包含多个信息点，请逐个信息点独立判断，不要遗漏。"
        "对每个信息点：若买家已明确表达，输出忠于原话的简短中文值（不要自行改写或臆测）；"
        "若对话中未提及或不明确，输出 null。"
        "只输出一个 JSON 对象，键为下面给出的 key，值为字符串或 null，不要输出任何解释或多余文字。\n\n"
        f"信息点：\n{spec}"
    )
    try:
        raw = ai_client.chat([
            {"role": "system", "content": sys},
            {"role": "user", "content": f"对话：\n{convo}"},
        ])
        data = json.loads(_strip_json(raw))
    except Exception as e:
        logger.warning(f"槽位抽取失败（忽略，本轮不更新槽位）: {e}")
        return {}
    out: dict = {}
    valid_keys = {s["key"] for s in slot_defs}
    for k, v in (data or {}).items():
        if k in valid_keys and v not in (None, "", "null", "无", "未提及"):
            out[k] = str(v).strip()
    return out


def _last_buyer(history: list[dict]) -> str:
    return next((h.get("content", "") for h in reversed(history)
                 if h.get("role") == "buyer"), "")


def _update_rolling_summary(old_summary: str, history: list[dict]) -> str:
    """答疑型/无目标身份：把旧摘要 + 最近对话压缩成一段持续更新的客户画像，
    让 AI 记住最近 6 条以外说过的话，且 token 不膨胀。"""
    convo = "\n".join(
        f'{"买家" if h.get("role") == "buyer" else "卖家"}：{h.get("content", "")}'
        for h in history[-8:]
    )
    sys = (
        "你在维护一位客户的「记忆摘要」。基于已有摘要和最近对话，输出更新后的摘要，"
        "用要点形式记录这位客户：想要什么、关注/在意什么、已沟通过的结论、待办。"
        "只保留对后续回复有用的信息，控制在 120 字内，不要寒暄，不要编造。只输出摘要本身。"
    )
    try:
        return ai_client.chat([
            {"role": "system", "content": sys},
            {"role": "user", "content": f"已有摘要：{old_summary or '（无）'}\n\n最近对话：\n{convo}"},
        ]).strip()
    except Exception as e:
        logger.warning(f"滚动摘要更新失败（保留旧摘要）: {e}")
        return old_summary


def _detect_confirmation(history: list[dict]) -> bool:
    """收尾复述后，判断买家这句是否在「确认需求无误」。"""
    last = _last_buyer(history)
    if not last:
        return False
    try:
        raw = ai_client.chat([
            {"role": "system", "content": (
                "卖家刚刚复述了对客户需求的理解，请客户确认。"
                "判断客户这句话是否表示「确认/认可、需求无误」。"
                "只回一个词：yes 或 no。")},
            {"role": "user", "content": f"客户说：{last}"},
        ])
    except Exception:
        return False
    return raw.strip().lower().startswith("y")


def _generate_doc(persona: dict, slots: dict, history: list[dict]) -> str:
    """槽位收集齐后，生成一份需求总结文档，方便卖家照此完成订单。"""
    summary = render_summary(persona, slots)
    sys = (
        "你是订单需求整理助手。根据下面整理好的关键信息和原始对话，"
        "输出一份简洁、条理清晰的中文「客户需求文档」，分点列出，"
        "让卖家照此就能开始制作/接单。不要寒暄，不要编造对话里没有的信息。"
    )
    convo = "\n".join(
        f'{"买家" if h.get("role") == "buyer" else "卖家"}：{h.get("content", "")}'
        for h in history
    )
    try:
        return ai_client.chat([
            {"role": "system", "content": sys},
            {"role": "user", "content": f"目标：{persona.get('goal_text', '')}\n\n关键信息：\n{summary}\n\n原始对话：\n{convo}"},
        ])
    except Exception as e:
        logger.warning(f"需求文档生成失败，退回用关键信息拼接: {e}")
        return f"【客户需求（关键信息）】\n{summary}"


# ---------- 每轮更新入口 ----------

def update_after_turn(chat_id: str, persona: dict | None,
                      history: list[dict], *, buyer_id: str = "",
                      buyer_nick: str = "", item_id: str = "",
                      item_title: str = "") -> dict:
    """一轮对话结束后更新该客户记忆。返回更新后的记忆 dict。

    history 应包含本轮最新的买家/卖家消息（按时间正序）。
    """
    ensure(chat_id, buyer_id=buyer_id, buyer_nick=buyer_nick,
           item_id=item_id, item_title=item_title,
           persona_name=(persona or {}).get("name", ""),
           persona_id=(persona or {}).get("id"))

    mem = get(chat_id)
    slots: dict = dict(mem.get("slots") or {})
    goal_type = (persona or {}).get("goal_type", "none")

    if goal_type == "collect" and (persona or {}).get("slots"):
        stage = mem.get("goal_stage") or "collecting"
        extracted = _extract_slots(persona, history[-12:])
        for k, v in extracted.items():
            slots[k] = v  # 新值覆盖/补全；已填的保留
        fields = {"slots": slots, "summary": render_summary(persona, slots),
                  "persona_name": persona.get("name", "")}

        if stage != "done":
            filled = all_required_filled(persona, slots)
            if stage == "confirming":
                # 上一轮已请客户确认，看这句是不是在确认无误
                if _detect_confirmation(history):
                    fields["doc"] = _generate_doc(persona, slots, history)
                    fields["goal_done"] = 1
                    fields["goal_stage"] = "done"
                    logger.info(f"[记忆] {chat_id} 客户已确认需求，生成需求文档")
                else:
                    # 客户有补充/更正：留在确认态（已齐）或退回收集态（又缺了）
                    fields["goal_stage"] = "confirming" if filled else "collecting"
            elif filled:
                # 信息刚集齐：进入确认态，下一条 AI 回复会复述请客户确认（先不生成文档）
                fields["goal_stage"] = "confirming"
                logger.info(f"[记忆] {chat_id} 关键信息已集齐，进入复述确认")
        db.upsert_memory(chat_id, **fields)
    else:
        # 答疑型 / 无目标：维护滚动摘要（仅在买家这句有实质内容时更新，省调用）
        last_buyer = _last_buyer(history)
        if len(last_buyer.strip()) >= 6:
            summary = _update_rolling_summary(mem.get("summary", ""), history)
            db.upsert_memory(chat_id, summary=summary,
                             persona_name=(persona or {}).get("name", ""))
        elif last_buyer:
            db.upsert_memory(chat_id, persona_name=(persona or {}).get("name", ""))

    return get(chat_id)
