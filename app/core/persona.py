"""AI 人设、身份路由与目标设定。

- persona（默认人设）：未命中任何身份时用的兜底人设（存 DB settings 表）
- online_status      ：当前可服务/可接单时间，注入到 prompt
- personas（身份表）  ：按 商品ID / 链接 / 关键词 切换 AI 身份，每个身份可带
                        「目标设定」（goal_type=collect 收集需求 / answer 答疑）
                        见 app/db/database.py 的 personas 表与 app/core/memory.py

身份既可在后台「身份管理」里增删改（存 DB），也兼容 config.yml 的
persona_routes.routes（DB 为空时回落）。
"""
from __future__ import annotations

import fnmatch
import re

from loguru import logger

from app import config
from app.db import database as db

# 默认人设：闲鱼个人卖家（兜底人设示例，可在后台「身份管理」中按自己店铺改写）
DEFAULT_PERSONA = """你是这家闲鱼店铺的店主本人，利用空闲时间打理小店。
说话风格：真诚、接地气，像本人在和买家聊天，简洁不啰嗦，适当用语气词和表情（如~、哈、😊），但别太浮夸。
原则：以店主本人口吻自然回复；涉及价格、发货、交付等关键问题，给稳妥说法，拿不准就说“稍等帮你确认下哈”，不要乱承诺。

【回复规则，必须遵守】
1. 买家问价格/多少钱/怎么卖：按商品标价回复，绝对不要编造具体数字、不要说“看市场行情/我按行情来”、不要反问“您要哪种”。
2. 不要主动催单：不要说“方便的话直接下单”“直接下单”这类话，除非买家明确说要拍。
3. 回复尽量短，一两句即可。"""

DEFAULT_ONLINE_STATUS = ""  # 如：今晚8点后可接单 / 现在就能处理哦

URL_RE = re.compile(r"https?://[^\s<>'\"，。；、]+", re.IGNORECASE)
ITEM_ID_PATTERNS = [
    re.compile(r"(?:itemId|item_id|id)[=/](\d{6,})", re.IGNORECASE),
    re.compile(r"(?:itemId|item_id|id)=([0-9]{6,})", re.IGNORECASE),
    re.compile(r"/item/(\d{6,})", re.IGNORECASE),
    re.compile(r"!!(\d{6,})-"),
]

_SENTINEL = object()


def get_persona() -> str:
    return db.get_setting("persona", "") or DEFAULT_PERSONA


def get_online_status() -> str:
    return db.get_setting("online_status", DEFAULT_ONLINE_STATUS)


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def extract_urls(text: str = "") -> list[str]:
    return URL_RE.findall(text or "")


def extract_item_ids(text: str = "") -> list[str]:
    ids: list[str] = []
    for pattern in ITEM_ID_PATTERNS:
        ids.extend(pattern.findall(text or ""))
    return list(dict.fromkeys(ids))


def _match_pattern(pattern: str, value: str) -> bool:
    if not pattern or not value:
        return False
    if pattern.startswith("re:"):
        try:
            return re.search(pattern[3:], value, re.IGNORECASE) is not None
        except re.error:
            return False
    if "*" in pattern:
        return fnmatch.fnmatch(value.lower(), pattern.lower())
    return pattern.lower() in value.lower()


def _route_enabled() -> bool:
    return bool(config.get("persona_routes.enabled", True))


# ---------- 身份来源：DB 优先，回落 config.yml ----------

def _config_routes() -> list[dict]:
    routes = config.get("persona_routes.routes", []) or []
    out = []
    for r in routes:
        if not isinstance(r, dict):
            continue
        out.append({
            "id": None,
            "name": r.get("name") or r.get("label") or "custom",
            "item_ids": _as_list(r.get("item_ids") or r.get("item_id")),
            "url_patterns": _as_list(r.get("url_patterns") or r.get("urls")),
            "keywords": _as_list(r.get("keywords")),
            "persona_text": r.get("persona") or "",
            "goal_type": r.get("goal_type", "none"),
            "goal_text": r.get("goal_text") or r.get("note") or r.get("prompt") or "",
            "slots": r.get("slots") or [],
        })
    return out


def get_personas() -> list[dict]:
    """所有启用的身份（DB 优先；DB 无身份时回落 config.yml）。"""
    rows = db.list_personas(enabled_only=True)
    if rows:
        return [{
            "id": r["id"],
            "name": r["name"],
            "item_ids": r.get("item_ids") or [],
            "url_patterns": r.get("url_patterns") or [],
            "keywords": r.get("keywords") or [],
            "persona_text": r.get("persona_text") or "",
            "goal_type": r.get("goal_type") or "none",
            "goal_text": r.get("goal_text") or "",
            "slots": r.get("slots") or [],
        } for r in rows]
    return _config_routes()


