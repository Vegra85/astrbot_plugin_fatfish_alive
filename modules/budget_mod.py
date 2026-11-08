# -*- coding: utf-8 -*-
"""省 token：花得越多回得越少 —— AstrBot 这一层（原 astrbot_plugin_fatfish_budget/main.py，逻辑没变；核心在 core/budget.py）

- 记账：每次模型回完（on_llm_response）按 usage 记 token —— 输入没命中缓存 / 命中缓存、输出，按配置的价格折成美元；
  DeepSeek 峰谷价（工作日 UTC 01–04、06–10 点翻倍）。一轮里调了工具的，前面几次调用从 AstrBot 的累计里补上。
- 拦：要叫模型之前（on_waiting_llm_request，还没排队等会话锁）按场合掷骰子；掷不过就 stop_event()：
  这次不调模型、什么都不发、不报错。场合先看别的插件写的 event.set_extra("fatfish_kind", ...)，没写就私聊 = private、
  群里被 @ / 引用 / 唤醒 = direct。管理员不限；火力全开时谁都不掷。
- 指令（在 main.py 里注册，文字在这里）：/省token（状态）、/省token 火力全开 [小时数|一直]、省着点、重新算
"""

import re
import time

from astrbot.api import logger

from ..core.angel import release_angel
from ..core.budget import (
    DEFAULTS,
    KIND_NAME,
    Budget,
    parse_hours,
    parse_usage,
    pick_kind,
)

TAG = "[省token]"
MAX_FULL_HOURS = 72


