"""
发消息（v0.2.0 新增）：打字节奏 / 打字刹车 / 群里引用哪条 —— AstrBot 这一层（规则在 core/send.py）

AstrBot 发一轮回话的顺序（4.29，从源码里对的：pipeline/result_decorate、pipeline/respond）：
  发送前钩子（on_decorating_result，本插件在这里「上弦」）→ AstrBot 自己分段（「分段回复」开着时按句子切开）→
  AstrBot 自己加引用（「引用回复」开着时，在最前面插一个 Reply = 触发这一轮的那条消息）→
  一段一段发：每段先睡 AstrBot 的「分段间隔」，再 event.send(这一段) → 都发完 → after_message_sent 钩子
本插件不自己切段、不自己发：上弦时把这一轮的 event.send 换成一层薄壳，AstrBot 每发一段都先经过它：
  - 打字节奏：这一段该「打」多久（core/send.typing_delay），AstrBot 的分段间隔已经睡过的那部分算进去，只补差的（不会两边都等）；
  - 打字刹车：两段之间、补等的时候看一眼对方又说了什么（core/send.Ears），该停就不往下发了；
    已经发的才算她说过的：AstrBot 马上要存进对话记录的那句回话，改成只到发出去的那段为止（下一轮她知道自己没说完）；
  - 引用：第一段发出去之前，把 AstrBot 插的那个 Reply 换成「找她的那一条」（core/send.pick_quote），该不引用的就去掉。
after_message_sent（最高优先级）把 event.send 换回去，别的插件在这之后发的话不受影响。
三样都关着 / 流式输出 / 不是模型的回话（指令回复、别的插件发的）→ 不上弦，跟以前完全一样。
"""

import asyncio
import time
from collections import defaultdict, deque

import astrbot.api.message_components as Comp
from astrbot.api import logger

from ..core.send import (
    Asks,
    Conf,
    Daily,
    Ears,
    kept_text,
    pick_quote,
    progress_why,
    typing_delay,
)

TAG = "[发消息]"
TAG_B = "[刹车]"
RECALL = ("group_recall", "friend_recall")


def _plain(comps):
    return "".join(c.text for c in comps or () if isinstance(c, Comp.Plain))


def notice_of(event):
    """这是 QQ 的通知事件（撤回 / 戳一戳…，不是有人说了话）→ 原始的通知 dict；不是 → None"""
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    try:
        if hasattr(raw, "get") and raw.get("post_type") == "notice":
            return raw
    except Exception:  # noqa: BLE001, S110  这一层出错不能连累 AstrBot 发消息
        pass
    return None


