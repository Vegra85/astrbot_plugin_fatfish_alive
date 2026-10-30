#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rhythm_core.py 单独测（不用装 AstrBot）：python tests/test_rhythm_core.py [插件目录 默认上一层]"""

import importlib.util
import sys
from pathlib import Path

d = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
spec = importlib.util.spec_from_file_location("rhythm_core", d / "core" / "rhythm.py")
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)
RES = []


def check(c, m, why=""):
    RES.append((bool(c), m))
    print(
        ("  [过]   " if c else "  [没过] ")
        + m
        + ("" if c or not why else f"\n           {str(why)[:600]}")
    )


class Seq:
    """假的随机数：按顺序吐给定的数，吐完一直吐最后一个"""

    def __init__(self, *xs):
        self.xs = list(xs) or [0.5]

    def __call__(self, *a):
        return self.xs.pop(0) if len(self.xs) > 1 else self.xs[0]


def conf(**kw):
    return rc.Conf(dict(kw))


T = 1_000_000.0  # 假的「现在」
A, B, C = "30001", "30002", "30003"


def user(uid, name, text, ts, **kw):
    return {"role": "user", "uid": uid, "name": name, "text": text, "ts": ts, **kw}


print("[0 配置说明和代码里的默认值一致]")
import json

schema = json.loads((d / "_conf_schema.json").read_text(encoding="utf-8"))["rhythm"][
    "items"
]
check(
    set(schema) == set(rc.DEFAULTS),
    "配置项一样多、名字一样",
    set(schema) ^ set(rc.DEFAULTS),
)
diff = [
    k for k in schema if k in rc.DEFAULTS and schema[k].get("default") != rc.DEFAULTS[k]
]
check(
    not diff,
    "默认值一样",
    [(k, schema[k].get("default"), rc.DEFAULTS[k]) for k in diff],
)
tmap = {bool: "bool", int: "int", float: "float", str: "string", list: "list"}
wrong = [
    k
    for k in schema
    if k in rc.DEFAULTS and tmap.get(type(rc.DEFAULTS[k])) != schema[k].get("type")
]
check(not wrong, "类型一样", wrong)
check(all(schema[k].get("description") for k in schema), "每项都有中文说明")

print("\n[1 读配置]")
c = rc.Conf(
    {
        "window_sec": "200",
        "yes_score": "x",
        "ask_model": "false",
        "bot_names": "大鱼，小鱼 鱼鱼",
        "max_turns": 3.0,
        "reply_prob": None,
    }
)
check(
    c("window_sec") == 200
    and c("yes_score") == 4.0
    and c("ask_model") is False
    and c("max_turns") == 3,
    "数字字符串能读、填错了用默认、开关字符串能读",
    (c("window_sec"), c("yes_score"), c("ask_model"), c("max_turns")),
)
check(
    c("bot_names") == ["大鱼", "小鱼", "鱼鱼"]
    and c("reply_prob") == 0.3
    and rc.Conf()("groups") == [],
    "名单可以用逗号 / 空格分开；没填（None）用默认",
    c("bot_names"),
)
check(
    isinstance(c("online_min"), float) and isinstance(c("min_gap_sec"), int),
    "类型照默认值",
)

