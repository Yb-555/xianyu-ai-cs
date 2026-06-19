"""SQLite 封装。表：messages（会话）、qa_pairs（话术库/学习样本）、
rules（关键词规则）、reply_logs（回复日志）。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = ROOT / "data" / "xianyu_v2.db"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL;")
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     TEXT,
            item_id     TEXT,
            item_title  TEXT,
            role        TEXT,          -- buyer / seller / ai
            content     TEXT,
            msg_id      TEXT,          -- 平台 messageId，用于跨重启持久化去重
            raw_json    TEXT,          -- 原始 WS 报文，排障/审计用
            created_at  REAL
        );

        CREATE TABLE IF NOT EXISTS qa_pairs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            question    TEXT,
            answer      TEXT,
            source      TEXT,          -- auto / manual / correction
            item_id     TEXT,
            embedding   TEXT,          -- JSON 数组
            adopted     INTEGER DEFAULT 0,  -- 被采纳次数
            created_at  REAL
        );

        CREATE TABLE IF NOT EXISTS rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scope       TEXT,          -- global / item
            item_id     TEXT,
            keyword     TEXT,
            match_type  TEXT DEFAULT 'contains',  -- contains / equals / regex
            reply       TEXT,
            priority    INTEGER DEFAULT 0,
            enabled     INTEGER DEFAULT 1,
            created_at  REAL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key         TEXT PRIMARY KEY,
            value       TEXT,
            updated_at  REAL
        );

        CREATE TABLE IF NOT EXISTS reply_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     TEXT,
            buyer_msg   TEXT,
            reply       TEXT,
            source      TEXT,          -- rule / ai / fallback
            sent        INTEGER DEFAULT 0,
            created_at  REAL
        );

        -- AI 身份（按商品切换的人设 + 目标设定）
        CREATE TABLE IF NOT EXISTS personas (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT,
            item_ids      TEXT,          -- JSON 数组：命中的商品 ID
            url_patterns  TEXT,          -- JSON 数组：命中的链接/通配/re:
            keywords      TEXT,          -- JSON 数组：命中的关键词
            persona_text  TEXT,          -- 该身份的人设（system prompt 主体）
            goal_type     TEXT DEFAULT 'none',   -- collect(收集需求) / answer(答疑) / none
            goal_text     TEXT,          -- 目标的自然语言描述
            slots         TEXT,          -- JSON 数组 [{key,label,desc,optional}]，仅 collect 用
            priority      INTEGER DEFAULT 0,
            enabled       INTEGER DEFAULT 1,
            created_at    REAL
        );

        -- 每个客户（按会话 chat_id）的独立记忆模块
        CREATE TABLE IF NOT EXISTS customer_memory (
            chat_id       TEXT PRIMARY KEY,
            buyer_id      TEXT,
            buyer_nick    TEXT,
            item_id       TEXT,
            item_title    TEXT,
            persona_id    INTEGER,        -- 锁定的身份 id（一次确定后整轮沿用，不再中途跳变）
            persona_name  TEXT,
            summary       TEXT,          -- AI 维护的客户需求摘要（滚动更新）
            slots         TEXT,          -- JSON 对象 {slot_key: 值}，已收集的关键信息
            goal_done     INTEGER DEFAULT 0,
            goal_stage    TEXT DEFAULT 'collecting',  -- collecting / confirming / done
            doc           TEXT,          -- 收集齐+客户确认后生成的需求总结文档
            ai_paused     INTEGER DEFAULT 0,          -- 1=人工接管，AI 暂停自动回复
            status        TEXT DEFAULT 'active',      -- active / closed / archived
            unread_count  INTEGER DEFAULT 0,          -- 未回复的买家消息数
            last_message_at REAL,                     -- 最近一条消息时间
            updated_at    REAL,
            created_at    REAL
        );

        -- 报价策略（按产品类目，后台可配）
        CREATE TABLE IF NOT EXISTS pricing_strategies (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            category          TEXT UNIQUE NOT NULL,
            keywords          TEXT,          -- JSON 数组：命中关键词（也会和身份名匹配）
            price_range       TEXT,
            tier              TEXT DEFAULT 'low_price',
            base_prompt       TEXT,
            low_budget_prompt TEXT,
            upselling_prompt  TEXT,
            clarification_questions TEXT,   -- 报价前需了解的关键问题
            enabled           INTEGER DEFAULT 1,
            updated_at        REAL,
            created_at        REAL
        );

        -- 采集到的商品信息（用于快速配置身份）
        CREATE TABLE IF NOT EXISTS collected_items (
            item_id      TEXT PRIMARY KEY,
            title        TEXT,
            price        TEXT,
            description  TEXT,
            seller_nick  TEXT,
            images       TEXT,          -- JSON 数组
            url          TEXT,
            error        TEXT,
            collected_at REAL
        );
        """
    )
    conn.commit()
    _migrate(conn)
    _seed_personas(conn)
    _seed_pricing(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """给早期已建好的表补列（SQLite ADD COLUMN 幂等不可用，需先查）。"""
    cm = {r[1] for r in conn.execute("PRAGMA table_info(customer_memory)")}
    cm_adds = {
        "persona_id": "INTEGER",
        "goal_stage": "TEXT DEFAULT 'collecting'",
        "ai_paused": "INTEGER DEFAULT 0",
        "status": "TEXT DEFAULT 'active'",
        "unread_count": "INTEGER DEFAULT 0",
        "last_message_at": "REAL",
    }
    for col, ddl in cm_adds.items():
        if col not in cm:
            conn.execute(f"ALTER TABLE customer_memory ADD COLUMN {col} {ddl}")

    msg = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    for col, ddl in {"msg_id": "TEXT", "raw_json": "TEXT"}.items():
        if col not in msg:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_msg_id ON messages(msg_id)")

    # 报价策略表补列
    pricing_cols = {r[1] for r in conn.execute("PRAGMA table_info(pricing_strategies)")}
    if "clarification_questions" not in pricing_cols:
        conn.execute("ALTER TABLE pricing_strategies ADD COLUMN clarification_questions TEXT")

    conn.commit()


# 首次初始化时塞两个示例身份，便于直观看到「按商品切换身份 + 目标设定」如何工作
_SEED_PERSONAS = [
    {
        "name": "网页代做",
        "keywords": ["网页", "网站", "建站", "代做", "前端", "页面"],
        "persona_text": (
            "你是闲鱼上承接网页/网站代做的开发者本人。说话专业、靠谱、简洁，"
            "像本人和客户沟通需求，不啰嗦。"
        ),
        "goal_type": "collect",
        "goal_text": (
            "了解客户要做什么项目、用来干什么、对网站样式有什么要求、还有没有自己的想法。"
            "掌握到关键信息就点到为止，不用再过多追问。信息齐了就告诉客户你会据此安排制作。"
        ),
        "slots": [
            {"key": "project", "label": "项目是什么", "desc": "客户想做的网站/网页类型，如企业官网、作品集、落地页、商城等"},
            {"key": "purpose", "label": "用途", "desc": "这个网站用来干什么、给谁看、要达成什么目的"},
            {"key": "style", "label": "样式要求", "desc": "对网站风格/配色/参考站/排版的要求"},
            {"key": "ideas", "label": "客户自己的想法", "desc": "客户额外的想法、功能点或特别要求", "optional": True},
        ],
    },
    {
        "name": "数码配件答疑",
        "keywords": ["数码", "配件", "正品", "规格", "发货", "现货"],
        "persona_text": (
            "你是闲鱼上卖数码配件的店主本人，说话真诚接地气、简洁。"
            "按商品标价回复，不要编造价格数字。"
        ),
        "goal_type": "answer",
        "goal_text": (
            "解答客户关于规格、正品、发货时效等疑惑，点到为止，"
            "不要主动延伸过多问题，也不要乱承诺。"
        ),
        "slots": [],
    },
]


def _seed_personas(conn: sqlite3.Connection) -> None:
    cur = conn.execute("SELECT COUNT(*) FROM personas")
    if cur.fetchone()[0] > 0:
        return
    now = time.time()
    for i, p in enumerate(_SEED_PERSONAS):
        conn.execute(
            "INSERT INTO personas(name,item_ids,url_patterns,keywords,persona_text,"
            "goal_type,goal_text,slots,priority,enabled,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,1,?)",
            (p["name"], json.dumps([]), json.dumps([]),
             json.dumps(p.get("keywords", [])), p["persona_text"],
             p["goal_type"], p["goal_text"], json.dumps(p.get("slots", [])),
             len(_SEED_PERSONAS) - i, now),
        )
    conn.commit()


_SEED_PRICING = [
    {
        "category": "小程序",
        "keywords": ["小程序", "微信小程序", "公众号", "uniapp", "taro"],
        "price_range": "50-300元（简单需求）",
        "tier": "low_price",
        "base_prompt": "你当前处理的是小程序开发需求，采用低价快速交付模式。\n\n报价原则：\n- 适合简单需求：基础页面展示、简单表单、少量接口对接（1-3个）。\n- 报价严格控制在 50-300 元区间。\n- 必须先了解客户核心功能，再给出报价。\n- 可以提供「基础版」和「进阶版」两个方案供客户选择。\n- 语气要务实、专业，避免过度承诺功能和售后。",
        "low_budget_prompt": "如果客户预算非常有限（尤其是 100 元以下），要诚实说明这个预算能做的功能非常有限。\n建议先做一个最小可行版本（MVP），或者礼貌说明可能无法满足其全部需求。",
        "upselling_prompt": "如果客户需求较多，可以温和建议「先做一个核心功能版本，后续再迭代」，并说明进阶版本的价格区间。",
        "clarification_questions": "是否需要支付功能？是否需要用户登录/注册？大概需要几个页面？",
    },
    {
        "category": "网页定制",
        "keywords": ["网页", "网站", "建站", "页面", "前端", "h5", "落地页", "代做"],
        "price_range": "8.8元起",
        "tier": "low_price",
        "base_prompt": "你当前处理的是网页定制需求，属于极低价快速交付模式。\n\n报价原则：\n- 适合简单静态页面、基础修改、简单功能定制。\n- 报价从 8.8 元起，根据复杂度合理报价。\n- 必须严格控制交付范围，复杂交互或后台功能要说明需要增加预算。\n- 语气简洁高效，突出「快速交付」。",
        "low_budget_prompt": "当客户预算极低时，要明确告知这个价格能做的内容有限。\n如果需求过于复杂，建议客户简化需求或增加预算。",
        "upselling_prompt": "可以根据客户需求，推荐从基础版开始，后续再升级更复杂的功能。",
        "clarification_questions": "是静态页面还是需要简单交互？主要展示什么内容？",
    },
    {
        "category": "自动化脚本",
        "keywords": ["脚本", "自动化", "爬虫", "采集", "按键", "自动"],
        "price_range": "30元起",
        "tier": "low_price",
        "base_prompt": "你当前处理的是自动化脚本需求，采用低价快速交付模式。\n\n报价原则：\n- 适合简单重复操作、数据处理、基础爬虫、简单自动化任务。\n- 报价从 30 元起，根据复杂度合理报价。\n- 必须明确说明脚本的适用范围和限制条件。\n- 语气专业且务实。",
        "low_budget_prompt": "如果客户预算很低，要说明简单脚本和复杂脚本的价格差异。\n过于复杂的自动化需求建议增加预算或分阶段实现。",
        "upselling_prompt": "可以建议客户先做一个核心功能的脚本，后续再扩展其他功能。",
        "clarification_questions": "主要想自动化什么操作？数据来源是什么？",
    },
]


def _seed_pricing(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM pricing_strategies").fetchone()[0] > 0:
        return
    now = time.time()
    for p in _SEED_PRICING:
        conn.execute(
            "INSERT INTO pricing_strategies(category,keywords,price_range,tier,"
            "base_prompt,low_budget_prompt,upselling_prompt,clarification_questions,"
            "enabled,updated_at,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,1,?,?)",
            (p["category"], json.dumps(p["keywords"], ensure_ascii=False),
             p["price_range"], p["tier"], p["base_prompt"], p["low_budget_prompt"],
             p["upselling_prompt"], p.get("clarification_questions", ""), now, now),
        )
    conn.commit()


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _lock:
        cur = _connect().execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def execute(sql: str, params: tuple = ()) -> int:
    with _lock:
        conn = _connect()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid or cur.rowcount


# ---- 便捷方法 ----

def add_message(chat_id: str, role: str, content: str,
                item_id: str = "", item_title: str = "",
                msg_id: str = "", raw_json: str = "") -> int:
    return execute(
        "INSERT INTO messages(chat_id,item_id,item_title,role,content,msg_id,raw_json,created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (chat_id, item_id, item_title, role, content, msg_id, raw_json, time.time()),
    )


def message_exists(msg_id: str) -> bool:
    """平台 messageId 是否已入库（跨 worker 重启的持久化去重）。"""
    if not msg_id:
        return False
    return bool(query("SELECT 1 FROM messages WHERE msg_id=? LIMIT 1", (msg_id,)))


def add_qa(question: str, answer: str, source: str,
           embedding: list[float] | None = None, item_id: str = "") -> int:
    return execute(
        "INSERT INTO qa_pairs(question,answer,source,item_id,embedding,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (question, answer, source, item_id,
         json.dumps(embedding) if embedding else None, time.time()),
    )


def all_qa_with_embeddings() -> list[dict[str, Any]]:
    rows = query("SELECT id,question,answer,embedding,adopted FROM qa_pairs"
                 " WHERE embedding IS NOT NULL")
    for r in rows:
        r["embedding"] = json.loads(r["embedding"])
    return rows


def get_setting(key: str, default: str = "") -> str:
    rows = query("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0]["value"] if rows else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, time.time()),
    )


def set_worker_heartbeat() -> None:
    """Worker 进程定期调用，写入最近心跳时间戳。"""
    set_setting("worker_heartbeat", str(time.time()))


def get_worker_heartbeat_age() -> float | None:
    """距上次心跳过去了多少秒；从未心跳返回 None。"""
    ts = get_setting("worker_heartbeat", "")
    try:
        return time.time() - float(ts) if ts else None
    except ValueError:
        return None


def log_reply(chat_id: str, buyer_msg: str, reply: str, source: str, sent: bool) -> int:
    return execute(
        "INSERT INTO reply_logs(chat_id,buyer_msg,reply,source,sent,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (chat_id, buyer_msg, reply, source, int(sent), time.time()),
    )


# ---- 身份（personas） ----

def _loads(value, default):
    try:
        return json.loads(value) if value else default
    except Exception:
        return default


def list_personas(enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM personas"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY priority DESC, id ASC"
    rows = query(sql)
    for r in rows:
        r["item_ids"] = _loads(r.get("item_ids"), [])
        r["url_patterns"] = _loads(r.get("url_patterns"), [])
        r["keywords"] = _loads(r.get("keywords"), [])
        r["slots"] = _loads(r.get("slots"), [])
    return rows


def get_persona_row(pid: int) -> dict[str, Any] | None:
    rows = list_personas()
    for r in rows:
        if r["id"] == pid:
            return r
    return None


def upsert_persona(data: dict[str, Any]) -> int:
    fields = (
        data.get("name", "").strip(),
        json.dumps(data.get("item_ids", [])),
        json.dumps(data.get("url_patterns", [])),
        json.dumps(data.get("keywords", [])),
        data.get("persona_text", ""),
        data.get("goal_type", "none"),
        data.get("goal_text", ""),
        json.dumps(data.get("slots", [])),
        int(data.get("priority", 0)),
        int(bool(data.get("enabled", True))),
    )
    pid = data.get("id")
    if pid:
        execute(
            "UPDATE personas SET name=?,item_ids=?,url_patterns=?,keywords=?,persona_text=?,"
            "goal_type=?,goal_text=?,slots=?,priority=?,enabled=? WHERE id=?",
            (*fields, int(pid)),
        )
        return int(pid)
    return execute(
        "INSERT INTO personas(name,item_ids,url_patterns,keywords,persona_text,"
        "goal_type,goal_text,slots,priority,enabled,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (*fields, time.time()),
    )


def delete_persona(pid: int) -> None:
    execute("DELETE FROM personas WHERE id=?", (pid,))


# ---- 客户记忆（customer_memory） ----

def get_memory(chat_id: str) -> dict[str, Any] | None:
    rows = query("SELECT * FROM customer_memory WHERE chat_id=?", (chat_id,))
    if not rows:
        return None
    r = rows[0]
    r["slots"] = _loads(r.get("slots"), {})
    return r


def list_memories(limit: int = 200) -> list[dict[str, Any]]:
    rows = query(
        "SELECT chat_id,buyer_id,buyer_nick,item_id,item_title,persona_name,"
        "summary,goal_done,ai_paused,status,unread_count,last_message_at,updated_at"
        " FROM customer_memory ORDER BY COALESCE(last_message_at,updated_at) DESC LIMIT ?",
        (limit,))
    return rows


def set_pause(chat_id: str, paused: bool) -> None:
    """人工接管：暂停/恢复某客户的 AI 自动回复。"""
    upsert_memory(chat_id, ai_paused=int(bool(paused)))


def bump_unread(chat_id: str) -> None:
    """收到买家消息：未读 +1，刷新最近消息时间。"""
    now = time.time()
    if query("SELECT 1 FROM customer_memory WHERE chat_id=? LIMIT 1", (chat_id,)):
        execute("UPDATE customer_memory SET unread_count=unread_count+1,"
                "last_message_at=?,updated_at=? WHERE chat_id=?", (now, now, chat_id))
    else:
        upsert_memory(chat_id, unread_count=1, last_message_at=now)


def mark_replied(chat_id: str) -> None:
    """已自动回复：清零未读，刷新最近消息时间。"""
    upsert_memory(chat_id, unread_count=0, last_message_at=time.time())


def upsert_memory(chat_id: str, **fields: Any) -> None:
    now = time.time()
    existing = query("SELECT chat_id FROM customer_memory WHERE chat_id=?", (chat_id,))
    if "slots" in fields and not isinstance(fields["slots"], str):
        fields["slots"] = json.dumps(fields["slots"], ensure_ascii=False)
    if not existing:
        cols = ["chat_id", "created_at", "updated_at", *fields.keys()]
        vals = [chat_id, now, now, *fields.values()]
        placeholders = ",".join("?" for _ in cols)
        execute(f"INSERT INTO customer_memory({','.join(cols)}) VALUES({placeholders})",
                tuple(vals))
    else:
        sets = ",".join(f"{k}=?" for k in fields)
        sets = f"{sets},updated_at=?" if sets else "updated_at=?"
        execute(f"UPDATE customer_memory SET {sets} WHERE chat_id=?",
                (*fields.values(), now, chat_id))


def delete_memory(chat_id: str) -> None:
    execute("DELETE FROM customer_memory WHERE chat_id=?", (chat_id,))


# ---- 采集商品（collected_items） ----

def upsert_collected_item(info: dict[str, Any]) -> None:
    images = info.get("images") or []
    execute(
        "INSERT INTO collected_items(item_id,title,price,description,seller_nick,"
        "images,url,error,collected_at) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(item_id) DO UPDATE SET title=excluded.title,price=excluded.price,"
        "description=excluded.description,seller_nick=excluded.seller_nick,"
        "images=excluded.images,url=excluded.url,error=excluded.error,"
        "collected_at=excluded.collected_at",
        (str(info.get("item_id", "")), info.get("title", ""), info.get("price", ""),
         info.get("description", ""), info.get("seller_nick", ""),
         json.dumps(images, ensure_ascii=False), info.get("url", ""),
         info.get("error", ""), info.get("collected_at") or time.time()),
    )


def get_collected_item(item_id: str) -> dict[str, Any] | None:
    rows = query("SELECT * FROM collected_items WHERE item_id=?", (item_id,))
    if not rows:
        return None
    r = rows[0]
    r["images"] = _loads(r.get("images"), [])
    return r


# ---- 报价策略（pricing_strategies） ----

def list_pricing(active_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM pricing_strategies"
    if active_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY category"
    rows = query(sql)
    for r in rows:
        r["keywords"] = _loads(r.get("keywords"), [])
    return rows


def get_pricing(category: str) -> dict[str, Any] | None:
    rows = query("SELECT * FROM pricing_strategies WHERE category=?", (category,))
    if not rows:
        return None
    r = rows[0]
    r["keywords"] = _loads(r.get("keywords"), [])
    return r


def upsert_pricing(data: dict[str, Any]) -> None:
    now = time.time()
    execute(
        "INSERT INTO pricing_strategies(category,keywords,price_range,tier,base_prompt,"
        "low_budget_prompt,upselling_prompt,clarification_questions,enabled,updated_at,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(category) DO UPDATE SET keywords=excluded.keywords,"
        "price_range=excluded.price_range,tier=excluded.tier,base_prompt=excluded.base_prompt,"
        "low_budget_prompt=excluded.low_budget_prompt,upselling_prompt=excluded.upselling_prompt,"
        "clarification_questions=excluded.clarification_questions,"
        "enabled=excluded.enabled,updated_at=excluded.updated_at",
        (data.get("category", "").strip(),
         json.dumps(data.get("keywords", []), ensure_ascii=False),
         data.get("price_range", ""), data.get("tier", "low_price"),
         data.get("base_prompt", ""), data.get("low_budget_prompt", ""),
         data.get("upselling_prompt", ""), data.get("clarification_questions", ""),
         int(bool(data.get("enabled", True))), now, now),
    )


def delete_pricing(category: str) -> None:
    execute("DELETE FROM pricing_strategies WHERE category=?", (category,))


def list_collected_items(limit: int = 100) -> list[dict[str, Any]]:
    rows = query("SELECT * FROM collected_items ORDER BY collected_at DESC LIMIT ?", (limit,))
    for r in rows:
        r["images"] = _loads(r.get("images"), [])
    return rows
