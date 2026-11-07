# -*- coding: utf-8 -*-
"""
大肥鱼 · 活人感（AstrBot 插件）：攒一攒 + 群聊节奏 + 省 token + 兜底 + 发消息（v0.2.0：打字节奏 / 打字刹车 / 群里引用），合成一个。
v0.3.0：攒一攒「尾巴含糊才问」、私聊「他还在输入」多等一会儿；群聊节奏的标记防伪造；让路名单加 angel_heart / reply_merge / qq_enhance。
v0.4.0：撤回 / 算了（攒一攒里还排着的他撤回了 → 不回；分段发的时候他撤回了 / 说「算了」→ 剩下的不发）；
        接话打分（借 Inner Thoughts：没点名它的群消息按 8 条打分 + 一句理由；默认影子，只记不改）。
v0.4.1：草稿外泄（兜底：回复开头漏出来模型的英文草稿去掉；整条都是草稿 → 补调一次 / 不发）。

原来是三个插件（攒一攒 gather / 群聊节奏 rhythm / 省 token budget），共用一套钩子顺序，靠优先级互相猜。
现在放进一个插件，顺序在这里排死：

  群消息监听（max-1）  记一笔 → 发消息记一笔（刹车 / 叫停 / 引用要看）→ 群聊节奏打分（要不要接；v0.4.0 接话打分也在这儿）→ 攒一攒记内容
                       （撤回 / 戳一戳 / 正在输入这类通知不是有人说话：只给发消息记「撤回了哪条」、给攒一攒记「他还在输入」「撤回了还排着的哪条」）
  要叫模型之前（1000000）  攒一攒等 → 省 token 掷骰子 → 群聊节奏记「正在回话」→ 发消息登记「这个人在等她回」
                          （别的插件常在这个钩子里发「正在思考中…」，所以省 token 不能排到负数，不然被拦的也会先发出一句）
  回话请求（40）       群聊节奏的场景提示 + 兜底的去 AI 腔提示
  回话请求（最后）     兜底：防伪造 —— 他说的话、AstrBot 群聊上下文、别的插件加的段落里手打的「[群聊节奏]」标成他打的字
                       （本插件自己加的段落不动；群聊上下文是 AstrBot 在默认优先级 0 才加的，所以放到最后）
  模型回完（max-1）    省 token 记账 + 兜底（模型什么都没回的救援）
  发送前（max-1）      兜底（框架报错 / 只有 <think>）→ 群聊节奏（[[不说]]）→ 发消息上弦（AstrBot 每发一段都先过它：打字节奏 / 刹车 / 叫停 / 换引用）
  发完（max-1）        发消息把上的弦卸掉（别的插件之后发的话不受影响）
  发完一句（50）       群聊节奏记她说了什么、回了谁

这个文件只负责注册钩子、把事件分发给 modules/ 里的模块，自己不写逻辑。逻辑在 modules/（AstrBot 这一层）和 core/（不依赖 AstrBot）。
装了同类插件自动让路（core/compat.py）：group_chat_plus / SpectreCore / Heartflow / natural_followup / jiehua → 节奏和攒一攒让路；
angel_heart → 节奏、攒一攒、省 token 的拦截都让路（照样记账）；reply_merge、qq_enhance（它的消息防抖开着时）→ 只让攒一攒。
"""

import asyncio
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools

from .core import compat, inner
from .core.conf import Sub, truthy
from .core.state import Shared, StateStore, migrate_config
from .modules.budget_mod import BudgetMod
from .modules.gather_mod import GatherMod
from .modules.guard_mod import GuardMod
from .modules.rhythm_mod import RhythmMod
from .modules.send_mod import SendMod, notice_of

