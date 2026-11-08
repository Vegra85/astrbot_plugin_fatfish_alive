# -*- coding: utf-8 -*-
"""配置分组：_conf_schema.json 按模块分了组（alive / gather / rhythm / budget / guard / send / compat）。
老代码里写的是 self.config.get(key, default) / key in self.config，这里给每个分组包一层，老代码几乎不用改。

v0.5.0 档位：配置最上面的「档位」（alive.preset）选了安静 / 默认 / 话多，COVER 里那 12 项按档位走、用户填的不看；
选「自定义」全按细项。档位只在这一层生效（Sub.get / in / [] 都先看档位）—— 各模块照旧 self.config.get(...)，
一个 if 都不用加。为什么放这里：模块里到处 if 档位，漏一处就是「面板上写着安静、实际照旧插嘴」。"""

import math

# ───────── 档位（v0.5.0）─────────
PRESETS = ("quiet", "default", "chatty", "custom")
PRESET_NAME = {"quiet": "安静", "default": "默认", "chatty": "话多", "custom": "自定义"}
_ALIAS = {
    "quiet": "quiet",
    "安静": "quiet",
    "default": "default",
    "默认": "default",
    "chatty": "chatty",
    "话多": "chatty",
    "custom": "custom",
    "自定义": "custom",
}
# 档位管的 12 项：(分组, 键) → (安静, 默认, 话多)。「默认」一列 = _conf_schema.json 里的默认值（升级的人行为不变，单测会对）
# 为什么挑这 12 项：「话多不多」主要就是这几样 —— 没 @ 时多明显才接、接几轮、在线时插嘴的概率和上限、在线多久、
# 冷场找不找话、每小时主动说几句；省 token 的曲线决定花了钱以后收不收；攒一攒的等待决定是不是「秒回」。
# 每档的道理写在 COVER_WHY 里（README「档位」那张表照抄它）。
COVER = {
    ("rhythm", "yes_score"): (5.0, 4.0, 3.0),
    ("rhythm", "no_score"): (2.5, 1.5, 1.0),
    ("rhythm", "max_turns"): (3, 5, 8),
    ("rhythm", "reply_prob"): (0.1, 0.3, 0.5),
    ("rhythm", "max_replies"): (2, 6, 10),
    ("rhythm", "online_min"): (4.0, 8.0, 12.0),
    ("rhythm", "online_max"): (8.0, 15.0, 20.0),
    ("rhythm", "probe_prob"): (0.0, 0.3, 0.5),
    ("rhythm", "lines_per_hour"): (1, 4, 6),
    ("budget", "half_usd"): (0.25, 0.45, 0.9),
    ("budget", "floor"): (0.05, 0.1, 0.2),
    ("gather", "base_sec"): (3.0, 2.0, 1.5),
}
COVER_WHY = {
    ("rhythm", "yes_score"): "安静：要「它刚回的就是他 + 说「你」+ 在问 + 紧跟着」这种很明显的才直接接；话多：它刚回的人紧跟着说一句就接",
    ("rhythm", "no_score"): "两条线中间的交给模型看一眼：安静少问（也少花判断的钱），话多多问",
    ("rhythm", "max_turns"): "同一个人不 @ 连着聊几轮就停下等 @：安静 3 轮，话多 8 轮（防两个机器人对聊的上限一直都在）",
    ("rhythm", "reply_prob"): "最直接决定插嘴多不多：在线时没点名的话，十句挑一句 / 三句 / 五句",
    ("rhythm", "max_replies"): "一次在线里自己插嘴的上限",
    ("rhythm", "online_min"): "说上话以后跟着聊多久：在线越久，插嘴的机会越多",
    ("rhythm", "online_max"): "同上（在线时长在最少和最多之间随机）",
    ("rhythm", "probe_prob"): "安静的人冷场了不会主动找话；话多的一半时候会顺着话题再说一句",
    ("rhythm", "lines_per_hour"): "冷场试探 + 收尾话，每个群每小时的总上限",
    ("budget", "half_usd"): "安静也更省：花到 $0.25 意愿就掉一半；话多到 $0.9 才掉一半（只影响它自己想插嘴的那些，有人找它照回）",
    ("budget", "floor"): "花得再多也留着的意愿",
    ("gather", "base_sec"): "安静的人回得慢一点、更愿意等你说完；话多的回得快",
}
COVER_GROUPS = frozenset(g for g, _ in COVER)
_IDX = {"quiet": 0, "default": 1, "chatty": 2}