print("\n[2 接话打分]")
fu = rc.Followup(conf())
v, why, s = fu.check("g", A, "老王", "你刚才说的啥意思", T, [])
check(v == "no" and why == "", "机器人还没在这个群说过话：不算（也不写原因）", (v, why))
fu.note_turn("g", "收到，我觉得火锅最好吃", [A], t0=T - 12, sent=T - 10)
v, why, s = fu.check("g", A, "老王", "你刚才说的啥意思", T, [])
check(
    v == "yes"
    and s >= 4
    and all(
        x in why for x in ("它刚回的就是他", "说的是「你」", "在问", "紧跟着它 10 秒")
    ),
    "它刚回的人、说「你」、在问、紧跟着它 → 接",
    (v, why, s),
)
fu.c.src["topic_overlap"] = (
    False  # 活人感新增了「话题词没对上两个扣分」（默认开）；这两条老断言按没有这条规则的老算法写的，新规则的断言在 test_alive_new.py
)
v, why, s = fu.check("g", B, "小李", "今天天气不错", T, [])
check(
    v == "no" and s == 1.5,
    "别人随口一句（只占「紧跟着」「第一句」）→ 不接（1.5 分）",
    (v, why, s),
)
v, why, s = fu.check("g", B, "小李", "这个怎么弄", T, [])
check(
    v == "ask" and s == 2.5 and "在问" in why,
    "在问但没别的信号（2.5 分）→ 让模型看",
    (v, why, s),
)
fu.c.src.pop("topic_overlap", None)
v, why, s = fu.check("g", B, "小李", "火锅哪家好吃", T, [])
check(
    v == "yes" and "接了它的词「好吃、火锅」" in why,
    "接了它说的词（两个以上）→ 加分多",
    (v, why, s),
)
v, why, s = fu.check("g", B, "小李", "你们说呢", T, [])
check(
    "说的是「你」" not in why and v == "ask",
    "「你们」只加一点（跟大家说的）",
    (v, why, s),
)
v, why, s = fu.check("g", B, "小李", "你刚才说的啥意思", T + 200, [])
check(v == "no" and why == "", "过了接话窗口（150 秒）：不算", (v, why))
fu2 = rc.Followup(conf(ask_model=False))
fu2.note_turn("g", "嗯", [A], T - 10, T - 10)
check(
    fu2.check("g", B, "小李", "这个怎么弄", T, [])[0] == "no",
    "关了「拿不准让模型看」：中间分数算不接",
)

print("\n[3 扣分 / 别的信号]")
hist = [user(C, "阿珍", "哈哈", T - 8), user("30004", "路人", "我也觉得", T - 5)]
v, why, s = fu.check("g", A, "老王", "你刚才说的啥意思", T, hist)
check(
    "中间有别人插了 2 句" in why and s == 4.5,
    "中间有别人插了两句 → 扣 1.5 分（6 → 4.5）",
    (why, s),
)
hist = [user(C, "阿珍", "@老王 你来不来", T - 3)]
v, why, s = fu.check("g", A, "老王", "来啊你说呢", T, hist)
check(
    "别人刚 @ 了他" in why and s < 4,
    "别人刚 @ 了他 → 多半在回那个人（扣分）",
    (v, why, s),
)
fu.note_turn("g", "老王你也来尝尝", [B], T - 10, T - 10)
v, why, s = fu.check("g", A, "老王123", "好呀", T, [])
check("它刚点了他的名" in why, "它刚点了他的名（群名片后面的数字去掉再比）", (why, s))
fu.note_turn("g", "嗯嗯", [B], T - 100, T - 100)
v, why, s = fu.check("g", A, "老王", "胖头鱼你也来", T, [], names=["胖头鱼"])
check(
    "叫了它的名字" in why and "接了它的词" not in why,
    "叫了它的名字 → 加分（名字里的字不算「接了它的词」）",
    (why, s),
)
fu.note_turn("g", "胖头鱼最喜欢火锅", [A], T - 10, T - 10)
v, why, s = fu.check("g", B, "小李", "胖头鱼", T, [], names=["胖头鱼"])
check("接了它的词" not in why, "只说了它的名字：不算接了它的词", why)

print("\n[4 光发图]")
fu.note_turn("g", "嗯", [A], T - 10, T - 10)
fu_p = rc.Followup(conf(), rand=Seq(0.1))
fu_p.note_turn("g", "嗯", [A], T - 10, T - 10)
v, why, s = fu_p.check("g", A, "老王", "你看这个？", T, [], only_pic=True)
check(
    v == "yes" and "只发了图" in why,
    "光发图也够分：按 pic_prob（0.4）概率接（这次掷到 0.1）",
    (v, why, s),
)
fu_p.rand = Seq(0.9)
v, why, s = fu_p.check("g", A, "老王", "你看这个？", T, [], only_pic=True)
check(v == "no" and "图 这次不接" in why, "掷到 0.9：这次不接", (v, why))
v, why, s = fu.check("g", B, "小李", "[图片]", T, [], only_pic=True)
check(v == "no", "光发图、分不够：不问模型、直接不接", (v, why, s))

