#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动自检（v0.5.0）的单元测试（不用装 AstrBot 也能跑；装了的话顺便对一遍键名）：python tests/test_selfcheck.py [插件目录 默认上一层]
  1 AstrBot 默认配置：没有冲突（只有两条建议：@全体成员、群聊上下文没开）；键名都是 AstrBot 4.29 默认配置里真有的
  2 认出几种冲突：主动回复、流式输出、隔离对话、空的唤醒词、额外唤醒前缀；建议：上下文太多 / 没开、@ 发送人、引用、分段回复
  3 活人感那块关着的不报；几份配置文件分开报；配置坏了不崩；只读（一个字不改）
  4 日志一条一行、说怎么改；/活人感 自检 的回话；main.py / 面板接上了
旧代码（v0.4.3）没有自检：core/selfcheck.py 都没有 —— 会没过。"""

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
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
    from core import selfcheck as SC  # noqa: E402
except ImportError as e:  # 旧代码（v0.4.3）：没有自检
    check(False, "core/selfcheck.py（启动自检）在", e)
    done()

# AstrBot 4.29.4 DEFAULT_CONFIG 里自检会看的那几项（照 astrbot/core/config/default.py 抄的；装了 AstrBot 的话下面再拿真的对一遍）
BASE = {
    "platform_settings": {
        "unique_session": False,
        "reply_prefix": "",
        "reply_with_mention": False,
        "reply_with_quote": False,
        "segmented_reply": {"enable": False, "only_llm_result": True, "interval_method": "random", "interval": "1.5,3.5"},
        "ignore_at_all": False,
    },
    "provider_settings": {"enable": True, "wake_prefix": "", "streaming_response": False,
                          "unsupported_streaming_strategy": "realtime_segmenting"},
    "provider_ltm_settings": {
        "group_icl_enable": False,
        "group_message_max_cnt": 1000,
        "active_reply": {"enable": False, "method": "possibility_reply", "possibility_reply": 0.1, "whitelist": []},
    },
    "wake_prefix": ["/"],
}
MODS = {"rhythm": True, "gather": True, "send": True, "guard": True, "typing_human": False, "typing_brake": False, "quote_mode": "smart"}


def conf(**over):
    c = copy.deepcopy(BASE)
    for path, v in over.items():
        cur = c
        ks = path.split("__")
        for k in ks[:-1]:
            cur = cur.setdefault(k, {})
        cur[ks[-1]] = v
    return c


def ids(cfg, **mods):
    return [f["id"] for f in SC.check(cfg, dict(MODS, **mods))]


def lv(cfg, rid, **mods):
    return next((f["level"] for f in SC.check(cfg, dict(MODS, **mods)) if f["id"] == rid), None)


# ═══════════════ 1 AstrBot 默认配置 ═══════════════
print("[1 AstrBot 默认配置]")
got = SC.check(conf(), MODS)
check(
    not [f for f in got if f["level"] == "冲突"] and sorted(f["id"] for f in got) == ["at_all", "icl_off"],
    "AstrBot 默认配置：没有冲突，只有两条建议（@全体成员也会叫醒它、群聊上下文没开）",
    [(f["id"], f["level"]) for f in got],
)


def astrbot_defaults():
    """装了 AstrBot 的话拿它真的默认配置（DEFAULT_CONFIG）→ dict / None。
    在临时目录里起一个子进程读：import astrbot.core 会在当前目录建 data/，不能弄脏插件目录"""
    if importlib.util.find_spec("astrbot") is None:
        return None
    code = "import json; from astrbot.core.config.default import DEFAULT_CONFIG as D; print(json.dumps(D, default=str))"
    try:
        with tempfile.TemporaryDirectory(prefix="alive-ut-") as tmp:
            r = subprocess.run([sys.executable, "-c", code], cwd=tmp, env=dict(os.environ, ASTRBOT_ROOT=tmp),
                               capture_output=True, text=True, timeout=180)
        line = [x for x in r.stdout.splitlines() if x.startswith("{")][-1]
        return json.loads(line)
    except Exception:  # noqa: BLE001
        return None


DEFAULT_CONFIG = astrbot_defaults()
if DEFAULT_CONFIG is None:
    print("  （没装 AstrBot：跳过「拿 AstrBot 真的默认配置对键名」那两项）")
if DEFAULT_CONFIG is not None:
    missing = [k for k in SC.KEYS if SC._get(DEFAULT_CONFIG, k, "∅") == "∅"]
    check(not missing, f"自检读的 {len(SC.KEYS)} 个 AstrBot 键在它的默认配置里都真有（不是猜的）", missing)
    real = SC.check(DEFAULT_CONFIG, MODS)
    check(
        sorted(f["id"] for f in real) == ["at_all", "icl_off"],
        "拿 AstrBot 真的默认配置查：跟上面一样（没有冲突）",
        [f["id"] for f in real],
    )

# ═══════════════ 2 认出几种冲突 ═══════════════
print("\n[2 认出几种冲突 / 建议]")
f = [x for x in SC.check(conf(provider_ltm_settings__active_reply__enable=True), MODS) if x["id"] == "active_reply"]
check(
    f and f[0]["level"] == "冲突" and "回复概率 0.1" in f[0]["now"] and "所有群" in f[0]["now"]
    and f[0]["where"].endswith("群聊上下文感知 →「主动回复」") and f[0]["fix"].startswith("关掉"),
    "AstrBot 自带的「主动回复」开着 → 冲突（跟群聊节奏抢着回），写明回复概率、在哪关",
    f,
)
check(
    "白名单" in SC.check(conf(provider_ltm_settings__active_reply__enable=True,
                              provider_ltm_settings__active_reply__whitelist=["napcat:GroupMessage:7001"]), MODS)[0]["now"],
    "主动回复只开在白名单里的群：写明",
)
check("active_reply" not in ids(conf(provider_ltm_settings__active_reply__enable=True,
                                       provider_ltm_settings__active_reply__possibility_reply=0)),
      "主动回复开着但概率 0（等于没开）：不报")
check(lv(conf(provider_settings__streaming_response=True), "streaming") == "冲突",
      "「流式输出」开着（QQ 不支持流式，默认边生成边发）→ 冲突：兜底、[[不说]]、发消息都管不到")
check("streaming" not in ids(conf(provider_settings__streaming_response=True,
                                    provider_settings__unsupported_streaming_strategy="turn_off")),
      "流式开着、但「不支持流式回复的平台」选了关闭流式：QQ 上不流式，不报")
check(lv(conf(platform_settings__unique_session=True), "unique_session") == "冲突",
      "「隔离对话」开着 → 冲突（活人感按一个群一个会话算）")
w = [x for x in SC.check(conf(wake_prefix=["/", ""]), MODS) if x["id"] == "wake_empty"]
check(w and w[0]["level"] == "冲突" and "「」" in w[0]["now"], "「唤醒词」里有一项空的 → 冲突（群里每句都会叫模型）", w)
pw = [x for x in SC.check(conf(provider_settings__wake_prefix="chat"), MODS) if x["id"] == "provider_wake"]
check(pw and pw[0]["level"] == "冲突" and "「chat」" in pw[0]["now"],
      "「额外唤醒前缀」填了 → 冲突（群聊节奏决定接的那句没前缀，AstrBot 不叫模型）", pw)
check("provider_wake" not in ids(conf(provider_settings__wake_prefix="/")),
      "额外唤醒前缀填的就是唤醒词「/」：AstrBot 自己会去掉重复的，等于没填 —— 不报（照 agent_request.py）")
check("provider_wake" in ids(conf(provider_settings__wake_prefix="/chat")),
      "填「/chat」：去掉重复的「/」还剩「chat」—— 报")
check("at_all" not in ids(conf(platform_settings__ignore_at_all=True)), "打开了「忽略 @ 全体成员」：那条建议就没了")
check(lv(conf(provider_ltm_settings__group_icl_enable=True), "icl_many") == "建议"
      and "icl_off" not in ids(conf(provider_ltm_settings__group_icl_enable=True)),
      "群聊上下文开着、一次最多带 1000 条（AstrBot 默认）→ 建议改成 50～100")
check("icl_many" not in ids(conf(provider_ltm_settings__group_icl_enable=True, provider_ltm_settings__group_message_max_cnt=80)),
      "带 80 条：不报")
check(lv(conf(platform_settings__reply_with_mention=True), "mention") == "建议",
      "「回复时 @ 发送人」开着 → 建议关（它自己插话也 @ 人）")
check(lv(conf(platform_settings__reply_with_quote=True), "quote_raw", quote_mode="off") == "建议"
      and "quote_raw" not in ids(conf(platform_settings__reply_with_quote=True))
      and "quote_raw" in ids(conf(platform_settings__reply_with_quote=True), send=False),
      "AstrBot 的「引用回复」开着、活人感的引用选了「不管」/ 发消息关了 → 建议（攒一攒并了几条时引用挂错）；默认 smart 不报")
check(lv(conf(), "seg_off", typing_brake=True) == "建议" and "seg_off" not in ids(conf(), typing_brake=True, send=False),
      "开了打字刹车、AstrBot 的「分段回复」没开 → 建议（没有可刹的）；发消息关了不报")
seg_on = dict(platform_settings__segmented_reply={"enable": True, "interval_method": "random", "interval": "1.5,3.5"})
check(lv(conf(**seg_on), "seg_slow", typing_human=True) == "建议" and "seg_off" not in ids(conf(**seg_on), typing_brake=True),
      "开了打字节奏、分段间隔还是默认 1.5,3.5 → 建议调小；开了分段回复就不报「没开分段」")
check("seg_slow" not in ids(conf(platform_settings__segmented_reply={"enable": True, "interval_method": "random", "interval": "0.3,0.8"}),
                            typing_human=True)
      and "seg_slow" not in ids(conf(platform_settings__segmented_reply={"enable": True, "interval_method": "log", "interval": "1.5,3.5"}),
                                typing_human=True),
      "间隔调小了 / 按字数算间隔（log）：不报")
many = conf(provider_ltm_settings__active_reply__enable=True, platform_settings__unique_session=True,
            provider_settings__streaming_response=True)
got = SC.check(many, MODS)
lvls = [x["level"] for x in got]
check(lvls == sorted(lvls, key=SC.LEVELS.index) and lvls.count("冲突") == 3, "冲突排在建议前面", [(x["id"], x["level"]) for x in got])

# ═══════════════ 3 关着的不报 / 几份配置 / 坏配置 / 只读 ═══════════════
print("\n[3 活人感那块关着的不报；几份配置文件；坏配置；只读]")
check("active_reply" not in ids(conf(provider_ltm_settings__active_reply__enable=True), rhythm=False)
      and "icl_off" not in ids(conf(), rhythm=False) and "mention" not in ids(conf(platform_settings__reply_with_mention=True), rhythm=False),
      "群聊节奏关着：主动回复不算跟它抢、群聊上下文 / @ 发送人也不提")
check("streaming" not in ids(conf(provider_settings__streaming_response=True), rhythm=False, guard=False, send=False)
      and "unique_session" not in ids(conf(platform_settings__unique_session=True), rhythm=False, send=False),
      "流式输出 / 隔离对话：跟它打架的那几块都关着就不报")
res = SC.check_all([("默认", conf()), ("测试群专用", conf(provider_ltm_settings__active_reply__enable=True))], MODS)
ar = [x for x in res["findings"] if x["id"] == "active_reply"]
check(
    res["profiles"] == ["默认", "测试群专用"] and ar and ar[0]["profile"] == "测试群专用"
    and res["conflicts"] == 1 and "2 份配置文件" in SC.summary(res) and "配置文件「测试群专用」" in SC.line(ar[0]),
    "AstrBot 有几份配置文件：每份都查，写明是哪一份",
    (res["profiles"], [(x["id"], x["profile"]) for x in res["findings"]]),
)
one = SC.check_all([("默认", conf())], MODS)
check(all(x["profile"] == "" for x in one["findings"]) and "配置文件" not in SC.line(one["findings"][0]), "只有一份：不啰嗦是哪份")
for bad in (None, {}, {"platform_settings": None, "wake_prefix": None}, {"provider_ltm_settings": {"active_reply": "坏的"}},
            {"provider_ltm_settings": {"group_icl_enable": "true", "group_message_max_cnt": "很多"}}, "不是字典"):
    try:
        SC.check(bad, MODS)
        SC.check_all([("x", bad)], MODS)
        ok = True
    except Exception as e:  # noqa: BLE001
        ok = e
    check(ok is True, f"配置是坏的（{str(bad)[:30]}）：不崩", ok)
c0 = conf(provider_ltm_settings__active_reply__enable=True, wake_prefix=["/", ""])
c1 = copy.deepcopy(c0)
SC.check_all([("默认", c1)], MODS)
check(c1 == c0, "只读：查完一个字都没改 AstrBot 的配置")

# ═══════════════ 4 日志 / 指令 / 接上了 ═══════════════
print("\n[4 写进日志的样子；/活人感 自检；main.py 和面板接上了]")
res = SC.check_all([("默认", conf(provider_ltm_settings__active_reply__enable=True))], MODS)
ln = SC.line(res["findings"][0])
check(
    "\n" not in ln and ln.startswith("冲突：") and "现在：" in ln and "改：AstrBot 配置 → 扩展功能 → 群聊上下文感知 →「主动回复」" in ln,
    "日志一条一行：冲突 / 建议 + 什么设置（现在怎样）+ 为什么 + 改：在哪、怎么改",
    ln,
)
check("1 项冲突" in SC.summary(res) and "只读" in SC.summary(res), "开头一行总的：查了几项、几项冲突几项建议、只读", SC.summary(res))
check(SC.summary({"rules": 12, "conflicts": 0, "suggests": 0, "profiles": ["默认"]}).endswith("没有跟活人感打架的"), "都没问题：一句话说没有")
rp = SC.report(res)
check(rp.startswith("鱼力max · 活人感 自检") and "【冲突】" in rp and "改：" in rp and "为什么：" in rp, "/活人感 自检 回的那段话：每项写现在、为什么、怎么改", rp[:300])
main_src = (d / "main.py").read_text(encoding="utf-8")
ini = main_src.split("async def initialize(self):", 1)[-1].split("\n    def ", 1)[0]
check("self.run_selfcheck(log=True)" in ini, "插件加载时（initialize，热重载也走这里）查一遍、写进日志")
check(
    'in ("自检", "检查", "selfcheck", "check")' in main_src and "SC.report(self.run_selfcheck(log=True))" in main_src
    and 'm == "alive" and str(body.get("op") or "") == "selfcheck"' in main_src and '"selfcheck": self.selfcheck_last' in main_src,
    "指令 /活人感 自检、面板「重新检查」按钮都重新查一遍（也写日志）；面板接口带着上一次的结果",
)
check("save_config" not in main_src.split("def run_selfcheck", 1)[-1].split("\n    def ", 1)[0]
      and "save_config" not in (d / "core" / "selfcheck.py").read_text(encoding="utf-8"),
      "自检那条路上没有存 AstrBot 配置的代码（不替用户改）")
html = (d / "views" / "panel" / "index.html").read_text(encoding="utf-8")
check('data-tab="c"' in html and 'id="c-run"' in html and 'id="c-list"' in html and 'op: "selfcheck"' in html,
      "面板有「自检」标签页和「重新检查」按钮")
done()