class BudgetMod:
    name = "budget"

    def __init__(self, host):
        self.host = host
        self.config = host.sub("budget")
        self.b = Budget(self._cfg(), host.store.get("budget"))
        self._warned = {}
        host.store.register("budget", self.b.dump)

    def _save(self, force=False):
        self.host.save(force)

    def _on(self):
        return self.host.active("budget")

    def _cfg(self):
        out = {}
        for k in DEFAULTS:
            try:
                if k in self.config:
                    out[k] = self.config.get(k)
            except Exception:
                continue
        return out

    def _warn_once(self, key, text, every=600):
        now = time.time()
        if now - self._warned.get(key, 0) > every:
            self._warned[key] = now
            logger.warning(f"{TAG} {text}")

    # ───────── 读消息 ─────────
    def _admins(self):
        try:
            return {
                str(x) for x in (self.host.context.get_config().get("admins_id") or [])
            }
        except Exception:
            return set()

    def _is_admin(self, event, uid):
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        return bool(uid) and uid in self._admins()

    @staticmethod
    def _private(event):
        try:
            return bool(event.is_private_chat()) or not event.get_group_id()
        except Exception:
            return False

    @staticmethod
    def _where(event):
        try:
            gid = event.get_group_id()
        except Exception:
            gid = ""
        return f"群 {gid}" if gid else "私聊"

    @staticmethod
    def _short(event):
        return re.sub(r"\s+", " ", (getattr(event, "message_str", "") or "")).strip()[
            :20
        ]

    # ───────── 要叫模型之前：掷骰子 ─────────
    def gate(self, event):
        """掷不过 → stop_event()：AstrBot 直接 return，不调模型、不发东西、不报错（会话锁之前，不占队）。→ True = 拦下了"""
        if not self._on():
            return False
        try:
            self.b.configure(self._cfg())
            uid = str(event.get_sender_id() or "")
            kind = pick_kind(
                event.get_extra("fatfish_kind"),
                self._private(event),
                bool(getattr(event, "is_at_or_wake_command", False)),
            )
            name = event.get_sender_name() or uid
            where = self._where(event)
            d = self.b.decide(
                kind,
                conv=event.unified_msg_origin,
                uid=uid,
                name=name,
                admin=self._is_admin(event, uid),
                where=where,
            )
        except Exception as e:
            self._warn_once("gate", f"掷骰子出错了（这次照常回）：{e!r}"[:200], 60)
            return False
        self._save()
        try:
            event.set_extra(
                "fatfish_budget",
                {"ok": d["ok"], "p": d["p"], "kind": kind, "by": d["by"]},
            )
        except Exception:
            pass
        if d["ok"]:
            if d["by"] == "roll":
                logger.debug(
                    f"{TAG} {where} {name}「{self._short(event)}」→ 回（{d['why']}）"
                )
            return False
        release_angel(self.host.context, event, "活人感拦下")  # 跟天使之心一起装：替它把这一轮记成结束（v0.4.3）
        event.stop_event()
        logger.info(
            f"{TAG} {where} {name}「{self._short(event)}」→ 这次不回（{d['why']}）"
        )
        return True

    # ───────── 模型回完：记账 ─────────
    @staticmethod
    def _run_total(event):
        """这一轮（可能调了几次工具）AstrBot 自己累计的用量；拿不到 → None（AstrBot 内部的东西，以后改了就退回只记最后一次）"""
        try:
            from astrbot.core.pipeline.process_stage import follow_up

            r = follow_up._ACTIVE_AGENT_RUNNERS.get(event.unified_msg_origin)
            if (
                r is None
                or getattr(getattr(r.run_context, "context", None), "event", None)
                is not event
            ):
                return None
            return parse_usage(r.stats.token_usage)
        except Exception:
            return None

    def _usages(self, event, resp):
        """→ [(没命中, 命中, 输出), ...]：最后这次调用（DeepSeek 的命中数从原始返回里读）+ 这一轮前面几次（调工具的时候）"""
        out = []
        final = parse_usage(
            getattr(getattr(resp, "raw_completion", None), "usage", None)
        )
        if final is None:
            final = parse_usage(getattr(resp, "usage", None))
        if final is not None:
            out.append(final)
        total = self._run_total(event)
        if total is not None:
            last = parse_usage(getattr(resp, "usage", None)) or (0, 0, 0)
            prev = tuple(max(0, a - b) for a, b in zip(total, last))
            if any(prev):
                out.append(prev)
        return out

    def on_resp(self, event, resp):
        if not self._on():
            return
        try:
            self.b.configure(self._cfg())
            parts = self._usages(event, resp)
            if not any(
                any(p) for p in parts
            ):  # 接口没回 usage 时 AstrBot 给的是全 0：别悄悄当成 $0
                self._warn_once(
                    "nousage",
                    "模型这次没给 token 用量（usage），记不了账；一直这样的话花费不涨，省 token 不起作用",
                )
                if not parts:
                    return
            usd = sum(
                self.b.record(*p, calls=1 if i == 0 else 0) for i, p in enumerate(parts)
            )
        except Exception as e:
            self._warn_once("record", f"记账出错了：{e!r}"[:200], 60)
            return
        self._save()
        miss, hit, out = (sum(p[i] for p in parts) for i in range(3))
        logger.debug(
            f"{TAG} 记账：输入 {miss}（缓存命中另 {hit}）输出 {out} → ${usd:.5f}"
            + ("（高峰价）" if self.b.is_peak() else "")
            + ("（含调工具的几次）" if len(parts) > 1 else "")
        )

    # ───────── 给别的插件 / 本插件别的模块用 ─────────
    def record_usage(self, resp_or_usage) -> float:
        """自己用 context.llm_generate 调了模型、想算进省 token：把返回的 LLMResponse（或 usage）交给这里 → 花了多少美元"""
        x = resp_or_usage
        u = (
            parse_usage(getattr(getattr(x, "raw_completion", None), "usage", None))
            or parse_usage(getattr(x, "usage", None))
            or parse_usage(x)
        )
        if not u:
            return 0.0
        usd = self.b.record(*u)
        self._save()
        return usd

    def full_on(self) -> bool:
        """火力全开开着没"""
        return bool(self.b.full_state()["on"])

    def ask(self, kind="extra", conv="", uid="", name="", where="") -> tuple:
        """没有消息事件的主动发言（定时冒泡、主动私聊）叫模型之前问一下 → (回不回, 说明)"""
        uid = str(uid or "")
        self.b.configure(self._cfg())
        d = self.b.decide(
            kind,
            conv=conv,
            uid=uid,
            name=name,
            admin=bool(uid) and uid in self._admins(),
            where=where,
        )
        self._save()
        if not d["ok"]:
            logger.info(
                f"{TAG} {where or conv} {KIND_NAME.get(kind, kind)} 这次算了（{d['why']}）"
            )
        return d["ok"], d["why"]

    # ───────── 管理员指令（返回要回的话）─────────
    def status_text(self):
        self.b.configure(self._cfg())
        return self.b.status_text()

    def cmd_full(self, how, unit, who):
        self.b.configure(self._cfg())
        h = parse_hours(
            f"{how}{unit}", self.b.cfg["full_default_hours"], MAX_FULL_HOURS
        )
        if h is None:
            return (
                "用法：/省token 火力全开 [小时数|一直]，比如「/省token 火力全开 6」「/省token 火力全开 30分钟」"
                f"「/省token 火力全开 一直」（最多 {MAX_FULL_HOURS} 小时，再长就用「一直」）"
            )
        st = self.b.set_full(h, who)
        self._save(force=True)
        logger.info(f"{TAG} 火力全开：{Budget.full_text(st)}（{who} 开的）")
        return f"火力全开！{Budget.full_text(st)}。这段时间所有场合都不省；发「/省token 省着点」就关。"

    def cmd_save(self, who):
        was = self.b.full_state()["on"]
        self.b.set_full(0)
        self._save(force=True)
        self.b.configure(self._cfg())
        w = self.b.curve(self.b.spent())
        if was:
            logger.info(f"{TAG} 火力全开关了（{who}）")
        return (
            "好，火力全开关了，回到省 token。" if was else "本来就没开火力全开。"
        ) + f"现在回复意愿 {w:.0%}。"

    def cmd_reset(self, who):
        self.b.configure(self._cfg())
        self.b.reset_now(who)
        self._save(force=True)
        logger.info(f"{TAG} 回复意愿从现在重新算（{who}）")
        return (
            f"好，花费从现在重新算，回复意愿回到 {self.b.curve(self.b.spent()):.0%}。"
            f"记的账不动；{self.b.cfg['window_hours']:g} 小时后自动恢复按实际花费算。"
        )

    # ───────── 面板 ─────────
    def snapshot(self):
        self.b.configure(self._cfg())
        return self.b.snapshot()

    def web_action(self, body):
        if not isinstance(body, dict):
            body = {}
        do = str(body.get("do") or "")
        self.b.configure(self._cfg())
        if do == "full":
            h = body.get("hours")
            try:
                h = (
                    float(h)
                    if h is not None
                    else float(self.b.cfg["full_default_hours"])
                )
            except (TypeError, ValueError):
                return {"status": "error", "message": "小时数不对"}
            h = -1.0 if h < 0 else min(float(MAX_FULL_HOURS), h)
            st = self.b.set_full(h, "面板")
            logger.info(
                f"{TAG} 面板：火力全开 {Budget.full_text(st)}"
                if st["on"]
                else f"{TAG} 面板：火力全开关了"
            )
        elif do == "reset":
            self.b.reset_now("面板")
            logger.info(f"{TAG} 面板：回复意愿从现在重新算")
        elif do == "reset_clear":
            self.b.reset_clear()
            logger.info(f"{TAG} 面板：恢复按实际花费算")
        else:
            return {"status": "error", "message": f"不认识的操作：{do[:20]}"}
        self._save(force=True)
        return {"status": "ok", "data": self.snapshot()}
