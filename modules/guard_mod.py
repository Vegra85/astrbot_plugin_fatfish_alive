# -*- coding: utf-8 -*-
"""兜底（新）：借 mindscape 的思路。三件事，各一个开关：
  1. 报错拦截：AstrBot 调模型出错时会把「Error occurred during AI execution…」这类报错原样发到聊天里 → 拦下不发（默认开）
  2. 空回复救援：模型只回了 <think> 思考、没有正文 → 补调一次模型要一句正文；还是空就不发（默认开）
  3. 去 AI 腔：「作为一个AI」「希望对你有帮助」这类客服腔，提醒模型别用、发出去前再去掉（默认关：语气交给人设自己管）
报错拦截 / 去 AI 腔 在发送前拦（on_decorating_result）；空回复救援 两处：发送前（只有 <think> 的那种）、模型回完（什么都没回的那种）
"""

import asyncio
import time
from collections import deque

from astrbot.api import logger
from astrbot.api.event import MessageChain, MessageEventResult
import astrbot.api.message_components as Comp

from ..core.conf import truthy
from ..core.guard import (
    AI_TONE_RULE,
    is_empty_error,
    is_error_text,
    neutral_marks,
    strip_ai_tone,
    strip_think,
    think_only,
)

TAG = "[兜底]"
KEYS = ("errors", "think_stripped", "rescued", "rescue_failed", "tone", "fake_mark")


def _chain_has_content(chain):
    """消息链里有没有真东西（一个空的 Plain 不算：AstrBot 4.29.3 里模型只回了 <think> 时就是这样一条空链）"""
    items = getattr(chain, "chain", None) if chain is not None else None
    for c in items or ():
        if not isinstance(c, Comp.Plain) or strip_think(getattr(c, "text", "") or ""):
            return True
    return False


def _part(text):
    try:
        from astrbot.core.agent.message import TextPart

        return TextPart(text=text).mark_as_temp()
    except Exception:
        return {"type": "text", "text": text, "_no_save": True}


