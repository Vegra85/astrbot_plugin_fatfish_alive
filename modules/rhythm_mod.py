# -*- coding: utf-8 -*-
"""
群聊节奏（接话 + 在线一阵 + 冷场试探 + 收尾）—— AstrBot 这一层（原 astrbot_plugin_fatfish_rhythm/main.py；核心在 core/rhythm.py）

AstrBot 在群里默认只有被 @ / 唤醒才回。真人不是这样：
  - 没 @ 它、但明显在接它刚才的话 → 也接（接话）
  - 说上话以后会「在线」聊一阵：这期间没点名的话也挑着接（在线一阵）
  - 聊冷了试探一句；到点说句收尾话再潜水；收尾后一阵子不主动插话（冷场试探 + 收尾）
这一层只做 AstrBot 的壳：
  - 群消息监听：决定接 → event.set_extra("fatfish_kind", "follow" / "chat") + event.is_at_or_wake_command = True，
    让这条消息走 AstrBot 正常的回话流程（跟被 @ 一样：带对话记录、人格、群聊上下文，别的插件的钩子都照常跑）
  - on_llm_request：给这一轮附一句「这句没点你名…」（只给这一轮，不进聊天记录）
  - on_decorating_result：模型回了 [[不说]] → 这条不发、也不进对话记录；但这一轮带着的群聊上下文 + 这条消息
    留到这个会话下一次叫模型时补进去（AstrBot 的群聊上下文注入一叫模型就用掉了，不补就丢了）
  - after_message_sent：记下机器人刚说了什么、回了谁（接话打分用）；进 / 续「在线」
  - 后台每隔几秒看一眼在线的群：冷场试探 / 到点收尾（context.llm_generate + context.send_message）
跟本插件的省 token 模块是一体的（主动说之前问 ask("extra")，自己调模型的用量记 record_usage）；认 AI 还是别的插件（装了才用）。
活人感新增：「好了 / 就这样 / 睡了」不接；接话打分加「话题词没对上两个」一项。
v0.2.0：接话认错对象（跟别人的话更像 / 开头叫的是别人 → 扣分；「好几遍」不算在问）；打字刹车停了一半时只记真发出去的。
v0.4.0：接话打分（借 Inner Thoughts，判断在 core/inner.py）—— 没点名它的群消息（接话窗口里 / 在线聊着）按 8 条打分 + 一句理由：
  影子（默认）：照旧按原来的规则接不接，打分只记下来（面板、每天一个文件），跟实际接没接比；
  生效：换成按分数决定；硬规矩照旧（收尾句不接、同一个人不 @ 最多接几轮、在线时挑着接的间隔 / 次数、正在回话）；
        拿不准的「在接它的话」问一下判断模型（同一个模型、同样的问法），省 token 那道照样在叫模型之前拦。
  事后的证据（看要不要生效用）：没接、他 90 秒内又 @ 了它 → 多半漏接；接了、模型自己选了 [[不说]] → 多半不该插。
"""

import asyncio
import json
import time
from collections import deque

from astrbot.api import logger
from astrbot.api.event import MessageChain, MessageEventResult
from astrbot.api.provider import ProviderRequest
import astrbot.api.message_components as Comp

from ..core import inner as IT
from ..core.rhythm import (
    SILENT,
    Conf,
    Daily,
    Followup,
    Rhythm,
    carry_block,
    chat_hint,
    clean_line,
    follow_hint,
    is_closing,
    is_welcome,
    judge_body,
    judge_system,
    judge_yes,
    line_prompt,
    line_system,
    welcome_hint,
)

try:
    from astrbot.core.agent.message import TextPart
except Exception:  # 老版本 / 以后挪了地方：退回 dict
    TextPart = None
try:
    from astrbot.core.utils.session_lock import session_lock_manager
except Exception:
    session_lock_manager = None
try:
    from astrbot.core.star.session_llm_manager import SessionServiceManager
except Exception:
    SessionServiceManager = None

AIGUARD = "astrbot_plugin_fatfish_aiguard"
TAG = "[节奏]"
TAG_F = "[接话]"
BUSY_SEC = 90  # 「正在回话」最多算这么久（模型出错、被别的插件拦下时不会有 after_message_sent）
TAG_I = "[接话打分]"
IT_AFTER_SEC = 90.0  # （v0.4.0）没接的那句之后多少秒内他又 @ 了它，算「多半漏接了」
IT_FLUSH_SEC = 120.0  # 打分记录过这么久（事后的证据都齐了）再写进文件

_IMG_TYPES = tuple(
    t for t in (getattr(Comp, "Image", None), getattr(Comp, "Face", None)) if t
)
_SHOWN = {
    "Record": "[语音]",
    "Video": "[视频]",
    "File": "[文件]",
    "Forward": "[聊天记录]",
    "Json": "[卡片]",
}


def _part(text, temp):
    if TextPart is not None:
        p = TextPart(text=text)
        return p.mark_as_temp() if temp else p
    return {"type": "text", "text": text, **({"_no_save": True} if temp else {})}


def _ptext(p):
    """请求里的一个内容块 → 会存进对话记录的文字（只给这一轮的提示、图片这些 → ""）"""
    if isinstance(p, dict):
        return (
            ""
            if p.get("_no_save") or p.get("type") != "text"
            else str(p.get("text") or "")
        )
    if getattr(p, "_no_save", False) or getattr(p, "type", "") != "text":
        return ""
    return str(getattr(p, "text", "") or "")