def preset_of(root):
    """总配置里的档位 → quiet / default / chatty / custom。
    没填 / 认不出来 → custom（按细项）：AstrBot 加载配置时会把缺的项补成默认值「默认」，所以真跑起来不会没填；
    没填多半是测试或者手写的配置，这时候不替人做主，跟以前一样按细项"""
    try:
        a = root.get("alive") if root is not None else None
    except Exception:  # noqa: BLE001  配置读不到：当没填
        a = None
    v = a.get("preset") if isinstance(a, dict) else None
    return _ALIAS.get(str(v or "").strip().lower(), _ALIAS.get(str(v or "").strip(), "custom"))


def preset_value(preset, group, key):
    """这一项在这个档位下的值；不归档位管 / 自定义 → None"""
    vals = COVER.get((group, key))
    if vals is None or preset not in _IDX:
        return None
    return vals[_IDX[preset]]


class Sub:
    """总配置里的一个分组，当成扁平的配置字典用（只读）。总配置是 AstrBotConfig（dict）或者普通 dict，缺了分组就当空的。
    v0.5.0：档位不是「自定义」时，COVER 里的项按档位给值（get / in / [] / items 都一样），用户填的不看"""

    def __init__(self, root, name):
        self._root = root if root is not None else {}
        self._name = name

    def _d(self):
        try:
            v = self._root.get(self._name)
        except Exception:
            v = None
        return v if isinstance(v, dict) else {}

    def _preset(self):
        if self._name not in COVER_GROUPS:
            return None
        p = preset_of(self._root)
        return p if p in _IDX else None

    def _over(self):
        """档位盖住的那几项：{键: 档位给的值}（自定义 → {}）"""
        p = self._preset()
        if p is None:
            return {}
        return {k: vals[_IDX[p]] for (g, k), vals in COVER.items() if g == self._name}

    def get(self, key, default=None):
        p = self._preset()
        if p is not None:
            v = preset_value(p, self._name, key)
            if v is not None:
                return v
        return self._d().get(key, default)

    def __contains__(self, key):
        return key in self._d() or key in self._over()

    def __getitem__(self, key):
        over = self._over()
        if key in over:
            return over[key]
        return self._d()[key]

    def _merged(self):
        d = dict(self._d())
        d.update(self._over())
        return d

    def __iter__(self):
        return iter(self._merged())

    def keys(self):
        return self._merged().keys()

    def items(self):
        return self._merged().items()

    def __len__(self):
        return len(self._merged())

    def __bool__(self):
        return True


def _same(a, b):
    try:
        x, y = float(a), float(b)
        return math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-9)
    except (TypeError, ValueError):
        return a == b


def preset_changed(root):
    """档位管的那几项里，用户自己填的跟「默认」档不一样的 → ["rhythm.reply_prob", ...]（升级时用）"""
    out = []
    for (g, k), vals in COVER.items():
        try:
            d = root.get(g)
        except Exception:  # noqa: BLE001
            d = None
        if isinstance(d, dict) and k in d and d[k] is not None and not _same(d[k], vals[1]):
            out.append(f"{g}.{k}")
    return out


def upgrade_preset(root):
    """升级到 v0.5.0 的那一次：档位是「默认」（AstrBot 刚补上的），可档位管的项里有你以前改过的 →
    档位改成「自定义」，行为跟以前一样。→ 改过的那几项（[] = 不用动）。只改传进来的 dict，存盘交给外面"""
    if preset_of(root) != "default":
        return []
    changed = preset_changed(root)
    if changed:
        a = root.get("alive")
        if not isinstance(a, dict):
            a = {}
            root["alive"] = a
        a["preset"] = "custom"
    return changed


def clean_desc(desc):
    """配置说明去掉「（档位管）」这种尾巴，拿来在面板 / 日志里当名字"""
    s = str(desc or "")
    for tail in ("（档位管）",):
        s = s.replace(tail, "")
    return s.strip()


def covered_rows(root, schema=None):
    """面板总览：现在档位盖住了哪几项 → [{ref, name, value 档位给的, mine 你填的}]；自定义 → []"""
    p = preset_of(root)
    if p not in _IDX:
        return []
    rows = []
    for (g, k), vals in COVER.items():
        try:
            d = root.get(g)
        except Exception:  # noqa: BLE001
            d = None
        mine = d.get(k) if isinstance(d, dict) else None
        name = k
        try:
            name = clean_desc(schema[g]["items"][k]["description"]) or k
        except Exception:  # noqa: BLE001  拿不到说明就用键名
            pass
        rows.append({"ref": f"{g}.{k}", "name": name, "value": vals[_IDX[p]], "mine": mine})
    return rows


def truthy(v, default=True):
    """配置里的开关：True/False，也认「开 / on / 1 / true」这些字符串；没填用默认"""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "开")