print("\n[5 连着接几轮]")
fu = rc.Followup(conf(max_turns=2, owner_max_turns=4, turn_reset_min=10))
fu.note_turn("g", "嗯", [A], T - 5, T - 5)
check(fu.took("g", A, T) == 1 and fu.took("g", A, T + 1) == 2, "记轮数：1、2")
v, why, s = fu.check("g", A, "老王", "你刚才说的啥意思", T + 2, [])
check(
    v == "no" and "连着接了 2 轮" in why and fu.capped("g", A, T + 2),
    "同一个人不 @ 连着接到上限 → 不接（要 @ 才接）",
    (v, why),
)
v, why, s = fu.check("g", A, "老王", "你刚才说的啥意思", T + 2, [], is_owner=True)
check(v == "yes", "管理员上限高一点（4 轮）", (v, why))
fu.reset("g", A)
check(
    fu.check("g", A, "老王", "你刚才说的啥意思", T + 2, [])[0] == "yes"
    and not fu.capped("g", A, T + 2),
    "有人 @ 它（reset）→ 重新算",
)
fu.took("g", B, T)
fu.took("g", B, T)
check(
    fu.capped("g", B, T + 60)
    and not fu.capped("g", B, T + 601)
    and fu.turns("g", B, T + 601) == 0,
    "10 分钟没接：轮数清零",
)
fu = rc.Followup(conf(turn_reset_min=10))
for i in range(50):
    fu.took("g", f"4{i:04d}", T + i)
fu.took("g", A, T + 2000)
check(
    list(fu.st("g")["turns"]) == [A],
    "轮数表不会只涨不跌：早该清零的人记新一轮时顺手清掉",
    len(fu.st("g")["turns"]),
)

print("\n[6 让模型判断]")
hist = [
    user(A, "老王", "今天吃啥", T - 40),
    {"role": "assistant", "text": "火锅吧", "ts": T - 30},
    user(B, "小李", "好主意", T - 10, re="老王"),
]
body = rc.judge_body(hist, "阿珍", "这个怎么弄", T, "胖头鱼")
check(
    "[40秒前] 老王: 今天吃啥" in body
    and "[30秒前] 胖头鱼: 火锅吧" in body
    and "小李（回复 老王）: 好主意" in body
    and body.endswith("[刚刚] 阿珍: 这个怎么弄   ← 判断这一条"),
    "给模型看的：最近几条 + 要判断的这条",
    body,
)
sysp = rc.judge_system("胖头鱼")
check(
    "「胖头鱼」（是个 AI）" in sysp
    and "只回答一个字：是 或 否" in sysp
    and "{bot}" not in sysp,
    "判断的 system prompt 填好了名字",
    sysp,
)
yes = ["是", "是。", "是的", "**是**", "<think>想一想</think>是", "「是」", "Yes"]
no = [
    "不是",
    "否",
    "不",
    "不是。",
    "否，他在跟别人说",
    "",
    None,
    "嗯",
    "<think>是吗</think>否",
]
check(
    all(rc.judge_yes(x) for x in yes),
    "「是」认得出",
    [x for x in yes if not rc.judge_yes(x)],
)
check(
    not any(rc.judge_yes(x) for x in no),
    "「不是」「否」、空的、看不懂的 → 算不是",
    [x for x in no if rc.judge_yes(x)],
)

