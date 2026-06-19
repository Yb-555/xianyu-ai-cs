"""IM 监听与自动回复（基于实地调研结果）。

闲鱼 IM 底层是钉钉 IMPaaS（明文 JSON over WebSocket），无加密。

检测：hook WebSocket，解析 lwp=/s/sync 的 syncPushPackage。
  - data[].objectType == 40000 即「文本消息」
  - data[].data 是 base64（钉钉自定义二进制，内嵌可读 JSON）
  - 从中提取：文本、senderUserId、会话 cid、itemId、messageId、买家昵称
  - 自己（卖家）的 userId 从任意帧的 headers.reg-uid 自动识别，用于过滤自己发的消息

发送：Playwright 操作 DOM。
  - 输入框：textarea[placeholder*="请输入消息"]
  - 发送：placeholder 说明「按 Enter 发送」，直接按回车
  - 路由：PoC 默认回复当前打开的会话；多会话路由按昵称点击会话项（见 reply_to_conversation）
"""
from __future__ import annotations

import asyncio
import base64
import json
import re

from loguru import logger
from playwright.async_api import Page

from app import config
from app.core import item_collector, memory, persona, ratelimit, rules
from app.db import database as db
from app.worker.browser import BrowserManager

# ---- DOM 选择器（调研确认；用 class 前缀匹配，扛得住哈希变化）----
SEL_INPUT = 'textarea[placeholder*="请输入消息"]'
SEL_CONV_ITEM = '[class*="conversation-item"]'
SEL_CONV_ACTIVE = '[class*="conversation-item-active"]'

OBJ_TEXT_MESSAGE = 40000  # 文本消息的 objectType

# 已知的系统/群聊 UID，这些不是买家私聊，切框校验时应跳过
# 4611686018427387296 是钉钉系统账号，其他可配置
_SYSTEM_UIDS: set[str] = set()


def _load_system_uids() -> set[str]:
    """从配置加载系统/群聊 UID 列表，合并内置已知 UID。"""
    uids = {"4611686018427387296"}
    extra = config.get("safety.system_uids", [])
    if isinstance(extra, list):
        uids.update(str(uid) for uid in extra)
    return uids


def _extract_text(decoded: str) -> str | None:
    m = re.search(r'"text":\{"text":"((?:[^"\\]|\\.)*)"', decoded)
    if not m:
        return None
    try:
        return json.loads(f'"{m.group(1)}"')  # 处理转义/unicode
    except Exception:
        return m.group(1)


def _extract_field(decoded: str, pattern: str) -> str | None:
    m = re.search(pattern, decoded)
    return m.group(1) if m else None