class RhythmMod:
    """群聊节奏：没 @ 但在接它的话也接；说上话以后在线聊一阵；冷场试探、到点收尾"""

    name = "rhythm"

    def __init__(self, host):
        self.host = host
        self.context = host.context
        self.config = host.sub("rhythm")
        self.conf = Conf(self.config)
        self.data_dir = host.data_dir
        st = host.store.get("rhythm")
        self.daily = Daily(st.get("stats"))
        self.decisions = deque(st.get("decisions") or [], maxlen=60)  # 最近的接话判断
        self.lines = deque(
            st.get("lines") or [], maxlen=40
        )  # 最近主动说的（试探 / 收尾）
        self.fu = Followup(self.conf)
        self.rh = Rhythm(self.conf)
        self.buf = host.shared.recent  # 群 -> 最近的群聊（含机器人自己说的）
        self.umo = {}  # 群 -> 主动发话用的会话
        self.bot_nick = {}  # self_id -> QQ 昵称
        self.cards = {}  # (self_id, 群号) -> 群名片
        self.busy = {}  # 群 -> 开始回话的时间
        self.lining = set()  # 正在想试探 / 收尾那句的群
        self.carry = {}  # 会话 -> 上次 [[不说]] 那一轮没存下的群聊上下文
        # （v0.4.0）接话打分：今天的数、最近 40 次、还没写进文件的
        it = st.get("inner") if isinstance(st.get("inner"), dict) else {}
        self.it_daily = IT.Daily(it.get("stats"))
        self.it_recent = deque(
            [x for x in it.get("recent") or [] if isinstance(x, dict)], maxlen=40
        )
        self._it_seq = max((int(x.get("id") or 0) for x in self.it_recent), default=0)
        self._it_pending = []
        self._it_cleaned = ""
        self._ai_cache = (None, set())
        self._alive = True
        self._tasks = set()
        self._ticker = None
        host.store.register("rhythm", self.dump)

    def dump(self):
        return {
            "stats": self.daily.today(),
            "decisions": list(self.decisions),
            "lines": list(self.lines),
            "inner": {
                "stats": self.it_daily.today(time.strftime("%Y-%m-%d")),
                "recent": list(self.it_recent),
            },
        }

    def _save(self, force=False):
        self.host.save(force)

    def _ensure_loops(self):
        self.host.ensure_loops()

    def start(self):
        """起后台的试探 / 收尾循环（需要在事件循环里）"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._ticker is None or self._ticker.done():
            self._ticker = asyncio.create_task(self._tick_loop())

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def close(self):
        self._alive = False
        for t in list(self._tasks) + [self._ticker]:
            if t is not None and not t.done():
                t.cancel()
        self._it_flush(force=True)  # 还没写进文件的打分记录（v0.4.0）

    # ───────── 小工具 ─────────
    @staticmethod
    def _gk(event):
        return f"{event.get_platform_id()}:{event.get_group_id()}"

    def _group_ok(self, gid):
        gs = self.conf("groups")
        return not gs or str(gid) in gs

    @staticmethod
    def _kind(event):
        """这一轮是不是本插件决定接的（follow / chat）。别的插件改掉了 fatfish_kind 就不算了 ——
        比如「攒一攒」把同一个人前面直接 @ 它的那条并了进来（它会把 fatfish_kind 清掉）：这时再附「这句没 @ 你」就错了"""
        k = event.get_extra("rhythm_kind")
        return k if k and event.get_extra("fatfish_kind") == k else None

    @staticmethod
    def _at_me(event):
        """这条消息 @ 了它自己（AstrBot 唤醒以后 @ 它的那段还留在消息组件里，只是 message_str 里去掉了）"""
        sid = str(event.get_self_id() or "")
        try:
            return bool(sid) and any(
                isinstance(c, Comp.At) and str(getattr(c, "qq", "") or "") == sid
                for c in event.get_messages() or []
            )
        except Exception:
            return False

    def _mine_active(self, event):
        """AstrBot「会话管理」里给这个群关掉了本插件：消息监听不会跑，但钩子照样会调 —— 这里也别让它进在线、主动说话"""
        hs = event.get_extra("activated_handlers")
        if hs is None:
            return True
        mine = self.host.module_path()
        return any(getattr(h, "handler_module_path", "") == mine for h in hs)

    def _busy(self, gk, now=None):
        t = self.busy.get(gk)
        return bool(t) and (now or time.time()) - t < BUSY_SEC

    @staticmethod
    def _raw_types(event):
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        try:
            segs = raw.get("message") if hasattr(raw, "get") else None
        except Exception:
            segs = None
        return (
            [s.get("type") for s in segs if isinstance(s, dict)]
            if isinstance(segs, list)
            else []
        )

    def _parse(self, event, sid):
        """这条消息：文字、给打分 / 记录看的样子、@ 了谁、引用了谁、是不是光发图"""
        text, shown, re_name = [], [], ""
        to_other = pic = other = False
        for c in event.get_messages() or []:
            name = type(c).__name__
            if isinstance(c, Comp.Plain):
                text.append(c.text)
                shown.append(c.text)
            elif isinstance(c, Comp.At):
                q = str(getattr(c, "qq", "") or "")
                if q and q not in (sid, "all"):
                    to_other = True
                    shown.append(f"@{getattr(c, 'name', '') or q} ")
            elif isinstance(c, Comp.Reply):
                if str(getattr(c, "sender_id", "") or "") == sid:
                    re_name = "你"
                else:  # 引用的是别人（或者拿不到是谁）：在跟那个人说
                    to_other = True
                    re_name = str(getattr(c, "sender_nickname", "") or "某人")
            elif _IMG_TYPES and isinstance(c, _IMG_TYPES):
                pic = True
                shown.append("[图片]" if name == "Image" else "[表情]")
            else:
                other = True
                shown.append(_SHOWN.get(name, ""))
        if "mface" in self._raw_types(
            event
        ):  # QQ 商城表情：AstrBot 会丢掉，从原始消息里认
            pic = True
            shown.append("[表情]")
        t = "".join(text).strip()
        return {
            "text": t,
            "shown": "".join(shown).strip() or t,
            "to_other": to_other,
            "re": re_name,
            "only_pic": pic and not t and not other,
            "empty": not t and not pic and not other,
        }

    def _names(self, sid, gid):
        names = self.conf("bot_names") or [
            self.bot_nick.get(sid),
            self.cards.get((sid, gid)),
        ]
        out = []
        for n in names:
            if n and n not in out:
                out.append(n)
        return out

    async def _learn_name(self, event, sid, gid, need_nick, need_card):
        """没填机器人的名字：用它的 QQ 昵称和在这个群的群名片"""
        bot = getattr(event, "bot", None)
        if bot is None:
            return
        if need_nick:
            try:
                info = await bot.call_action("get_login_info")
                self.bot_nick[sid] = str((info or {}).get("nickname") or "")
            except Exception:
                pass
        if need_card:
            try:
                info = await bot.call_action(
                    "get_group_member_info",
                    group_id=int(gid),
                    user_id=int(sid),
                    no_cache=False,
                )
                self.cards[(sid, gid)] = str((info or {}).get("card") or "")
            except Exception:
                pass

    def _peer(self, name):
        """装了、开着的别的大肥鱼插件（用它的公开方法）；没装 / 没开 → None"""
        try:
            md = self.context.get_registered_star(name)
        except Exception:
            return None
        if md is None or not getattr(md, "activated", True):
            return None
        return getattr(md, "star_cls", None)

    def _budget_ok(self, umo, gid):
        """主动说一句之前问省 token 模块（本插件自己的；没开就直接说）→ (说不说, 说明)"""
        b = self.host.budget
        if b is None or not self.host.active("budget"):
            return True, ""
        try:
            ok, why = b.ask("extra", conv=umo, where=f"群 {gid}")
            return bool(ok), str(why or "")
        except Exception as e:
            logger.debug(f"{TAG} 问省 token 出错（照常说）：{e!r}")
            return True, ""

    def _budget_record(self, resp):
        """自己调模型（判断 / 试探 / 收尾）用掉的 token 记到省 token 模块"""
        b = self.host.budget
        if b is not None and self.host.active("budget"):
            try:
                b.record_usage(resp)
            except Exception as e:
                logger.debug(f"{TAG} 记账到省 token 出错：{e!r}")

    def _is_ai(self, uid):
        """认 AI 插件认出来的别人家的 AI：先问它（公开方法 is_bot），没装 / 没开就读它存的 state.json"""
        fn = getattr(self._peer(AIGUARD), "is_bot", None)
        if callable(fn):
            try:
                return bool(fn(str(uid)))
            except Exception:
                pass
        p = self.data_dir.parent / AIGUARD / "state.json"
        try:
            mt = p.stat().st_mtime
        except OSError:
            return False
        if mt != self._ai_cache[0]:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                bots = {str(k) for k in ((d or {}).get("bots") or {})}
            except Exception:
                bots = set()
            self._ai_cache = (mt, bots)
        return str(uid) in self._ai_cache[1]

    def _decide(self, gk, uid, name, text, score, why, verdict, took, kind="follow"):
        self.decisions.append(
            {
                "t": int(time.time()),
                "g": gk.split(":", 1)[-1],
                "who": name,
                "uid": str(uid),
                "text": (text or "")[:60],
                "score": round(float(score or 0), 1),
                "verdict": verdict,
                "took": bool(took),
                "why": (why or "")[:140],
                "kind": kind,
            }
        )
        self._save()

    # ───────── 每条群消息：要不要接 ─────────
    async def on_group(self, event):
        """群消息：要不要接（没 @ 但在接它的话 / 在线时挑着接）"""
        # 消息监听里漏出去的异常，AstrBot 会当成插件出错：被 @ 的那条会往群里发一句报错、整条消息停掉不回。自己兜住
        try:
            await self._on_group(event)
        except Exception as e:
            logger.warning(
                f"{TAG} 处理这条群消息出错（这条当没看见，AstrBot 照常处理）：{e!r}"[
                    :300
                ]
            )

    async def _on_group(self, event):
        if not self.conf("enable") or not self.host.active("rhythm"):
            return
        self._ensure_loops()
        gid, uid, sid = (
            str(event.get_group_id() or ""),
            str(event.get_sender_id() or ""),
            str(event.get_self_id() or ""),
        )
        if not gid or not uid or uid == sid or not self._group_ok(gid):
            return
        gk, now = self._gk(event), time.time()
        self.umo[gk] = f"{event.get_platform_id()}:GroupMessage:{gid}"
        if not self.conf("bot_names") and sid:
            need_nick = sid not in self.bot_nick
            need_card = gid.isdigit() and sid.isdigit() and (sid, gid) not in self.cards
            if need_nick:
                self.bot_nick[sid] = ""  # 先占上位置：别一条消息问一次
            if need_card:
                self.cards[(sid, gid)] = ""
            if need_nick or need_card:
                self._spawn(self._learn_name(event, sid, gid, need_nick, need_card))
        m = self._parse(event, sid)
        name = event.get_sender_name() or uid
        is_ai = self.conf("skip_ai") and self._is_ai(uid)
        hist = list(self.buf[gk])  # 这条之前的群聊（打分用）
        entry = {
            "role": "user",
            "uid": uid,
            "name": name,
            "text": m["shown"][:200],
            "ts": now,
        }
        if m["re"]:
            entry["re"] = m["re"]
        self.buf[gk].append(entry)
        self.rh.heard(gk, now)
        event.set_extra("rhythm_t", now)

        if event.is_at_or_wake_command:  # @ / 引用它 / 唤醒词：AstrBot 本来就会回
            self.fu.reset(gk, uid)  # 这个人的接话轮数重新算
            event.set_extra("rhythm_addr", True)
            self._it_at_after(
                gk, uid, now
            )  # （v0.4.0）他前面没被接的那句：多半是漏接了
            return
        if is_ai:
            self.daily.add("skip_ai")
            self._save()
            logger.info(
                f"{TAG} 群{gid} {name}({uid}) 是别人家的 AI（认 AI 插件认出来的）没 @ 就不接"
            )
            return
        if m["to_other"] or m["empty"]:
            return  # 在跟别人说（@ 别人 / 引用别人）→ 不抢话
        if self.rh.cooling(gk, now):
            return  # 刚收尾潜水：不主动接（被 @ 照样回）
        owner = bool(event.is_admin())
        text = m["text"] or m["shown"]
        names = self._names(sid, gid)
        # （v0.4.0）接话打分：在接话窗口里 / 在线聊着的才打（影子：只记；生效：按分数接不接）
        mode = self.conf("inner_mode")
        rec = None
        if mode != "off":
            rec = self._it_score(gk, uid, name, text, now, hist, m["only_pic"], names)
            if rec is not None:
                rec["block"] = self._it_block(gk, uid, text, now, owner, rec["kind"])
        if rec is not None and mode == "live":
            await self._it_live(
                event, gk, uid, name, text, now, hist, owner, names, rec
            )
            return
        await self._old_decide(event, gk, uid, name, text, now, hist, m, owner, names)
        if rec is not None:
            self._it_shadow(event, rec)

    async def _old_decide(self, event, gk, uid, name, text, now, hist, m, owner, names):
        """原来的接话 / 挑着接（v0.4.0 原样挪进来；接话打分是影子 / 关着时走这里）"""
        gid = gk.split(":", 1)[-1]

        # ── 接话：没 @ 它，但可能在接它刚才的话 ──
        verdict, why, score = self.fu.check(
            gk, uid, name, text, now, hist, m["only_pic"], owner, names
        )
        if verdict == "ask":
            ok, how = await self._judge(
                event.unified_msg_origin, hist, name, text, now, names
            )
            verdict = (
                "model_yes"
                if ok
                else ("model_no" if how.startswith("模型说") else "model_fail")
            )
            event.set_extra(
                "rhythm_judged", verdict
            )  # （v0.4.0）接话打分的影子拿不准时用模型这次的答案
            if ok and (
                self.rh.cooling(gk) or self.fu.capped(gk, uid, time.time(), owner)
            ):
                ok, how = False, how + "，但等模型的时候它刚收尾 / 这个人的轮数到了"
            why2 = f"{why}；{how}"
            self._decide(gk, uid, name, text, score, why2, verdict, ok)
            if ok:
                self._trigger(event, gk, uid, name, text, "follow", why2)
            else:
                logger.info(
                    f"{TAG_F} 群{gid} {name}「{text[:30]}」→ {how} 不当成跟它说（{why}）"
                )
            return
        if verdict == "yes":
            self._decide(gk, uid, name, text, score, why, "yes", True)
            self._trigger(event, gk, uid, name, text, "follow", why)
            return
        if why:  # 在接话窗口里、但分不够 / 轮数到了
            # 只占了「紧跟着它」的路人话不记（群里一热闹，面板会被刷屏）；有点信号的、轮数到了的、图没接的才记
            if any(
                w and not w.startswith("紧跟着它") and w != "没什么信号"
                for w in why.split("、")
            ):
                self._decide(gk, uid, name, text, score, why, "no", False)
                logger.info(
                    f"{TAG_F} 群{gid} {name}「{text[:30]}」→ 不当成跟它说（{why}；{score:g} 分）"
                )
            else:
                logger.debug(
                    f"{TAG_F} 群{gid} {name}「{text[:30]}」→ 不当成跟它说（{why}）"
                )

        # ── 在线一阵：正聊着，没点名的也挑着接 ──
        if self.conf("closing_skip") and is_closing(text):
            return  # 「好了 / 就这样 / 睡了」：说完就走的话，不挑着接
        if (
            not self.rh.is_active(gk)
            or self._busy(gk, now)
            or self.fu.capped(gk, uid, now, owner)
        ):
            return
        if self.rh.want_reply(gk, False, now, m["only_pic"]):
            self._trigger(event, gk, uid, name, text, "chat", "")

    def _trigger(self, event, gk, uid, name, text, kind, why):
        """决定接这条：标上 fatfish_kind（省 token 插件看这个），再让它走 AstrBot 正常的回话流程（跟被 @ 一样）"""
        now = time.time()
        event.set_extra("fatfish_kind", kind)
        event.set_extra("rhythm_kind", kind)
        event.set_extra("rhythm_why", why)
        event.is_at_or_wake_command = True
        gid = gk.split(":", 1)[-1]
        if kind == "follow":
            n = self.fu.took(gk, uid, now)
            logger.info(
                f"{TAG_F} 群{gid} {name}「{text[:30]}」没 @ 它 但在接它的话 → 接（{why}；第 {n} 轮）"
            )
        else:
            logger.info(
                f"{TAG} 群{gid} 正聊着（已经 {self.rh.minutes_in(gk, now)} 分钟）{name}「{text[:30]}」没点名 → 挑着接"
            )

    # ───────── 接话打分（v0.4.0，借 Inner Thoughts；判断在 core/inner.py）─────────
    def _it_score(self, gk, uid, name, text, now, hist, only_pic, names):
        """这条要不要打分（在接话窗口里、或者在线聊着）→ 打分记录 / None（不在范围里、出错）"""
        st = self.fu.st(gk)
        win = float(self.conf("window_sec"))
        near = bool(st["sent"]) and -5 <= now - st["sent"] <= win
        follow_ok = bool(self.conf("follow_enable")) and near
        active = self.rh.is_active(gk)
        if not (follow_ok or active):
            return None
        try:
            ev = IT.evaluate(
                text,
                uid,
                name,
                now,
                hist,
                bot={
                    "text": st["text"],
                    "sent": st["sent"],
                    "t0": st["t0"],
                    "answered": st["answered"],
                },
                names=names,
                only_pic=only_pic,
                active=active,
                noise=self.host.shared.noise(gk, now),
                turns=self.fu.turns(gk, uid, now),
                window=win,
                growth=self.conf("inner_growth"),
                cap=self.conf("inner_growth_cap"),
            )
        except Exception as e:
            logger.debug(f"{TAG_I} 打分出错（这条不打）：{e!r}")
            return None
        kind = IT.kind_of(ev, uid in st["answered"])
        if kind == "follow" and not follow_ok:
            kind = "chat"
        if kind == "chat" and not active:
            kind = "follow" if follow_ok else ""
        thr, band = float(self.conf("inner_threshold")), float(self.conf("inner_band"))
        self._it_seq += 1
        hint = "、".join(
            w for k in IT.KEYS if ev["s"][k] >= 3.5 for w in ev["up"].get(k) or ()
        )
        return {
            "id": self._it_seq,
            "t": int(now),
            "ts": now,
            "gk": gk,
            "g": gk.split(":", 1)[-1],
            "uid": str(uid),
            "who": str(name or "")[:16],
            "text": str(text or "")[:60],
            "s": ev["s"],
            "base": ev["base"],
            "quiet": ev["quiet"],
            "boost": ev["boost"],
            "total": ev["total"],
            "thr": thr,
            "v": IT.verdict(ev["total"], thr, band),
            "kind": kind,
            "why": IT.reason(ev),
            "hint": hint[:80],
            "it": "",
            "now": "",
            "judge": "",
            "block": "",
            "out": [],
            "mode": self.conf("inner_mode"),
        }

    def _it_block(self, gk, uid, text, now, owner, kind):
        """跟以前一样的硬规矩（打分过线也不接）→ 原因 / ""。影子里也算，这样「打分会不会接」就是生效以后真会怎样"""
        if not kind:
            return "这时候既不在接话窗口里、也没在线"
        if self.conf("closing_skip") and is_closing(text):
            return "收尾句不接"
        if self.fu.capped(gk, uid, now, owner):
            return (
                f"不 @ 它已经连着接了 {self.fu.turns(gk, uid, now)} 轮（要 @ 它才接）"
            )
        if kind == "chat":
            if self._busy(gk, now):
                return "它正在回别人"
            return self.rh.chat_block(gk, now)
        return ""

    async def _it_live(self, event, gk, uid, name, text, now, hist, owner, names, rec):
        """接话打分生效：按分数决定接不接（拿不准的「在接它的话」问一下判断模型；挑着接的按线定）"""
        v, kind, gid = rec["v"], rec["kind"], rec["g"]
        if v == "maybe" and not rec["block"]:
            if (
                kind == "follow"
                and self.conf("ask_model")
                and float(self.conf("inner_band")) > 0
            ):
                ok, how = await self._judge(
                    event.unified_msg_origin, hist, name, text, now, names
                )
                rec["judge"] = (
                    "yes" if ok else ("no" if how.startswith("模型说") else "fail")
                )
                v = "take" if ok else "skip"
                if ok and (
                    self.rh.cooling(gk) or self.fu.capped(gk, uid, time.time(), owner)
                ):
                    rec["block"] = "等模型的时候它刚收尾 / 这个人的轮数到了"
            else:
                v = "take" if rec["total"] >= rec["thr"] else "skip"
        take = v == "take" and not rec["block"]
        rec["it"] = "take" if take else "skip"
        rec["now"] = kind if take else ""
        why = rec["why"] + (f"；{rec['block']}" if rec["block"] else "")
        if take or rec["judge"]:
            label = {"yes": "model_yes", "no": "model_no", "fail": "model_fail"}.get(
                rec["judge"], "inner_yes" if take else "inner_no"
            )
            self._decide(
                gk, uid, name, text, rec["total"], why, label, take, kind or "follow"
            )
        self._it_note(rec, event)
        if take:
            if kind == "chat":
                self.rh.note_try(gk, now)
            self._trigger(event, gk, uid, name, text, kind, rec["hint"] or rec["why"])
            logger.info(
                f"{TAG_I} 群{gid} {name}「{text[:30]}」→ 接（{rec['total']:g} 分，线 {rec['thr']:g}；{why}）"
            )
        else:
            logger.info(
                f"{TAG_I} 群{gid} {name}「{text[:30]}」→ 不接（{rec['total']:g} 分，线 {rec['thr']:g}；{why}）"
            )

    def _it_shadow(self, event, rec):
        """影子：原来的规则已经定了接不接；这里只补上「打分会怎样」，记下来比"""
        v = rec["v"]
        judged = event.get_extra("rhythm_judged") or ""
        if v == "maybe":
            if rec["kind"] == "follow" and judged in ("model_yes", "model_no"):
                rec["judge"] = "yes" if judged == "model_yes" else "no"
                v = "take" if judged == "model_yes" else "skip"
            elif (
                rec["kind"] != "follow"
                or not self.conf("ask_model")
                or float(self.conf("inner_band")) <= 0
            ):
                v = "take" if rec["total"] >= rec["thr"] else "skip"
            else:
                v = "ask"  # 生效的话会问一下模型；影子里不另外问（不花钱）
        if v == "take" and rec["block"]:
            v = "skip"
        rec["it"] = v
        rec["now"] = self._kind(event) or ""
        self._it_note(rec, event)
        took = bool(rec["now"])
        if v in ("take", "skip") and (v == "take") != took:
            logger.info(
                f"{TAG_I}（影子）群{rec['g']} {rec['who']}「{rec['text'][:30]}」→ 打分{'会接' if v == 'take' else '不接'}"
                f"（{rec['total']:g} 分，线 {rec['thr']:g}），现在的规则{'接了' if took else '没接'}；{rec['why']}"
            )
        else:
            logger.debug(
                f"{TAG_I}（影子）群{rec['g']} {rec['who']}「{rec['text'][:30]}」→ {v}（{rec['total']:g} 分）现在{'接了' if took else '没接'}"
            )

    def _it_note(self, rec, event=None):
        self.it_daily.note(rec, time.strftime("%Y-%m-%d"))
        self.it_recent.append(rec)
        self._it_pending.append(rec)
        if event is not None:
            event.set_extra("inner_rec", rec["id"])
        self._save()

    def _it_at_after(self, gk, uid, now):
        """他 @ 了它：他 90 秒内没被接的那句（最近一句）多半是漏接了 → 记一笔"""
        for rec in reversed(self.it_recent):
            if rec.get("gk") != gk or rec.get("uid") != str(uid):
                continue
            if now - float(rec.get("ts") or 0) > IT_AFTER_SEC:
                return
            if not rec.get("now") and "at_after" not in (rec.get("out") or []):
                rec.setdefault("out", []).append("at_after")
                self.it_daily.outcome(rec, "at_after", time.strftime("%Y-%m-%d"))
                self._save()
                logger.info(
                    f"{TAG_I} 群{rec.get('g')} {rec.get('who')} 没被接的「{str(rec.get('text'))[:20]}」之后又 @ 了它"
                    f"（打分当时说{'接' if rec.get('it') == 'take' else '不接' if rec.get('it') == 'skip' else '拿不准'}）→ 记成多半漏接了"
                )
            return

    def _it_silent(self, event):
        """接了、模型自己选了 [[不说]]：多半不该插 → 记一笔"""
        rid = event.get_extra("inner_rec")
        if not rid:
            return
        for rec in reversed(self.it_recent):
            if rec.get("id") == rid:
                if "silent" not in (rec.get("out") or []):
                    rec.setdefault("out", []).append("silent")
                    self.it_daily.outcome(rec, "silent", time.strftime("%Y-%m-%d"))
                return

    def _it_flush(self, now=None, force=False):
        """打分记录过了 2 分钟（事后的证据都齐了）写进 plugin_data/…/inner/日期.jsonl；留 inner_log_days 天"""
        now = float(now or time.time())
        days = int(self.conf("inner_log_days"))
        ready = [
            r
            for r in self._it_pending
            if force or now - float(r.get("ts") or 0) >= IT_FLUSH_SEC
        ]
        if not ready:
            return
        self._it_pending = [r for r in self._it_pending if r not in ready]
        if days <= 0:
            return
        d = self.data_dir / "inner"
        try:
            d.mkdir(parents=True, exist_ok=True)
            by_day = {}
            for r in ready:
                row = {
                    k: v for k, v in r.items() if k not in ("gk", "ts", "hint", "mode")
                }
                row["mode"] = r.get("mode")
                day = time.strftime(
                    "%Y-%m-%d", time.localtime(float(r.get("ts") or now))
                )
                by_day.setdefault(day, []).append(json.dumps(row, ensure_ascii=False))
            for day, rows in by_day.items():
                with open(d / f"{day}.jsonl", "a", encoding="utf-8") as f:
                    f.write("\n".join(rows) + "\n")
            today = time.strftime("%Y-%m-%d", time.localtime(now))
            if self._it_cleaned != today:
                self._it_cleaned = today
                for f in d.glob("*.jsonl"):
                    if now - f.stat().st_mtime > days * 86400:
                        f.unlink()
        except Exception as e:  # 写不进去不影响接话
            logger.debug(f"{TAG_I} 写打分记录出错：{e!r}")

    async def _judge(self, umo, hist, name, text, now, names):
        """拿不准：让模型看最近几条判断。返回 (是不是, 怎么判的)"""
        if self.daily.today().get("judge", 0) >= int(self.conf("judge_daily_max")):
            return False, "今天让模型判断的次数到上限了"
        if not await self._llm_allowed(umo):
            return False, "这个会话没开 AI（问了也不会回，不花这个钱）"
        try:
            pid = str(
                self.conf("judge_provider_id") or ""
            ).strip() or await self.context.get_current_chat_provider_id(umo)
        except Exception as e:
            return False, f"没有能用的模型（{e!r}"[:80] + "）"
        self.daily.add("judge")
        bot = names[0] if names else "机器人"
        try:
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=pid,
                    prompt=judge_body(hist, name, text, now, bot),
                    system_prompt=judge_system(bot),
                ),
                float(self.conf("judge_timeout_sec")),
            )
        except asyncio.TimeoutError:
            self._save()
            return False, f"模型 {int(self.conf('judge_timeout_sec'))} 秒没答"
        except Exception as e:
            self._save()
            logger.warning(f"{TAG_F} 让模型判断失败：{e!r}"[:200])
            return False, "模型判断出错"
        self._budget_record(resp)
        out = str(getattr(resp, "completion_text", "") or "").strip()
        ok = judge_yes(out)
        if ok:
            self.daily.add("judge_yes")
        self._save()
        return ok, f"模型说「{out[:6]}」"

    # ───────── 要叫模型了：记下开始想的时间；接话 / 挑着接的这一轮附一句 ─────────
    def mark_busy(self, event):
        """前面的模块（攒一攒、省 token）都没拦，才算「正在回话」"""
        if (
            not self.host.active("rhythm")
            or event.is_private_chat()
            or not event.get_group_id()
        ):
            return
        self.busy[self._gk(event)] = time.time()

    def on_llm_req(self, event, req: ProviderRequest):
        if (
            not self.host.active("rhythm")
            or event.is_private_chat()
            or not event.get_group_id()
        ):
            return
        event.set_extra("rhythm_t0", time.time())
        event.set_extra(
            "rhythm_req", req
        )  # 模型要是回 [[不说]]：从这里拿这一轮带着的群聊上下文
        carry = self.carry.pop(event.unified_msg_origin, None)
        if carry:  # 上次 [[不说]] 那一轮没存下的：补进这一轮（跟着这一轮存进记录）
            try:
                req.extra_user_content_parts.insert(0, _part(carry, temp=False))
                logger.info(
                    f"{TAG} 群{event.get_group_id()} 上次没接话那一段群聊补进了这一轮"
                )
            except Exception as e:
                logger.warning(f"{TAG} 补上次没接话那一段失败：{e!r}")
        kind = self._kind(event)
        if (
            not kind and self._at_me(event) and is_welcome(event.message_str)
        ):  # 有人 @ 它说欢迎 = 在欢迎它（v0.1.1）
            try:
                req.extra_user_content_parts.append(
                    _part(welcome_hint(event.get_sender_name() or ""), temp=True)
                )
                logger.info(
                    f"{TAG} 群{event.get_group_id()} {event.get_sender_name()} @ 它说欢迎 → 提醒它是在欢迎它自己"
                )
            except Exception as e:
                logger.warning(f"{TAG} 附欢迎提示失败：{e!r}")
        if not kind or not self.conf("enable"):
            return
        gk = self._gk(event)
        hint = (
            follow_hint(event.get_extra("rhythm_why"))
            if kind == "follow"
            else chat_hint(self.rh.minutes_in(gk))
        )
        try:
            req.extra_user_content_parts.append(
                _part(hint, temp=True)
            )  # 只给这一轮，不进聊天记录
        except Exception as e:
            logger.warning(f"{TAG} 附提示失败：{e!r}")

    # ───────── 模型回了 [[不说]]：这条不发、也不进对话记录 ─────────
    def on_decorate(self, event):
        if (
            not self.host.active("rhythm")
            or event.is_private_chat()
            or not event.get_group_id()
        ):
            return
        res = event.get_result()
        if res is None or not res.chain:
            return
        try:
            text = "".join(c.text for c in res.chain if isinstance(c, Comp.Plain))
        except Exception:
            return
        if SILENT not in text:
            return
        event.set_result(MessageEventResult())
        event.stop_event()
        gk = self._gk(event)
        self.busy.pop(gk, None)
        self._carry_over(event)
        self._it_silent(event)  # （v0.4.0）接话打分：接了、模型选了不说
        self.daily.add("silent")
        self._save()
        logger.info(f"{TAG} 群{event.get_group_id()} 它想了想没接（模型回了 {SILENT}）")

    def _carry_over(self, event):
        """这一轮不进对话记录了：把它本来要存的文字（这条消息 + AstrBot 注入的群聊上下文，已经被这一轮用掉了）留给下一轮"""
        req = event.get_extra("rhythm_req")
        if req is None:
            return
        try:
            parts = [
                _ptext(p)
                for p in (getattr(req, "extra_user_content_parts", None) or [])
            ]
            said = getattr(req, "prompt", None) or event.message_str
            text = carry_block(
                parts, said, event.get_sender_name() or str(event.get_sender_id() or "")
            )
        except Exception as e:
            logger.debug(f"{TAG} 留上下文失败：{e!r}")
            return
        if text:
            self.carry[event.unified_msg_origin] = text

    # ───────── 发完一句：记下它说了什么、回了谁；进 / 续在线 ─────────
    def after_sent(self, event):
        if event.is_private_chat() or not event.get_group_id():
            return
        gid = str(event.get_group_id())
        if (
            not self.host.active("rhythm")
            or not self.conf("enable")
            or not self._group_ok(gid)
            or not self._mine_active(event)
        ):
            return
        res = event.get_result()
        try:
            text = " ".join(
                c.text for c in (res.chain if res else []) if isinstance(c, Comp.Plain)
            ).strip()
            if not text and res and any(isinstance(c, Comp.Image) for c in res.chain):
                text = "[图片]"
        except Exception:
            text = ""
        cut = event.get_extra("alive_sent_text")
        if cut is not None:  # 打字刹车停了一半（v0.2.0）：只记真发出去的那几段
            text = str(cut).strip()
            if not text:  # 叫停在第一段之前（v0.4.0）：她一句都没说出去 —— 不算说过话，「正在回话」也清掉
                self.busy.pop(self._gk(event), None)
                return
        if not text:
            return
        gk, now = self._gk(event), time.time()
        uid, sid = str(event.get_sender_id() or ""), str(event.get_self_id() or "")
        self.umo.setdefault(gk, f"{event.get_platform_id()}:GroupMessage:{gid}")
        t0 = event.get_extra("rhythm_t0") or event.get_extra("rhythm_t") or now
        self.fu.note_turn(gk, text, [uid] if uid and uid != sid else [], t0, now)
        self.host.shared.note_said(gk, [uid] if uid and uid != sid else [], now)
        self.buf[gk].append({"role": "assistant", "text": text[:200], "ts": now})
        self.busy.pop(gk, None)
        if event.get_extra("rhythm_sent"):
            return  # 同一轮发了好几段（比如中间有工具调用的提示）：只算一次
        event.set_extra("rhythm_sent", True)
        kind = self._kind(event)
        if kind:
            self.daily.add(kind)
            self._save()
        model = False
        try:
            model = bool(res and res.is_model_result())
        except Exception:
            model = False
        if kind or model:  # 模型回的话才算「说上话了」（别的插件的指令回复不算）
            if self.rh.spoke(gk, kind != "chat", now) == "enter":
                st = self.rh.status(gk, now)
                logger.info(
                    f"{TAG} 群{gid} 聊起来了 → 在线 {round(st['left_sec'] / 60, 1)} 分钟"
                )

    def _tick_every(self):
        xs = [
            float(self.conf(k)) * 60 / 5
            for k in ("idle_min", "online_min", "probe_wait_min")
        ]
        return max(1.0, min([20.0] + [x for x in xs if x > 0]))

    async def _tick_loop(self):
        while self._alive:
            try:
                await asyncio.sleep(self._tick_every())
                self._it_flush()  # （v0.4.0）打分记录写进文件
                if not self.conf("enable") or not self.host.active("rhythm"):
                    continue
                now = time.time()
                for gk in list(self.rh.active_groups()):
                    if self._busy(gk, now) or gk in self.lining:
                        continue  # 正在回话 / 正在想主动说的那句 等会儿再看
                    act = self.rh.tick(gk, now)
                    if act in ("probe", "exit"):
                        self._spawn(self._line(gk, act))
                    elif act == "quiet":
                        logger.info(
                            f"{TAG} 群{gk.split(':', 1)[-1]} 没人聊了 安静回到观望"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"{TAG} 后台出错：{e!r}"[:200])

    async def _llm_allowed(self, umo):
        try:
            if not (
                self.context.get_config(umo=umo).get("provider_settings") or {}
            ).get("enable", True):
                return False
        except Exception:
            pass
        if SessionServiceManager is not None:
            try:
                if not await SessionServiceManager.is_session_enabled(umo):
                    return False
                if not await SessionServiceManager.is_llm_enabled_for_session(umo):
                    return False
            except Exception:
                pass
        return True

    async def _persona_prompt(self, umo):
        """当前默认人格（AstrBot 配置里选的那个）"""
        try:
            p = await self.context.persona_manager.get_default_persona_v3(umo)
            return str((p or {}).get("prompt") or "")
        except Exception:
            return ""

    async def _line(self, gk, act):
        """机器人主动说一句：冷场试探（probe）/ 收尾潜水（exit）"""
        tag = "冷场试探" if act == "probe" else "收尾潜水"
        gid = gk.split(":", 1)[-1]
        umo = self.umo.get(gk)
        self.lining.add(gk)
        try:
            if not umo:
                return
            now = time.time()
            if not self.rh.line_ok(gk, now):
                logger.info(
                    f"{TAG} 群{gid} {tag}：这个群这小时主动说够了（每小时最多 {int(self.conf('lines_per_hour'))} 句）这次不说"
                )
                return
            if not await self._llm_allowed(umo):
                return
            ok, why = self._budget_ok(umo, gid)
            if not ok:
                logger.info(f"{TAG} 群{gid} {tag}：省 token 插件说这次算了（{why}）")
                return
            pid = str(
                self.conf("line_provider_id") or ""
            ).strip() or await self.context.get_current_chat_provider_id(umo)
            system = line_system(
                await self._persona_prompt(umo), act, time.strftime("%H:%M")
            )
            prompt = line_prompt(list(self.buf[gk]), now, act)
            self.rh.note_line(gk, now)
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=pid, prompt=prompt, system_prompt=system
                ),
                60,
            )
            self._budget_record(resp)
            line = clean_line(getattr(resp, "completion_text", "") or "")
            if not line:
                logger.info(f"{TAG} 群{gid} {tag}：它想了想没说")
                return
            if self._busy(gk):
                logger.info(f"{TAG} 群{gid} {tag}：它正在回别人 这句不说了")
                return
            await self.context.send_message(umo, MessageChain().message(line))
            self.host.send.note_self(umo)  # 群里引用要知道「她中间说过话」
            t = time.time()
            hist = list(self.buf[gk])
            mine = max(
                (i for i, h in enumerate(hist) if h.get("role") == "assistant"),
                default=-1,
            )
            answered = {
                h.get("uid") for h in hist[mine + 1 :][-6:] if h.get("role") == "user"
            }  # 顺着谁的话说的
            self.fu.note_turn(gk, line, answered, t, t)
            self.buf[gk].append({"role": "assistant", "text": line, "ts": t})
            self.daily.add(act)
            self.lines.append({"t": int(t), "g": gid, "kind": act, "text": line[:120]})
            self._save(force=True)
            logger.info(f"{TAG} 群{gid} {tag} → {line}")
            await self._remember(
                umo,
                "（群里安静了一会儿）" if act == "probe" else "（你要下线 / 潜水了）",
                line,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"{TAG} 群{gid} {tag} 失败：{e!r}"[:200])
        finally:
            self.lining.discard(gk)

    async def _remember(self, umo, hint, line):
        """主动说的这句也记进 AstrBot 的对话记录（不然它下一轮不知道自己说过「我先潜水了」）"""
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return
            pair = (
                {"role": "user", "content": hint},
                {"role": "assistant", "content": line},
            )
            if session_lock_manager is not None:
                async with session_lock_manager.acquire_lock(
                    umo
                ):  # 别跟正在进行的回话抢着写
                    await cm.add_message_pair(cid, *pair)
            else:
                await cm.add_message_pair(cid, *pair)
        except Exception as e:
            logger.debug(f"{TAG} 记进对话记录失败：{e!r}")

    # ───────── 面板 ─────────
    def snapshot(self):
        now = time.time()
        groups = []
        for gk in sorted(set(self.rh.g) | set(self.umo)):
            st = self.rh.status(gk, now)
            fst = self.fu.st(gk)
            groups.append(
                {
                    "gk": gk,
                    "g": gk.split(":", 1)[-1],
                    **st,
                    "said_ago": int(now - fst["sent"]) if fst["sent"] else None,
                    "noise": self.host.shared.noise(gk, now),
                    "turns": {
                        u: v[0]
                        for u, v in fst["turns"].items()
                        if now - v[1] <= float(self.conf("turn_reset_min")) * 60
                    },
                }
            )
        order = {"在线": 0, "收尾冷却": 1, "观望": 2}
        groups.sort(
            key=lambda d: (order.get(d["mode"], 3), -(d.get("last_other") or 0))
        )
        return {
            "today": dict(self.daily.today()),
            "groups": groups[:50],
            "decisions": list(self.decisions)[-40:][::-1],
            "lines": list(self.lines)[-20:][::-1],
            "inner": {  # （v0.4.0）接话打分
                "mode": self.conf("inner_mode"),
                "threshold": self.conf("inner_threshold"),
                "band": self.conf("inner_band"),
                "growth": self.conf("inner_growth"),
                "cap": self.conf("inner_growth_cap"),
                "log_days": self.conf("inner_log_days"),
                "names": IT.NAMES,
                "keys": list(IT.KEYS),
                "today": dict(self.it_daily.today(time.strftime("%Y-%m-%d"))),
                "recent": [
                    {k: v for k, v in r.items() if k not in ("gk", "ts", "hint")}
                    for r in list(self.it_recent)[::-1]
                ],
            },
            "enabled": bool(self.conf("enable")),
            "config": {
                k: self.conf(k)
                for k in (
                    "follow_enable",
                    "rhythm_enable",
                    "window_sec",
                    "yes_score",
                    "no_score",
                    "ask_model",
                    "max_turns",
                    "reply_prob",
                    "online_min",
                    "online_max",
                    "idle_min",
                    "probe_prob",
                    "exit_line_prob",
                    "cooldown_min",
                    "lines_per_hour",
                    "closing_skip",
                    "topic_overlap",
                    "topic_penalty",
                    "other_overlap",
                    "call_other",
                    "inner_mode",
                    "inner_threshold",
                )
            },
        }

    def web_action(self, body):
        body = body if isinstance(body, dict) else {}
        op, gk = str(body.get("op") or ""), str(body.get("gk") or "")
        if gk not in self.rh.g:
            return {"status": "error", "message": "没有这个群"}
        if op == "lurk":
            self.rh.to_lurk(gk, time.time(), cool=False)
            logger.info(f"{TAG} 群{gk.split(':', 1)[-1]} 面板里让它回观望")
        elif op == "uncool":
            self.rh.uncool(gk)
            logger.info(f"{TAG} 群{gk.split(':', 1)[-1]} 面板里清掉收尾冷却")
        else:
            return {"status": "error", "message": "不认识的操作"}
        return {"status": "ok", "data": self.snapshot()}