print("\n[7 在线一阵]")
rh = rc.Rhythm(
    conf(online_min=8, online_max=15), rand=Seq(0.0), uniform=lambda a, b: (a + b) / 2
)
check(not rh.is_active("g") and rh.status("g", T)["mode"] == "观望", "一开始是观望")
check(rh.spoke("g", True, T) == "enter" and rh.is_active("g"), "说上话 → 在线")
st = rh.status("g", T + 60)
check(
    st["mode"] == "在线"
    and st["left_sec"] == int(11.5 * 60 - 60)
    and st["in_sec"] == 60,
    "在线 11.5 分钟（8~15 随机）",
    st,
)
check(rh.want_reply("g", False, T + 10) is False, "两句之间不到 45 秒：不接")
check(rh.want_reply("g", False, T + 50) is True, "过了 45 秒、掷到 0（< 0.3）→ 接")
check(rh.want_reply("g", False, T + 60) is False, "刚想接过（哪怕没说）：又要隔 45 秒")
check(rh.want_reply("g", True, T + 200) is False, "在 @ 别人 / 引用别人：不抢话")
rh.rand = Seq(0.2)
check(
    rh.want_reply("g", False, T + 200, only_pic=True) is False,
    "光发图：概率打折（0.3×0.3=0.09，掷到 0.2 不接）",
)
rh.rand = Seq(0.5)
check(rh.want_reply("g", False, T + 300) is False, "掷到 0.5（> 0.3）：不接")
rh0 = rc.Rhythm(conf(reply_prob=0.0, min_gap_sec=0), rand=Seq(0.0))
rh0.spoke("g", True, T)
check(rh0.want_reply("g", False, T + 1) is False, "概率调成 0：怎么都不接")
rh1 = rc.Rhythm(conf(reply_prob=1.0, min_gap_sec=0, max_replies=2), rand=Seq(0.99))
rh1.spoke("g", True, T)
ok1 = rh1.want_reply("g", False, T + 1)
rh1.spoke("g", False, T + 2)
ok2 = rh1.want_reply("g", False, T + 3)
rh1.spoke("g", False, T + 4)
check(
    ok1 and ok2 and not rh1.want_reply("g", False, T + 5),
    "概率调成 1：都接；每轮挑着接到上限（2）就不接了",
)
rh = rc.Rhythm(
    conf(online_min=10, online_max=10, max_minutes=12), uniform=lambda a, b: a
)
rh.spoke("g", True, T)
rh.spoke("g", True, T + 8 * 60)
check(
    rh.st("g")["until"] == T + 12 * 60,
    "一直被 @ 着：多待一会儿，但最多在线 12 分钟（max_minutes）",
    rh.st("g")["until"] - T,
)
rh.spoke("g", False, T + 9 * 60)
check(rh.st("g")["free"] == 1, "挑着接的那句：算进这一轮的次数")