class IMListener:
    def __init__(self, browser: BrowserManager) -> None:
        self.browser = browser
        self.page: Page = browser.page  # type: ignore[assignment]
        self.self_id: str = ""          # 卖家自己的 userId，自动识别
        self._seen_msg_ids: set[str] = set()
        # 发送串行锁：保证「切对话框→校验→发送」是原子操作，
        # 避免多个买家同时来消息时并发抢对话框、把回复发到错误的人那里。
        self._send_lock = asyncio.Lock()
        # 最近一次成功回复过的买家 sender uid，用于 last_buyer fallback
        self._last_replied_sender: str = ""
        # 延迟加载系统 UID 列表
        self._system_uids: set[str] = set()

    async def start(self) -> None:
        self.page.on("websocket", self._on_websocket)
        logger.info("IM 监听启动；刷新页面以捕获 WebSocket 重连…")
        # 页面 WS 在接入前已建立，reload 一次让监听器抓到新连接
        await self.page.reload()
        logger.info("已刷新，开始监听新消息")

    def _on_websocket(self, ws) -> None:
        logger.info(f"WS 连接: {ws.url}")
        ws.on("framereceived", self._on_frame)

    def _on_frame(self, payload) -> None:
        if not isinstance(payload, str) or '"lwp"' not in payload:
            return
        try:
            frame = json.loads(payload)
        except Exception:
            return
        # 自动识别自己的 userId
        reg_uid = frame.get("headers", {}).get("reg-uid", "")
        if reg_uid and not self.self_id:
            self.self_id = reg_uid.split("@")[0]
            logger.info(f"识别到卖家 userId: {self.self_id}")
        if frame.get("lwp") != "/s/sync":
            return
        pkg = frame.get("body", {}).get("syncPushPackage")
        if not pkg:
            return
        for item in pkg.get("data", []):
            if item.get("objectType") == OBJ_TEXT_MESSAGE:
                asyncio.create_task(self._handle_push_item(item))

    async def _handle_push_item(self, item: dict) -> None:
        try:
            raw = base64.b64decode(item.get("data", "") + "===")
        except Exception:
            return
        decoded = raw.decode("utf-8", errors="replace")

        text = _extract_text(decoded)
        if not text:
            return
        sender = _extract_field(decoded, r"senderUserId\W+(\d+)") \
            or _extract_field(decoded, r"peerUserId=(\d+)")
        msg_id = _extract_field(decoded, r'"messageId":"([0-9a-f]+)"')
        cid = _extract_field(decoded, r"sid=(\d+)")
        item_id = _extract_field(decoded, r"itemId=(\d+)") or ""
        nick = _extract_field(decoded, r"reminderTitle\W+([^\x00]{1,20}?)\W*reminderUrl")

        # 过滤：自己发的、重复的（内存快路径 + 持久化去重，跨重启不重复回旧消息）
        if sender and self.self_id and sender == self.self_id:
            return
        if msg_id and (msg_id in self._seen_msg_ids or db.message_exists(msg_id)):
            return
        if msg_id:
            self._seen_msg_ids.add(msg_id)

        logger.info(f"买家消息 [{nick or sender}] uid={sender} item={item_id}: {text}")
        await self._reply(text, cid or "", item_id, nick or "", sender or "",
                          msg_id=msg_id or "", raw=decoded)

    async def _reply(self, buyer_msg: str, cid: str, item_id: str,
                     nick: str, sender: str, msg_id: str = "", raw: str = "") -> None:
        # 安全失败：抓不到会话 cid 就不回，避免回落到共享的 page.url 导致串台
        if not cid and config.get("safety.require_cid", True):
            logger.warning(f"未能确定会话 cid（uid={sender} nick={nick}），跳过不回，避免串台")
            return
        chat_key = cid or self.page.url

        # 商品采集指令：小号发「【采集】+商品链接」即抓取商品信息入库（不走正常回复）
        if "【采集】" in buyer_msg or "采集商品" in buyer_msg:
            await self._handle_collect(buyer_msg, sender, nick)
            return

        # 该客户的历史与记忆（按 chat_id 隔离，互不串台）
        history = db.query(
            "SELECT role,content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT 12",
            (chat_key,),
        )[::-1]
        db.add_message(chat_key, "buyer", buyer_msg, item_id, msg_id=msg_id, raw_json=raw)
        db.bump_unread(chat_key)

        mem = memory.get(chat_key)
        # 人工接管：该买家已暂停 AI，则只记录消息、不自动回复（未读会累计，方便你回头处理）
        if mem.get("ai_paused"):
            logger.info(f"客户 {chat_key} 已人工接管（AI 暂停），仅记录消息不自动回复")
            return

        ok, reason = ratelimit.can_reply()
        if not ok:
            logger.info(f"跳过自动回复（{reason}）")
            return

        # 会话级身份解析：首次按商品/关键词命中后锁定，整轮沿用，不中途跳变
        persona_obj = persona.resolve_for_conversation(mem, text=buyer_msg, item_id=item_id)
        reply, source = rules.decide_reply(
            buyer_msg, item_id=item_id, history=history,
            memory=mem, persona_obj=persona_obj)
        logger.info(f"[{source}] 身份={persona.route_label(persona_obj)} 拟回复: {reply}")

        await ratelimit.human_delay(buyer_msg, reply)
        sent = await self._send_dom(reply, sender, nick)
        seller_role = "ai" if source == "ai" else "seller"
        db.add_message(chat_key, seller_role, reply, item_id)
        db.log_reply(chat_key, buyer_msg, reply, source, sent)
        if sent:
            db.mark_replied(chat_key)
        logger.info(f"已{'发送' if sent else '发送失败'}: {reply}")

        # 更新该客户记忆（收集需求型身份会抽取关键信息、必要时生成需求文档）
        try:
            turn_history = history + [
                {"role": "buyer", "content": buyer_msg},
                {"role": seller_role, "content": reply},
            ]
            memory.update_after_turn(
                chat_key, persona_obj, turn_history,
                buyer_id=sender, buyer_nick=nick, item_id=item_id)
        except Exception as e:
            logger.warning(f"更新客户记忆失败（不影响回复）: {e}")

    async def _handle_collect(self, buyer_msg: str, sender: str, nick: str) -> None:
        """处理「【采集】+商品链接」指令：复用已登录 Edge 抓商品信息入库并回执。"""
        item_id = item_collector.extract_item_id(buyer_msg)
        if not item_id:
            await self._send_dom("没识别到商品链接哈，把商品链接发我一下~", sender, nick)
            return
        logger.info(f"收到采集指令 item={item_id}（来自 {nick or sender}）")
        info = await item_collector.collect_with_context(self.browser.context, item_id)
        db.upsert_collected_item(info)
        if info.get("error"):
            reply = f"采集 #{item_id} 没成功：{info['error'][:50]}"
        else:
            reply = (f"✅ 已采集 #{item_id}\n标题：{(info.get('title') or '-')[:40]}\n"
                     f"价格：{info.get('price') or '-'}\n图片：{len(info.get('images') or [])} 张")
        await self._send_dom(reply, sender, nick)
        logger.info(f"采集完成 item={item_id} 标题={info.get('title')}")

    async def _send_dom(self, text: str, sender: str = "", nick: str = "") -> bool:
        """先切到该买家的会话，再发送。整段切框+校验+发送在锁内串行执行。

        路由：买家 userId 藏在会话项头像 URL 的 `!!<userId>-` 里，据此精准匹配。
        切换成功后再回读当前激活会话的 userId 做二次校验，确认 == 目标买家才发送，
        从根本上杜绝并发或误点导致「把回复发到别的客户那里」。

        校验增强：
        - 已知系统/群聊 UID（如 4611686018427387296）自动跳过校验（非买家私聊）
        - 可通过 safety.system_uids 配置额外系统 UID
        - 校验失败时打详细日志

        Fallback 策略（safety.route_fallback）：
        - "active"：回退到当前激活会话发送（默认）
        - "last_buyer"：切到最近成功回复过的买家会话
        - "none"：不发送，仅记录日志（最安全）
        """
        async with self._send_lock:
            # 延迟加载系统 UID 列表（避免 config 未就绪）
            if not self._system_uids:
                self._system_uids = _load_system_uids()

            if not config.get("safety.reply_to_active_only", True):
                switched = await self._switch_conversation(sender, nick)
                if not switched:
                    fb = config.get("safety.route_fallback", "active")
                    logger.warning(
                        f"找不到买家会话(uid={sender} nick={nick})，"
                        f"触发 fallback 策略: {fb}"
                    )
                    if fb == "none":
                        logger.info(f"fallback=none，跳过发送: {text[:60]}")
                        return False
                    elif fb == "last_buyer" and self._last_replied_sender:
                        logger.info(
                            f"尝试切到最近成功回复过的买家 uid={self._last_replied_sender}"
                        )
                        switched_back = await self._switch_conversation(
                            self._last_replied_sender
                        )
                        if not switched_back:
                            logger.warning(
                                f"切回最近买家 uid={self._last_replied_sender} 失败，"
                                "回退到当前会话发送"
                            )
                    # fb == "active" 或 last_buyer 失败：回退到当前会话，继续发送
                elif sender and config.get("safety.verify_sender_uid", True):
                    # 发送前回读校验：当前激活会话必须就是目标买家
                    active_uid = await self._active_conversation_uid()
                    if active_uid:
                        if active_uid in self._system_uids:
                            # 系统/群聊会话，跳过校验（不是买家私聊）
                            logger.warning(
                                f"当前激活会话是系统/群聊(uid={active_uid})，"
                                f"跳过 uid 校验（非买家私聊，sender={sender} nick={nick}）"
                            )
                        elif active_uid != sender:
                            logger.warning(
                                f"切框校验失败：期望 uid={sender}({nick})，"
                                f"当前激活 uid={active_uid}，跳过不发"
                            )
                            return False
            try:
                box = self.page.locator(SEL_INPUT).first
                await box.click()
                await box.fill(text)
                await box.press("Enter")
                # 记录最近成功回复的买家，供 last_buyer fallback 使用
                if sender:
                    self._last_replied_sender = sender
                return True
            except Exception as e:
                logger.error(f"发送失败: {e}")
                return False

    async def _active_conversation_uid(self) -> str:
        """读取当前激活会话项头像 URL 里的买家 userId（`!!<userId>-`）。"""
        try:
            img = self.page.locator(f'{SEL_CONV_ACTIVE} img[src*="!!"]').first
            if await img.count() == 0:
                return ""
            src = await img.get_attribute("src") or ""
            m = re.search(r"!!(\d+)-", src)
            return m.group(1) if m else ""
        except Exception:
            return ""

    async def _switch_conversation(self, sender: str, nick: str = "") -> bool:
        """点击对应买家的会话项。优先用头像 URL 里的 userId 匹配，回退昵称。"""
        try:
            # 1) 按头像 URL 中的 userId 精准匹配
            if sender:
                item = self.page.locator(
                    SEL_CONV_ITEM,
                    has=self.page.locator(f'img[src*="!!{sender}-"]'),
                ).first
                if await item.count() > 0:
                    await item.click()
                    await asyncio.sleep(0.6)
                    return True
            # 2) 回退：按昵称文本匹配（昵称可能被打码，未必命中）
            if nick:
                item = self.page.locator(SEL_CONV_ITEM, has_text=nick).first
                if await item.count() > 0:
                    await item.click()
                    await asyncio.sleep(0.6)
                    return True
        except Exception as e:
            logger.warning(f"切换会话异常: {e}")
        return False