class GuardMod:
    name = "guard"

    def __init__(self, host):
        self.host = host
        self.config = host.sub("guard")
        st = host.store.get("guard")
        self.stats = st.get("stats") or {}
        self.recent = deque(st.get("recent") or [], maxlen=20)
        host.store.register("guard", self.dump)

    def dump(self):
        return {"stats": self.stats, "recent": list(self.recent)}

    def _save(self, force=False):
        self.host.save(force)

    def _on(self, key=None, default=True):
        if not self.host.active("guard"):
            return False
        return True if key is None else truthy(self.config.get(key), default)

    def today(self):
        day = time.strftime("%Y-%m-%d")
        if not isinstance(self.stats, dict) or self.stats.get("day") != day:
            self.stats = {"day": day}
        for k in KEYS:
            if not isinstance(self.stats.get(k), int):
                self.stats[k] = 0
        return self.stats

    def _note(self, kind, text, why=""):
        self.today()[kind] = self.today().get(kind, 0) + 1
        self.recent.append(
            {"t": int(time.time()), "kind": kind, "text": (text or "")[:80], "why": why}
        )
        self._save()

    # ───────── 回话请求的最后一步：防伪造标记（v0.3.0）─────────
    def neutralize(self, req, skip=()):
        """他说的话、系统提示、请求里别的段落（AstrBot 群聊上下文、别的插件加的）里手打的「[群聊节奏]」→「[他打的字：群聊节奏]」。
        skip = 本插件自己加的段落（id），不动。→ 改了几处"""
        if not self._on():
            return 0
        n = 0
        try:
            for attr in ("prompt", "system_prompt"):
                v = getattr(req, attr, None)
                if isinstance(v, str) and v:
                    new = neutral_marks(v)
                    if new != v:
                        setattr(req, attr, new)
                        n += 1
            for part in list(getattr(req, "extra_user_content_parts", None) or []):
                if id(part) in skip:
                    continue
                t = getattr(part, "text", None)
                if isinstance(t, str) and t:
                    new = neutral_marks(t)
                    if new != t:
                        part.text = new
                        n += 1
        except Exception as e:
            logger.debug(f"{TAG} 防伪造那一步出错（这一轮照常）：{e!r}")
        if n:
            self._note("fake_mark", "", "群友手打的「[群聊节奏]」标成了他打的字")
            logger.info(
                f"{TAG} 有人在消息里打了「[群聊节奏]」：标成「[他打的字：群聊节奏]」（防伪造）"
            )
        return n

    # ───────── 回话前：提醒别用客服腔（只有开了「去 AI 腔」才加）─────────
    def on_llm_req(self, event, req):
        if not self._on("strip_ai_tone", False):
            return
        try:
            req.extra_user_content_parts.append(_part(AI_TONE_RULE))
        except Exception as e:
            logger.warning(f"{TAG} 附去 AI 腔的提示失败：{e!r}")

    # ───────── 发送前 ─────────
    async def on_decorate(self, event):
        """→ True = 这条被拦下了（清空 + 停掉），后面的模块别再处理"""
        if not self._on():
            return False
        res = event.get_result()
        if res is None or not res.chain:
            return False
        try:
            plain = [c for c in res.chain if isinstance(c, Comp.Plain)]
            text = "".join(c.text for c in plain)
        except Exception:
            return False
        others = len(res.chain) - len(plain)  # 图、@、语音这些
        if others == 0 and self._on("block_errors") and is_error_text(text):
            if self._on("rescue_empty") and is_empty_error(
                text
            ):  # 模型空回（框架把它当报错）：补调一次
                new = await self._rescue(event)
                if new:
                    res.chain = [Comp.Plain(text=new)]
                    self._note("rescued", new, "模型空回（框架报了空输出）：补调了一次")
                    logger.info(
                        f"{TAG} 模型空回（框架报了空输出，报错没发出去）：补调了一次 → {new[:30]!r}"
                    )
                    return False
                self._note("rescue_failed", text, "模型空回，补调也没要到正文：不发")
            event.set_result(MessageEventResult())
            event.stop_event()
            self._note("errors", text, "框架报错没发出去")
            logger.warning(f"{TAG} 框架报错没发出去：{text.strip()[:80]!r}")
            return True
        if self._on("rescue_empty") and plain and "<think" in text.lower():
            if think_only(text) and others == 0:
                new = await self._rescue(event)
                if new:
                    res.chain = [Comp.Plain(text=new)]
                    self._note("rescued", new, "只有 <think> 没正文：补调了一次")
                    logger.info(
                        f"{TAG} 只有 <think> 没正文：补调了一次模型 → {new[:30]!r}"
                    )
                else:
                    event.set_result(MessageEventResult())
                    event.stop_event()
                    self._note(
                        "rescue_failed", text, "只有 <think>，补调也没要到正文：不发"
                    )
                    logger.warning(
                        f"{TAG} 只有 <think> 没正文，补调一次也没要到：这条不发"
                    )
                    return True
            else:  # 有正文、但 <think> 漏出来了：去掉
                for c in plain:
                    c.text = (
                        strip_think(c.text) if "<think" in c.text.lower() else c.text
                    )
                self._note("think_stripped", text, "<think> 漏出来了：去掉")
        if self._on("strip_ai_tone", False) and plain:
            gone_all = []
            for c in plain:
                new, gone = strip_ai_tone(c.text)
                if gone:
                    c.text = new
                    gone_all += gone
            if gone_all:
                self._note("tone", "、".join(gone_all), "去掉客服腔")
                logger.info(f"{TAG} 去 AI 腔：去掉了 {'、'.join(gone_all)[:60]!r}")
        return False

    # ───────── 模型回完：什么都没回的救援 ─────────
    async def on_llm_resp(self, event, resp):
        """模型回完了、却什么正文都没有（也没有图、没有要调的工具）：AstrBot 什么都不会发 → 补调一次，直接替它发出去"""
        if not self._on("rescue_empty"):
            return
        try:
            if getattr(resp, "role", "assistant") != "assistant" or getattr(
                resp, "tools_call_name", None
            ):
                return
            if _chain_has_content(getattr(resp, "result_chain", None)) or strip_think(
                getattr(resp, "completion_text", "") or ""
            ):
                return
            if event.is_stopped():
                return
            cur = event.get_result()
            if cur is not None and _chain_has_content(
                cur
            ):  # 已经有要发的内容了（工具调用的结果等）
                return
        except Exception:
            return
        new = await self._rescue(event)
        if not new:
            self._note("rescue_failed", "", "模型什么都没回，补调也没要到正文")
            logger.warning(f"{TAG} 模型什么都没回，补调一次也没要到正文：这条不发")
            return
        try:
            await event.send(MessageChain().message(new))
        except Exception as e:
            logger.warning(f"{TAG} 补调要到了正文但发不出去：{e!r}")
            return
        self._note("rescued", new, "模型什么都没回：补调了一次")
        logger.info(f"{TAG} 模型什么都没回：补调了一次 → {new[:30]!r}")

    async def _persona(self, umo):
        try:
            p = await self.host.context.persona_manager.get_default_persona_v3(umo)
            return str((p or {}).get("prompt") or "")
        except Exception:
            return ""

    async def _rescue(self, event):
        """补调一次模型要一句正文（轻量：人设 + 这条消息 + 群里最近几句）。要不到 → ''"""
        umo = event.unified_msg_origin
        said = (getattr(event, "message_str", "") or "").strip()
        if not said:
            return ""
        try:
            pid = await self.host.context.get_current_chat_provider_id(umo)
            system = (
                (await self._persona(umo))
                + "\n（你上一次没有给出正文。现在直接回复这条消息，一两句话，不要输出思考过程。）"
            )
            prompt = said
            gid = event.get_group_id()
            if gid:
                hist = list(
                    self.host.shared.recent.get(f"{event.get_platform_id()}:{gid}", ())
                )[-6:]
                if hist:
                    lines = "\n".join(
                        f"{h.get('name') or '你'}：{h.get('text', '')}"
                        for h in hist
                        if h.get("text")
                    )
                    prompt = f"群里最近在聊：\n{lines}\n\n现在 {event.get_sender_name() or '有人'} 说：{said}\n请直接回他。"
            timeout = float(self.config.get("rescue_timeout_sec", 25.0) or 25.0)
            resp = await asyncio.wait_for(
                self.host.context.llm_generate(
                    chat_provider_id=pid, prompt=prompt, system_prompt=system
                ),
                timeout,
            )
        except Exception as e:
            logger.debug(f"{TAG} 补调模型失败：{e!r}")
            return ""
        b = self.host.budget
        if b is not None and self.host.active("budget"):
            try:
                b.record_usage(resp)
            except Exception:
                pass
        return strip_think(getattr(resp, "completion_text", "") or "")

    def snapshot(self):
        c = self.config
        return {
            "today": dict(self.today()),
            "recent": list(self.recent)[-10:][::-1],
            "config": {
                "enable": truthy(c.get("enable"), True),
                "block_errors": truthy(c.get("block_errors"), True),
                "rescue_empty": truthy(c.get("rescue_empty"), True),
                "strip_ai_tone": truthy(c.get("strip_ai_tone"), False),
            },
        }
