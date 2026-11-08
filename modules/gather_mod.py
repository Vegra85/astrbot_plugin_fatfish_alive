# -*- coding: utf-8 -*-
"""攒一攒再回 —— AstrBot 这一层（原 astrbot_plugin_fatfish_gather/main.py，逻辑没变；核心在 core/gather.py）

真人常常连着发好几条（「在吗」「问你个事」「[图]」「这是啥」）。AstrBot 每条都单独叫一次模型、单独回 ——
回好几遍，像机器人，还费钱。这里让机器人「攒一攒」：等对方停下来，把这几条当一件事，只回一次。

- 每条消息一到（唤醒检查那一步，图还没下载）就记一笔：谁、什么时候；
- 消息处理那一步记下内容（文字、图、引用）；
- 快要叫模型的地方（on_waiting_llm_request，这时还没拿会话锁、别的消息照常处理）等一等：
  等的时候同一个人又来了要回的 → 这条静悄悄地停掉，由新的接着等、带上前面的；
  来的是不叫模型的（群里没 @ 的）→ 这条照样等完，把它也并进来（他 @ 别人 / 引用别人的只算计时、不并）；
- 等完了把这几条的文字按顺序用换行拼起来、图和引用都放进这条消息里，再交给模型。

活人感新增：「稍等 / 还没说完 / 等我发完」先不回（core/gather.py 的 hold_phrases）。
v0.2.0：并完以后在 event 上记下「找她的那一条」（fatfish_ask），群里引用的时候引用它、不引用最后补的一句。
v0.3.0（借作者自己的 QQ 机器人 v1.16.3）：规则等完以后再看一眼 ——
  私聊里他还在「正在输入」（NapCat 的通知）→ 再等等（最多 typing_extra_sec 秒）；
  最后一句尾巴含糊（那个 / 然后 / 逗号 / 省略号 / 列举到一半）→ 问一次判断模型「他说完了吗」（不带人设、最多等 4 秒、一轮最多问 2 次）
  → 回「等 N」就再等 N 秒（3~12）；关了 tail_ask 就固定多等 tail_extra_sec 秒。
v0.4.0（借作者自己的 QQ 机器人 11-03 v1.17.13）：还排着没回、他把那条撤回了 → 那条不回（recall_drop，默认开）：
  只有那一条 → 这一轮不回（静悄悄停掉）；在等的那条撤回了、他前面还有几条 → 只回剩下的；
  撤回通知比消息先到（图还在下载这种）→ 记 10 分钟，这条一到就认出来、不回。
"""

import asyncio
import copy
import re
import time
from collections import deque

from astrbot.api import logger
import astrbot.api.message_components as Comp

from ..core.conf import truthy
from ..core.gather import DEFAULTS, Gatherer, merge_lines

try:
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.filter.command_group import CommandGroupFilter

    _CMD_FILTERS = (CommandFilter, CommandGroupFilter)
except Exception:  # 以后挪了地方：只认「/ 开头」
    _CMD_FILTERS = ()
try:
    from astrbot.core.agent.message import TextPart
except Exception:
    TextPart = None

TAG = "[攒一攒]"
CORE_KEYS = (
    "base_sec",
    "pic_extra_sec",
    "short_extra_sec",
    "short_len",
    "quick_sec",
    "long_len",
    "quick_endings",
    "max_sec",
    "lookback_sec",
    "max_msgs",
    "hold_phrases",
    "hold_sec",
    "tail_vague",
    "tail_ask",
    "tail_extra_sec",
    "tail_ask_max",
    "typing_wait",
    "typing_extra_sec",
    "recall_drop",
)
RECALL = ("group_recall", "friend_recall")
RECALL_KEEP = 600.0  # （v0.4.0）撤回通知比消息先到：记多少秒
TAIL_SYS = (
    "你在帮一个 QQ 聊天机器人判断：对方是不是已经把这一串话说完了。下面是前面几行聊天、和他刚连着发的几条。"
    "只回一个：说完了（或者在等回应）就回「回」；明显一句话没说完、后面还要接着发 就回「等 N」（N = 还要等几秒 3 到 12）。不要解释。"
)
TAIL_TIMEOUT = 4.0  # 问「他说完了吗」最多等几秒（超时 / 出错 = 说完了）
TYPING_RECENT = 6.0  # 「正在输入」的通知多少秒内算还在输入


