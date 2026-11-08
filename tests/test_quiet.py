#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""免打扰时段（v0.5.0）的单元测试（不用装 AstrBot，一秒钟）：python tests/test_quiet.py [插件目录 默认上一层]
  1 时段怎么写：「01:00-07:00」、跨半夜「23:30-07:00」、几段、「1-7」「22:00-00:00」；写错的跳过并说出来
  2 现在在不在免打扰：跨半夜的边界（23:29 不在、23:30 在、00:00 在、06:59 在、07:00 不在）、还有几分钟结束、按时区算
  3 时区：北京时间 / UTC+8 不用 tzdata；留空跟服务器；认不出按北京时间
  4 拦什么：群里没人找它的（接话 / 插话 / 冒泡 / AstrBot 主动回复）拦；被 @ 的照回（另一项开着才拦）；私聊不管
  5 配置和接上了：配置项、main.py 叫模型之前、ask()、群聊节奏（接话 / 挑着接 / 冷场试探 / 收尾）、面板
旧代码（v0.4.3）没有免打扰：core/quiet.py 都没有 —— 会没过。"""

import calendar
import json
import sys
from datetime import timedelta, timezone
from pathlib import Path

d = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(d))
RES = []


def check(c, m, why=""):
    RES.append((bool(c), m))
    print(("  [过]   " if c else "  [没过] ") + m + ("" if c or not why else f"\n           {str(why)[:600]}"))


def done():
    ok = sum(1 for c, _ in RES if c)
    bad = [m for c, m in RES if not c]
    print("\n" + "=" * 60)
    print(f"  结果：{ok} 项过了" + (f"，{len(bad)} 项没过" if bad else "，全过"))
    for m in bad:
        print("    -", m)
    print("=" * 60)
    sys.exit(1 if bad else 0)


try:
    from core import quiet as Q  # noqa: E402
except ImportError as e:  # 旧代码（v0.4.3）：没有免打扰
    check(False, "core/quiet.py（免打扰）在", e)
    done()


def bj(h, m, day=8):
    """2026-11-<day> 北京时间 h:m 的时间戳（北京 = UTC+8）"""
    return calendar.timegm((2026, 11, day, h, m, 0)) - 8 * 3600


# ═══════════════ 1 时段怎么写 ═══════════════
print("[1 时段怎么写]")
sp, bad = Q.parse(["01:00-07:00", "23:30-07:00", "13-14", "7:05~9:30", "22:00-00:00", "00:00-24:00", "21：00到22：00"])
check(
    sp == [(60, 420), (1410, 420), (780, 840), (425, 570), (1320, 1440), (0, 1440), (1260, 1320)] and not bad,
    "认得：01:00-07:00、跨半夜 23:30-07:00、只写钟点 13-14、~ 和「到」、全角冒号、22:00-00:00（到半夜）、00:00-24:00（全天）",
    (sp, bad),
)
sp, bad = Q.parse(["7-7", "25:00-26:00", "07:60-08:00", "早上", "", "10:00"])
check(sp == [] and bad == ["7-7", "25:00-26:00", "07:60-08:00", "早上", "10:00"], "写错的（开始 = 结束、25 点、60 分、不是时段）跳过，说出是哪几段；空行不算", bad)
check(Q.parse("01:00-07:00，12:00-13:00")[0] == [(60, 420), (720, 780)], "写成一行、逗号隔开也认")
check(Q.parse(None) == ([], []) and Q.parse([]) == ([], []), "没填：没有时段")
check(Q.span_text(1410, 420) == "23:30-07:00" and Q.span_text(0, 1440) == "00:00-24:00", "面板 / 日志上写回「23:30-07:00」")

# ═══════════════ 2 现在在不在 ═══════════════
print("\n[2 现在在不在免打扰（跨半夜）]")
night = Q.parse(["23:30-07:00"])[0]
cases = [((23, 29), False), ((23, 30), True), ((23, 59), True), ((0, 0), True), ((3, 0), True), ((6, 59), True), ((7, 0), False), ((12, 0), False)]
wrong = [(h, m, want) for (h, m), want in cases if Q.state(bj(h, m), night, Q.BEIJING)["on"] != want]
check(not wrong, "23:30-07:00：23:29 不在、23:30 在、23:59 在、00:00 在、06:59 在、07:00 不在、12:00 不在", wrong)
st = Q.state(bj(23, 45), night, Q.BEIJING)
check(st == {"on": True, "span": "23:30-07:00", "left_min": 435}, "23:45 时：在免打扰，还有 7 小时 15 分（435 分钟）—— 跨半夜也算对", st)
check(Q.state(bj(6, 59), night, Q.BEIJING)["left_min"] == 1 and Q.state(bj(2, 0), night, Q.BEIJING)["left_min"] == 300, "06:59 还剩 1 分钟、02:00 还剩 5 小时")
day = Q.parse(["01:00-07:00"])[0]
check(
    [Q.state(bj(h, m), day, Q.BEIJING)["on"] for h, m in ((0, 59), (1, 0), (6, 59), (7, 0))] == [False, True, True, False],
    "01:00-07:00（不跨半夜）：00:59 不在、01:00 在、06:59 在、07:00 不在",
)
two = Q.parse(["01:00-07:00", "13:00-14:00"])[0]
st = Q.state(bj(13, 20), two, Q.BEIJING)
check(st["on"] and st["span"] == "13:00-14:00" and st["left_min"] == 40, "写了几段：哪段在就说哪段", st)
check(not Q.state(bj(12, 0), two, Q.BEIJING)["on"] and Q.state(bj(12, 0), [], Q.BEIJING) == {"on": False, "span": "", "left_min": 0}, "都不在 / 没有时段：不在")
ts = bj(23, 45)  # 北京 23:45 = UTC 15:45
check(Q.state(ts, night, Q.BEIJING)["on"] and not Q.state(ts, night, timezone.utc)["on"], "按配置的时区算：同一个时刻，北京 23:45 在免打扰、UTC 15:45 不在")
check(Q.state(bj(0, 0), Q.parse(["22:00-00:00"])[0], Q.BEIJING)["on"] is False and Q.state(bj(23, 59), Q.parse(["22:00-00:00"])[0], Q.BEIJING)["on"],
      "22:00-00:00：23:59 在、过了半夜就不在")

# ═══════════════ 3 时区 ═══════════════
print("\n[3 时区（不依赖 tzdata）]")
check(Q.make_tz("Asia/Shanghai") == (Q.BEIJING, "北京时间", True) and Q.make_tz(" asia/shanghai ")[0] is Q.BEIJING, "Asia/Shanghai = 北京时间（固定 UTC+8，不用装 tzdata）")
check(Q.make_tz("UTC+8")[0] is Q.BEIJING and Q.make_tz("UTC-5")[0].utcoffset(None) == timedelta(hours=-5) and Q.make_tz("UTC-5")[1] == "UTC-5",
      "UTC+8、UTC-5 这种直接认")
check(Q.make_tz("") == (None, "服务器时间", True), "留空：跟服务器的时间")
check(Q.make_tz("火星/奥林帕斯")[0] is Q.BEIJING and Q.make_tz("火星/奥林帕斯")[2] is False, "认不出：按北京时间，并且说没认出（外面写日志）")
check(Q.make_tz("Asia/Tokyo")[0].utcoffset(None) == timedelta(hours=9), "几个常用的（东京 UTC+9）也直接认")

# ═══════════════ 4 拦什么 ═══════════════
print("\n[4 拦什么：群里没人找它的拦，被 @ 的照回，私聊不管]")
on = {"on": True, "span": "01:00-07:00", "tz": "北京时间", "left_min": 30, "at_too": False}
off = dict(on, on=False)
check(Q.stop_why(on, kind="follow", woken=True) and Q.stop_why(on, kind="chat", woken=True) and Q.stop_why(on, kind="extra"),
      "免打扰时：接话 / 挑着接 / 冒泡（别的插件标的 fatfish_kind）→ 拦")
check(Q.stop_why(on, kind=None, woken=False), "AstrBot 自带的主动回复这种（没人找它、也没标）→ 拦")
check(Q.stop_why(on, kind=None, woken=True) == "", "有人 @ / 引用 / 唤醒词叫它（被唤醒、没标 kind）→ 照回")
check("被 @ 也不回" in Q.stop_why(dict(on, at_too=True), kind=None, woken=True), "「免打扰时被 @ 也不回」开着 → @ 也拦")
check(Q.stop_why(on, private=True, kind="extra") == "" and Q.stop_why(dict(on, at_too=True), private=True, woken=True) == "", "私聊：什么都不拦")
check(Q.stop_why(off, kind="chat") == "", "不在免打扰：什么都不拦")
check("免打扰时段（01:00-07:00 北京时间）" == Q.ask_why(on, "extra", "napcat:GroupMessage:7001"), "别的插件问 ask(冒泡, 群)：说不行，写明时段")
check(Q.ask_why(on, "extra", "napcat:FriendMessage:30001") == "" and Q.ask_why(on, "direct", "napcat:GroupMessage:7001") == ""
      and Q.ask_why(off, "extra", "napcat:GroupMessage:7001") == "",
      "问的是私聊 / 有人找它的 / 不在免打扰：照常")
check(Q.ask_why(on, "chat", "", where="群 7001") and Q.ask_why(on, "chat", "", where="私聊") == "", "没给会话的：看 where 是不是「群 …」")

# ═══════════════ 5 配置和接上了 ═══════════════
print("\n[5 配置项；main.py / 群聊节奏 / 面板接上了]")
sch = json.loads((d / "_conf_schema.json").read_text(encoding="utf-8"))["alive"]["items"]
check(
    sch.get("quiet_hours", {}).get("default") == [] and sch.get("quiet_hours", {}).get("type") == "list"
    and sch.get("quiet_tz", {}).get("default") == "Asia/Shanghai"
    and sch.get("quiet_at_too", {}).get("default") is False and sch.get("quiet_at_too", {}).get("type") == "bool",
    "配置：免打扰时段（默认空 = 不免打扰）、时区（默认北京时间）、免打扰时被 @ 也不回（默认关）",
)
qh, qa = sch.get("quiet_hours", {}).get("hint", ""), sch.get("quiet_at_too", {}).get("hint", "")
rd = (d / "README.md").read_text(encoding="utf-8")
rq = rd.split("## 免打扰时段", 1)[-1].split("\n## ", 1)[0]
check(
    all("唤醒词叫它的照回" in x and "只叫它名字、没 @ 的不算" in x for x in (qh, rq))
    and "唤醒词叫它也不回" in qa
    and not any("叫名字的照回" in x or "叫它名字也不回" in x for x in (qh, qa, rq)),
    "说明写准了：免打扰时 @ / 引用 / 唤醒词叫它的照回，只叫名字、没 @ 的不算（跟平时一样：那种是群聊节奏打分定，免打扰时不接）",
    (qh[-160:], qa),
)
check(all(sch.get(k, {}).get("description") and sch.get(k, {}).get("hint") for k in ("quiet_hours", "quiet_tz", "quiet_at_too"))
      and "私聊不受影响" in sch["quiet_hours"]["hint"] and "跨半夜" in sch["quiet_hours"]["hint"],
      "每项都有说明和提示（写了私聊不受影响、可以跨半夜）")
main_src = (d / "main.py").read_text(encoding="utf-8")
wait = main_src.split("async def on_waiting(", 1)[-1].split("\n    @filter", 1)[0]
check(
    wait.find("self.gather.wait(event)") < wait.find("self._quiet_stop(event)") < wait.find("self.budget.gate(event)") and "self._quiet_stop(event)" in wait,
    "main.py：叫模型之前先攒一攒等完，再看免打扰，再掷省 token 的骰子",
)
stop = main_src.split("def _quiet_stop(", 1)[-1].split("\n    def ", 1)[0]
check("Q.stop_why(" in stop and "release_angel(" in stop and stop.find("release_angel(") < stop.find("event.stop_event()"),
      "拦下之前先替天使之心把这一轮记成结束（硬规矩 5）")
check("q = self._quiet_for(kind, conv, where)" in main_src.split("def ask(", 1)[-1][:400], "给别的插件的 ask()：先看免打扰")
rm = (d / "modules" / "rhythm_mod.py").read_text(encoding="utf-8")
grp = rm.split("async def _on_group(", 1)[-1].split("\n    async def ", 1)[0]
check(
    grp.find("event.is_at_or_wake_command") < grp.find("self._quiet_skip(") < grp.find("_old_decide(") and "self._quiet_skip(" in grp,
    "群聊节奏：@ 它的照常（AstrBot 本来就回）；没 @ 的先看免打扰，再判断接话 / 挑着接",
)
line = rm.split("async def _line(", 1)[-1].split("\n    async def ", 1)[0]
check('q = self.host.quiet_now(now)' in line and line.find("quiet_now") < line.find("llm_generate"), "冷场试探 / 收尾话：免打扰时不说（连模型都不叫）")
html = (d / "views" / "panel" / "index.html").read_text(encoding="utf-8")
check('id="q-top"' in html and "现在在免打扰" in html and 'id="r-quiet"' in html, "面板：总览和群聊节奏页都写「现在在免打扰」")
check('"quiet": self.quiet_now()' in main_src and '"quiet": self.host.quiet_now(now)' in rm, "面板接口带着免打扰的状态")
done()
