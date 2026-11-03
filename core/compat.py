# -*- coding: utf-8 -*-
"""兼容：装了同类插件（也在做「读空气 / 接话 / 合并消息」）就让路，不然两个都在回，会重复回。

检测的是「装了并启用」的插件：看插件名、目录名、显示名里有没有这几个关键字（忽略大小写、下划线和横线）。
本插件自己和别的 fatfish_ 插件不算。

每个同类插件写明让哪几个模块让路（v0.3.0）：
  gather 攒一攒 / rhythm 群聊节奏 / budget 省 token 的拦截（照样记账，只是不再「掷骰子不回」）
  - 读空气 / 接话类（group_chat_plus、SpectreCore、Heartflow、natural_followup、jiehua）：攒一攒 + 群聊节奏
  - angel_heart（天使之心）：攒一攒 + 群聊节奏 + 省 token 的拦截 —— 它放行一条回复前先登记「处理中」、发出去才收口，
    要是我们在调模型之前把它放行的那条拦下，它那边要等 300 秒兜底才恢复
  - reply_merge（思维链式消息防抖）：只让攒一攒（它也在合并连发的几条）
  - qq_enhance（QQ 能力增强）：只在它的「消息防抖」开着时让攒一攒（它默认开着）"""

import json
import re
from pathlib import Path

ALL = ("gather", "rhythm")
KNOWN = {
    "group_chat_plus": (("groupchatplus",), ALL),
    "SpectreCore": (("spectrecore",), ALL),
    "Heartflow": (("heartflow",), ALL),
    "natural_followup": (("naturalfollowup",), ALL),
    "jiehua": (("jiehua",), ALL),
    "angel_heart": (("angelheart",), ("gather", "rhythm", "budget")),
    "reply_merge": (("replymerge",), ("gather",)),
    "qq_enhance": (("qqenhance",), ("gather",)),
}
OWN_PREFIX = "astrbot_plugin_fatfish_"
MOD_NAMES = {"gather": "攒一攒", "rhythm": "群聊节奏", "budget": "省 token 的拦截"}


def norm(s):
    return re.sub(r"[^a-z0-9一-鿿]", "", str(s or "").lower())


def _names(star):
    return [getattr(star, a, None) for a in ("name", "root_dir_name", "display_name")]


def _debounce_on(star, cfg_dir=None):
    """qq_enhance 的「消息防抖」开没开：先看插件实例上的配置，再看 data/config 里它的配置文件；都看不到当成开着（它的默认值）"""
    cfg = getattr(getattr(star, "star_cls", None), "config", None)
    try:
        d = cfg.get("debounce") if hasattr(cfg, "get") else None
        if isinstance(d, dict) and "enabled" in d:
            return bool(d.get("enabled"))
    except Exception:  # noqa: BLE001  看不到就当默认
        pass
    if cfg_dir:
        for n in {getattr(star, "root_dir_name", None), getattr(star, "name", None)} - {
            None,
            "",
        }:
            p = Path(cfg_dir) / f"{n}_config.json"
            try:
                d = (json.loads(p.read_text(encoding="utf-8-sig")) or {}).get(
                    "debounce"
                )
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(d, dict) and "enabled" in d:
                return bool(d.get("enabled"))
    return True


def scope_text(mods):
    return "、".join(MOD_NAMES.get(m, m) for m in mods)


def detect(stars, extra=(), own="", cfg_dir=None):
    """stars：get_all_stars() 的结果。→ [(标签, 插件名, 让路的模块)]，每个插件最多报一次。
    extra：配置里另外填的关键字（攒一攒和群聊节奏都让路）"""
    keys = dict(KNOWN)
    for x in extra or ():
        n = norm(x)
        if len(n) >= 3:
            keys[str(x).strip()] = ((n,), ALL)
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
        for label, (needles, mods) in keys.items():
            if any(n in blob for n in needles):
                if label == "qq_enhance" and not _debounce_on(st, cfg_dir):
                    break  # 它的消息防抖关着：不冲突
                shown = names[0]
                if shown not in seen:
                    seen.add(shown)
                    found.append((label, shown, tuple(mods)))
                break
    return found