def resolve_persona(text: str = "", item_id: str = "", link_url: str = "") -> dict | None:
    """返回当前消息命中的身份；靠前/高优先级的身份优先。未命中返回 None。"""
    if not _route_enabled():
        return None

    urls = extract_urls(text)
    if link_url:
        urls.insert(0, link_url)

    item_ids = []
    if item_id:
        item_ids.append(str(item_id))
    for value in [text, link_url, *urls]:
        item_ids.extend(extract_item_ids(value))
    item_ids = list(dict.fromkeys(i for i in item_ids if i))

    for persona in get_personas():
        p_item_ids = set(_as_list(persona.get("item_ids")))
        if p_item_ids and p_item_ids.intersection(item_ids):
            return persona

        patterns = _as_list(persona.get("url_patterns"))
        if patterns and any(_match_pattern(p, u) for p in patterns for u in urls):
            return persona

        keywords = _as_list(persona.get("keywords"))
        if keywords and any(k in text for k in keywords):
            return persona

    return None


def get_persona_by_id(pid: int) -> dict | None:
    for persona in get_personas():
        if persona.get("id") == pid:
            return persona
    return None


def resolve_for_conversation(mem: dict | None, text: str = "",
                             item_id: str = "", link_url: str = "") -> dict | None:
    """会话级身份解析：一旦某会话锁定了身份（记忆里有 persona_id），后续整轮沿用，
    不再因为某条消息恰好含别的商品关键词而中途跳变。未锁定时才重新解析。"""
    locked = (mem or {}).get("persona_id")
    if locked:
        p = get_persona_by_id(int(locked))
        if p:
            return p
    return resolve_persona(text=text, item_id=item_id, link_url=link_url)


# 向后兼容旧名字
def get_persona_routes() -> list[dict]:
    return get_personas()


def resolve_route(text: str = "", item_id: str = "", link_url: str = "") -> dict | None:
    return resolve_persona(text=text, item_id=item_id, link_url=link_url)


def route_label(persona: dict | None) -> str:
    if not persona:
        return "default"
    return str(persona.get("name") or "custom").strip() or "custom"


# ---------- 目标块 ----------

def _goal_block(persona: dict, memory: dict | None) -> str:
    goal_type = persona.get("goal_type", "none")
    goal_text = (persona.get("goal_text") or "").strip()
    slots = persona.get("slots") or []
    mem_slots = (memory or {}).get("slots") or {}
    goal_done = bool((memory or {}).get("goal_done"))

    if goal_type == "collect" and slots:
        stage = (memory or {}).get("goal_stage") or "collecting"
        missing = [s.get("label", s.get("key")) for s in slots
                   if not s.get("optional") and not mem_slots.get(s.get("key"))]
        lines = [f"【你的目标】{goal_text}" if goal_text else "【你的目标】了解清楚客户的需求。"]

        if goal_done or stage == "done":
            lines.append(
                "客户的需求已确认无误。不要再追问需求细节，"
                "告诉客户“信息我都记下了，这边帮你安排/制作，有进展同步你”，礼貌收尾即可。"
            )
        elif stage == "confirming" or not missing:
            # 信息看似已齐 → 先复述让客户确认，确认前不要说“开始安排”
            known = "；".join(
                f"{s.get('label', s.get('key'))}：{mem_slots.get(s.get('key'))}"
                for s in slots if mem_slots.get(s.get("key")))
            lines.append(
                "你了解到的客户需求如下：" + (known or "（见上）") + "。\n"
                "请用一两句话把上面这些需求复述给客户，问他“我这样理解对吗？还有要补充的吗”，"
                "请客户确认。在客户明确确认之前，先不要说“马上开始安排/制作”。"
            )
        else:
            lines.append("你需要从客户那里了解以下关键信息（只问还没问到的，问到的别再重复问，一次别问太多，点到为止）：")
            for s in slots:
                val = mem_slots.get(s.get("key"))
                tag = "（可选）" if s.get("optional") else ""
                status = f"已知：{val}" if val else "未知"
                lines.append(f"- {s.get('label', s.get('key'))}{tag}：{status}")
            lines.append(f"目前还缺：{'、'.join(missing)}。请围绕这些自然地引导客户补充，别一次全问。")
        return "\n".join(lines)

    if goal_type == "answer":
        base = goal_text or "解答客户疑惑，并告知可进行的时间，点到为止，不要主动延伸过多问题。"
        return f"【你的目标】{base}"

    return f"【你的目标】{goal_text}" if goal_text else ""


# ---- 报价与需求澄清流程模板（所有 Persona 共用） ----