print("\n[8 冷场试探 / 收尾]")
rh = rc.Rhythm(
    conf(online_min=10, online_max=10, idle_min=5, probe_prob=1.0, probe_wait_min=2),
    rand=Seq(0.0),
    uniform=lambda a, b: a,
)
rh.spoke("g", True, T)
check(rh.tick("g", T + 60) is None, "有人在聊：什么都不做")
check(
    rh.tick("g", T + 5 * 60) == "probe" and rh.st("g")["probed"] == T + 300,
    "5 分钟没人说话 → 试探一句",
)
check(rh.tick("g", T + 5 * 60 + 30) is None, "试探后等着")
rh.heard("g", T + 5 * 60 + 40)
check(
    rh.tick("g", T + 5 * 60 + 50) is None
    and rh.st("g")["probed"] == 0
    and rh.is_active("g"),
    "有人接话了：接着聊",
)
rh.tick("g", T + 10 * 60 + 41)
rh2 = rc.Rhythm(
    conf(online_min=10, online_max=10, idle_min=5, probe_prob=1.0, probe_wait_min=2),
    rand=Seq(0.0),
    uniform=lambda a, b: a,
)
rh2.spoke("g", True, T)
rh2.tick("g", T + 300)
check(
    rh2.tick("g", T + 300 + 121) == "quiet"
    and not rh2.is_active("g")
    and not rh2.cooling("g", T + 430),
    "试探了没人理：安静回观望（不算收尾、没有冷却）",
)
rh3 = rc.Rhythm(
    conf(online_min=10, online_max=10, idle_min=5, probe_prob=0.0),
    rand=Seq(0.5),
    uniform=lambda a, b: a,
)
rh3.spoke("g", True, T)
check(
    rh3.tick("g", T + 300) == "quiet" and not rh3.is_active("g"),
    "冷场、没掷中试探：默默回观望",
)
rh4 = rc.Rhythm(
    conf(online_min=10, online_max=10, exit_line_prob=1.0, cooldown_min=10),
    rand=Seq(0.0),
    uniform=lambda a, b: a,
)
rh4.spoke("g", True, T)
rh4.heard("g", T + 590)
check(
    rh4.tick("g", T + 600) == "exit"
    and not rh4.is_active("g")
    and rh4.cooling("g", T + 601),
    "到点了、大家还在聊 → 收尾一句，进冷却",
)
check(
    rh4.status("g", T + 660)["mode"] == "收尾冷却"
    and rh4.status("g", T + 660)["cool_sec"] == 540,
    "面板：收尾冷却还剩 9 分钟",
)
check(
    rh4.spoke("g", True, T + 700) is None and not rh4.is_active("g"),
    "冷却中被 @ 回了一句：不重新进在线",
)
check(rh4.want_reply("g", False, T + 700) is False, "冷却中：不挑着接")
check(rh4.spoke("g", True, T + 1201) == "enter", "冷却过了：说上话又进在线")
rh5 = rc.Rhythm(
    conf(online_min=10, online_max=10, exit_line_prob=1.0, exit_if_chat_within_min=3),
    rand=Seq(0.0),
    uniform=lambda a, b: a,
)
rh5.spoke("g", True, T)
rh5.heard("g", T + 100)
check(
    rh5.tick("g", T + 600) == "quiet",
    "到点了、但最近 3 分钟没人聊：默默走（不说收尾话）",
)
rh6 = rc.Rhythm(
    conf(online_min=10, online_max=10, exit_line=False),
    rand=Seq(0.0),
    uniform=lambda a, b: a,
)
rh6.spoke("g", True, T)
rh6.heard("g", T + 590)
check(
    rh6.tick("g", T + 600) == "quiet" and rh6.cooling("g", T + 601),
    "关了收尾话：到点默默走（照样冷却）",
)
rh4.to_lurk("g", T, cool=True)
rh4.uncool("g")
check(not rh4.cooling("g", T + 1), "面板里清掉冷却")
rh7 = rc.Rhythm(conf(lines_per_hour=2))
check(
    rh7.line_ok("g", T) and (rh7.note_line("g", T) or True) and rh7.line_ok("g", T + 1),
    "每小时主动说的句数：没到上限",
)
rh7.note_line("g", T + 2)
check(
    not rh7.line_ok("g", T + 3) and rh7.line_ok("g", T + 3601),
    "到上限（2 句）就不说；一小时后又可以",
)
rh8 = rc.Rhythm(conf(rhythm_enable=False))
check(
    rh8.spoke("g", True, T) is None
    and not rh8.is_active("g")
    and rh8.tick("g", T) is None
    and not rh8.want_reply("g"),
    "关了「在线一阵」：一直观望",
)
check(sorted(rc.Rhythm(conf()).active_groups()) == [], "没有在线的群")