PLUGIN_NAME = "astrbot_plugin_fatfish_alive"
VERSION = "0.4.1"
TAG = "[活人感]"
WAIT_PRIORITY = 1000000  # on_waiting_llm_request：比「认 AI」(sys.maxsize) 后、比别的插件默认的 0 先
LATE_PRIORITY = -sys.maxsize + 100  # on_llm_request 的最后一步：防伪造（v0.3.0）
CMD = "省token"
CMD_ALIAS = {"省Token", "省TOKEN"}
_LIVE = None  # 现在在跑的插件实例（给「一到就记一笔」的过滤器用）
QUOTE_LABEL = {
    "smart": "跟着 AstrBot 的「引用回复」",
    "always": "插件自己定",
    "off": "不管",
}


def _bare(event):
    """这条是不是光发了「省token」（唤醒前缀 AstrBot 已经去掉了）"""
    s = (
        re.sub(r"\s+", "", (getattr(event, "message_str", "") or ""))
        .lstrip("/／")
        .lower()
    )
    return s == CMD.lower()


class _Arrive(filter.CustomFilter):
    """唤醒检查那一步 AstrBot 会对每条消息调一次：趁这时候让攒一攒记一笔「他发了一条」（这时图还没下载）。
    永远放行，不影响别的。"""

    def filter(self, event, cfg):
        p = _LIVE
        if (
            p is not None and notice_of(event) is None
        ):  # 撤回 / 戳一戳这类通知不是有人说了一句
            try:
                p.gather.arrive(event)
            except Exception:
                pass
        return True


class _NotBare(filter.CustomFilter):
    """挂在指令组上：光发「/省token」时别让 AstrBot 回「参数不足」，交给下面的「看状态」
    （过滤器每条消息都跑；这里出错 AstrBot 会把错误发到群里并停掉这条，所以一律兜住）"""

    def filter(self, event, cfg):
        try:
            return not _bare(event)
        except Exception:
            return True


class _OnlyBare(filter.CustomFilter):
    """光发「/省token」（被唤醒的）= 看状态"""

    def filter(self, event, cfg):
        try:
            return bool(getattr(event, "is_at_or_wake_command", False)) and _bare(event)
        except Exception:
            return False


