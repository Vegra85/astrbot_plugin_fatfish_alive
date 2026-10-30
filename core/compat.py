# -*- coding: utf-8 -*-
"""兼容：装了同类插件（也在做「读空气 / 接话 / 合并消息」）就让路，不然两个都在回，会重复回。

检测的是「装了并启用」的插件：看插件名、目录名、显示名里有没有这几个关键字（忽略大小写、下划线和横线）。
本插件自己和别的 fatfish_ 插件不算。"""

import re

KNOWN = {
    "group_chat_plus": ("groupchatplus",),
    "SpectreCore": ("spectrecore",),
    "Heartflow": ("heartflow",),
    "natural_followup": ("naturalfollowup",),
    "jiehua": ("jiehua",),
}
OWN_PREFIX = "astrbot_plugin_fatfish_"


def norm(s):
    return re.sub(r"[^a-z0-9一-鿿]", "", str(s or "").lower())


def _names(star):
    return [getattr(star, a, None) for a in ("name", "root_dir_name", "display_name")]


def detect(stars, extra=(), own=""):
    """stars：get_all_stars() 的结果。→ [(标签, 插件名)]，每个插件最多报一次"""
    keys = dict(KNOWN)
    for x in extra or ():
        n = norm(x)
        if len(n) >= 3:
            keys[str(x).strip()] = (n,)
    found, seen = [], set()
    for st in stars or ():
        if not getattr(st, "activated", True):
            continue
        names = [str(x) for x in _names(st) if x]
        if not names:
            continue
        if any(x == own or x.startswith(OWN_PREFIX) for x in names):
            continue
        blob = " ".join(norm(x) for x in names)
        for label, needles in keys.items():
            if any(n in blob for n in needles):
                shown = names[0]
                if shown not in seen:
                    seen.add(shown)
                    found.append((label, shown))
                break
    return found