def _num(v, d):
    try:
        return type(d)(v)
    except (TypeError, ValueError):
        return d


class GatherMod:
    name = "gather"

    def __init__(self, host):
        self.host = host
        self.config = host.sub("gather")
        st = host.store.get("gather")
        self.stats = st.get("stats") or {}
        self.recent = deque(st.get("recent") or [], maxlen=40)
        self.g = Gatherer(self._core_cfg())
        self.g.probe = self._probe
        self.g.typing = self._typing
        self.g.asker = self._ask_tail
        self.by_ev = {}  # id(事件) -> Msg
        self.typing_at = {}  # （v0.3.0）攒一攒的 key（会话|QQ）-> 最近一次「正在输入」通知的时间
        self.recalled = {}  # （v0.4.0）撤回了的消息 id -> 什么时候撤回的（撤回通知比消息先到时，消息一到就认出来）
        self._warned = {}
        host.store.register("gather", self.dump)

    def dump(self):
        return {"stats": self.stats, "recent": list(self.recent)}

    def _save(self, force=False):
        self.host.save(force)

    # ───────── 配置 ─────────
    def _c(self, key, default):
        return _num(self.config.get(key, default), default)

    def _core_cfg(self):
        return {k: self.config.get(k, DEFAULTS[k]) for k in CORE_KEYS}

    def _list(self, key):
        v = self.config.get(key) or []
        if isinstance(v, str):
            v = v.replace("，", ",").split(",")
        return [str(x).strip() for x in v if str(x).strip()]

    def _on(self, event=None):
        return self.host.active("gather", event)   # v0.4.2：带 event → 私聊里只管群的同类插件不算让路

    def _oops(self, where, e, m=None):
        """出错了：这条不攒（照 AstrBot 原来的样子处理），记一笔日志（同一处 60 秒内只记一次）。
        收消息那一步的异常要是漏给 AstrBot，它会给用户发一句「插件出错」并把这条停掉 —— 所以这里全接住"""
        if m is not None:
            try:
                self.g.mark(m, "skip")
            except Exception:
                pass
        now = time.time()
        if now - self._warned.get(where, 0) > 60:
            self._warned[where] = now
            logger.warning(f"{TAG} {where}出错了（这条照常处理、不攒）：{e!r}"[:300])

    def _today(self):
        day = time.strftime("%Y-%m-%d")
        if not isinstance(self.stats, dict) or self.stats.get("day") != day:
            self.stats = {"day": day}
        for k in (
            "merges",
            "msgs",
            "saved",
            "turns",
            "tail_asks",
            "tail_waits",
            "typing_waits",
            "recalled",
            "recall_skip",
        ):  # 存档缺了哪项（手改过 / 坏了）也不会 KeyError
            if not isinstance(self.stats.get(k), int):
                self.stats[k] = 0
        return self.stats

    # ───────── 哪些管、哪些不管 ─────────
    @staticmethod
    def _key(event):
        return f"{event.unified_msg_origin}|{event.get_sender_id()}"

    def _scene_ok(self, event):
        if not self._on(event):
            return False
        sid, uid = str(event.get_self_id() or ""), str(event.get_sender_id() or "")
        if not uid or uid == sid:
            return False  # 机器人自己发的
        gid = str(event.get_group_id() or "")
        if gid:
            return self._c("group", True) and gid not in self._list("skip_groups")
        return bool(event.is_private_chat()) and self._c("private", True)

    def _is_cmd(self, event):
        """指令：/ 开头的（可以在配置里加别的开头），或者 AstrBot 认出来是某个插件注册的指令"""
        raw = str(getattr(event.message_obj, "message_str", "") or "").strip()
        first = next(
            (c.text for c in event.get_messages() or [] if isinstance(c, Comp.Plain)),
            "",
        ).strip()
        prefixes = (
            self._list("skip_prefixes") if "skip_prefixes" in self.config else ["/"]
        )
        for p in prefixes:
            if raw.startswith(p) or first.startswith(p):
                return True
        for h in event.get_extra("activated_handlers") or []:
            # 任何一个插件（包括本插件自己的 /省token、/活人感）注册的指令，被这条消息触发了 → 当指令：不攒、不等
            if _CMD_FILTERS and any(
                isinstance(f, _CMD_FILTERS)
                for f in getattr(h, "event_filters", []) or []
            ):
                return True
        return False

    # ───────── 一条消息到了：先记一笔（唤醒检查那一步，图还没下载）─────────
    def arrive(self, event):
        if not self._scene_ok(event) or id(event) in self.by_ev:
            return
        t = getattr(event, "created_at", None) or time.time()
        m = self.g.note(self._key(event), t=t, ev=event)
        self.by_ev[id(event)] = m
        if len(self.by_ev) > 400:  # 清掉早就没用的
            live = {id(x) for p in self.g.piles.values() for x in p.msgs}
            self.by_ev = {k: v for k, v in self.by_ev.items() if id(v) in live}

    def _msg(self, event):
        m = self.by_ev.get(id(event))
        return m if m is not None and m.ev is event else None

    # ───────── 「正在输入」（v0.3.0）─────────
    def on_notice(self, event, raw):
        """QQ 的通知：私聊里「对方正在输入」→ 记下时间（攒一攒等完以后他还在输入就再等等）；停了 → 清掉。
        （v0.4.0）撤回 → 还排着没回的那条不回了"""
        try:
            if not self._on():
                return
            if raw.get("notice_type") in RECALL:
                self._recall(event, raw)
                return
            if (
                raw.get("notice_type") != "notify"
                or raw.get("sub_type") != "input_status"
            ):
                return
            if raw.get("group_id") not in (None, 0, "0", ""):
                return
            key = self._key(event)
            st = str(raw.get("status_text") or "")
            if str(raw.get("event_type")) == "1" or "输入" in st:
                self.typing_at[key] = time.time()
                if len(self.typing_at) > 500:
                    now = time.time()
                    self.typing_at = {
                        k: t for k, t in self.typing_at.items() if now - t < 60
                    }
            else:
                self.typing_at.pop(key, None)
        except Exception as e:
            self._oops("记「正在输入」的时候", e)

    def _typing(self, m):
        t = self.typing_at.get(m.key)
        return bool(t) and time.time() - t <= TYPING_RECENT

    # ───────── 撤回（v0.4.0）─────────
    def _recall(self, event, raw):
        """他撤回了一条：还排着没回的 → 不算进来；还没到的（撤回通知先到）→ 记着，到了再认"""
        if not truthy(self.config.get("recall_drop"), True):
            return
        mid = raw.get("message_id")
        if mid in (None, ""):
            return
        now = time.time()
        if len(self.recalled) > 300:
            self.recalled = {
                k: t for k, t in self.recalled.items() if now - t < RECALL_KEEP
            }
        self.recalled[str(mid)] = now
        self.g.set_cfg(self._core_cfg())
        x = self.g.recall(self._key(event), str(mid))
        if x is not None:
            self._today()["recalled"] += 1
            self._save()
            logger.info(
                f"{TAG} {x.where} {x.who} 把还排着没回的一条撤回了（「{x.line[:20]}」）→ 不回这条"
            )

    def _recalled_early(self, m):
        """这条到了：撤回通知已经先到了 → 记成撤回了（不并、不回）"""
        mid = str((m.data or {}).get("mid") or "")
        t = self.recalled.get(mid) if mid else None
        if t is None or time.time() - t > RECALL_KEEP:
            return
        if self.g.recall_msg(m):
            self._today()["recalled"] += 1
            logger.info(
                f"{TAG} {m.where} {m.who} 撤回了一条（撤回通知比消息先到）→ 不回这条"
            )

    # ───────── 尾巴含糊：问一次「他说完了吗」（v0.3.0）─────────
    def _before_lines(self, m):
        """他这一串之前群里的几句（私聊 / 拿不到 → []）"""
        ev = m.ev
        try:
            gid = str(ev.get_group_id() or "")
            if not gid:
                return []
            hist = list(
                self.host.shared.recent.get(f"{ev.get_platform_id()}:{gid}", ())
            )
        except Exception:
            return []
        rows = [h for h in hist if float(h.get("ts") or 0) < m.start and h.get("text")]
        return [
            f"{'机器人' if h.get('role') == 'assistant' else (h.get('name') or '某人')}: "
            + re.sub(r"\s+", " ", str(h.get("text")))[:80]
            for h in rows[-3:]
        ]

    async def _ask_tail(self, m, mem):
        """尾巴含糊 → 问判断模型（只给这一串话 + 前 3 行聊天，不带人设）→ 还要等几秒（0 = 说完了）"""
        ev = m.ev
        st = self._today()
        st["tail_asks"] += 1
        who = m.who or "他"
        last = mem[-1].line if mem else m.line
        said = [" ".join(str(x.line or "").split())[:120] for x in (mem or [m])[-4:]]
        rq = (
            "【前面的聊天】\n"
            + ("\n".join(self._before_lines(m)) or "（没有）")
            + f"\n【{who} 刚连着发的】\n"
            + "\n".join(f"{who}: {x}" for x in said)
        )
        try:
            pid = (
                str(self.config.get("tail_provider_id") or "").strip()
                or str(self.host.sub("rhythm").get("judge_provider_id") or "").strip()
            )
            if not pid:
                pid = await self.host.context.get_current_chat_provider_id(
                    ev.unified_msg_origin
                )
            resp = await asyncio.wait_for(
                self.host.context.llm_generate(
                    chat_provider_id=pid, prompt=rq, system_prompt=TAIL_SYS
                ),
                TAIL_TIMEOUT,
            )
        except Exception as e:
            logger.info(
                f"{TAG} {m.where} {who} 尾巴含糊，问了一下没问成（{type(e).__name__}）→ 当成说完了"
            )
            return 0
        b = self.host.budget
        if b is not None and self.host.active("budget"):
            try:
                b.record_usage(resp)
            except Exception:
                pass
        out = re.sub(
            r"<(think|thinking)>.*?</\1>",
            "",
            getattr(resp, "completion_text", "") or "",
            flags=re.S | re.I,
        )
        hit = re.search(r"等\s*(\d{1,2})", out)
        if hit:
            n = max(3, min(12, int(hit.group(1))))
            logger.info(
                f"{TAG} {m.where} {who} 尾巴含糊（「{last[-12:]}」），问了一下 → 再等 {n} 秒"
            )
            return n
        logger.info(
            f"{TAG} {m.where} {who} 尾巴含糊（「{last[-12:]}」），问了一下 → 说完了"
        )
        return 0

    # ───────── 记下内容 ─────────
    @staticmethod
    def _raw_segs(event):
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        try:
            segs = raw.get("message") if hasattr(raw, "get") else None
        except Exception:
            segs = None
        return (
            [s for s in segs if isinstance(s, dict)] if isinstance(segs, list) else None
        )

    @staticmethod
    def _to_other(event):
        """@ 了别人、或者引用的是别人的话：在跟那个人说（不叫模型的话，就不并进跟机器人说的那一轮）"""
        sid, uid = str(event.get_self_id() or ""), str(event.get_sender_id() or "")
        for c in event.get_messages() or []:
            if isinstance(c, Comp.At):  # AtAll 的 qq 是 "all"
                q = str(getattr(c, "qq", "") or "")
                if q and q not in (sid, "all"):
                    return True
            elif isinstance(c, Comp.Reply):
                q = str(getattr(c, "sender_id", "") or "")
                if q and q not in ("0", sid, uid):  # 拿不到是谁（0 / 空）的不算
                    return True
        return False

    def _quiet(self, m):
        """这条不会叫模型：@ 别人 / 引用别人的 → aside（在跟别人说：照样重新计时，但不并进来）；别的照样并进他那一轮"""
        self.g.mark(m, "aside" if (m.data or {}).get("to_other") else "quiet")

    def _fill(self, event, m):
        comps = list(event.get_messages() or [])
        segs = self._raw_segs(event)
        text = (event.message_str or "").strip()
        imgs = [c for c in comps if isinstance(c, Comp.Image)]
        mface = any(s.get("type") == "mface" for s in segs or [])
        sticker = any(
            str(
                (s.get("data") if isinstance(s.get("data"), dict) else {}).get(
                    "sub_type"
                )
            )
            == "1"
            for s in segs or []
            if s.get("type") == "image"
        )
        face = any(isinstance(c, Comp.Face) for c in comps)
        pic = bool(imgs or mface or face)
        if text:
            line = text
        elif imgs and not sticker:
            line = "[图片]"
        elif pic:
            line = "[表情]"
        elif any(isinstance(c, Comp.Record) for c in comps):
            line = "[语音]"
        elif any(isinstance(c, Comp.Video) for c in comps):
            line = "[视频]"
        elif any(isinstance(c, Comp.File) for c in comps):
            line = "[文件]"
        else:
            line = ""
        paths = []
        for c in imgs:
            ref = c.path or c.file or c.url
            if ref:
                paths.append(str(ref))
        m.who = event.get_sender_name() or str(event.get_sender_id())
        gid = str(event.get_group_id() or "")
        m.where = f"群 {gid}" if gid else "私聊"
        self.g.fill(
            m,
            line,
            pic=(pic and not text),
            data={
                "comps": comps,
                "segs": segs,
                "text": text,
                "imgs": paths,
                "to_other": self._to_other(event),
                "mid": str(getattr(event.message_obj, "message_id", "") or ""),
            },
        )
        if self._is_cmd(event):
            self.g.mark(m, "skip")
        if self.recalled:
            self._recalled_early(m)  # （v0.4.0）

    def on_message(self, event):
        """每条消息都记下内容（不拦）"""
        m = None
        try:
            self.host.ensure_loops()
            m = self._msg(event)
            if m is None or m.data is not None:
                return
            self._fill(event, m)
        except Exception as e:
            self._oops("记消息内容的时候", e, m)

    def on_message_tail(self, event):
        """插件都跑完了：这条不会叫模型的，告诉核心「它不会再来了」（等它的那条就不用多等）"""
        m = None
        try:
            m = self._msg(event)
            if m is None or m.state not in ("early", "new"):
                return
            if getattr(event, "_has_send_oper", False):
                self.g.mark(m, "skip")  # 别的插件已经回过这条了
            elif not (
                getattr(event, "is_at_or_wake_command", False)
                and not getattr(event, "call_llm", False)
            ):
                self._quiet(m)
        except Exception as e:
            self._oops("收尾的时候", e, m)

    def _probe(self, m):
        """等的时候问一下：还没定的那几条后来怎么样了"""
        ev = m.ev
        if ev is None:
            return None
        try:
            if ev.get_extra("_follow_up_captured"):
                return "dropped"  # AstrBot 把它塞进了正在跑的那一轮
            if ev.is_stopped():
                return "dropped"  # 被别的插件停掉了
        except Exception:
            return None
        if (
            getattr(ev, "_execution_active", None) is False
        ):  # 这条已经处理完了，没走到叫模型那一步
            if m.state != "new":
                return "skip"
            return "aside" if (m.data or {}).get("to_other") else "quiet"
        return None

    # ───────── 快要叫模型了：等一等 ─────────
    def _provider_wake_prefix(self, event):
        try:
            cfg = self.host.context.get_config(umo=event.unified_msg_origin)
            pwp = str((cfg.get("provider_settings") or {}).get("wake_prefix") or "")
            for bwp in cfg.get("wake_prefix") or []:
                if bwp and pwp.startswith(bwp):
                    pwp = pwp[len(bwp) :]
            return pwp
        except Exception:
            return ""

    def _will_call(self, event, pwp):
        """AstrBot 在这个钩子之后还会看「对话唤醒前缀」：对不上就不叫模型 —— 那这条也就不是「要回的」"""
        if event.get_extra("provider_request") is not None or not pwp:
            return True
        if event.get_platform_name() == "webchat":
            return True
        return (event.message_str or "").startswith(pwp)

    def _mine_active(self, event):
        mine = self.host.module_path()
        hs = event.get_extra("activated_handlers")
        if hs is None:
            return True
        return any(getattr(h, "handler_module_path", "") == mine for h in hs)

    async def wait(self, event):
        """→ True = 这条被静悄悄地停掉了（后面又来了要回的 / 已经并进前一轮回过了），后面的模块别再处理"""
        m = self._msg(event)
        try:
            return await self._wait(event, m)
        except Exception as e:
            self._oops("等着并的时候", e, m)
            return False

    async def _wait(self, event, m):
        if m is None or not self._on():
            return False
        if m.state == "early":
            if not self._mine_active(event):  # 这个会话里关了这个插件
                self.g.mark(m, "skip")
                return False
            self._fill(
                event, m
            )  # 更高优先级的插件直接叫了模型（收消息那一步还没轮到我）
        if m.state in ("skip", "dropped"):
            return False
        pwp = self._provider_wake_prefix(event)
        if not self._will_call(event, pwp):
            self._quiet(m)
            return False
        m.data = m.data or {}
        if (
            event.get_extra("provider_request") is not None
            and not m.data.get("text")
            and not any(
                isinstance(
                    c, (Comp.Image, Comp.Record, Comp.Video, Comp.File, Comp.Reply)
                )
                for c in m.data.get("comps") or []
            )
        ):
            self._quiet(m)  # 只 @ 不说话（AstrBot 自己会问「想聊什么」）：不攒
            return False
        kind = event.get_extra("fatfish_kind")
        # 直接找机器人的（@ / 私聊 / 引用 / 唤醒词）：没写 fatfish_kind、而且是被唤醒的。
        # 没被唤醒、也没写 kind 的（比如 AstrBot 自带的「主动回复」自己 request_llm）不算
        m.data["kind"] = kind
        m.data["direct"] = not kind and bool(
            getattr(event, "is_at_or_wake_command", False)
        )
        self.g.set_cfg(self._core_cfg())
        self.host.ensure_loops()
        verdict, mem = await self.g.hold(m)
        st = self._today()
        if verdict == "recalled":  # （v0.4.0）排着的只有他撤回的那条：这一轮不回
            event.stop_event()
            st["recall_skip"] += 1
            self._save()
            logger.info(f"{TAG} {m.where} {m.who} 把还排着的那条撤回了 → 这一轮不回")
            return True
        if verdict != "go":
            event.stop_event()  # 静悄悄地停掉：不叫模型、什么都不发
            st["saved"] += 1
            self._save()
            logger.debug(
                f"{TAG} {m.where} {m.who}：后面又来了，这条先不回（{'并到下一条' if verdict == 'handoff' else '已经回过了'}）"
            )
            return True
        st["turns"] += 1
        more = getattr(m, "more", None) or {}
        if more.get("typed"):
            st["typing_waits"] += 1
            logger.info(
                f"{TAG} {m.where} {m.who} 还在输入 → 多等了 {more['typed']:.0f} 秒"
            )
        if more.get("tail_waits"):
            st["tail_waits"] += int(more["tail_waits"])
        event.set_extra("fatfish_ask", self._ask(mem))
        if (
            m not in mem
        ):  # （v0.4.0）在等的这条他撤回了：这一轮只回剩下的（发消息模块按这几条看「撤回了她在回的那句」）
            mids = [
                str((x.data or {}).get("mid") or "")
                for x in mem
                if x.state == "carrier"
            ] or [str((mem[-1].data or {}).get("mid") or "")]
            event.set_extra("fatfish_mids", [x for x in mids if x])
            self._merge(event, m, mem, pwp)
            logger.info(
                f"{TAG} {m.where} {m.who} 把最后那条撤回了 → 只回前面的 {len(mem)} 条"
            )
        if len(mem) > 1:
            if m in mem:
                self._merge(event, m, mem, pwp)
            waited = round(max(0.0, time.time() - m.first), 1)
            pics = sum(len((x.data or {}).get("imgs") or []) for x in mem)
            st["merges"] += 1
            st["msgs"] += len(mem)
            self.recent.append(
                {
                    "t": int(time.time()),
                    "who": m.who,
                    "where": m.where,
                    "n": len(mem),
                    "waited": waited,
                    "pics": pics,
                    "preview": merge_lines([x.line for x in mem]).replace("\n", " / ")[
                        :60
                    ],
                }
            )
            logger.info(
                f"{TAG} {m.where} {m.who} 连着发了 {len(mem)} 条，等了 {waited} 秒，一起回"
            )
        self._save()
        return False

    @staticmethod
    def _ask(mem):
        """这几条里「找她的那一条」（v0.2.0，群里引用用）：最后一条直接找她的（@ / 引用 / 唤醒词）或接她话的；
        不是最后一条消息 —— 他 @ 完随手补的一句、她自己挑着接的那条都不算。一条都没有 → {}（她自己插话，不引用）"""
        for x in reversed(mem):
            dd = x.data or {}
            if x.state == "carrier" and (
                dd.get("direct") or dd.get("kind") == "follow"
            ):
                return {"mid": dd.get("mid") or "", "t": x.t}
        return {}

    # ───────── 把这几条并成一条 ─────────
    def _merge(self, event, m, mem, pwp):
        own = event.message_str or ""
        lines = []
        for x in mem:
            ln = x.line
            if pwp and ln.startswith(pwp):
                ln = ln[len(pwp) :]
            lines.append(ln)
        text = merge_lines(lines)
        head = pwp if pwp and own.startswith(pwp) else ""
        # 1) 文字：AstrBot 用 event.message_str 当这一轮用户说的话（会存进聊天记录，只存这一遍）
        event.message_str = head + text
        try:
            event.message_obj.message_str = head + text
        except Exception:
            pass
        # 2) 消息链：前后几条的图、引用、语音都放进来（AstrBot 从这里拿图给模型；看图插件也从这里找图）
        chain = []
        for x in mem:
            comps = (
                list(event.get_messages() or [])
                if x is m
                else list((x.data or {}).get("comps") or [])
            )
            if (
                chain
                and any(isinstance(c, Comp.Plain) for c in comps)
                and any(isinstance(c, Comp.Plain) for c in chain)
            ):
                chain.append(Comp.Plain(text="\n"))
            chain.extend(comps)
        try:
            event.message_obj.message = chain
        except Exception:
            pass
        # 3) QQ 原始消息段（看图插件靠它认表情包、商城表情、QQ 原来的文件名）：拼一份新的，不改原来那份
        segs = []
        for x in mem:
            s = (x.data or {}).get("segs")
            if s is None:
                segs = None
                break
            segs.extend(s)
        raw = getattr(event.message_obj, "raw_message", None)
        if segs is not None and hasattr(raw, "get"):
            try:
                new = copy.copy(raw)
                new["message"] = segs
                event.message_obj.raw_message = new
            except Exception:
                pass
        # 4) 别的插件自己叫的模型（request_llm）：AstrBot 用的是那个请求里的话和图
        pr = event.get_extra("provider_request")
        if pr is not None:
            try:
                if (
                    m not in mem
                ):  # （v0.4.0）这条他撤回了：请求里的话整个换成剩下的，这条的图也拿掉
                    pr.prompt = text
                    gone = set((m.data or {}).get("imgs") or [])
                    pr.image_urls[:] = [u for u in pr.image_urls or [] if u not in gone]
                elif (pr.prompt or "").strip() == own.strip():
                    pr.prompt = text
                else:
                    others = merge_lines(
                        [ln for x, ln in zip(mem, lines) if x is not m]
                    )
                    if others and TextPart is not None:
                        pr.extra_user_content_parts.append(
                            TextPart(text=f"（{m.who} 前后还连着说了：\n{others}）")
                        )
                have = set(pr.image_urls or [])
                for x in mem:
                    if x is m:
                        continue
                    for p in (x.data or {}).get("imgs") or []:
                        if p not in have:
                            pr.image_urls.append(p)
                            have.add(p)
            except Exception as e:
                logger.warning(f"{TAG} 并进插件的请求失败：{e!r}")
        # 5) 告诉别的插件：并了几条；里面有直接找机器人的（@ / 私聊 / 引用），就按直接找的算
        event.set_extra("fatfish_gathered", len(mem))
        carriers = [x for x in mem if x.state == "carrier"]
        if any((x.data or {}).get("direct") for x in carriers):
            event.set_extra("fatfish_kind", None)
            event.is_at_or_wake_command = True

    # ───────── 面板 ─────────
    def snapshot(self):
        return {
            "today": dict(self._today()),
            "recent": list(self.recent)[-20:][::-1],
            "waiting": [
                {"where": k.split("|")[0], "n": n, "sec": s}
                for k, n, s in self.g.waiting()
            ],
            "config": {
                "enable": self._c("enable", True),
                "private": self._c("private", True),
                "group": self._c("group", True),
                **{k: self._c(k, DEFAULTS[k]) for k in CORE_KEYS},
            },
        }