print("\n[9 给模型的话]")
fh = rc.follow_hint("它刚回的就是他、说的是「你」、紧跟着它 6 秒")
check(
    rc.SILENT in fh
    and "（你刚回的就是他、说的是「你」、紧跟着你 6 秒）" in fh
    and "没 @ 你" in rc.follow_hint(""),
    "接话那一轮：说清楚没 @ 它、为什么（换成「你」的口气）、不是跟它说就 [[不说]]",
    fh,
)
check(
    "已经聊了 7 分钟" in rc.chat_hint(7.9) and rc.SILENT in rc.chat_hint(1),
    "挑着接那一轮：聊了几分钟、能不接",
)
sp = rc.line_system("你是一条胖头鱼。", "probe", "21:05")
check(
    sp.startswith("你是一条胖头鱼。")
    and "现在是 21:05" in sp
    and "突然安静" in sp
    and rc.SILENT in sp,
    "试探：人格 + 几点 + 冷场了",
    sp,
)
se = rc.line_system("", "exit", "")
check(
    "下线" in se and "别提问" in se and not se.startswith("\n"),
    "收尾：没有人格也行",
    se,
)
hist = [
    user(A, "老王", "今天吃火锅", T - 120),
    {"role": "assistant", "text": "好耶", "ts": T - 100},
]
lp = rc.line_prompt(hist, T, "probe")
check(
    "[2分钟前] 老王: 今天吃火锅" in lp
    and "[1分钟前] 你: 好耶" in lp
    and "（大家突然都不说话了）" in lp,
    "试探时给模型看最近的群聊",
    lp,
)
check(
    "（你想下线 / 潜水了）" in rc.line_prompt([], T, "exit")
    and "没记下来" in rc.line_prompt([], T, "exit"),
    "收尾的要求；没记录也能写",
)
check(
    rc.clean_line("<think>嗯</think>\n「我先去吃饭了，你们聊」\n第二行")
    == "我先去吃饭了，你们聊",
    "去掉 <think>、引号，只要第一行",
)
check(
    rc.clean_line("[[不说]]") == ""
    and rc.clean_line("  ") == ""
    and rc.clean_line("好吧 [[不说]]") == "",
    "[[不说]] / 空的：不发",
)
long = rc.clean_line("哈" * 300)
check(len(long) == 121 and long.endswith("…"), "太长：截断", len(long))
check(rc.is_silent("[[不说]]") and not rc.is_silent("说"), "认得 [[不说]]")
ctx = (
    "<system_reminder>群聊上下文：\n[小李/12:00:01]: @阿珍 晚上吃啥\n</system_reminder>"
)
cb = rc.carry_block(["", ctx], "随便说说", "阿珍")
check(
    cb.startswith(rc.CARRY_HEAD)
    and cb.index("@阿珍 晚上吃啥") < cb.index("阿珍: 随便说说"),
    "没接话那一轮：留下当时带着的群聊上下文 + 这条消息（按先后）",
    cb,
)
cb2 = rc.carry_block(
    [cb, "<system_reminder>新的上下文</system_reminder>"], "又一条", "小李"
)
check(
    cb2.count(rc.CARRY_HEAD) == 1
    and cb2.index("随便说说") < cb2.index("新的上下文") < cb2.index("小李: 又一条"),
    "连着两轮没接话：前一段接着带上（不重复加开头）",
    cb2,
)
check(
    rc.carry_block([], "", "") == "" and rc.carry_block(["  "], None, "x") == "",
    "什么都没有：不留",
)
big = rc.carry_block(["字" * 9000], "最后一句", "阿珍", max_chars=4000)
check(
    len(big) <= len(rc.CARRY_HEAD) + 4001 and big.endswith("阿珍: 最后一句"),
    "太长：只留最后那部分",
    len(big),
)

print("\n[11 有人 @ 它说欢迎（v0.1.1）]")
check(
    all(
        rc.is_welcome(t)
        for t in (
            "欢迎新人！",
            "欢迎加入本群！Ciallo～",
            "欢迎欢迎",
            "欢迎~",
            "欢迎你呀",
            "新人报到",
        )
    ),
    "认得出欢迎：欢迎新人 / 欢迎加入本群 / 欢迎欢迎 / 欢迎你",
    [t for t in ("欢迎新人！", "欢迎加入本群！Ciallo～") if not rc.is_welcome(t)],
)
check(
    not any(
        rc.is_welcome(t) for t in ("你欢迎谁", "不受欢迎", "这个节目很受欢迎", "晚安")
    ),
    "别的不算：不受欢迎 / 很受欢迎",
)
wh = rc.welcome_hint("阿珍")
check(
    "欢迎你进群" in wh and "不是阿珍" in wh and "别反过来欢迎他" in wh,
    "提示：是在欢迎你、你是新来的、别反过来欢迎他",
    wh,
)

print("\n[10 今天的数]")
dd = rc.Daily({"day": "2000-01-01", "follow": 9})
check(
    dd.today("2026-10-27")["follow"] == 0
    and dd.add("follow", day="2026-10-27") == 1
    and dd.add("probe", day="2026-10-27") == 1,
    "换天清零；加数",
)
check(set(rc.Daily.KEYS) <= set(dd.today("2026-10-27")), "几样数都有")

ok = sum(1 for c, _ in RES if c)
bad = [m for c, m in RES if not c]
print(
    "\n"
    + "=" * 50
    + f"\n  结果：{ok} 项过了"
    + (f"，{len(bad)} 项没过" if bad else "，全过")
)
for m in bad:
    print("    -", m)
sys.exit(1 if bad else 0)