PRICING_CLARIFICATION_TEMPLATE = """【报价与需求澄清流程 - 必须严格遵守】

当客户询问价格、预算或提到"多少钱""报价""预算""怎么卖""什么价"等词时：
1. 如果当前对话中客户的核心需求还不够清晰（例如没说具体功能），请先简要问 1-2 个关键问题。
   - 小程序类：问是否需要支付、登录、后台管理等核心功能。
   - 网页定制类：问页面类型和主要功能。
   - 自动化脚本类：问具体要自动化什么操作。
2. 了解清楚后再给出报价。
3. 报价时必须说明价格区间 + 包含的内容，并可以提供「基础版」和「进阶版」两个方案供客户选择。
4. 语气务实、专业、诚实，不要为了成交而过度承诺。

注意：如果客户已经说清楚了需求，或者你已经在之前的对话中了解过，不要重复提问，直接报价即可。"""

# ---- 安全与合规最高优先级约束（所有 Persona 共用，始终在最前面） ----

SAFETY_TEMPLATE = """【安全与合规最高优先级约束 - 必须严格遵守】

你是一个严格遵守闲鱼平台规则的专业客服，任何回复都必须符合以下要求：

一、严禁生成以下内容（违反即为严重错误）：
- 虚假宣传、夸大效果、承诺"包过""必中""保证"等无法兑现的内容
- 引导用户进行线下交易、加微信、加QQ、转账、支付宝私下支付等绕过平台的行为
- 涉及政治、宗教、色情、暴力、毒品、赌博、诈骗等违法或敏感话题
- 教唆、诱导或协助用户进行刷单、刷好评、虚假交易、刷流量等违规行为
- 使用攻击性、辱骂性、歧视性语言，或对用户进行人身攻击
- 泄露他人隐私、散布谣言或虚假信息
- 发布或诱导发布违反法律法规的内容

二、遇到违规或高风险请求时的处理方式：
1. 必须礼貌但坚定地拒绝
2. 简要说明"根据平台规则无法提供该服务"
3. 尝试把话题引导回正常交易范围内
4. 避免与用户争论或解释过多平台规则细节

三、报价与承诺原则：
- 报价必须诚实、合理，不得为了成交而报明显不合理或无法履行的价格
- 不得过度承诺功能、交付时间、售后服务
- 可以提供基础版和进阶版两个方案，但必须明确说明各自包含的内容
- 当客户预算明显过低且无法满足其需求时，应诚实说明，而不是勉强接单

四、整体回复风格要求：
- 专业、诚实、克制、友好
- 避免过度热情或使用销售话术
- 遇到模糊需求时，优先通过提问来澄清，而不是直接给出解决方案或报价
- 保持客观，不对竞品进行贬低或负面评价

五、违规后果提醒：
如果你生成了违规内容，可能会导致账号被限流、封禁或承担法律责任。请将合规性放在所有回复的最高优先级。"""


def build_system_prompt(text: str = "", item_id: str = "", link_url: str = "",
                        memory: dict | None = None, persona: object = _SENTINEL) -> str:
    """组装最终注入 AI 的 system prompt。

    persona 可显式传入已解析好的身份（避免重复解析）；不传则按 text/item_id/link 解析。
    memory 为该客户的记忆 dict（含 summary/slots/goal_done），用于避免串台、避免重复提问。
    """
    if persona is _SENTINEL:
        persona = resolve_persona(text=text, item_id=item_id, link_url=link_url)
    persona = persona or None

    persona_text = (persona or {}).get("persona_text")
    parts = [str(persona_text).strip() if persona_text else get_persona()]

    # 安全合规约束始终在最前面
    parts.insert(0, SAFETY_TEMPLATE)
    logger.debug(
        "[Persona] 注入安全合规约束模板 | "
        f"persona={(persona or {}).get('name', 'default')}"
    )

    goal = _goal_block(persona, memory) if persona else ""
    if goal:
        parts.append(goal)

    # 报价策略：按产品类目注入价格区间/低预算/加售话术（后台 pricing 表可配）
    from app.core import pricing
    price_block = pricing.build_block(text=text, item_id=item_id, persona=persona)
    if price_block:
        parts.append("【报价策略】\n" + price_block)
        # 有报价策略时，同时注入报价澄清流程模板，指导 AI 如何主动了解需求后再报价
        parts.append(PRICING_CLARIFICATION_TEMPLATE)
        persona_name = (persona or {}).get("name", "default")
        logger.debug(
            "[Persona] 注入报价策略 + 澄清流程模板 | "
            f"persona={persona_name} | "
            f"block_len={len(price_block)}"
        )

    # 客户记忆摘要：让 AI 记住这位客户此前说过的，且只针对这位客户
    # 注意 .get(k, "") 在值为 None(DB NULL) 时返回 None，必须再 or "" 兜底，否则 .strip() 崩
    summary = ((memory or {}).get("summary") or "").strip()
    if summary:
        parts.append(
            "【这位客户的已知情况（仅针对当前这位客户，不要套用到别人身上）】\n" + summary
        )
    return "\n\n".join(parts)
