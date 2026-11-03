#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接话打分（v0.4.0，借 Inner Thoughts）的单元测试（不用装 AstrBot，一秒钟）：python tests/test_inner.py [插件目录 默认上一层]
测的是 core/inner.py（8 条怎么打、沉默加成、过线、理由、今天的数）、配置、群聊节奏里拆出来的「挑着接的硬规矩」。
AstrBot 那一层（影子不改接不接、生效按分数接、事后的证据）在 tests/e2e_rhythm.py。"""

import json
import sys
from pathlib import Path

d = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(d))
RES = []


def check(c, m, why=""):
    RES.append((bool(c), m))
    print(
        ("  [过]   " if c else "  [没过] ")
        + m
        + ("" if c or not why else f"\n           {str(why)[:600]}")
    )


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
    from core import inner as I  # noqa: E402
    from core import rhythm as rc  # noqa: E402
except ImportError as e:  # 旧代码（v0.3.0）没有这个模块
    check(False, "core/inner.py 在（接话打分）", e)
    done()

T = 1_000_000.0
BOT = ["大头鲢"]


def H(role, uid, name, text, ts):
    return {"role": role, "uid": uid, "name": name, "text": text, "ts": ts}


def me(text, ts):
    return H("assistant", "", "", text, ts)


# 它 10 秒前刚回了老王一句
BOT_A = {
    "text": "今天天气不错，适合出去走走",
    "sent": T - 10,
    "t0": T - 14,
    "answered": {"30001"},
}
HIST_A = [
    H("user", "30001", "老王", "今天天气怎么样", T - 20),
    me(BOT_A["text"], T - 10),
]


def ev(text, uid="30001", name="老王", hist=None, bot=BOT_A, **kw):
    return I.evaluate(
        text, uid, name, T, HIST_A if hist is None else hist, bot, names=BOT, **kw
    )


print("[1 八条、名字照论文]")
check(
    list(I.KEYS) == ["rel", "gap", "imp", "urg", "coh", "ori", "bal", "dyn"]
    and [I.NAMES[k] for k in I.KEYS]
    == ["相关", "信息缺口", "预期影响", "紧迫", "连贯", "原创", "平衡", "节奏"],
    "8 条：相关 / 信息缺口 / 预期影响 / 紧迫 / 连贯 / 原创 / 平衡 / 节奏",
)
e = ev("你也喜欢出去走走吗")
check(
    set(e["s"]) == set(I.KEYS) and all(0 <= v <= 5 for v in e["s"].values()),
    "每条 0~5 分",
    e["s"],
)
check(
    abs(e["base"] - sum(e["s"].values()) / 8) < 0.01
    and abs(e["total"] - e["base"] * e["boost"]) < 0.01,
    "总分 = 8 条平均 × 沉默加成",
    e,
)

print("\n[2 典型的几种：该接的过线、不该接的差得远]")
check(
    e["total"] >= 3.6
    and I.verdict(e["total"]) == "take"
    and I.kind_of(e, True) == "follow",
    f"它刚回了他、他紧跟着接它的词问它（{e['total']}）：接，算「接话」",
    e,
)
check(
    e["s"]["rel"] >= 4.5 and e["s"]["gap"] >= 4.5 and e["s"]["coh"] >= 4,
    "相关、信息缺口、连贯都高",
    e["s"],
)
r = I.reason(e)
check(
    "它刚回的就是他" in r and "接了它的词" in r and "冲它问的" in r,
    "理由写的是往高拉的原因（它刚回的就是他、接了它的词、冲它问的）",
    r,
)
e2 = ev("哈哈哈")
check(
    I.verdict(e2["total"]) == "skip"
    and e2["s"]["gap"] == 0
    and "只是应一声" in I.reason(e2),
    f"他只是「哈哈哈」（{e2['total']}）：不接，理由里写「只是应一声」",
    (e2["total"], I.reason(e2)),
)
e3 = ev("好的谢谢")
check(
    I.verdict(e3["total"]) == "skip" and e3["s"]["gap"] == 0,
    f"「好的谢谢」几个连着应也算应一声（{e3['total']}）：不接",
    e3["s"],
)
e4 = ev("好了睡了")
check(
    I.verdict(e4["total"]) == "skip"
    and e4["s"]["dyn"] == 0
    and e4["s"]["imp"] == 0
    and "收尾句" in I.reason(e4),
    f"「好了睡了」连着说的收尾句也认（{e4['total']}）：节奏、预期影响 0",
    (e4["s"], I.reason(e4)),
)
check(
    I.is_closing("好了睡了")
    and I.is_closing("晚安")
    and not I.is_closing("好了我们开始吧")
    and rc.is_closing("好了")
    and not rc.is_closing("好了睡了"),
    "收尾句：接话打分认连着说的；群聊节奏原来那个照旧（只认一个词）",
)
H2 = HIST_A + [H("user", "30002", "小李", "饿了", T - 5)]
B2 = dict(BOT_A, sent=T - 60, t0=T - 64)
e5 = ev("小李你吃了吗", uid="30003", name="阿珍", hist=H2, bot=B2)
check(
    I.verdict(e5["total"]) == "skip"
    and e5["s"]["rel"] <= 0.5
    and e5["s"]["gap"] <= 1
    and "开头叫的是「小李」" in I.reason(e5),
    f"开头叫的是群里别人（{e5['total']}）：相关压到 0.5、问的是别人（信息缺口 ≤ 1）",
    (e5["s"], I.reason(e5)),
)
e6 = ev("你说的那个东西在哪买的", uid="30002", name="小李", bot=B2)
check(
    e6["s"]["rel"] >= 3
    and "在说它刚才的话" in I.reason(e6)
    and I.kind_of(e6) == "follow",
    "「你说的…」：在说它刚才的话（相关 +1.5，算接话）",
    (e6["s"], I.reason(e6)),
)
check(
    I.verdict(e6["total"]) == "maybe",
    f"别人（不是它刚回的）问它刚说的（{e6['total']}）：拿不准（生效时问一下判断模型）",
)
e7 = ev(
    "有人知道明天几点开会吗", uid="30004", name="路人", bot=dict(BOT_A, sent=T - 400)
)
check(
    not e7["near"]
    and e7["s"]["rel"] == 1
    and e7["s"]["gap"] >= 4
    and e7["s"]["imp"] >= 4
    and I.kind_of(e7) == "chat",
    "它好久没说话了、有人在群里求助：相关低、信息缺口和预期影响高，算「挑着接」",
    e7["s"],
)
H8 = HIST_A + [H("user", "30002", "小李", "+1", T - 5)]
e8 = ev("+1", uid="30003", name="阿珍", hist=H8, bot=B2)
check(
    e8["s"]["ori"] == 0.5
    and "在复读" in I.reason(e8)
    and I.verdict(e8["total"]) == "skip",
    "在复读：原创 0.5，不接",
    (e8["s"], I.reason(e8)),
)
e9 = ev("急急急 在吗", uid="30002", name="小李", bot=B2)
check(e9["s"]["urg"] >= 2.5, "「急 / 在吗」：紧迫高", e9["s"])
H10 = [
    H("user", "30002", "小李", "周末去爬山吗", T - 30),
    H("user", "30003", "阿珍", "去哪座山", T - 25),
    H("user", "30002", "小李", "就城东那座", T - 20),
    H("user", "30003", "阿珍", "那座太陡了吧", T - 15),
]
e10 = I.evaluate("还好吧不算陡", "30002", "小李", T, H10, {}, names=BOT, active=True)
check(
    "他在跟阿珍一来一回" in str(e10["dn"]["coh"]) and e10["s"]["coh"] <= 1,
    "他在跟别人一来一回：连贯低",
    e10["s"],
)

print("\n[3 平衡：照顾少说话的人，它自己别说太多]")
busy = []
for i in range(10):
    busy.append(
        H("user", "3000" + str(2 + i % 3), "某人", f"第{i}句话", T - 100 + i * 5)
    )
    if i % 3 != 2:
        busy.append(me(f"它说的第{i}句", T - 98 + i * 5))
eb = I.evaluate(
    "那你觉得呢", "30002", "小李", T, busy[-20:], {}, names=BOT, active=True
)
check(
    eb["s"]["bal"] <= 1.5 and "它最近说得多" in I.reason(eb),
    "它最近 20 句里说了 35% 以上：平衡低",
    (eb["s"], I.reason(eb)),
)
quiet_hist = [H("user", "30002", "小李", f"闲聊{i}", T - 100 + i) for i in range(8)]
eq = I.evaluate(
    "我也想说两句", "30009", "潜水的", T, quiet_hist, {}, names=BOT, active=True
)
eq2 = I.evaluate(
    "我也想说两句", "30002", "小李", T, quiet_hist, {}, names=BOT, active=True
)
check(
    eq["s"]["bal"] > eq2["s"]["bal"] and "他很少说话" in str(eq["up"]["bal"]),
    "很少说话的人：平衡更高（照顾少说话的人）",
    (eq["s"]["bal"], eq2["s"]["bal"]),
)
et = ev("你也喜欢出去走走吗", turns=3)
check(
    et["s"]["bal"] <= e["s"]["bal"] - 1
    and "已经连着接了他 3 轮" in str(et["dn"]["bal"]),
    "它已经不 @ 连着接了他 3 轮：平衡 -1",
    (et["s"]["bal"], e["s"]["bal"]),
)

print("\n[4 节奏：插得进去吗]")
en = ev("你也喜欢出去走走吗", noise=9)
check(
    en["s"]["dyn"] <= 2 and "群里刷得快" in str(en["dn"]["dyn"]),
    "群里刷得快：节奏低",
    en["s"],
)
el = I.evaluate(
    "有人在吗",
    "30002",
    "小李",
    T,
    [H("user", "30003", "阿珍", "嗯", T - 600)],
    {},
    names=BOT,
    active=True,
)
check(
    "冷了一会儿有人开口" in str(el["up"]["dyn"]), "冷了一阵有人开口：节奏 +0.5", el["s"]
)

print("\n[5 沉默越久越想说（× 1.02，最多 × 1.3）]")
h0 = [me("它说的", T - 50)] + [
    H("user", "3000" + str(i % 3), "某人", f"话{i}", T - 40 + i) for i in range(10)
]
check(
    I.silence(h0) == 10 and I.silence([me("x", T)]) == 0 and I.silence([]) == 0,
    "它上次说话以后群里过了几句：10 / 0 / 0",
)
check(
    I.boost_of(0) == 1.0
    and abs(I.boost_of(10) - 1.02**10) < 0.001
    and I.boost_of(40) == 1.3
    and I.boost_of(10, growth=1.0) == 1.0
    and I.boost_of(10, cap=1.0) == 1.0,
    "沉默加成：0 句 ×1、10 句 ×1.219、40 句封顶 ×1.3；growth / cap 设成 1 = 不加",
)
es = I.evaluate("周末有人去爬山吗", "30005", "路人", T, h0, {}, names=BOT, active=True)
es0 = I.evaluate(
    "周末有人去爬山吗", "30005", "路人", T, h0, {}, names=BOT, active=True, growth=1.0
)
check(
    es["quiet"] == 10
    and abs(es["total"] - es0["total"] * 1.02**10) < 0.02
    and "它沉默了 10 句" in I.reason(es),
    f"同一句：沉默 10 句以后 {es0['total']} → {es['total']}；理由里写沉默了几句",
    (es["total"], es0["total"], I.reason(es)),
)

print("\n[6 过线、拿不准、算哪种]")
check(
    I.verdict(3.6) == "take"
    and I.verdict(3.59) == "maybe"
    and I.verdict(2.8) == "maybe"
    and I.verdict(2.79) == "skip"
    and I.verdict(3.0, 3.0, 0) == "take"
    and I.verdict(2.99, 3.0, 0) == "skip",
    "线 3.2、上下 0.4：≥3.6 接、<2.8 不接、中间拿不准；band 0 = 全按线定",
)
check(
    I.kind_of({"near": True, "s": {"rel": 1, "coh": 1}}, answered=True) == "follow"
    and I.kind_of({"near": True, "s": {"rel": 3, "coh": 1}}) == "follow"
    and I.kind_of({"near": True, "s": {"rel": 2, "coh": 2}}) == "chat"
    and I.kind_of({"near": False, "s": {"rel": 5, "coh": 5}}) == "chat",
    "算哪种：在接话窗口里、跟它有关 / 接得上 → 接话；不然 → 挑着接",
)

print("\n[7 今天的数（影子拿来比）]")
dd = I.Daily({"day": "2000-01-01", "scored": 9})
day = "2026-11-03"
dd.note({"it": "take", "now": "follow"}, day)
dd.note({"it": "take", "now": ""}, day)
dd.note({"it": "skip", "now": "chat"}, day)
dd.note({"it": "skip", "now": ""}, day)
dd.note({"it": "ask", "now": ""}, day)
t = dd.today(day)
check(
    t["scored"] == 5
    and t["it_take"] == 2
    and t["it_skip"] == 2
    and t["it_ask"] == 1
    and t["now_take"] == 2
    and t["agree"] == 2
    and t["it_more"] == 1
    and t["it_less"] == 1,
    "换天清零；打分说接 / 不接 / 拿不准、实际接了、一样、会多接、会少接",
    t,
)
dd.outcome({"it": "skip"}, "at_after", day)
dd.outcome({"it": "take"}, "at_after", day)
dd.outcome({"it": "take"}, "silent", day)
dd.outcome({"it": "ask"}, "silent", day)
t = dd.today(day)
check(
    t["at_after"] == 2
    and t["at_after_it_skip"] == 1
    and t["at_after_it_take"] == 1
    and t["silent"] == 2
    and t["silent_it_take"] == 1
    and t["silent_it_skip"] == 0,
    "事后的证据：没接后来又 @ 它（按打分怎么说分开数）、接了模型选不说",
    t,
)

print("\n[8 配置]")
schema = json.loads((d / "_conf_schema.json").read_text(encoding="utf-8"))
ri = schema["rhythm"]["items"]
check(
    ri["inner_mode"]["default"] == "shadow"
    and ri["inner_mode"]["options"] == list(I.MODES)
    and len(ri["inner_mode"]["labels"]) == 3
    and "影子" in ri["inner_mode"]["labels"][0],
    "接话打分默认影子（三个选项都有中文标签）",
)
check(
    all(ri[k]["default"] == rc.DEFAULTS[k] for k in ri if k.startswith("inner_"))
    and rc.DEFAULTS["inner_threshold"] == 3.2
    and rc.DEFAULTS["inner_band"] == 0.4
    and rc.DEFAULTS["inner_growth"] == 1.02
    and rc.DEFAULTS["inner_growth_cap"] == 1.3
    and rc.DEFAULTS["inner_log_days"] == 14,
    "线 3.2、拿不准 ±0.4、沉默 ×1.02 最多 ×1.3、记录留 14 天；配置说明和代码一致",
)
c = rc.Conf({"inner_mode": "乱写"})
c2 = rc.Conf({"inner_mode": " live "})
check(
    c("inner_mode") == "shadow" and c2("inner_mode") == "live",
    "模式填错了按影子；前后有空格也认",
)

print("\n[9 在线时挑着接：硬规矩拆出来（不掷骰子），原来的掷骰子照旧]")
seq = iter([0.1, 0.9])
rh = rc.Rhythm(rc.Conf({"reply_prob": 0.5, "min_gap_sec": 45}), rand=lambda: next(seq))
check(rh.chat_block("g", T) == "不在线", "不在线：不能挑着接")
rh.spoke("g", True, T)
check(
    "不到 45 秒" in rh.chat_block("g", T + 10) and rh.chat_block("g", T + 50) == "",
    "离上次说话不到 45 秒：不行；过了：行",
)
check(rh.chat_block("g", T + 50, to_other=True) == "在跟别人说", "在跟别人说：不行")
check(
    rh.want_reply("g", now=T + 50) is True
    and "不到 45 秒" in rh.chat_block("g", T + 60),
    "掷中了：想接也算一次（之后 45 秒内不再想）",
)
check(rh.want_reply("g", now=T + 200) is False, "没掷中：不接")
rh.note_try("g", T + 300)
check("不到 45 秒" in rh.chat_block("g", T + 310), "note_try：生效时按分数接了也记一次")

done()
