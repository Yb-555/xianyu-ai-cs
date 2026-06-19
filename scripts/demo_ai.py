"""B 路线·一键演示：验证 AI 回复 + 话术学习效果（不碰浏览器）。

用法：
  1) 在 config.yml 填好 DeepSeek 的 api_key
  2) .venv\\Scripts\\python.exe -m scripts.demo_ai
"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")

from app import config
from app.core import ai_client, knowledge, rules
from app.db import database as db

# 示范话术（模拟你过往的真实客服风格）
SAMPLE_QA = [
    ("什么时候发货", "拍下后我当天就安排发货哈，一般48小时内到，急用提前说~"),
    ("可以便宜点吗", "亲这个价格真的很实在啦，诚心要的话给你包个邮~"),
    ("支持七天无理由吗", "支持的，收到不满意7天内退都行，运费我来出~"),
    ("是正品吗", "百分百正品哈，假一赔十，可以放心拍~"),
    ("还有货吗", "有货的亲，现在拍今天就能发~"),
]

# 模拟买家新问题（措辞和样本不完全一样，考验学习效果）
BUYER_MSGS = [
    "啥时候能发货啊",
    "能不能再优惠一点",
    "这个是真的假的呀",
]


def main() -> None:
    cfg = config.load_config(reload=True)
    key = cfg.get("ai", {}).get("api_key", "")
    if not key or key.startswith("sk-xxxx"):
        print("❌ 请先在 config.yml 填入 DeepSeek 的 api_key 再运行。")
        return

    print("=" * 60)
    print("第 1 步：先不学习，直接让 AI 裸答")
    print("=" * 60)
    for msg in BUYER_MSGS:
        reply = ai_client.chat([
            {"role": "system", "content": cfg["ai"]["system_prompt"]},
            {"role": "user", "content": msg},
        ])
        print(f"买家：{msg}\nAI（无话术）：{reply}\n")

    print("=" * 60)
    print("第 2 步：录入店铺话术（话术学习）")
    print("=" * 60)
    for q, a in SAMPLE_QA:
        knowledge.learn(q, a, "manual")
    print(f"已学习 {len(SAMPLE_QA)} 条话术\n")

    print("=" * 60)
    print("第 3 步：再让 AI 回答相同问题（带话术学习 + 完整决策链）")
    print("=" * 60)
    for msg in BUYER_MSGS:
        hits = knowledge.retrieve(msg)
        reply, source = rules.decide_reply(msg)
        ref = "; ".join(f"{h['question']}({h['score']})" for h in hits) or "无"
        print(f"买家：{msg}")
        print(f"AI（学习后）[{source}]：{reply}")
        print(f"  ↳ 参考话术：{ref}\n")


if __name__ == "__main__":
    main()
