#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""档位 + 「哪里不对 → 改哪项」对照表（v0.5.0）的单元测试（不用装 AstrBot，一两秒）：python tests/test_preset.py [插件目录 默认上一层]
  1 配置说明：档位在最上面、四个选项；档位管的 8～12 项都真有、「默认」档 = 原来的默认值（升级的人行为不变）、说明里写清楚「档位管」
  2 档位怎么盖：安静 / 默认 / 话多盖住那几项、别的照旧；自定义 = 全按细项；读配置的那一层（各模块照旧 config.get）就生效
  3 同样的群聊：安静接得比话多少（接话的轮数、挑着接的概率、省 token 的意愿、攒一攒等多久）
  4 升级：以前改过档位管的项 → 档位自动设成「自定义」，行为不变
  5 面板：总览能看现在档位盖住了哪几项
  6 对照表：提到的配置项都真的存在、中文名对得上；README 那一节、面板标签页都有
旧代码（v0.4.3）跑这个：档位那几项不认（安静照样按你填的插嘴），对照表 / 面板没有 —— 会没过。"""

import json
import re
import sys
from pathlib import Path

d = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(d))
from core import budget as B, conf as cf, gather as G, rhythm as rc  # noqa: E402

RES = []


def check(c, m, why=""):
    RES.append((bool(c), m))
    print(("  [过]   " if c else "  [没过] ") + m + ("" if c or not why else f"\n           {str(why)[:600]}"))


SCHEMA = json.loads((d / "_conf_schema.json").read_text(encoding="utf-8"))
COVER = getattr(cf, "COVER", {})
PRESETS = getattr(cf, "PRESETS", ())


def defaults():
    return {g: {k: v.get("default") for k, v in gv["items"].items()} for g, gv in SCHEMA.items()}


def cfg(preset=None, **groups):
    c = defaults()
    if preset is None:
        c["alive"].pop("preset", None)
    else:
        c["alive"]["preset"] = preset
    for g, v in groups.items():
        c[g].update(v)
    return c


# ═══════════════ 1 配置说明 ═══════════════
print("[1 配置说明：档位在最上面；档位管的几项]")
ai = SCHEMA["alive"]["items"]
pi = ai.get("preset") or {}
check(
    list(SCHEMA)[0] == "alive" and list(ai)[0] == "preset",
    "「档位」是整份配置最上面一项（总开关那组的第一项）",
    (list(SCHEMA)[:1], list(ai)[:2]),
)
check(
    pi.get("default") == "default"
    and pi.get("options") == ["quiet", "default", "chatty", "custom"]
    and list(PRESETS) == pi.get("options")
    and len(pi.get("labels") or []) == 4
    and all(x in "".join(pi.get("labels") or []) for x in ("安静", "默认", "话多", "自定义")),
    "四个选项：安静 / 默认 / 话多 / 自定义（都有中文标签），默认「默认」",
    pi,
)
check(8 <= len(COVER) <= 12, f"档位管 8～12 项（现在 {len(COVER)} 项）", list(COVER))
miss = [f"{g}.{k}" for g, k in COVER if k not in (SCHEMA.get(g) or {}).get("items", {})]
check(not miss, "档位管的项在配置里都真有", miss)
diff = [
    (f"{g}.{k}", SCHEMA[g]["items"][k]["default"], v[1])
    for (g, k), v in COVER.items()
    if k in SCHEMA[g]["items"] and SCHEMA[g]["items"][k]["default"] != v[1]
]
check(not diff and COVER, "「默认」档每一项 = 原来的默认值（升级的人选「默认」行为不变）", diff)
tmap = {"int": int, "float": float}
bad_t = [
    f"{g}.{k}"
    for (g, k), v in COVER.items()
    if k in SCHEMA[g]["items"] and not all(isinstance(x, tmap.get(SCHEMA[g]["items"][k]["type"], object)) for x in v)
]
check(not bad_t, "每档的值跟配置项的类型一样（整数就是整数）", bad_t)


def fmt(v):
    return f"{v:g}" if isinstance(v, float) else str(v)


nolabel = [
    f"{g}.{k}"
    for (g, k), v in COVER.items()
    if k in SCHEMA[g]["items"]
    and not (
        SCHEMA[g]["items"][k]["description"].endswith("（档位管）")
        and SCHEMA[g]["items"][k]["hint"].startswith("【档位管】档位不是「自定义」时这项不看")
        and f"安静 {fmt(v[0])} / 默认 {fmt(v[1])} / 话多 {fmt(v[2])}" in SCHEMA[g]["items"][k]["hint"]
    )
]
check(not nolabel and COVER, "档位管的项：说明后面标「档位管」，提示里写清楚「档位不是自定义时这项不看」和每档是多少", nolabel)
others = [
    f"{g}.{k}" for g, gv in SCHEMA.items() for k, it in gv["items"].items()
    if (g, k) not in COVER and "档位管" in it.get("description", "")
]
check(not others, "不归档位管的项不标「档位管」", others)
ph = pi.get("hint") or ""
check(
    all(cf.clean_desc(SCHEMA[g]["items"][k]["description"]) in ph for g, k in COVER) and "自定义" in ph,
    "档位的提示里列了它管哪几项（用配置里的中文名）",
    [k for g, k in COVER if cf.clean_desc(SCHEMA[g]["items"][k]["description"]) not in ph],
)
# 每档的道理：话多的那一头 = 更爱说
UP = {"yes_score", "no_score", "base_sec"}  # 这几项越大越安静
wrong = []
for (g, k), (q, dft, c) in COVER.items():
    ok = (q > dft > c) if k in UP else (q < dft < c) or (k == "probe_prob" and q == 0 < dft < c)
    if not ok:
        wrong.append((k, q, dft, c))
check(not wrong and COVER, "每档的方向说得通：安静 → 默认 → 话多 一路更爱说（接话线越来越低、概率越来越高、等得越来越短…）", wrong)
check(
    set(getattr(cf, "COVER_WHY", {})) == set(COVER) and all(len(x) >= 8 for x in cf.COVER_WHY.values()),
    "每项都写了为什么这么定（README 那张表照抄）",
)

# ═══════════════ 2 档位怎么盖 ═══════════════
print("\n[2 档位怎么盖：读配置那一层]")
mine = {"reply_prob": 0.9, "max_turns": 20, "window_sec": 200, "yes_score": 1.0}
for p, want in (("quiet", 0.1), ("default", 0.3), ("chatty", 0.5)):
    s = cf.Sub(cfg(p, rhythm=mine), "rhythm")
    check(
        s.get("reply_prob") == want and s.get("window_sec") == 200,
        f"档位「{cf.PRESET_NAME.get(p, p)}」：挑着接的概率按档位走 {want}（你填的 0.9 不看）；不归档位管的照你填的（接话窗口 200 秒）",
        (s.get("reply_prob"), s.get("window_sec")),
    )
s = cf.Sub(cfg("custom", rhythm=mine), "rhythm")
check(
    s.get("reply_prob") == 0.9 and s.get("max_turns") == 20 and s.get("yes_score") == 1.0,
    "「自定义」：全按你填的细项",
    (s.get("reply_prob"), s.get("max_turns")),
)
r = {"alive": {"preset": "quiet"}, "rhythm": {"window_sec": 99}}
s = cf.Sub(r, "rhythm")
check(
    "reply_prob" in s and s["reply_prob"] == 0.1 and dict(s.items()).get("max_turns") == 3 and "window_sec" in s
    and "nope" not in s and s.get("nope", 7) == 7,
    "配置里压根没写的项也按档位给（in / [] / items 都一样）；没有的项照旧拿默认",
    dict(s.items()),
)
check(
    cf.Sub(cfg("quiet", guard={"rescue_timeout_sec": 9.0}), "guard").get("rescue_timeout_sec") == 9.0
    and cf.Sub(cfg("quiet"), "send").get("typing_cps") == 2.5,
    "别的分组（兜底、发消息…）不受档位影响",
)
check(
    cf.Sub(cfg("安静", rhythm=mine), "rhythm").get("reply_prob") == 0.1
    and cf.Sub(cfg("话多", rhythm=mine), "rhythm").get("reply_prob") == 0.5
    and cf.Sub(cfg("乱写", rhythm=mine), "rhythm").get("reply_prob") == 0.9
    and cf.Sub(cfg(None, rhythm=mine), "rhythm").get("reply_prob") == 0.9,
    "写中文的档位名也认；认不出 / 没写档位 → 按细项（不替人做主）",
)
# 各模块照旧 config.get(...) 就生效（没在模块里加 if）
qc = rc.Conf(cf.Sub(cfg("quiet", rhythm=mine), "rhythm"))
check(
    qc("yes_score") == 5.0 and qc("max_turns") == 3 and qc("probe_prob") == 0.0 and qc("online_max") == 8.0,
    "群聊节奏读配置（core/rhythm.Conf）拿到的是档位的值",
    (qc("yes_score"), qc("max_turns")),
)
gs = cf.Sub(cfg("quiet", gather={"base_sec": 9.0}), "gather")
g = G.Gatherer({k: gs.get(k, G.DEFAULTS[k]) for k in G.DEFAULTS})
check(
    g.cfg["base_sec"] == 3.0 and G.wait_for("那个东西你看下", False, g.cfg) == 3.0,
    "攒一攒（modules/gather_mod 照旧 config.get）：安静档等 3 秒（你填的 9 不看）",
    g.cfg["base_sec"],
)
bs = cf.Sub(cfg("quiet", budget={"half_usd": 5.0, "floor": 0.5}), "budget")
bq = B.Budget({k: bs.get(k) for k in B.DEFAULTS if k in bs})
check(
    bq.cfg["half_usd"] == 0.25 and bq.cfg["floor"] == 0.05,
    "省 token（modules/budget_mod 照旧「k in config」再 get）：安静档的半价点 0.25、底线 0.05",
    (bq.cfg["half_usd"], bq.cfg["floor"]),
)

# ═══════════════ 3 同样的群聊：安静接得比话多少 ═══════════════
print("\n[3 同样的群聊：安静接得比话多少]")
T = 1_000_000.0


def follow_run(preset):
    """它回了老王一句以后，老王不 @ 接着说了 6 句（每句都很明显是在接它的话）→ 接了几句"""
    fu = rc.Followup(rc.Conf(cf.Sub(cfg(preset), "rhythm")))
    fu.note_turn("g", "收到，我觉得火锅最好吃", ["30001"], t0=T - 12, sent=T - 10)
    took, t = 0, T
    for text in ("你刚才说的啥意思", "你再说说你的看法呢", "那你觉得明天会下雨吗", "你还在吗", "你怎么不说话了呀", "你说的火锅是哪家"):
        v, why, score = fu.check("g", "30001", "老王", text, t, [])
        if v == "yes":
            took += 1
            fu.took("g", "30001", t)
            fu.note_turn("g", "好的", ["30001"], t0=t + 1, sent=t + 2)
        t += 5
    return took


nq, nd, nc = follow_run("quiet"), follow_run("default"), follow_run("chatty")
check(nq < nd < nc or (nq < nc and nd <= nc), f"接话：同样 6 句，安静接 {nq} 句 < 默认 {nd} 句 < 话多 {nc} 句（不 @ 连着接几轮的上限 3 / 5 / 8）", (nq, nd, nc))
v_q = rc.Followup(rc.Conf(cf.Sub(cfg("quiet"), "rhythm")))
v_c = rc.Followup(rc.Conf(cf.Sub(cfg("chatty"), "rhythm")))
for f in (v_q, v_c):
    f.note_turn("g", "收到，我觉得火锅最好吃", ["30001"], t0=T - 12, sent=T - 10)
vq = v_q.check("g", "30001", "老王", "真的吗", T, [])
vc = v_c.check("g", "30001", "老王", "真的吗", T, [])
check(
    vq[0] != "yes" and vc[0] == "yes",
    f"不太明显的一句「真的吗」（{vq[2]:g} 分）：安静不直接接（线 5），话多接（线 3）",
    (vq, vc),
)


def chat_run(preset, rolls):
    rh = rc.Rhythm(rc.Conf(cf.Sub(cfg(preset, rhythm={"min_gap_sec": 0}), "rhythm")), rand=lambda: next(rolls))
    rh.spoke("g", True, T)
    return sum(1 for i in range(20) if rh.want_reply("g", now=T + 60 + i * 50) and rh.spoke("g", False, T + 61 + i * 50) is None)


rolls = [0.05, 0.2, 0.35, 0.45, 0.6, 0.8] * 4
cq, cc = chat_run("quiet", iter(rolls)), chat_run("chatty", iter(rolls))
check(cq < cc and cq <= 2, f"在线时挑着接：同样的骰子，安静接 {cq} 次（概率 0.1、一次在线最多 2 次）< 话多 {cc} 次", (cq, cc))
w_q = B.Budget({k: cf.Sub(cfg("quiet"), "budget").get(k) for k in B.DEFAULTS if k in cf.Sub(cfg("quiet"), "budget")}).curve(0.45)
w_c = B.Budget({k: cf.Sub(cfg("chatty"), "budget").get(k) for k in B.DEFAULTS if k in cf.Sub(cfg("chatty"), "budget")}).curve(0.45)
check(w_q < 0.55 < w_c, f"省 token：都花了 $0.45，安静的回复意愿 {w_q:.0%} < 话多的 {w_c:.0%}", (w_q, w_c))

# ═══════════════ 4 升级 ═══════════════
print("\n[4 升级到 v0.5.0：以前改过档位管的项 → 自动「自定义」，行为不变]")
up = getattr(cf, "upgrade_preset", None)
c1 = cfg("default", rhythm={"reply_prob": 0.5}, gather={"base_sec": 2.5})
ch = up(c1) if up else None
check(
    ch == ["rhythm.reply_prob", "gather.base_sec"] and c1["alive"]["preset"] == "custom"
    and cf.Sub(c1, "rhythm").get("reply_prob") == 0.5,
    "AstrBot 给新加的档位补的是「默认」、可你以前改过挑着接的概率和基础等几秒 → 档位改成「自定义」，还是按你填的",
    (ch, c1["alive"]),
)
c2 = cfg("default")
check(up and up(c2) == [] and c2["alive"]["preset"] == "default", "都是默认值（新装 / 没改过）：档位保持「默认」", c2["alive"])
c3 = cfg("quiet", rhythm={"reply_prob": 0.5})
check(up and up(c3) == [] and c3["alive"]["preset"] == "quiet", "已经自己选了档位的：不动", c3["alive"])
c4 = cfg("default", budget={"half_usd": "0.45", "floor": 0.1000000000001})
check(up and up(c4) == [] and c4["alive"]["preset"] == "default", "数是一样的（字符串的 0.45、浮点误差）：不算改过", c4)
c5 = {"rhythm": {"reply_prob": 0.9}}
check(up and up(c5) == [] and "alive" not in c5, "没有档位这一项的（老配置还没被 AstrBot 补全）：不动，本来就按细项", c5)
main_src = (d / "main.py").read_text(encoding="utf-8")
check(
    "self._migrate_config()\n        self._migrate_preset()" in main_src and "preset_checked" in main_src,
    "main.py：先搬旧插件的配置、再做档位的升级检查（搬进来的也算以前改过的），只做一次",
)

# ═══════════════ 5 面板 ═══════════════
print("\n[5 面板：总览能看档位盖住了哪几项]")
rows = cf.covered_rows(cfg("quiet", rhythm={"reply_prob": 0.9}), SCHEMA) if hasattr(cf, "covered_rows") else []
rp = [x for x in rows if x["ref"] == "rhythm.reply_prob"]
check(
    len(rows) == len(COVER) and rp and rp[0]["value"] == 0.1 and rp[0]["mine"] == 0.9
    and rp[0]["name"] == "在线一阵：没点名的消息挑着接的概率",
    "安静档：12 项都列出来（中文名、按档位走的值、你填的那个）",
    rp,
)
check(cf.covered_rows(cfg("custom"), SCHEMA) == [] if hasattr(cf, "covered_rows") else False, "自定义：一项都没被盖")
html = (d / "views" / "panel" / "index.html").read_text(encoding="utf-8")
check(
    all(x in html for x in ('id="o-preset"', 'id="o-preset-rows"', "档位盖住的几项")),
    "面板总览：有「档位」那一格和「档位盖住的几项」那张表",
)
check(
    "covered_rows(self.config, self._schema)" in main_src and '"preset": {' in main_src,
    "面板接口：snapshot 里带着档位和盖住的几项",
)

# ═══════════════ 6 哪里不对 → 改哪项 ═══════════════
print("\n[6 「哪里不对 → 改哪项」对照表]")
try:
    from core import doctor, selfcheck as SC  # noqa: E402
except ImportError as e:  # 旧代码没有
    doctor = SC = None
    check(False, "core/doctor.py（对照表）在", e)
if doctor is not None:
    F = doctor.FAQ
    check(10 <= len(F) <= 13, f"{len(F)} 条常见症状（10 条左右）")
    allsym = " ".join(e["symptom"] for e in F)
    need = ("太吵", "太安静", "太慢", "一条一条", "@ 了不回", "花钱", "机器人", "报错")
    check(all(w in allsym for w in need), "任务里点名的几种都有：太吵 / 太安静 / 回得太慢 / 一条一条回 / @ 了不回 / 花钱太快 / 跟别的机器人聊个没完 / 报错发到群里",
          [w for w in need if w not in allsym])
    check(all(e["symptom"] and e["look"] and e["fix"] for e in F), "每条都有：哪里不对、先看什么、改哪项")
    ours = [r for r in doctor.refs() if not r.startswith("astrbot:")]
    gone = [r for r in ours if r.partition(".")[2] not in (SCHEMA.get(r.partition(".")[0]) or {}).get("items", {})]
    check(ours and not gone, f"对照表里提到的 {len(set(ours))} 个活人感配置项都真的存在", gone)
    ast = [r for r in doctor.refs() if r.startswith("astrbot:")]
    gone2 = [r for r in ast if r.split(":", 1)[1] not in SC.RULES]
    check(ast and not gone2, "提到的 AstrBot 设置都是自检里查的那几项（位置写得出来）", gone2)
    rr = doctor.rows(SCHEMA, SC.RULES)
    names = {x["ref"]: x["name"] for e in rr for x in e["fix"]}
    check(
        names.get("rhythm.reply_prob") == "在线一阵：没点名的消息挑着接的概率"
        and names.get("gather.base_sec") == "基础等几秒"
        and names.get("astrbot:active_reply") == "「主动回复」"
        and all("档位管" not in n for n in names.values()),
        "写的是配置里的中文名（档位管的去掉「（档位管）」）；AstrBot 的写它在 WebUI 里的名字",
        names,
    )
    # README 那一节：照抄同一张表；提到的键都真的存在、同一行里写着中文名
    rd = (d / "README.md").read_text(encoding="utf-8")
    m = re.search(r"\n## (?:[^\w\s]+ )?哪里不对 → 改哪项.*?(?=\n## )", rd, re.S)  # 标题前可以有 emoji（11-09 README 风格）
    sec = m.group(0) if m else ""
    check(bool(sec), "README 有「哪里不对 → 改哪项」一节")
    check(all(e["symptom"] in sec for e in F), "README 里每条症状都在", [e["symptom"] for e in F if e["symptom"] not in sec])
    keys = re.findall(r"`([a-z]+)\.([a-z_]+)`", sec)
    nokey = [f"{g}.{k}" for g, k in keys if k not in (SCHEMA.get(g) or {}).get("items", {})]
    check(keys and not nokey, f"README 那一节提到的 {len(set(keys))} 个配置键都真的存在", nokey)
    noname = []
    for ln in sec.splitlines():
        for g, k in re.findall(r"`([a-z]+)\.([a-z_]+)`", ln):
            it = (SCHEMA.get(g) or {}).get("items", {}).get(k)
            if it and cf.clean_desc(it["description"]) not in ln:
                noname.append(f"{g}.{k}")
    check(keys and not noname, "README 里每个键旁边都写着它的中文名（跟配置里的说明一字不差）", noname)
    miss_rd = sorted({r for r in ours if f"`{r}`" not in sec})
    check(not miss_rd, "对照表里的每个配置项 README 里都写了", miss_rd)
    # 指向档位管的项：不先把档位改成「自定义」，照做也白改（档位不是自定义时那几项不看）
    pf = getattr(doctor, "PRESET_FIRST", "先把「档位」改成「自定义」")
    cov = {f"{g}.{k}" for g, k in COVER}
    no_pf = [(e["id"], r) for e in F for r, how in e["fix"] if r in cov and pf not in how]
    check(
        not no_pf and any(r in cov for r in ours),
        "对照表里指向档位管的项（挑着接的概率、接话分数线、基础等几秒、半价点、底线、连着接几轮…），「怎么改」里都写了「先把「档位」改成「自定义」」",
        no_pf,
    )
    no_pf_rd = []
    for ln in sec.splitlines():
        for item in ln.split("<br>"):
            for r in re.findall(r"`([a-z]+\.[a-z_]+)`", item):
                if r in cov and pf not in item:
                    no_pf_rd.append(r)
    check(not no_pf_rd, "README 那张表里也一样（同一份数据）", no_pf_rd)
    check(
        'data-tab="f"' in html and "哪里不对" in html and 'id="f-list"' in html and '"faq": self._faq' in main_src,
        "面板有「哪里不对」标签页（数据就是这张表）",
    )

ok = sum(1 for c, _ in RES if c)
bad = [m for c, m in RES if not c]
print("\n" + "=" * 60)
print(f"  结果：{ok} 项过了" + (f"，{len(bad)} 项没过" if bad else "，全过"))
for m in bad:
    print("    -", m)
print("=" * 60)
sys.exit(1 if bad else 0)