class SendMod:
    """打字节奏 + 打字刹车 + 群里只引用找她的那一条"""

    name = "send"

    def __init__(self, host):
        self.host = host
        self.config = host.sub("send")
        self.conf = Conf(self.config)
        st = host.store.get("send")
        self.daily = Daily(st.get("stats"))
        self.recent = deque(st.get("recent") or [], maxlen=20)  # 最近几次刹车
        self.ears = Ears()
        self.asks = Asks()
        # 会话 -> 群里最近的消息 [(时间, 谁, 消息 id)]（她自己说的 谁 = "self"）
        self.timeline = defaultdict(lambda: deque(maxlen=200))
        host.store.register("send", self.dump)

    def dump(self):
        return {"stats": self.daily.today(), "recent": list(self.recent)}

    def _on(self):
        return self.host.active("send") and bool(self.conf("enable"))

    # ───────── 每条消息 / 通知：记下来（刹车、引用要看）─────────
    def on_notice(self, event, raw):
        """撤回：记下撤回了哪条（她正在回的就是这条 → 刹车）"""
        if not self._on():
            return
        try:
            if raw.get("notice_type") in RECALL:
                self.ears.recall(event.unified_msg_origin, raw.get("message_id"))
        except Exception:  # noqa: BLE001, S110  这一层出错不能连累 AstrBot 发消息
            pass

    def heard(self, event):
        """有人说了一句：一到就记（群聊节奏可能要等模型判断好几秒，刹车不能等它）。
        这时 is_at_or_wake_command 只认 @ / 引用 / 唤醒词 / 私聊；群聊节奏决定接的，判断完再由 woke() 补上"""
        if not self._on():
            return
        try:
            key = event.unified_msg_origin
            uid, sid = str(event.get_sender_id() or ""), str(event.get_self_id() or "")
            if not uid or uid == sid:
                return
            text = (event.message_str or "").strip()
            now = time.time()
            event.set_extra("alive_heard_t", now)
            self.ears.heard(key, uid, text, woke=self._woke(event, text), t=now)
            if event.get_group_id():
                self.timeline[key].append(
                    (now, uid, str(getattr(event.message_obj, "message_id", "") or ""))
                )
        except Exception as e:  # noqa: BLE001  这一层出错不能连累 AstrBot 发消息
            logger.debug(f"{TAG} 记消息出错：{e!r}")

    @staticmethod
    def _woke(event, text):
        """这条之后会不会再叫模型回他（指令不算）"""
        return bool(getattr(event, "is_at_or_wake_command", False)) and not (
            text or ""
        ).startswith("/")

    def woke(self, event):
        """群聊节奏判断完了：决定接这条（没 @ 但在接她的话 / 挑着接）→ 记成「会再回他」"""
        t = event.get_extra("alive_heard_t")
        if not t or not self._on():
            return
        if self._woke(event, event.message_str):
            self.ears.wake(
                event.unified_msg_origin, str(event.get_sender_id() or ""), t
            )

    # ───────── 要叫模型了：登记「这个人在等她回」─────────
    @staticmethod
    def _ask_of(event):
        """这一轮「找她的那一条」：攒一攒并过的用它记下的；否则 @ / 引用她 / 叫她 / 接她的话 = 这条本身；她自己插话 → None"""
        a = event.get_extra("fatfish_ask")
        if isinstance(a, dict):
            return dict(a) if a.get("mid") else None
        kind = event.get_extra("fatfish_kind")
        if kind == "follow" or (
            not kind and getattr(event, "is_at_or_wake_command", False)
        ):
            mid = getattr(event.message_obj, "message_id", None)
            return {"mid": str(mid)} if mid not in (None, "") else None
        return None

    def waiting(self, event):
        if not self._on():
            return
        try:
            ask = self._ask_of(event) or {}
            if ask:
                ask.update(
                    uid=str(event.get_sender_id() or ""),
                    name=event.get_sender_name() or "",
                )
            event.set_extra("alive_ask", ask)
            if ask and event.get_group_id():
                self.asks.add(
                    event.unified_msg_origin,
                    id(event),
                    ask["uid"],
                    ask["name"],
                    ask["mid"],
                )
        except Exception as e:  # noqa: BLE001  这一层出错不能连累 AstrBot 发消息
            logger.debug(f"{TAG} 登记等回的人出错：{e!r}")

    def on_llm_req(self, event):
        event.set_extra("alive_t_req", time.time())

    # ───────── 发送前：上弦 ─────────
    def arm(self, event):
        """发送前钩子的最后一步（兜底、群聊节奏都处理完了）：要管的话，把这一轮的 event.send 换成薄壳"""
        try:
            if event.is_stopped():
                self.asks.done(event.unified_msg_origin, id(event))
                return
            if not self._on():
                return
            res = event.get_result()
            if res is None or not res.chain or not res.is_llm_result():
                return  # 流式输出、指令回复、别的插件发的话：不管
            typing, brake = self.conf("typing_human"), self.conf("typing_brake")
            mode = self.conf("quote_mode")
            if not (typing or brake or mode != "off"):
                return
            if "send" in event.__dict__ and event.get_extra("alive_send"):
                return  # 已经上过弦了
            group = bool(event.get_group_id())
            now = time.time()
            t_req = event.get_extra("alive_t_req")
            key = event.unified_msg_origin
            mids = {str(getattr(event.message_obj, "message_id", "") or "")}
            ask = event.get_extra("alive_ask") or {}
            if ask.get("mid"):
                mids.add(str(ask["mid"]))
            st = {
                "i": 0,
                "t_last": now,
                "lead": max(0.0, now - float(t_req)) if t_req else 0.0,
                "last_in": event.message_str or "",
                "full": _plain(res.chain),
                "sent": [],
                "dropped": [],
                "braked": None,
                "typing": typing,
                "brake": brake,
                "quote": mode,
                "quote_done": mode == "off",
                "group": group,
                "had_send": "send" in event.__dict__,
                "old_send": event.__dict__.get("send"),
                "rnd": {
                    "key": key,
                    "uid": str(event.get_sender_id() or ""),
                    "name": event.get_sender_name() or "",
                    "t0": float(t_req or now),
                    "mids": mids,
                    "heard0": self.ears.heard_n(key),
                    "group": group,
                    "flood": self.conf("brake_flood"),
                },
            }
            orig = event.send

            async def send(message, _st=st, _orig=orig):
                return await self._send(event, _orig, _st, message)

            event.send = send
            event.set_extra("alive_send", st)
            if typing:
                self.daily.add("typed")
        except Exception as e:  # noqa: BLE001  这一层出错不能连累 AstrBot 发消息
            logger.warning(
                f"{TAG} 上弦出错（这一轮照 AstrBot 原来的样子发）：{e!r}"[:200]
            )

    async def _send(self, event, orig, st, message):
        """AstrBot 每发一段都先到这里"""
        try:
            comps = list(getattr(message, "chain", None) or [])
        except Exception:  # noqa: BLE001  这一层出错不能连累 AstrBot 发消息
            comps = []
        text = _plain(comps)
        if st["braked"]:  # 刹过车了：后面的都不发
            st["dropped"].append(text)
            return None
        if not any(not isinstance(c, (Comp.Reply, Comp.At)) for c in comps):
            return await orig(message)
        i = st["i"]
        st["i"] += 1
        why = None
        try:
            if not st["quote_done"]:
                self._fix_quote(event, st, message)
            d = (
                typing_delay(
                    i,
                    text,
                    st["lead"],
                    st["last_in"],
                    self.conf("typing_cps"),
                    self.conf("typing_max_sec"),
                )
                if st["typing"]
                else 0.0
            )
            # AstrBot 的分段间隔已经睡过的：算进打字时间，只补差的
            el0 = time.time() - st["t_last"]
            # 第一段一定发；之后每段开打之前（两段之间）、打的时候都看一眼
            if st["brake"] and i > 0:
                why = self.ears.why(st["rnd"], boundary=True)
                if why is None and d > el0:
                    why = await self._typing_wait(st, d, el0)
            elif d > el0:
                await asyncio.sleep(d - el0)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  这一层出错不能连累 AstrBot 发消息
            logger.debug(f"{TAG} 打字节奏 / 刹车出错（这段照常发）：{e!r}")
            why = None
        if why:
            self._braked(event, st, why, text)
            return None
        r = await orig(message)
        st["t_last"] = time.time()
        st["sent"].append(text)
        if st["group"]:
            self.timeline[event.unified_msg_origin].append((st["t_last"], "self", ""))
        return r

    async def _typing_wait(self, st, d, el0):
        """边「打」边看：打了不到 30% 有值得停的就停；30%~80% 只认强信号；80% 以后先打完"""
        t_s = time.time()
        while True:
            el = el0 + (time.time() - t_s)
            if el >= d:
                return None
            why = progress_why(self.ears, st["rnd"], el / d if d > 0 else 1.0)
            if why:
                return why
            await asyncio.sleep(min(0.25, max(0.02, d - el)))

    def _braked(self, event, st, why, text):
        st["braked"] = why
        st["dropped"].append(text)
        sent = " ".join(x for x in st["sent"] if x).strip()
        event.set_extra("alive_sent_text", sent)
        ok = self._trim_history(event, st["sent"])
        self.daily.add("braked")
        self.recent.append(
            {
                "t": int(time.time()),
                "where": f"群 {event.get_group_id()}" if st["group"] else "私聊",
                "who": st["rnd"]["name"],
                "why": why[:60],
                "sent": len(st["sent"]),
                "left": text[:40],
            }
        )
        self.host.save()
        logger.info(
            f"{TAG_B} {event.unified_msg_origin} 打到第 {st['i']} 段停了：{why}；没发的从「{text[:30]}」起"
            + ("" if ok else "（对话记录没改成：记下的还是整段）")
        )

    def _trim_history(self, event, sent):
        """AstrBot 马上要把这一轮存进对话记录（存的是模型回的整段）：改成只到发出去的那段为止。
        用的是 AstrBot 内部正在跑的那一轮（pipeline/process_stage/follow_up 里登记的）；对不上就不改"""
        try:
            from astrbot.core.pipeline.process_stage.follow_up import (
                _ACTIVE_AGENT_RUNNERS,
            )

            r = _ACTIVE_AGENT_RUNNERS.get(event.unified_msg_origin)
            ctx = getattr(getattr(r, "run_context", None), "context", None)
            if r is None or getattr(ctx, "event", None) is not event:
                return False
            last = r.run_context.messages[-1]
            if getattr(last, "role", "") != "assistant":
                return False
            first = next((x.strip() for x in sent if x and x.strip()), "")
            if isinstance(last.content, str):
                if first and first in last.content:
                    last.content = kept_text(last.content, sent)
                    return True
                return False
            for p in last.content or []:
                t = getattr(p, "text", None)
                if getattr(p, "type", "") == "text" and t and first and first in t:
                    p.text = kept_text(t, sent)
                    return True
        except Exception as e:  # noqa: BLE001  这一层出错不能连累 AstrBot 发消息
            logger.debug(f"{TAG_B} 改对话记录出错：{e!r}")
        return False

    def _fix_quote(self, event, st, message):
        """第一段发出去之前：引用换成「找她的那一条」（或者去掉）"""
        st["quote_done"] = True
        chain = getattr(message, "chain", None)
        if not isinstance(chain, list):
            return
        mine = str(getattr(event.message_obj, "message_id", "") or "")
        astr = [c for c in chain if isinstance(c, Comp.Reply) and str(c.id) == mine]
        key = event.unified_msg_origin
        try:
            if st["quote"] == "smart" and not astr:
                return  # AstrBot 没开「引用回复」：smart 不管
            head = event.get_extra("alive_ask") or self._ask_of(event) or {}
            head = dict(head, uid=st["rnd"]["uid"], name=st["rnd"]["name"])
            waiting = self.asks.others(key, id(event), st["rnd"]["uid"])
            mid = pick_quote(
                st["group"], head, waiting, st["full"], list(self.timeline[key])
            )
            if (
                mid
                and not astr
                and not all(
                    isinstance(c, (Comp.Plain, Comp.Image, Comp.At)) for c in chain
                )
            ):
                mid = None  # 语音 / 文件这些不挂引用（AstrBot 自己也不挂）
            if astr and mid == mine:
                self.daily.add("quote_kept")
                return
            new = [c for c in chain if not any(c is a for a in astr)]
            if mid:
                new.insert(0, Comp.Reply(id=mid))
            chain[:] = new
            if astr:
                self.daily.add("quote_moved" if mid else "quote_dropped")
            elif mid:
                self.daily.add("quote_added")
            logger.info(
                f"{TAG} {key} 引用："
                + (
                    f"换成找她的那一条（{mid}）"
                    if mid
                    else "这一句不挂引用（私聊 / 一来一回 / 她自己插话）"
                )
            )
        finally:
            self.asks.done(key, id(event))

    # ───────── 发完：换回去 ─────────
    def after_sent(self, event):
        st = event.get_extra("alive_send")
        if not st:
            if (
                event.get_group_id() and self._on()
            ):  # 别的插件 / 没上弦的回话：也记一笔「她说了话」（引用看有没有人插话）
                self.timeline[event.unified_msg_origin].append(
                    (time.time(), "self", "")
                )
            return
        event.set_extra("alive_send", None)
        try:
            if st["had_send"]:
                event.send = st["old_send"]
            else:
                event.__dict__.pop("send", None)
        except Exception:  # noqa: BLE001, S110  这一层出错不能连累 AstrBot 发消息
            pass
        self.asks.done(event.unified_msg_origin, id(event))
        if st["braked"] and len(st["dropped"]) > 1:
            logger.info(
                f"{TAG_B} {event.unified_msg_origin} 一共没发 {len(st['dropped'])} 段："
                + " / ".join(x for x in st["dropped"] if x)[:80]
            )

    def note_self(self, key):
        """她在群里主动说了一句（冷场试探 / 收尾）：引用看「中间有没有人说话」时算上"""
        self.timeline[str(key)].append((time.time(), "self", ""))

    # ───────── 面板 / 指令 ─────────
    def today(self):
        return self.daily.today()

    def snapshot(self):
        return {
            "today": dict(self.daily.today()),
            "recent": list(self.recent)[-10:][::-1],
            "config": {
                k: self.conf(k)
                for k in (
                    "enable",
                    "typing_human",
                    "typing_cps",
                    "typing_max_sec",
                    "typing_brake",
                    "brake_flood",
                    "quote_mode",
                )
            },
        }
