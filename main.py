# -*- coding: utf-8 -*-
"""
大肥鱼 · 活人感（AstrBot 插件）：攒一攒 + 群聊节奏 + 省 token + 兜底，合成一个。

原来是三个插件（攒一攒 gather / 群聊节奏 rhythm / 省 token budget），共用一套钩子顺序，靠优先级互相猜。
现在放进一个插件，顺序在这里排死：

  群消息监听（max-1）  记一笔 → 群聊节奏打分（要不要接）→ 攒一攒记内容
  要叫模型之前（1000000）  攒一攒等 → 省 token 掷骰子 → 群聊节奏记「正在回话」
                          （别的插件常在这个钩子里发「正在思考中…」，所以省 token 不能排到负数，不然被拦的也会先发出一句）
  回话请求（40）       群聊节奏的场景提示 + 兜底的去 AI 腔提示
  模型回完（max-1）    省 token 记账 + 兜底（模型什么都没回的救援）
  发送前（max-1）      兜底（框架报错 / 只有 <think>）→ 群聊节奏（[[不说]]）
  发完一句             群聊节奏记她说了什么、回了谁

这个文件只负责注册钩子、把事件分发给 modules/ 里的模块，自己不写逻辑。逻辑在 modules/（AstrBot 这一层）和 core/（不依赖 AstrBot）。
装了 group_chat_plus / SpectreCore / Heartflow / natural_followup / jiehua 任何一个：节奏和攒一攒自动让路，只留省 token 和兜底。
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

from .core import compat
from .core.conf import Sub, truthy
from .core.state import Shared, StateStore, migrate_config
from .modules.budget_mod import BudgetMod
from .modules.gather_mod import GatherMod
from .modules.guard_mod import GuardMod
from .modules.rhythm_mod import RhythmMod

PLUGIN_NAME = "astrbot_plugin_fatfish_alive"
VERSION = "0.1.0"
TAG = "[活人感]"
WAIT_PRIORITY = 1000000  # on_waiting_llm_request：比「认 AI」(sys.maxsize) 后、比别的插件默认的 0 先
CMD = "省token"
CMD_ALIAS = {"省Token", "省TOKEN"}
_LIVE = None  # 现在在跑的插件实例（给「一到就记一笔」的过滤器用）


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
        if p is not None:
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
            f"省 token{self._tag('budget')} · 兜底{self._tag('guard')}"
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
                    self.context.get_all_stars(), extra=extra, own=PLUGIN_NAME
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
                        f"{TAG} [兼容] 因为装了 {'、'.join(labels)}，接话节奏和攒一攒已让路（只保留省 token 和兜底）"
                        "；想不让路：配置 → 兼容 → 强制开"
                    )
                elif not first:
                    logger.info(
                        f"{TAG} [兼容] 同类插件没有了，接话节奏和攒一攒恢复工作"
                    )
                if labels or not first:
                    self.yield_log.append(
                        {
                            "t": int(now),
                            "found": [{"label": a, "plugin": b} for a, b in found],
                        }
                    )
                    self.yield_log = self.yield_log[-20:]
                    self.store.save()
            self._yield_found = found
        return self._yield_found

    def yield_reason(self, name):
        """这个模块现在让路吗：不让 → ""；让 → "group_chat_plus、jiehua"（装了谁）"""
        if name not in ("gather", "rhythm"):
            return ""
        c = self.sub("compat")
        if not truthy(c.get("auto_yield"), True) or truthy(
            c.get("force_" + name), False
        ):
            return ""
        return "、".join(x[0] for x in self.yield_reasons())

    def active(self, name):
        """这个模块现在在干活吗：总开关开着、它自己的开关开着、没在让路"""
        if not self.alive_on() or not truthy(self.sub(name).get("enable"), True):
            return False
        return not self.yield_reason(name)

    def _tag(self, name):
        if not self.alive_on():
            return "（总开关关着）"
        if not truthy(self.sub(name).get("enable"), True):
            return "（关）"
        why = self.yield_reason(name)
        return f"（让路：装了 {why}）" if why else ""

    # ───────── 钩子 ─────────
    @filter.custom_filter(_Arrive)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=sys.maxsize - 1)
    async def on_message(self, event: AstrMessageEvent):
        """每条消息：记一笔 → 群聊节奏（要不要接）→ 攒一攒（记内容）。都不拦；出错各自兜住"""
        if not self.alive_on():
            return
        try:
            self.note_msg(event)
        except Exception:
            pass
        if event.get_group_id():
            await self.rhythm.on_group(event)
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
        if self.budget.gate(event):
            return
        self.rhythm.mark_busy(event)

    @filter.on_llm_request(priority=40)
    async def on_llm_req(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.alive_on():
            return
        self.rhythm.on_llm_req(event, req)
        self.guard.on_llm_req(event, req)

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
        if await self.guard.on_decorate(event):
            return
        self.rhythm.on_decorate(event)

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
        why = self.yield_reason("rhythm") or self.yield_reason("gather")
        if why:
            lines.append(
                f"因为装了 {why}，接话节奏和攒一攒已让路（只保留省 token 和兜底）"
            )
        lines.append(
            f"今天：攒了 {g['merges']} 次（省 {g['saved']} 次调用）· 接话 {r.get('follow', 0)} 次 / 挑着接 {r.get('chat', 0)} 次 / "
            f"想了想没接 {r.get('silent', 0)} 次 · 省 token 回了 {npass} 次、跳过 {nskip} 次（现在意愿 {w:.0%}）"
        )
        lines.append(
            f"兜底：拦下报错 {gd['errors']} 次 · 补了空回复 {gd['rescued']} 次 · 去客服腔 {gd['tone']} 次"
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
            for n in ("gather", "rhythm", "budget", "guard")
        }
        return {
            "alive": {
                "version": VERSION,
                "enabled": self.alive_on(),
                "today": dict(self.stats),
                "modules": mods,
                "yield": {
                    "auto": truthy(self.sub("compat").get("auto_yield"), True),
                    "found": [{"label": a, "plugin": b} for a, b in found],
                    "log": self.yield_log[-10:][::-1],
                },
                "migrated": self.store.data.get("meta", {}).get("migrated"),
            },
            "gather": self.gather.snapshot(),
            "rhythm": self.rhythm.snapshot(),
            "budget": self.budget.snapshot(),
            "guard": self.guard.snapshot(),
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
