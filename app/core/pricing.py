"""报价策略：按产品类目给 AI 注入报价话术（价格区间 / 低预算处理 / 加售引导）。

策略存数据库表 pricing_strategies，后台可改。匹配方式：用策略的 keywords 去命中
「当前消息 + 已锁定身份的名称/关键词 + 商品ID」——因为身份在一段会话里是固定的，
所以一旦对话被识别为某类目，整段对话的报价上下文都稳定，不会因买家某句没提关键词而丢。
低预算话术仅在买家这句提到"预算低/便宜/有限"等时追加。

每个策略可带 clarification_questions，用于指导 AI 在报价前主动了解客户需求。
"""
from __future__ import annotations

from loguru import logger

from app.db import database as db

_BUDGET_LOW_WORDS = ["低", "少", "便宜", "预算有限", "有限", "没多少", "不多", "穷", "划算"]

# 按产品类目的默认需求澄清问题（供无 DB 记录时回落）
_DEFAULT_CLARIFICATION: dict[str, str] = {
    "小程序": "是否需要支付功能？是否需要用户登录/注册？大概需要几个页面？",
    "网页定制": "是静态页面还是需要简单交互？主要展示什么内容？",
    "自动化脚本": "主要想自动化什么操作？数据来源是什么？",
}


def match_strategy(text: str = "", item_id: str = "", persona: dict | None = None) -> dict | None:
    """返回命中的报价策略；未命中返回 None。

    匹配逻辑（按优先级）：
    1. 已锁定身份的 category 字段（最精准）
    2. 已锁定身份的名称匹配策略关键词
    3. 已锁定身份的关键词列表匹配策略关键词
    4. 当前消息文本 + 商品 ID 匹配
    """
    hay_parts = [text or "", item_id or ""]
    persona_name = ""
    if persona:
        # 优先用身份的 category 字段（如果存了的话）
        cat = persona.get("category", "")
        if cat:
            hay_parts.insert(0, cat)
        persona_name = persona.get("name", "")
        hay_parts.append(persona_name)
        hay_parts.extend(persona.get("keywords", []) or [])
    hay = " ".join(hay_parts)
    if not hay.strip():
        return None
    for s in db.list_pricing(active_only=True):
        matched_kw = None
        for kw in (s.get("keywords") or []):
            if kw and kw in hay:
                matched_kw = kw
                break
        if matched_kw:
            logger.debug(
                "[Pricing] 命中策略 | "
                f"category={s.get('category', '')} | "
                f"tier={s.get('tier', '')} | "
                f"price_range={s.get('price_range', '')} | "
                f"locked_persona={persona_name} | "
                f"matched_by={matched_kw}"
            )
            return s
    if persona_name:
        logger.debug(
            "[Pricing] 未命中策略 | "
            f"locked_persona={persona_name} | "
            f"hay_preview={hay[:80]}"
        )
    return None


def _is_low_budget(text: str) -> bool:
    return any(w in (text or "") for w in _BUDGET_LOW_WORDS)


def render(strategy: dict, text: str = "") -> str:
    parts = [(strategy.get("base_prompt") or "").strip()]
    pr = strategy.get("price_range")
    if pr:
        parts.append(f"【参考报价区间】{pr}")
    low_budget = _is_low_budget(text) and strategy.get("low_budget_prompt")
    if low_budget:
        parts.append("【客户预算偏低时】" + strategy["low_budget_prompt"].strip())
    if strategy.get("upselling_prompt"):
        parts.append("【可适当加售】" + strategy["upselling_prompt"].strip())
    # 需求澄清问题：指导 AI 报价前主动了解客户需求
    cq = strategy.get("clarification_questions", "") or _DEFAULT_CLARIFICATION.get(
        strategy.get("category", ""), "")
    has_clarification = bool(cq)
    if cq:
        parts.append("【报价前需了解的关键问题】" + cq)

    # 结构化日志：记录本次渲染的决策信息
    logger.info(
        "[Pricing] 渲染报价块 | "
        f"category={strategy.get('category', '')} | "
        f"tier={strategy.get('tier', '')} | "
        f"price_range={pr or '无'} | "
        f"low_budget_appended={'是' if low_budget else '否'} | "
        f"clarification_injected={'是' if has_clarification else '否'} | "
        f"upselling_injected={'是' if strategy.get('upselling_prompt') else '否'}"
    )

    return "\n".join(p for p in parts if p)


def build_block(text: str = "", item_id: str = "", persona: dict | None = None) -> str:
    """主接口：给 build_system_prompt 用，返回报价话术块（无命中返回空串）。"""
    strategy = match_strategy(text=text, item_id=item_id, persona=persona)
    if not strategy:
        persona_name = (persona or {}).get("name", "")
        logger.debug(
            "[Pricing] build_block 无命中 | "
            f"locked_persona={persona_name} | "
            f"text_preview={text[:60]}"
        )
        return ""
    return render(strategy, text=text)


def get_pricing_prompt(category: str, message: str = "") -> str:
    """按类目获取报价 Prompt（供测试接口 / 外部调用）。

    参数：
        category: 产品类目（如"小程序""网页定制""自动化脚本"）
        message: 当前客户消息（用于检测低预算意图）
    返回：
        完整的报价话术 Prompt 字符串
    """
    strategy = db.get_pricing(category)
    if not strategy:
        # 回落默认策略
        if category in _DEFAULT_CLARIFICATION:
            logger.debug(f"[Pricing] 使用默认策略（无 DB 记录）| category={category}")
        else:
            logger.warning(f"[Pricing] 未找到报价策略 | category={category}")
            return f"未找到类目「{category}」的报价策略"
    return render(strategy, text=message)
