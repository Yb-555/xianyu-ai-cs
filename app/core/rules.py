"""回复决策：AI（带身份目标 + 客户记忆）→ 兜底。"""
from __future__ import annotations

from loguru import logger

from app import config
from app.core import ai_client, persona

_SENTINEL = object()

# 报价/价格意图关键词
_PRICING_KEYWORDS = ["多少钱", "报价", "预算", "价格", "怎么卖", "什么价", "价位", "多少钱一个",
                     "怎么收费", "收费", "费用", "价目", "价格表", "多少米", "多少r", "多少"]


def contains_pricing_intent(text: str) -> bool:
    """检测消息是否包含报价/价格意图。"""
    t = text.strip().lower()
    matched = any(kw in t for kw in _PRICING_KEYWORDS)
    if matched:
        logger.debug(
            "[Pricing] 检测到报价意图 | "
            f"message_preview={text[:80]}..."
        )
    return matched


def decide_reply(text: str, item_id: str = "", history: list[dict] | None = None,
                 link_url: str = "", memory: dict | None = None,
                 persona_obj: object = _SENTINEL
                 ) -> tuple[str, str]:
    """返回 (回复内容, 来源)。来源 ∈ ai / fallback。

    memory     ：该客户的记忆 dict（避免串台、避免重复提问）。
    persona_obj：已解析好的身份；不传则按 text/item_id/link 解析。

    报价意图检测：
    - 当检测到客户消息包含报价/价格关键词时，
    - 优先使用已锁定身份（persona_obj）的类目来获取报价策略
    - 报价策略 + 需求澄清流程会通过 build_system_prompt 注入到 AI 的 system prompt 中
    - 让 AI 在报价前先主动了解关键需求，再给出合理报价
    """
    # 报价意图检测日志
    if contains_pricing_intent(text):
        locked_name = ""
        if persona_obj is not _SENTINEL and persona_obj:
            locked_name = persona_obj.get("name", "")
        elif memory:
            locked_name = memory.get("persona_name", "")
        logger.info(
            "[Pricing] 触发报价流程 | "
            f"locked_persona={locked_name} | "
            f"item_id={item_id} | "
            f"message_preview={text[:60]}..."
        )

    if config.get("ai.enabled", True):
        try:
            return _ai_reply(text, history or [], item_id=item_id, link_url=link_url,
                             memory=memory, persona_obj=persona_obj), "ai"
        except Exception as e:
            logger.warning(f"AI 回复失败，使用兜底回复: {e}")
    return config.get("fallback_reply", "稍等哈～"), "fallback"


def _ai_reply(text: str, history: list[dict], item_id: str = "", link_url: str = "",
              memory: dict | None = None, persona_obj: object = _SENTINEL) -> str:
    if persona_obj is _SENTINEL:
        persona_obj = persona.resolve_persona(text=text, item_id=item_id, link_url=link_url)
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": persona.build_system_prompt(
                text=text, item_id=item_id, link_url=link_url,
                memory=memory, persona=persona_obj),
        }
    ]
    # 近期会话上下文
    for h in history[-6:]:
        role = "assistant" if h.get("role") in ("seller", "ai") else "user"
        messages.append({"role": role, "content": h.get("content", "")})
    messages.append({"role": "user", "content": text})
    return ai_client.chat(messages)