class FatfishAlive(Star):
    """大肥鱼 · 活人感"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        global _LIVE
        self.config = config if config is not None else {}
        try:
            self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            self.data_dir = Path(os.getcwd()) / "data" / "plugin_data" / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = StateStore(
            self.data_dir, warn=lambda m: logger.warning(f"{TAG} {m}")
        )
        self.shared = Shared()
        self._yield_found, self._yield_at, self._yield_logged = [], 0.0, None
        self._saver = None
        self._closed = False
        # 先建省 token（群聊节奏要用它），其余的顺序无所谓
        self.budget = BudgetMod(self)
        self.gather = GatherMod(self)
        self.rhythm = RhythmMod(self)
        self.guard = GuardMod(self)
        self.send = SendMod(self)
        self.stats = (self.store.get("alive").get("stats")) or {}
        self.yield_log = list(self.store.get("alive").get("yield_log") or [])[-20:]
        self.store.register(
            "alive", lambda: {"stats": self.stats, "yield_log": self.yield_log[-20:]}
        )
        _LIVE = self
        self._migrate_config()
        if self.store.migrated:
            logger.info(
                f"{TAG} 第一次加载：把旧插件的存档搬进来了（{'、'.join(self.store.migrated)}；旧目录没动）"
            )
        self._register_web()

    async def initialize(self):
        self.ensure_loops()
        self.yield_reasons()  # 开机就查一次让路，日志里留一笔
        logger.info(
            f"{TAG} v{VERSION} 已启用：攒一攒{self._tag('gather')} · 群聊节奏{self._tag('rhythm')} · "
            f"省 token{self._tag('budget')} · 兜底{self._tag('guard')} · 发消息{self._tag('send')}"
        )

    # ───────── 给各模块用的 ─────────
    def sub(self, name):
        return Sub(self.config, name)

    def module_path(self):
        return type(self).__module__

    def save(self, force=False):
        self.store.save(force)
        self.ensure_loops()

    def ensure_loops(self):
        if self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._saver is None or self._saver.done():
            self._saver = loop.create_task(self._save_loop())
        self.rhythm.start()

    async def _save_loop(self):
        while not self._closed:
            await asyncio.sleep(5)
            if self.store.dirty:
                self.store.save(force=True)

    async def terminate(self):
        global _LIVE
        self._closed = True
        if _LIVE is self:
            _LIVE = None
        self.rhythm.close()
        if self._saver:
            self._saver.cancel()
        self.store.save(force=True)

    def _migrate_config(self):
        """第一次加载：旧三个插件的配置里改过的项，搬进新插件对应的分组（新插件里已经改过的不动）。只试一次"""
        meta = self.store.data.setdefault("meta", {})
        if meta.get("config_migrated"):
            return
        meta["config_migrated"] = True
        try:
            schema = json.loads(
                (Path(__file__).parent / "_conf_schema.json").read_text(
                    encoding="utf-8"
                )
            )
            moved = migrate_config(
                self.config, self.data_dir.parent.parent / "config", schema
            )
            if moved:
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
                self.store.save(force=True)
                logger.info(
                    f"{TAG} 把旧插件配置里改过的项搬过来了："
                    + "；".join(f"{g}（{'、'.join(d)}）" for g, d in moved.items())
                )
        except Exception as e:
            logger.warning(
                f"{TAG} 搬旧配置失败（不影响使用，新配置都是默认值）：{e!r}"[:200]
            )

    # ───────── 总开关 / 让路 ─────────
    def alive_on(self):
        return truthy(self.sub("alive").get("enabled"), True)

    def yield_reasons(self):
        """装了并启用的同类插件 → [(标签, 插件名)]。20 秒查一次；变了就在日志里写一笔（面板的让路记录也有）"""
        now = time.time()
        if now - self._yield_at >= 20:
            self._yield_at = now
            try:
                extra = list(self.sub("compat").get("extra_plugins") or [])
                found = compat.detect(
                    self.context.get_all_stars(),
                    extra=extra,
                    own=PLUGIN_NAME,
                    cfg_dir=self.data_dir.parent.parent / "config",
                )
            except Exception as e:
                logger.debug(f"{TAG} 查同类插件出错：{e!r}")
                found = self._yield_found
            labels = [x[0] for x in found]
            if labels != self._yield_logged:
                first = self._yield_logged is None
                self._yield_logged = labels
                self._yield_found = found
                if labels:
                    logger.info(
                        f"{TAG} [兼容] 因为装了 {'、'.join(labels)}，让路："
                        + "；".join(
                            f"{x[0]} → {compat.scope_text(x[2])}" for x in found
                        )
                        + "；想不让路：配置 → 兼容 → 强制开"
                    )
                elif not first:
                    logger.info(
                        f"{TAG} [兼容] 同类插件没有了，接话节奏和攒一攒恢复工作"
                    )
                if labels or not first:
                    self.yield_log.append(
                        {
                            "t": int(now),
                            "found": [
                                {"label": a, "plugin": b, "mods": list(m)}
                                for a, b, m in found
                            ],
                        }
                    )
                    self.yield_log = self.yield_log[-20:]
                    self.store.save()
            self._yield_found = found
        return self._yield_found

    def yield_reason(self, name):
        """这个模块现在让路吗：不让 → ""；让 → "group_chat_plus、jiehua"（装了谁）。
        budget = 省 token 的拦截（掷骰子不回）让路；记账照常（v0.3.0，装了 angel_heart 时）"""
        if name not in ("gather", "rhythm", "budget"):
            return ""
        c = self.sub("compat")
        if not truthy(c.get("auto_yield"), True) or truthy(
            c.get("force_" + name), False
        ):
            return ""
        return "、".join(x[0] for x in self.yield_reasons() if name in x[2])

    def active(self, name):
        """这个模块现在在干活吗：总开关开着、它自己的开关开着、没在让路（省 token 让路只是不拦，照样记账 → 算在干活）"""
        if not self.alive_on() or not truthy(self.sub(name).get("enable"), True):
            return False
        if name == "budget":
            return True
        return not self.yield_reason(name)

    def _tag(self, name):
        if not self.alive_on():
            return "（总开关关着）"
        if not truthy(self.sub(name).get("enable"), True):
            return "（关）"
        why = self.yield_reason(name)
        if why and name == "budget":
            return f"（只记账、不拦：装了 {why}）"
        return f"（让路：装了 {why}）" if why else ""

    # ───────── 钩子 ─────────
    @filter.custom_filter(_Arrive)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=sys.maxsize - 1)
    async def on_message(self, event: AstrMessageEvent):
        """每条消息：记一笔 → 发消息记一笔 → 群聊节奏（要不要接）→ 攒一攒（记内容）。都不拦；出错各自兜住"""
        if not self.alive_on():
            return
        raw = notice_of(event)
        if raw is not None:
            # QQ 的通知（撤回 / 戳一戳 / 正在输入…）：不是有人说了一句，只记「撤回了哪条」「他还在输入」
            self.send.on_notice(event, raw)
            self.gather.on_notice(event, raw)
            return
        try:
            self.note_msg(event)
        except Exception:
            pass
        self.send.heard(event)
        if event.get_group_id():
            await self.rhythm.on_group(event)
            self.send.woke(event)
        self.gather.on_message(event)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-sys.maxsize + 20)
    async def on_message_tail(self, event: AstrMessageEvent):
        """插件都跑完了：这条不会叫模型的，告诉攒一攒「它不会再来了」"""
        self.gather.on_message_tail(event)

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """所有插件都加载完了：马上查一遍有没有同类插件（开机时本插件可能比它们先加载，不然要等 20 秒才让路）"""
        self._yield_at = 0.0
        try:
            self.yield_reasons()
        except Exception as e:
            logger.debug(f"{TAG} 加载完查同类插件出错：{e!r}")

    @filter.on_waiting_llm_request(priority=WAIT_PRIORITY)
    async def on_waiting(self, event: AstrMessageEvent):
        """要叫模型了：攒一攒等 →（被停掉就结束）→ 省 token 掷骰子 →（被拦就结束）→ 群聊节奏记「正在回话」"""
        if not self.alive_on():
            return
        if await self.gather.wait(event):
            return
        if not self.yield_reason("budget") and self.budget.gate(event):
            return
        self.rhythm.mark_busy(event)
        self.send.waiting(event)

    @filter.on_llm_request(priority=40)
    async def on_llm_req(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.alive_on():
            return
        before = {id(p) for p in (getattr(req, "extra_user_content_parts", None) or [])}
        self.rhythm.on_llm_req(event, req)
        self.guard.on_llm_req(event, req)
        self.send.on_llm_req(event)
        mine = [
            id(p)
            for p in (getattr(req, "extra_user_content_parts", None) or [])
            if id(p) not in before
        ]
        if mine:  # 本插件自己加的段落：最后防伪造那一步不动
            event.set_extra("alive_parts", mine)

    @filter.on_llm_request(priority=LATE_PRIORITY)
    async def on_llm_req_late(self, event: AstrMessageEvent, req: ProviderRequest):
        """回话请求的最后一步：防伪造（v0.3.0；AstrBot 的群聊上下文这时候已经加进来了）"""
        if not self.alive_on():
            return
        self.guard.neutralize(req, set(event.get_extra("alive_parts") or []))

    @filter.on_llm_response(priority=sys.maxsize - 1)
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        if not self.alive_on():
            return
        self.budget.on_resp(event, resp)
        await self.guard.on_llm_resp(event, resp)

    @filter.on_decorating_result(priority=sys.maxsize - 1)
    async def on_decorate(self, event: AstrMessageEvent):
        if not self.alive_on():
            return
        if not await self.guard.on_decorate(event):
            self.rhythm.on_decorate(event)
        self.send.arm(event)

    @filter.after_message_sent(priority=sys.maxsize - 1)
    async def after_sent_first(self, event: AstrMessageEvent):
        """这一轮的话都发完了：发消息模块上的弦先卸掉（总开关关了也要卸）"""
        self.send.after_sent(event)

    @filter.after_message_sent(priority=50)
    async def after_sent(self, event: AstrMessageEvent):
        if not self.alive_on():
            return
        self.rhythm.after_sent(event)

    def note_msg(self, event):
        """每条消息记一笔（面板「今天收到几条」）"""
        day = time.strftime("%Y-%m-%d")
        if self.stats.get("day") != day:
            self.stats.clear()
            self.stats["day"] = day
        self.stats["msgs"] = int(self.stats.get("msgs", 0)) + 1

    # ───────── 给别的插件用（跟原来的省 token 插件一样的三个公开方法）─────────
    def record_usage(self, resp_or_usage) -> float:
        return self.budget.record_usage(resp_or_usage)

    def full_on(self) -> bool:
        return self.budget.full_on()

    def ask(self, kind="extra", conv="", uid="", name="", where="") -> tuple:
        if not self.active("budget"):
            return True, ""
        return self.budget.ask(kind, conv=conv, uid=uid, name=name, where=where)

    # ───────── 指令 ─────────
    @filter.custom_filter(_NotBare)
    @filter.command_group(CMD, alias=set(CMD_ALIAS))
    def budget_cmd(self):
        """省 token：看状态、火力全开、省着点、重新算"""
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.custom_filter(_OnlyBare)
    async def cmd_bare(self, event: AstrMessageEvent):
        """光发 /省token：看状态"""
        yield event.plain_result(self.budget.status_text())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @budget_cmd.command("状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """看花了多少、回复意愿、火力全开"""
        yield event.plain_result(self.budget.status_text())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @budget_cmd.command("火力全开")
    async def cmd_full(self, event: AstrMessageEvent, how: str = "", unit: str = ""):
        """暂时不省：/省token 火力全开 [小时数|一直]（默认 3 小时）"""
        yield event.plain_result(
            self.budget.cmd_full(
                how, unit, event.get_sender_name() or str(event.get_sender_id())
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @budget_cmd.command("省着点")
    async def cmd_save(self, event: AstrMessageEvent):
        """关掉火力全开"""
        yield event.plain_result(
            self.budget.cmd_save(event.get_sender_name() or str(event.get_sender_id()))
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @budget_cmd.command("重新算")
    async def cmd_reset(self, event: AstrMessageEvent):
        """花费从现在重新算（过了统计窗口自动失效）"""
        yield event.plain_result(
            self.budget.cmd_reset(event.get_sender_name() or str(event.get_sender_id()))
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("活人感")
    async def cmd_alive(self, event: AstrMessageEvent):
        """看活人感的状态：哪些模块开着、有没有让路、今天的数"""
        yield event.plain_result(self.status_text())

    def status_text(self):
        g, r, gd = self.gather._today(), self.rhythm.daily.today(), self.guard.today()
        bt = self.budget.b.today()
        npass, nskip = sum(bt["pass"].values()), sum(bt["skip"].values())
        w = self.budget.b.curve(self.budget.b.spent())
        lines = [
            f"大肥鱼 · 活人感 v{VERSION}"
            + ("" if self.alive_on() else "（总开关关着：什么都不做）"),
            f"攒一攒：{'开' if self.active('gather') else '不工作'}{self._tag('gather')}　群聊节奏：{'开' if self.active('rhythm') else '不工作'}{self._tag('rhythm')}",
            f"省 token：{'开' if self.active('budget') else '不工作'}{self._tag('budget')}　兜底：{'开' if self.active('guard') else '不工作'}{self._tag('guard')}",
        ]
        found = self.yield_reasons() if self.alive_on() else []
        for label, _, mods in found:
            gone = [m for m in mods if self.yield_reason(m)]
            if gone:
                lines.append(f"因为装了 {label}，{compat.scope_text(gone)}已让路")
        lines.append(
            f"今天：攒了 {g['merges']} 次（省 {g['saved']} 次调用；还排着他撤回了、没回 {g['recall_skip']} 轮）· "
            f"接话 {r.get('follow', 0)} 次 / 挑着接 {r.get('chat', 0)} 次 / "
            f"想了想没接 {r.get('silent', 0)} 次 · 省 token 回了 {npass} 次、跳过 {nskip} 次（现在意愿 {w:.0%}）"
        )
        lines.append(
            f"兜底：拦下报错 {gd['errors']} 次 · 补了空回复 {gd['rescued']} 次 · 去客服腔 {gd['tone']} 次"
        )
        sd, sc = self.send.today(), self.send.conf
        lines.append(
            f"发消息{'' if self.active('send') else '（不工作）'}：打字节奏{'开' if sc('typing_human') else '关'} · "
            f"打字刹车{'开' if sc('typing_brake') else '关'} · 撤回 / 算了叫停{'开' if sc('halt') else '关'} · "
            f"群里引用 {QUOTE_LABEL.get(sc('quote_mode'), sc('quote_mode'))}；"
            f"今天刹车 {sd['braked']} 次 · 叫停 {sd['halted']} 次（没发 {sd['halt_segs']} 段）· "
            f"引用换成找她的那条 {sd['quote_moved'] + sd['quote_added']} 次 · 去掉不该挂的引用 {sd['quote_dropped']} 次"
        )
        it = self.rhythm.it_daily.today(time.strftime("%Y-%m-%d"))
        mode = self.rhythm.conf("inner_mode")
        lines.append(
            f"接话打分（{inner.MODE_NAME.get(mode, mode)}，线 {self.rhythm.conf('inner_threshold'):g}）：今天打了 {it['scored']} 次 · "
            f"打分说接 {it['it_take']} / 不接 {it['it_skip']} / 拿不准 {it['it_ask']} · 实际接了 {it['now_take']}"
            + (
                f" · 打分会多接 {it['it_more']}、会少接 {it['it_less']}"
                if mode == "shadow"
                else ""
            )
            + f" · 没接后来又 @ 它 {it['at_after']} · 接了模型选不说 {it['silent']}"
        )
        return "\n".join(lines)

    # ───────── 面板（AstrBot WebUI → 插件 → 打开页面）─────────
    def _register_web(self):
        try:
            if (
                importlib.util.find_spec("astrbot.api.web") is None
            ):  # 4.29 以后才有：老版本就不开面板
                return
        except Exception:
            return
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/state", self.web_state, ["GET"], "活人感：状态"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/action",
            self.web_action,
            ["POST"],
            "活人感：火力全开 / 重新算 / 改群的状态",
        )

    def snapshot(self):
        found = self.yield_reasons()
        mods = {
            n: {
                "on": self.active(n),
                "enable": truthy(self.sub(n).get("enable"), True),
                "yield": self.yield_reason(n),
            }
            for n in ("gather", "rhythm", "budget", "guard", "send")
        }
        return {
            "alive": {
                "version": VERSION,
                "enabled": self.alive_on(),
                "today": dict(self.stats),
                "modules": mods,
                "yield": {
                    "auto": truthy(self.sub("compat").get("auto_yield"), True),
                    "found": [
                        {
                            "label": a,
                            "plugin": b,
                            "mods": [x for x in m if self.yield_reason(x)],
                        }
                        for a, b, m in found
                    ],
                    "log": self.yield_log[-10:][::-1],
                },
                "migrated": self.store.data.get("meta", {}).get("migrated"),
            },
            "gather": self.gather.snapshot(),
            "rhythm": self.rhythm.snapshot(),
            "budget": self.budget.snapshot(),
            "guard": self.guard.snapshot(),
            "send": self.send.snapshot(),
        }

    async def web_state(self):
        return {"status": "ok", "data": self.snapshot()}

    async def web_action(self):
        from astrbot.api.web import request

        body = await request.json({})
        body = body if isinstance(body, dict) else {}
        m = str(body.get("m") or "")
        if m == "budget":
            return self.budget.web_action(body)
        if m == "rhythm":
            return self.rhythm.web_action(body)
        return {"status": "error", "message": f"不认识的模块：{m[:20]}"}
