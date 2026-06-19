"""根据采集到的商品信息，用 AI 生成一个「身份」配置草稿。

让 DeepSeek 读商品标题/价格/详情，自动判断该用哪种目标：
  - 服务/定制类（代做、定制、设计、开发、剪辑…）→ collect（收集需求型，配关键信息槽位）
  - 实物/虚拟商品类 → answer（答疑型，解答+促成交易，点到为止）
并产出人设文案、目标描述、（收集型的）槽位。结果用于后台「身份管理」预填，人工可改。
"""
from __future__ import annotations

import json
import re

from loguru import logger

from app.core import ai_client

_SYS = """你是闲鱼卖家的运营助手。根据给定的商品信息，为这件商品设计一个用于"自动回复买家"的 AI 身份配置。
判断商品属于哪类并选择目标类型：
- 若是"服务/定制/代做/设计/开发"等需要先了解客户需求才能成交的 → goal_type = "collect"，并给出 3~5 个需要向客户了解的关键信息点(slots)。
- 若是普通实物或虚拟商品，主要是答疑促成交易 → goal_type = "answer"，slots 为空数组。

只输出一个 JSON 对象，字段如下，不要输出任何多余文字或解释：
{
  "name": "身份名称(简短，含商品要点)",
  "persona_text": "人设：你是这件商品的卖家本人，怎么说话、卖什么、注意什么。要求真诚接地气、简洁、自然亲切、不编造价格。",
  "goal_type": "collect 或 answer",
  "goal_text": "这个身份回复时要达成的目标，一句话",
  "slots": [ {"key":"英文key","label":"中文名","desc":"给AI看的说明"} ]
}"""


def _strip_json(text: str) -> str:
    text = re.sub(r"^```(?:json)?", "", text.strip()).strip()
    text = re.sub(r"```$", "", text).strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return m.group(0) if m else text


def generate(item: dict) -> dict:
    """item 至少含 item_id/title，可含 price/description/page_text。返回身份草稿 dict。"""
    title = item.get("title") or f"商品{item.get('item_id','')}"
    price = item.get("price") or ""
    detail = (item.get("page_text") or item.get("description") or "")[:1500]
    user = f"商品标题：{title}\n价格：{price}\n商品详情/页面文本：\n{detail}"

    draft = {
        "name": title[:30],
        "item_ids": [str(item.get("item_id", ""))],
        "keywords": [],
        "url_patterns": [],
        "persona_text": "",
        "goal_type": "answer",
        "goal_text": "",
        "slots": [],
        "priority": 0,
        "enabled": True,
    }
    try:
        raw = ai_client.chat([
            {"role": "system", "content": _SYS},
            {"role": "user", "content": user},
        ])
        data = json.loads(_strip_json(raw))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"AI 生成身份失败，退回基础草稿: {e}")
        draft["persona_text"] = (
            f"你是闲鱼上「{title}」的卖家本人，说话真诚接地气、简洁、自然亲切，不编造价格。"
        )
        draft["goal_text"] = "解答买家关于这件商品的疑问，促成交易，点到为止。"
        draft["_ai_error"] = str(e)
        return draft

    if data.get("name"):
        draft["name"] = str(data["name"])[:30]
    draft["persona_text"] = str(data.get("persona_text") or "").strip()
    gt = str(data.get("goal_type") or "answer").strip()
    draft["goal_type"] = gt if gt in ("collect", "answer", "none") else "answer"
    draft["goal_text"] = str(data.get("goal_text") or "").strip()
    slots = []
    if draft["goal_type"] == "collect":
        for s in (data.get("slots") or []):
            if isinstance(s, dict) and s.get("key"):
                slots.append({"key": str(s["key"]).strip(),
                              "label": str(s.get("label") or s["key"]).strip(),
                              "desc": str(s.get("desc") or "").strip(),
                              "optional": bool(s.get("optional"))})
    draft["slots"] = slots
    return draft
