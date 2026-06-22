# 项目文件架构说明（闲鱼工作台自动化 v2）

> 一句话：Playwright 接管 Edge 监听闲鱼 IM（钉钉明文 WebSocket）→ 按商品切换 AI 身份 →
> 每客户独立记忆 → 精准回到对应对话框发送。FastAPI 后端 + DeepSeek，自带后台管理界面。

## 目录总览

```
D:\xianyu\v2\
├─ app\                     主应用（FastAPI 后端 + Playwright Worker）
│  ├─ main.py               FastAPI 入口：挂 API 路由、托管后台页面、lifespan 收尾
│  ├─ config.py             读 config.yml（缺则回退 config.example.yml），点号取值 get("ai.model")
│  ├─ __init__.py
│  │
│  ├─ api\                  后台 REST API 层
│  │  ├─ routes.py          全部 /api/* 接口：Worker启停 / 人设 / 身份CRUD / 话术 /
│  │  │                     规则 / 客户记忆(查看·编辑·清空·暂停) / 多轮试聊 / 日志 / 概览
│  │  └─ __init__.py
│  │
│  ├─ core\                 业务核心（不碰浏览器，纯逻辑，可单测）
│  │  ├─ ai_client.py       OpenAI 兼容客户端封装（chat / embed），改 base_url 即换厂商
│  │  ├─ persona.py         身份与人设：按 商品ID>链接>关键词 解析身份(会话级锁定)、
│  │  │                     组装 system prompt（人设 + 目标块 + 客户记忆 + 可服务时间）
│  │  ├─ memory.py          每客户独立记忆：槽位抽取、滚动摘要、收尾复述确认、生成需求文档
│  │  ├─ rules.py           回复决策：AI（带身份目标 + 客户记忆）→ 兜底
│  │  ├─ item_collector.py  商品采集：复用已登录 Edge(CDP) 打开商品页抓标题/价格/图片
│  │  ├─ persona_gen.py      AI 按商品信息生成身份草稿（自动判服务类→收集需求/实物→答疑）
│  │  ├─ pricing.py          报价策略：按类目注入价格区间/低预算/加售话术（后台可配）
│  │  ├─ ratelimit.py       安全控制：拟人延迟、每日上限、营业时段
│  │  ├─ worker_manager.py  后台启停 Worker 子进程；自动拉起带调试端口的 Edge
│  │  └─ __init__.py
│  │
│  ├─ worker\               Playwright 浏览器侧（接管 Edge、收发消息）
│  │  ├─ browser.py         BrowserManager：CDP 接管 Edge / 持久化模式，定位工作台页
│  │  ├─ im_listener.py     IM 监听核心：hook WebSocket 解析新消息 → 决策 → 精准切对话框
│  │  │                     发送（串行锁 + 头像userId回读校验防回错人）→ 更新记忆
│  │  ├─ auto_rate.py       自动评价（已停用，保留代码）
│  │  └─ __init__.py
│  │
│  ├─ db\                   数据持久化
│  │  ├─ database.py        SQLite 封装：建表 + 迁移 + 全部增删改查 helper
│  │  └─ __init__.py
│  │
│  └─ web\
│     └─ index.html         后台管理单页（左侧分组导航 + 各功能面板，原生 JS，无构建）
│
├─ scripts\                 可独立运行的脚本
│  ├─ run_worker.py         启动 Worker：接管 Edge → 监听 IM → 自动回复（worker_manager 调它）
│  ├─ demo_ai.py            命令行试 AI 回复的小脚本（调试用）
│  └─ __init__.py
│
├─ start.bat                一键启动：杀残留 → 起后端 → 自动上线 Worker → 开后台页面
├─ stop.bat                 一键停止：杀掉本项目的后端与 Worker
├─ scripts\launcher.ps1     上面两个 bat 的实际逻辑（ASCII，避免中文编码问题）
├─ config.yml               真实配置（含 DeepSeek key，已 .gitignore）
├─ config.example.yml       配置模板（可提交，给别人参考）
├─ requirements.txt         Python 依赖
├─ README.md               快速开始
├─ ARCHITECTURE.md         本文件
│
├─ data\                    运行时生成
│  └─ xianyu_v2.db          SQLite 数据库（全部数据都在这）
├─ logs\                    运行时日志（worker.log 等）
├─ edge_debug\              调试 Edge 的用户数据目录（持久化登录态，运行时生成）
├─ .venv\                   Python 虚拟环境（Python 3.13）
└─ .claude\launch.json      预览/启动配置（给 Claude Code 用）
```

## 两个进程

这是**两个独立进程**，通过同一个 SQLite 数据库共享数据：

1. **FastAPI 后端**（`python -m app.main` → http://127.0.0.1:8090）
   提供后台管理界面 + API；不直接碰浏览器。在「概览」里点按钮启停 Worker。
2. **Playwright Worker**（`python -m scripts.run_worker`）
   常驻，接管 Edge 监听消息、自动回复。由后端的 worker_manager 以子进程拉起，
   也可手动单独跑（注意别和后台重复起，会双重回复）。

## 数据流

**① 自动回复（线上）**
```
买家发消息
 → im_listener 从 WebSocket 帧解析(文本/cid/itemId/买家userId/msgId)
 → 持久化去重(msgId 已入库则跳过)
 → memory 取该会话记忆；persona 解析/沿用身份
 → rules 决策回复(关键词规则 → AI[人设+目标+记忆+话术] → 兜底)
 → 若该客户被「人工接管」则只记录不回
 → 按买家 userId 切到对应对话框 + 回读校验 → 发送(串行锁)
 → 写 messages / reply_logs，memory 抽槽位/更新摘要/必要时生成需求文档
```

**② 后台管理**
```
浏览器打开 index.html
 → 调 /api/* → routes.py → core/* 与 db/database.py
 →（身份管理、客户记忆增删改、多轮试聊、话术、规则、日志、Worker启停）
```

## 数据库表（data/xianyu_v2.db）

| 表 | 作用 |
|---|---|
| `messages` | 所有对话消息（按 chat_id 会话隔离）；含 msg_id 去重、raw_json 原始报文 |
| `customer_memory` | 每客户独立记忆：需求摘要、槽位、目标状态、人工接管、未读/状态 |
| `personas` | AI 身份（命中条件 + 人设 + 目标设定 + 槽位） |
| `qa_pairs` | 话术库（学习样本，存储 + 可向量化 embedding） |
| `rules` | 关键词规则 |
| `reply_logs` | 回复日志（审计） |
| `settings` | 键值设置（默认人设、可服务时间等） |
| `collected_items` | 采集到的商品信息（标题/价格/图片/卖家），可一键导入身份 |
| `pricing_strategies` | 报价策略（按类目：价格区间/基础话术/低预算/加售），后台可配 |

## 配置要点（config.yml）

- `ai.*` — DeepSeek 接口与 key、模型、温度
- `browser.*` — CDP 接管地址、工作台 URL、Edge 路径
- `safety.*` — 拟人延迟、每日上限、营业时段、`require_cid`(抓不到cid不回)、路由兜底
- `knowledge.*` — 话术库向量化（embedding）相关配置
- `persona_routes.*` — 身份切换总开关（具体身份现在在后台「身份管理」里存数据库）
