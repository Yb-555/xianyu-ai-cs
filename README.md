# 闲鱼店铺 AI 智能客服 Agent

> 接管自己闲鱼店铺的卖家工作台，**按商品自动切换 AI 身份**回复买家咨询；
> 每个客户独立记忆、互不串台，支持需求收集、报价策略、商品采集与人工接管。
>
> 技术栈：**Playwright 浏览器自动化 + FastAPI 后端 + LLM（DeepSeek，OpenAI 兼容）+ 多身份 Agent + 客户记忆 + SQLite**

---

## 这是什么

一套面向**个人电商店铺**的 AI 客服自动化系统。它复用你已登录的浏览器接管闲鱼卖家工作台，监听买家消息，结合「商品身份 + 目标流程 + 客户记忆」让大模型生成贴合店铺口吻的回复，并精准回到对应对话框发送。

核心解决三个真实痛点：

1. **多商品身份混乱** —— 不同商品需要不同的卖家口吻和报价，AI 按商品自动切换身份。
2. **多客户串台** —— 多个买家同时咨询时，AI 要记住"谁是谁、各自要什么"，且绝不能回错人。
3. **回复像不像人** —— 自然的回复节奏、营业时段、限流，避免机械感。

> 说明：本项目用于打理**自己的**店铺客服，仅供学习与个人使用，请遵守相关平台规则与法律法规。

---

## 核心能力

| 能力 | 说明 | 关键模块 |
|---|---|---|
| **按商品切换身份** | 按 商品ID / 链接 / 关键词 命中对应"卖家身份"（人设 + 目标 + 报价），**会话级锁定**不中途跳变 | `core/persona.py` |
| **每客户独立记忆** | 槽位抽取、滚动摘要、收尾复述确认，集齐需求自动生成「需求文档」，A/B 客户互不串台 | `core/memory.py` |
| **有状态目标 Agent** | 收集需求型身份按「收集 → 复述确认 → 生成需求文档」状态机驱动，AI 只追问缺失项、集齐即收尾 | `core/persona.py`、`core/memory.py` |
| **话术库 + 向量化** | 历史问答 / 人工话术沉淀为样本，支持文本向量化（embedding，OpenAI 兼容）做话术学习 | `db`、`core/ai_client.py` |
| **AI 自动生成身份** | 贴一个商品链接 → 采集标题/价格/详情 → AI 判断服务类/实物类 → 自动产出人设、目标、槽位草稿 | `core/persona_gen.py`、`core/item_collector.py` |
| **精准不回错人** | 发送前切到对应买家对话框并**回读买家 userId 校验**，多消息**串行处理**，消息 `msgId` 去重 | `worker/im_listener.py` |
| **报价策略** | 按类目配置价格区间 / 低预算话术 / 加售引导，AI 报价更稳 | `core/pricing.py` |
| **人工接管** | 一键暂停某客户的 AI，自己接手聊，AI 只记录不回复 | `api/routes.py` |
| **健壮运行** | Worker 子进程心跳上报 + 掉线自动重启 + 单实例端口锁，避免重复回复 | `core/worker_manager.py` |
| **后台管理** | 原生 JS 单页后台：身份管理、客户记忆、AI 试聊、回复日志、报价策略、Worker 启停 | `web/index.html` |

---

## 技术架构

两个进程，通过同一个 SQLite 共享数据：

```
                ┌──────────────── FastAPI 后端 (app.main) ────────────────┐
  浏览器后台 ──▶ │  /api/*  路由 → core/* 业务逻辑 → SQLite                  │
                │  托管后台单页 index.html，启停 Worker（worker_manager）   │
                └──────────────────────────┬───────────────────────────────┘
                                           │ 同一个 data/xianyu_v2.db
                ┌──────────────────────────┴───────────────────────────────┐
  接管的浏览器 ◀ │  Playwright Worker (scripts.run_worker)                  │
  (CDP 接管)     │  hook WebSocket 解析消息 → 决策(规则→AI→兜底) → 精准发送  │
                └────────────────────────────────────────────────────────────┘
```

**一条消息的处理链路：**

```
买家发消息
 → im_listener 从 WebSocket 帧解析(文本 / cid / itemId / 买家 userId / msgId)
 → 持久化去重(msgId 已入库则跳过)
 → memory 取该会话记忆；persona 解析/沿用身份
 → rules 决策回复(关键词规则 → AI[人设 + 目标 + 报价策略 + 客户记忆] → 兜底)
 → 若该客户被「人工接管」则只记录不回
 → 按买家 userId 切到对应对话框 + 回读校验 → 发送(串行锁)
 → 写 messages / reply_logs，memory 抽槽位 / 更新摘要 / 必要时生成需求文档
```

> 完整目录与数据库表说明见 [ARCHITECTURE.md](ARCHITECTURE.md)；面向使用者的操作手册见 [使用说明书.md](使用说明书.md)。

---

## 技术栈

- **后端**：Python 3.13 · FastAPI · Uvicorn · Pydantic
- **浏览器自动化**：Playwright（CDP 接管 / 持久化两种模式）
- **大模型**：OpenAI 兼容客户端（默认 DeepSeek），改 `base_url` 即可换厂商
- **向量化**：文本 embedding（话术库沉淀，OpenAI 兼容接口）
- **存储**：SQLite（9 张表，含消息去重、客户记忆、审计日志）
- **前端**：原生 HTML/JS 单页后台（无构建）

---

## 快速开始

```powershell
# 1. 安装依赖（首次）
.\setup.bat              # 创建 .venv 并安装 requirements.txt
.venv\Scripts\python.exe -m playwright install chromium

# 2. 配置
copy config.example.yml config.yml    # 填入你的 ai.api_key（DeepSeek）

# 3. 一键启动（起后端 + 自动上线 Worker + 打开后台）
.\start.bat              # 后台地址 http://127.0.0.1:8090

# 收工
.\stop.bat
```

首次运行会拉起一个带调试端口的浏览器，在其中登录一次自己的店铺工作台即可（之后记住登录态）。

---

## 工程亮点（面向阅读源码）

- **真·LLM 应用工程**：不是简单调 API，而是把 **动态 prompt 编排（安全约束 + 人设 + 目标 + 报价 + 记忆）、多身份路由、有状态目标流程、长期记忆** 组合成一个可用系统。
- **状态隔离与一致性**：按会话隔离记忆 + 发送前 userId 回读校验 + 串行锁 + msgId 去重，系统性地解决了"回错人 / 串台 / 重复回复"。
- **可靠性设计**：子进程心跳、掉线自动重启、单实例端口锁，面向"长时间无人值守运行"。
- **可配置与可扩展**：身份 / 报价 / 话术 / 规则全部后台可配、即时生效；LLM 厂商可一行配置切换。

---

## 配置说明（节选）

`config.yml`（含密钥，已 `.gitignore`，请从 `config.example.yml` 复制）：

| 配置 | 说明 |
|---|---|
| `ai.api_key` / `ai.base_url` / `ai.model` | LLM 接入（默认 DeepSeek，OpenAI 兼容） |
| `knowledge.*` | 话术库向量化（embedding）相关配置 |
| `reply_delay.*` | 回复节奏参数（自然延迟） |
| `safety.business_hours` / `daily_reply_limit` | 营业时段 / 每日回复上限（限流） |
| `safety.require_cid` / `verify_sender_uid` | 防串台 / 发送前校验买家身份 |

---

## 许可证

MIT，仅供个人与学习用途。
