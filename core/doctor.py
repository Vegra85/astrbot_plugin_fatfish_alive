# -*- coding: utf-8 -*-
"""「哪里不对 → 改哪项」对照表（v0.5.0）—— 面板「哪里不对」标签页就是这张表；README 那一节照抄。
为什么放成数据：面板、README、单测看的是同一份，单测对一遍「提到的配置项真的存在、中文名对得上」，
改了配置项名字忘了改这里，单测就会没过。

fix 里每一项：(配置项, 怎么改)。配置项两种写法：
  「分组.键」= 活人感自己的配置（中文名从 _conf_schema.json 的说明里拿）；
  「astrbot:自检的项」= AstrBot 自己的设置（位置照自检那一项写的，见 core/selfcheck.py 的 RULES）。"""

from .conf import clean_desc

# 指向档位管的项时，「怎么改」里都得带这句：档位不是「自定义」时那几项不看，照做也白改（单测会对）
PRESET_FIRST = "先把「档位」改成「自定义」"

FAQ = [
    {
        "id": "noisy",
        "symptom": "群里太吵 / 插嘴太多",
        "look": "面板「群聊节奏」：今天接话、挑着接各几次，「最近的接话判断」里接的都是什么；面板「自检」：AstrBot 自带的「主动回复」开没开",
        "fix": [
            ("alive.preset", "改成「安静」"),
            ("rhythm.reply_prob", "还嫌多：先把「档位」改成「自定义」，再把这项调低（比如 0.1）"),
            ("rhythm.yes_score", "先把「档位」改成「自定义」，再调高（比如 5），没 @ 时要更明显才接"),
            ("rhythm.rhythm_enable", "不想要「说上话以后跟着聊一阵」就关掉"),
            ("astrbot:active_reply", "开着就关掉（它跟群聊节奏抢着回）"),
        ],
    },
    {
        "id": "silent",
        "symptom": "群里太安静 / 没 @ 就不说话",
        "look": "面板总览：群聊节奏是不是在让路（装了同类插件）、现在是不是在免打扰；面板「群聊节奏」：最近的接话判断是不是都「分不够」",
        "fix": [
            ("alive.preset", "改成「话多」"),
            ("rhythm.enable", "确认开着"),
            ("rhythm.groups", "填了群号的话，确认这个群在里面"),
            ("rhythm.yes_score", "先把「档位」改成「自定义」，再调低（比如 3）"),
            ("alive.quiet_hours", "看现在是不是在免打扰时段"),
        ],
    },
    {
        "id": "slow",
        "symptom": "回得太慢",
        "look": "面板「攒一攒」：最近几次合并「等了几秒」；日志里的 [攒一攒]（尾巴含糊问了模型、他还在输入）",
        "fix": [
            ("gather.base_sec", "先把「档位」改成「自定义」，再调小（或者直接换「话多」档：1.5 秒）"),
            ("gather.tail_vague", "嫌「尾巴含糊多看一眼」慢就关掉"),
            ("gather.typing_wait", "私聊里嫌等他打完字慢就关掉"),
            ("send.typing_human", "开着打字节奏的话，关掉或者调高下一项"),
            ("send.typing_cps", "调高"),
        ],
    },
    {
        "id": "one_by_one",
        "symptom": "连着发几条，它一条一条回",
        "look": "面板总览：攒一攒是不是在工作、是不是在让路（装了消息防抖类插件）。一句回话分成几段发出来不是这个问题，那是 AstrBot 的「分段回复」",
        "fix": [
            ("gather.enable", "确认开着"),
            ("gather.private", "私聊连发的：确认开着"),
            ("gather.group", "群里连发的：确认开着"),
            ("gather.skip_groups", "这个群不能在里面"),
            ("compat.force_gather", "在让路、又想用攒一攒就打开（或者关掉那个插件的防抖）"),
        ],
    },
    {
        "id": "no_reply",
        "symptom": "被 @ 了不回",
        "look": "日志里那条后面有没有 [省token]「这次不回」、[兜底]、免打扰；面板「省 token」→ 最近的决定；面板「自检」",
        "fix": [
            ("budget.never_skip_sought", "确认开着（有人找它就一定回）"),
            ("alive.quiet_at_too", "免打扰时也想回 @ 就关掉"),
            ("astrbot:provider_wake", "填了就清空（消息开头没这个前缀，AstrBot 不叫模型）"),
        ],
    },
    {
        "id": "cost",
        "symptom": "花钱太快",
        "look": "面板「省 token」：最近 24 小时花了多少、哪种场合回得多；面板「自检」：群聊上下文一次带多少条",
        "fix": [
            ("alive.preset", "改成「安静」（也更省）"),
            ("budget.half_usd", "先把「档位」改成「自定义」，再调低"),
            ("budget.floor", "先把「档位」改成「自定义」，再调低"),
            ("budget.price_miss", "换了模型就把三个价格改成它的价格"),
            ("astrbot:icl_many", "调到 50～100"),
        ],
    },
    {
        "id": "bot_loop",
        "symptom": "跟别的机器人聊个没完",
        "look": "面板「群聊节奏」→ 各个群：「连着接了几轮」",
        "fix": [
            ("rhythm.max_turns", "先把「档位」改成「自定义」，再调小（或者直接换「安静」档：3 轮）"),
            ("rhythm.skip_ai", "打开，再装「鱼力max · 认 AI」（它认出来的别家 AI 不接话）"),
        ],
    },
    {
        "id": "leak",
        "symptom": "报错、[[不说]]、英文草稿或者思考过程发到群里了",
        "look": "日志里的 [兜底]；面板「自检」：流式输出开没开",
        "fix": [
            ("astrbot:streaming", "开着就关掉（边生成边发，兜底和群聊节奏都拦不到）"),
            ("guard.enable", "确认开着"),
            ("guard.block_errors", "确认开着"),
            ("guard.strip_draft", "确认开着（去开头的英文草稿）"),
            ("guard.rescue_empty", "确认开着（模型只想没说时补一句正文）"),
        ],
    },
    {
        "id": "double",
        "symptom": "一句话回了两遍 / 跟别的插件抢着回",
        "look": "面板总览：「让路情况」；面板「自检」：AstrBot 自带的「主动回复」",
        "fix": [
            ("astrbot:active_reply", "关掉"),
            ("compat.auto_yield", "确认开着"),
            ("compat.extra_plugins", "名单里没有的同类插件，把它目录名的一段填进来"),
        ],
    },
    {
        "id": "night",
        "symptom": "半夜还在群里说话",
        "look": "面板总览：免打扰那一行",
        "fix": [
            ("alive.quiet_hours", "填上，比如 01:00-07:00"),
            ("alive.quiet_tz", "服务器不在国内时确认一下（默认北京时间）"),
        ],
    },
    {
        "id": "quote",
        "symptom": "引用 / @ 挂得不对（私聊也挂引用、自己插话也 @ 人）",
        "look": "面板「自检」：「回复时 @ 发送人」「引用回复」",
        "fix": [
            ("send.quote_mode", "选「跟着 AstrBot 的引用回复」（默认）或「插件自己定」"),
            ("astrbot:mention", "关掉"),
        ],
    },
    {
        "id": "cut",
        "symptom": "话说到一半停了 / 后面几段没发",
        "look": "日志里的 [刹车]、[叫停]：写着因为什么停的",
        "fix": [
            ("send.halt", "不想他一说「算了」就停，关掉"),
            ("send.typing_brake", "关掉打字刹车（默认就是关的）"),
        ],
    },
]


