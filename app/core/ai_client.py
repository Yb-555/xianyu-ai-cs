"""OpenAI 兼容客户端：chat 生成 + embedding。改 base_url/model 即可切换厂商。"""
from __future__ import annotations

from functools import lru_cache

from openai import OpenAI

from app import config


@lru_cache(maxsize=1)
def _chat_client() -> OpenAI:
    return OpenAI(
        base_url=config.get("ai.base_url"),
        api_key=config.get("ai.api_key"),
    )


@lru_cache(maxsize=1)
def _embed_client() -> OpenAI:
    return OpenAI(
        base_url=config.get("knowledge.embedding_base_url", config.get("ai.base_url")),
        api_key=config.get("knowledge.embedding_api_key", config.get("ai.api_key")),
    )


def chat(messages: list[dict[str, str]]) -> str:
    """messages 为标准 OpenAI 格式 [{role, content}]。"""
    resp = _chat_client().chat.completions.create(
        model=config.get("ai.model"),
        messages=messages,
        temperature=config.get("ai.temperature", 0.6),
        max_tokens=config.get("ai.max_tokens", 400),
    )
    return (resp.choices[0].message.content or "").strip()


def embed(text: str) -> list[float]:
    resp = _embed_client().embeddings.create(
        model=config.get("knowledge.embedding_model"),
        input=text,
    )
    return resp.data[0].embedding
