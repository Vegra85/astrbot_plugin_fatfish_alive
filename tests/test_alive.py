#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""活人感合并新增的单元测试（不用装 AstrBot，几秒钟）：python tests/test_alive.py [插件目录 默认上一层]
测的是合并才有的东西：存档 / 配置迁移、让路检测、兜底的判断、攒一攒的「稍等」、群聊节奏的「收尾句不接」和「话题词没对上扣分」。
原来三个插件自己的单元测试在 test_gather.py / test_rhythm.py / test_budget.py。"""

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

d = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(d))
from core import compat, conf as cf, gather as G, guard, rhythm as rc, state as stt  # noqa: E402

RES = []


def check(c, m, why=""):
    RES.append((bool(c), m))
    print(
        ("  [过]   " if c else "  [没过] ")
        + m
        + ("" if c or not why else f"\n           {str(why)[:600]}")
    )


SCHEMA = json.loads((d / "_conf_schema.json").read_text(encoding="utf-8"))
T = 1_000_000.0


def star(name, root=None, display=None, activated=True):
    return SimpleNamespace(
        name=name,
        root_dir_name=root or name,
        display_name=display or "",
        activated=activated,
    )


# ═══════════════ 1 配置说明 ═══════════════
print("[1 配置说明]")
check(
    list(SCHEMA) == ["alive", "gather", "rhythm", "budget", "guard", "compat"],
    "六个分组：总开关 / 攒一攒 / 群聊节奏 / 省 token / 兜底 / 兼容",
    list(SCHEMA),
)
miss = [
    (g, k)
    for g, v in SCHEMA.items()
    for k, i in v["items"].items()
    if not i.get("description") or not i.get("hint")
]
check(not miss, "每一项都有中文说明和提示", miss[:5])
check(all(v.get("type") == "object" for v in SCHEMA.values()), "分组都是 object")
check(SCHEMA["guard"]["items"]["strip_ai_tone"]["default"] is False, "去 AI 腔默认关")
check(SCHEMA["compat"]["items"]["auto_yield"]["default"] is True, "自动让路默认开")
check(
    set(SCHEMA["gather"]["items"]) >= {"hold_phrases", "hold_sec"}
    and set(SCHEMA["rhythm"]["items"])
    >= {"closing_skip", "topic_overlap", "topic_penalty"},
    "新增的几项都在配置里（稍等 / 收尾句 / 话题词）",
)
meta = (d / "metadata.yaml").read_text(encoding="utf-8")
check(
    "astrbot_plugin_fatfish_alive" in meta
    and "license: MIT" in meta
    and "0.1.0" in meta,
    "metadata.yaml：名字 / 许可证 / 版本",
)

# ═══════════════ 2 分组配置的读法 ═══════════════
print("\n[2 分组配置的读法]")
root = {"gather": {"enable": False, "base_sec": 3}, "rhythm": None}
sg = cf.Sub(root, "gather")
check(
    sg.get("enable") is False
    and sg.get("base_sec") == 3
    and sg.get("x", 9) == 9
    and "base_sec" in sg,
    "分组当扁平字典读",
)
check(
    cf.Sub(root, "rhythm").get("a", 1) == 1
    and cf.Sub(root, "nope").get("a", 2) == 2
    and cf.Sub(None, "x").get("a", 3) == 3,
    "分组缺了 / 是空的：当没填",
)
check(
    cf.truthy("false", True) is False
    and cf.truthy("true", False) is True
    and cf.truthy(None, True) is True
    and cf.truthy(None, False) is False,
    "truthy：字符串 false / true、没填用默认",
)

# ═══════════════ 3 存档：合并 + 迁移 ═══════════════
print("\n[3 存档迁移]")
tmp = Path(tempfile.mkdtemp(prefix="alive-ut-"))
pd = tmp / "plugin_data"
old = {
    "gather": {"stats": {"day": "2026-10-28", "merges": 5}, "recent": [{"n": 3}]},
    "rhythm": {
        "stats": {"day": "2026-10-28", "follow": 7},
        "decisions": [],
        "lines": [],
    },
    "budget": {"spent": [[T, 0.5]], "full": None},
}
for n, v in old.items():
    p = pd / f"astrbot_plugin_fatfish_{n}"
    p.mkdir(parents=True)
    (p / "state.json").write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
warns = []
st = stt.StateStore(pd / "astrbot_plugin_fatfish_alive", warn=warns.append)
check(
    sorted(st.migrated) == ["budget", "gather", "rhythm"],
    "第一次装：三个旧存档都接上了",
    st.migrated,
)
check(
    st.get("gather")["stats"]["merges"] == 5
    and st.get("rhythm")["stats"]["follow"] == 7
    and st.get("budget")["spent"] == [[T, 0.5]],
    "内容原样搬过来",
)
check(
    (pd / "astrbot_plugin_fatfish_gather" / "state.json").exists(),
    "旧目录原样留着（只读，不删）",
)
saved = json.loads(
    (pd / "astrbot_plugin_fatfish_alive" / "state.json").read_text(encoding="utf-8")
)
check(
    saved["meta"]["migrated"]["from"] == ["gather", "rhythm", "budget"]
    and saved["schema"] == 1,
    "迁完马上写了盘、打了「迁过了」的标记",
    saved.get("meta"),
)
st2 = stt.StateStore(pd / "astrbot_plugin_fatfish_alive")
check(
    st2.migrated == [] and st2.get("gather")["stats"]["merges"] == 5,
    "第二次起：不再迁，用新存档",
)
(pd / "astrbot_plugin_fatfish_gather" / "state.json").write_text(
    json.dumps({"stats": {"merges": 99}}), encoding="utf-8"
)
st3 = stt.StateStore(pd / "astrbot_plugin_fatfish_alive")
check(st3.get("gather")["stats"]["merges"] == 5, "旧存档后来变了也不会覆盖新的")

pd2 = tmp / "pd2"
(pd2 / "astrbot_plugin_fatfish_gather").mkdir(parents=True)
(pd2 / "astrbot_plugin_fatfish_gather" / "state.json").write_text(
    "{坏的", encoding="utf-8"
)
(pd2 / "astrbot_plugin_fatfish_rhythm").mkdir(parents=True)
(pd2 / "astrbot_plugin_fatfish_rhythm" / "state.json").write_text(
    json.dumps(old["rhythm"]), encoding="utf-8"
)
s4 = stt.StateStore(pd2 / "astrbot_plugin_fatfish_alive")
mg = s4.data["meta"]["migrated"]
check(
    mg["from"] == ["rhythm"] and mg["failed"] == ["gather"] and s4.get("gather") == {},
    "旧存档坏了：这一块从零开始，其余照迁，写明哪块失败",
    mg,
)
s5 = stt.StateStore(tmp / "empty" / "astrbot_plugin_fatfish_alive")
check(
    s5.migrated == [] and s5.get("gather") == {},
    "旁边什么旧存档都没有：什么也不迁、不报错",
)
bad = tmp / "bad" / "astrbot_plugin_fatfish_alive"
bad.mkdir(parents=True)
(bad / "state.json").write_text("not json", encoding="utf-8")
check(stt.StateStore(bad).get("rhythm") == {}, "新存档本身坏了：当没有，不崩")

# 存档节流
clk = [100.0]
s6 = stt.StateStore(tmp / "thr" / "x", clock=lambda: clk[0])
s6.register("gather", lambda: {"n": clk[0]})
check(
    s6.save() is True and s6.save() is False and s6.dirty,
    "存档最多 3 秒一次（后面的等后台补）",
)
clk[0] += 3.5
check(s6.save() is True, "过了 3 秒又能存")
check(s6.save(force=True) is True, "force 马上存")
check(
    json.loads((tmp / "thr" / "x" / "state.json").read_text(encoding="utf-8"))[
        "gather"
    ]["n"]
    == clk[0],
    "存下去的是登记过的那块的最新内容",
)

# ═══════════════ 4 配置迁移 ═══════════════
print("\n[4 配置迁移]")
cdir = tmp / "config"
cdir.mkdir()
(cdir / "astrbot_plugin_fatfish_gather_config.json").write_text(
    json.dumps({"base_sec": 3.5, "private": False, "max_sec": 15.0, "未知项": 1}),
    encoding="utf-8",
)
(cdir / "astrbot_plugin_fatfish_rhythm_config.json").write_text(
    json.dumps({"window_sec": 200, "bot_names": ["小鱼"]}), encoding="utf-8"
)
(cdir / "astrbot_plugin_fatfish_budget_config.json").write_text(
    json.dumps({"half_usd": 0.8}), encoding="utf-8"
)
defaults = {
    g: {k: i.get("default") for k, i in v["items"].items()} for g, v in SCHEMA.items()
}
cfg = json.loads(json.dumps(defaults))
moved = stt.migrate_config(cfg, cdir, SCHEMA)
check(
    cfg["gather"]["base_sec"] == 3.5
    and cfg["gather"]["private"] is False
    and cfg["rhythm"]["window_sec"] == 200
    and cfg["rhythm"]["bot_names"] == ["小鱼"]
    and cfg["budget"]["half_usd"] == 0.8,
    "旧配置里改过的项搬进新分组",
    moved,
)
check(
    "未知项" not in cfg["gather"] and "max_sec" not in moved.get("gather", {}),
    "没改过的（等于默认）和不认识的不搬",
    moved,
)
cfg2 = json.loads(json.dumps(defaults))
cfg2["gather"]["base_sec"] = 9.0
stt.migrate_config(cfg2, cdir, SCHEMA)
check(
    cfg2["gather"]["base_sec"] == 9.0 and cfg2["gather"]["private"] is False,
    "新插件里已经改过的不动，其余照搬",
)
cfg3 = json.loads(json.dumps(defaults))
check(
    stt.migrate_config(cfg3, tmp / "nope", SCHEMA) == {} and cfg3 == defaults,
    "旁边没有旧配置：什么也不动",
)

# ═══════════════ 5 让路检测 ═══════════════
print("\n[5 让路检测]")
mine = "astrbot_plugin_fatfish_alive"
stars = [
    star(mine),
    star("astrbot_plugin_fatfish_aiguard"),
    star("astrbot_plugin_group_chat_plus"),
    star("astrbot_plugin_other"),
]
f = compat.detect(stars, own=mine)
check(
    [x[0] for x in f] == ["group_chat_plus"]
    and f[0][1] == "astrbot_plugin_group_chat_plus",
    "装了 group_chat_plus：认出来；自己和别的 fatfish 插件不算",
    f,
)
for nm, lab in (
    ("astrbot_plugin_SpectreCore", "SpectreCore"),
    ("astrbot_plugin_heartflow", "Heartflow"),
    ("natural-followup", "natural_followup"),
    ("astrbot_plugin_jiehua", "jiehua"),
    ("astrbot_plugin_GroupChatPlus", "group_chat_plus"),
):
    check(
        [x[0] for x in compat.detect([star(nm)], own=mine)] == [lab],
        f"认得出 {lab}（目录名 {nm}，忽略大小写和下划线横线）",
    )
check(
    compat.detect([star("astrbot_plugin_x", display="心流 Heartflow 插件")])
    and compat.detect([star("astrbot_plugin_x", display="心流 Heartflow 插件")])[0][0]
    == "Heartflow",
    "显示名里有关键字也认",
)
check(
    compat.detect([star("astrbot_plugin_jiehua", activated=False)], own=mine) == [],
    "装了但没启用：不算",
)
check(
    compat.detect([star("astrbot_plugin_chat"), star("astrbot_plugin_heart")], own=mine)
    == [],
    "名字有点像但不是：不误判",
)
check(
    compat.detect([], own=mine) == [] and compat.detect(None) == [],
    "没有插件 / None：空",
)
fx = compat.detect([star("astrbot_plugin_smoothchat")], extra=["smoothchat"], own=mine)
check(fx and fx[0][0] == "smoothchat", "自己填的「另外要让路的插件」也认")
check(
    compat.detect([star("astrbot_plugin_ab")], extra=["ab", ""], own=mine) == [],
    "填得太短（不到 3 个字）的不要，免得误判一大片",
)
two = compat.detect(
    [star("astrbot_plugin_jiehua"), star("astrbot_plugin_heartflow")], own=mine
)
check([x[0] for x in two] == ["jiehua", "Heartflow"], "装了两个：都报出来")
check(
    compat.detect(
        [
            star("astrbot_plugin_jiehua", root="astrbot_plugin_jiehua"),
            star("astrbot_plugin_jiehua"),
        ],
        own=mine,
    ).__len__()
    == 1,
    "同一个插件只报一次",
)

# ═══════════════ 6 兜底的判断 ═══════════════
print("\n[6 兜底：报错 / <think> / 客服腔]")
for t in (
    "Error occurred during AI execution. Reason: x",
    "Error occurred while processing agent request: boom",
    "LLM 响应错误: 429",
    "AstrBot 请求失败。\n错误类型: X",
    "Traceback (most recent call last):\n  File",
    "Error code: 401 - {...}",
    "APIConnectionError: Connection error.",
):
    check(guard.is_error_text(t), f"认出框架 / 接口报错：{t[:30]}")
for t in (
    "今天我看到一个 Error occurred during AI execution 的提示好烦",
    "我的代码报 Traceback 了怎么办",
    "好的，没问题",
    "",
    None,
    "LLM 响应错误是啥意思",
):
    check(not guard.is_error_text(t), f"正常话不误拦：{str(t)[:24]}")
check(
    guard.is_empty_error(
        "LLM 响应错误: All chat models failed: EmptyModelOutputError: OpenAI completion has no usable output. response_id=x"
    )
    and not guard.is_empty_error("LLM 响应错误: 429 Too Many Requests")
    and not guard.is_empty_error("模型说 no usable output 就是没输出"),
    "认出「模型空回被框架当成报错」那一种（补救用）；别的报错不算",
)
check(
    guard.strip_think("<think>想想</think>你好") == "你好"
    and guard.strip_think("<thinking>x</thinking>\n\n行") == "行",
    "去掉 <think> / <thinking>",
)
check(
    guard.strip_think("你好<think>没闭合的想法") == "你好",
    "没闭合的 <think> 从那里到结尾都去掉",
)
check(guard.strip_think("没有思考的话") == "没有思考的话", "没有 <think> 的原样")
check(
    guard.think_only("<think>只想了没说</think>")
    and guard.think_only("<think>还在想")
    and not guard.think_only("<think>x</think>说了")
    and not guard.think_only("普通话"),
    "只有 <think> 没正文：判出来；有正文 / 没 <think> 的不算",
)
t, gone = guard.strip_ai_tone("好的，这个问题是这样。希望这些对你有帮助！")
check(t == "好的，这个问题是这样" and gone, "去掉结尾的「希望对你有帮助」", (t, gone))
t, gone = guard.strip_ai_tone(
    "作为一个AI，我觉得火锅好吃。如果你还有其他问题，请随时告诉我。"
)
check(
    "作为" not in t and "随时告诉我" not in t and "火锅好吃" in t,
    "去掉开头的「作为一个AI」和结尾的客服话",
    (t, gone),
)
t, gone = guard.strip_ai_tone("今天吃啥？有什么可以帮你的吗？")
check("可以帮你" not in t and "今天吃啥" in t, "去掉「有什么可以帮你的吗」", (t, gone))
check(guard.strip_ai_tone("普通的一句话。") == ("普通的一句话。", []), "正常话不动")
check(
    guard.strip_ai_tone("希望对你有帮助") == ("希望对你有帮助", []),
    "整句都是客服话：原样留着（不能把整句吃光）",
)
check(
    "别用" in guard.AI_TONE_RULE and guard.AI_TONE_RULE.startswith("（"),
    "提醒别用客服腔的那句话在",
)

# ═══════════════ 7 攒一攒：「稍等」多等一阵 ═══════════════
print("\n[7 攒一攒：「稍等 / 还没说完」多等一阵]")
for t in (
    "稍等",
    "稍等一下",
    "等一下",
    "等我发完",
    "我还没说完",
    "等我找找图",
    "先别回",
    "话还没说完呢哈",
):
    check(G.is_hold(t), f"算「稍等」：{t}")
for t in (
    "等我一下吗？",
    "你稍等一下吗",
    "我昨天等了他一个小时才来",
    "明天等你们来了再说这个事情吧真的是",
    "等一下他就来了",
    "",
    "好的",
    "没完没了",
):
    check(not G.is_hold(t), f"不算：{t}")
check(
    G.wait_for("稍等", False) == 45.0 and G.wait_for("我还没说完", False) == 45.0,
    "「稍等」：等 45 秒（hold_sec）",
)
check(
    G.wait_for("稍等", False, dict(G.DEFAULTS, hold_phrases=False)) == 3.0,
    "关掉这条：按「很短」算 3 秒",
)
check(
    G.wait_for("稍等", False, dict(G.DEFAULTS, hold_sec=1.0)) == 2.0,
    "hold_sec 比基础还短：至少基础",
)
check(
    G.wait_for("稍等", True) == 45.0 and G.wait_for("", True) == 4.0, "图没字的不受影响"
)

FAST = {
    "base_sec": 0.3,
    "pic_extra_sec": 0.3,
    "short_extra_sec": 0.15,
    "quick_sec": 0.15,
    "max_sec": 1.2,
    "lookback_sec": 1.0,
    "settle_sec": 0.5,
    "hold_sec": 1.6,
}


async def fill(g, key, line, state="new"):
    m = g.note(key)
    g.fill(m, line, state=state)
    return m


async def hold_flow():
    g = G.Gatherer(FAST)
    m1 = await fill(g, "p|1", "稍等")
    t0 = time.time()
    h = asyncio.ensure_future(g.hold(m1))
    await asyncio.sleep(0.8)
    check(not h.done(), "说了「稍等」：0.8 秒了还在等（普通的 0.3 秒就回了）")
    m2 = await fill(g, "p|1", "我把图找出来了你看")
    h2 = asyncio.ensure_future(g.hold(m2))
    r2 = await h2
    r1 = await h
    el = time.time() - t0
    check(
        r1[0] == "handoff"
        and r2[0] == "go"
        and [x.line for x in r2[1]] == ["稍等", "我把图找出来了你看"],
        "他接着说了：前一条让位，两条并成一次回",
        (r1, r2[0], [x.line for x in r2[1]]),
    )
    check(el < 1.6, f"接着说完就回，不用等满 hold_sec（{round(el, 2)} 秒）")
    # 一直没接着说：等到 hold_sec 就回
    m3 = await fill(g, "p|2", "稍等")
    t1 = time.time()
    r3 = await g.hold(m3)
    el = time.time() - t1
    check(
        r3[0] == "go" and 1.3 <= el <= 2.2,
        f"一直没接着说：等到 hold_sec（{round(el, 2)} 秒）还是回了",
        (r3[0], el),
    )
    # 一直在发：上限照常封顶
    g2 = G.Gatherer(dict(FAST, hold_sec=30.0, max_sec=1.0))
    m4 = await fill(g2, "p|3", "稍等")
    t2 = time.time()
    hh = asyncio.ensure_future(g2.hold(m4))
    for i in range(6):
        await asyncio.sleep(0.25)
        await fill(g2, "p|3", f"还在发第{i}句我接着说", state="quiet")
    r4 = await hh
    check(
        r4[0] == "go" and time.time() - t2 < 4.0,
        "稍等以后他一直在发：不会无限等",
        (r4[0], time.time() - t2),
    )
    # 关掉：不多等
    g3 = G.Gatherer(dict(FAST, hold_phrases=False))
    m5 = await fill(g3, "p|4", "稍等")
    t3 = time.time()
    await g3.hold(m5)
    check(time.time() - t3 < 0.9, "关掉「稍等」这条：照普通的等")


asyncio.run(hold_flow())

# ═══════════════ 8 群聊节奏：收尾句不接 / 话题词没对上扣分 ═══════════════
print("\n[8 群聊节奏：收尾句 / 话题词]")
for t in (
    "好了",
    "就这样吧",
    "我睡了",
    "晚安",
    "拜拜",
    "先这样",
    "不聊了",
    "走了",
    "88",
    "晚安啦！",
):
    check(rc.is_closing(t), f"算收尾句：{t}")
for t in ("好了吗", "你睡了吗", "晚安是什么意思啊你告诉我", "", "今天好冷", "走了好久"):
    check(not rc.is_closing(t), f"不算：{t}")


def mkfu(**kw):
    fu = rc.Followup(rc.Conf(dict(kw)))
    fu.note_turn("g", "收到，我觉得火锅最好吃", ["30001"], t0=T - 12, sent=T - 10)
    return fu


fu = mkfu()
v, why, s = fu.check("g", "30001", "老王", "好了", T, [])
check(
    v == "no" and "收尾句" in why,
    "它刚回的人说「好了」：不接（以前会因为「它刚回的就是他」分数够了接上）",
    (v, why, s),
)
v, why, s = fu.check("g", "30001", "老王", "晚安", T, [])
check(v == "no" and "收尾句" in why, "「晚安」：不接")
v, why, s = fu.check("g", "30001", "老王", "你说的火锅是啥意思", T, [])
check(v == "yes", "正常接着说的话：照接", (v, why))
fu_off = mkfu(closing_skip=False)
v, why, s = fu_off.check("g", "30001", "老王", "好了", T, [])
check(v != "no" or "收尾句" not in why, "关掉「收尾句不接」：不拦", (v, why))

# 话题词：别的人（不是它刚回的）、没叫名字、话题词没对上两个 → 扣分
fa = mkfu()
v1, why1, s1 = fa.check("g", "30002", "小李", "这个怎么弄", T, [])
fb = mkfu(topic_overlap=False)
v2, why2, s2 = fb.check("g", "30002", "小李", "这个怎么弄", T, [])
check(
    abs((s2 - s1) - 1.0) < 1e-9 and "话题词没对上两个" in why1 and "话题词" not in why2,
    "不是它刚回的人、话题词没对上：扣 1 分，原因里写明",
    (s1, s2, why1),
)
fc = mkfu(topic_penalty=2.5)
check(
    abs(fc.check("g", "30002", "小李", "这个怎么弄", T, [])[2] - (s2 - 2.5)) < 1e-9,
    "扣几分可以调（topic_penalty）",
)
check(
    mkfu().check("g", "30001", "老王", "这个怎么弄", T, [])[2]
    == mkfu(topic_overlap=False).check("g", "30001", "老王", "这个怎么弄", T, [])[2],
    "它刚回的就是他：不扣（他本来就是在接着聊）",
)
v3, why3, s3 = mkfu(bot_names=["胖头鱼"]).check(
    "g", "30002", "小李", "胖头鱼这个怎么弄", T, [], names=["胖头鱼"]
)
check("话题词没对上两个" not in why3, "叫了它的名字：不扣")
v4, why4, s4 = mkfu().check("g", "30002", "小李", "火锅最好吃的蘸料是什么", T, [])
check(
    "话题词没对上两个" not in why4,
    "话题词对上两个以上（火锅 / 最好 / 好吃）：不扣",
    why4,
)

# ═══════════════ 9 项目结构 ═══════════════
print("\n[9 文件齐全]")
need = [
    "main.py",
    "metadata.yaml",
    "_conf_schema.json",
    "LICENSE",
    "core/gather.py",
    "core/rhythm.py",
    "core/budget.py",
    "core/state.py",
    "core/compat.py",
    "core/guard.py",
    "core/conf.py",
    "modules/gather_mod.py",
    "modules/rhythm_mod.py",
    "modules/budget_mod.py",
    "modules/guard_mod.py",
    "views/panel/index.html",
]
check(
    all((d / x).is_file() for x in need),
    "插件文件齐全",
    [x for x in need if not (d / x).is_file()],
)
html = (d / "views" / "panel" / "index.html").read_text(encoding="utf-8")
check(
    all(x in html for x in ("总览", "攒一攒", "群聊节奏", "省 token", "兜底", "让路")),
    "面板：总览 + 三个标签 + 兜底 + 让路情况",
)

ok = sum(1 for c, _ in RES if c)
bad = [m for c, m in RES if not c]
print("\n" + "=" * 60)
print(f"  结果：{ok} 项过了" + (f"，{len(bad)} 项没过" if bad else "，全过"))
for m in bad:
    print("    -", m)
print("=" * 60)
sys.exit(1 if bad else 0)