def refs():
    """表里提到的所有配置项（单测拿去对）"""
    return [r for e in FAQ for r, _ in e["fix"]]


def name_of(ref, schema=None, rules=None):
    """配置项 → (中文名, 在哪改)。活人感的：中文名 = 配置说明（去掉「（档位管）」）；AstrBot 的：自检那一项写的位置"""
    if ref.startswith("astrbot:"):
        r = (rules or {}).get(ref.split(":", 1)[1]) or {}
        where = str(r.get("where") or "")
        name = where.rsplit("→", 1)[-1].strip() if where else ref
        return name, where
    g, _, k = ref.partition(".")
    desc = ""
    try:
        desc = str(schema[g]["items"][k]["description"])
    except Exception:  # noqa: BLE001  拿不到就用键名
        desc = ""
    grp = ""
    try:
        grp = str(schema[g]["description"]).split("（", 1)[0]
    except Exception:  # noqa: BLE001
        grp = g
    return (clean_desc(desc) or ref), f"活人感配置 → {grp}"


def rows(schema=None, rules=None):
    """给面板：[{symptom, look, fix: [{ref, name, where, how}]}]"""
    out = []
    for e in FAQ:
        fx = []
        for ref, how in e["fix"]:
            name, where = name_of(ref, schema, rules)
            fx.append({"ref": ref, "name": name, "where": where, "how": how})
        out.append({"id": e["id"], "symptom": e["symptom"], "look": e["look"], "fix": fx})
    return out
