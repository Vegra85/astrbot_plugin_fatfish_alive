# -*- coding: utf-8 -*-
"""启动自检（v0.5.0）—— AstrBot 自己的设置里，哪些会跟活人感打架（不依赖 AstrBot，单测能直接用）

只读：只看、只说怎么改，不替用户改 AstrBot 的配置。
键名都是照 AstrBot 4.29 的源码对的（astrbot/core/config/default.py 的 DEFAULT_CONFIG，WebUI 上的中文名和位置照
CONFIG_METADATA_3），各项为什么打架也是读源码查的：
  - 主动回复：builtin_stars/astrbot/group_chat_context.py need_active_reply —— 没 @ 的群消息按概率直接 request_llm
  - 流式输出：QQ（aiocqhttp）注册时 support_streaming_message=False，默认「实时分段回复」边生成边发，不经过发送前钩子
  - 隔离对话：event_bus.py 里 aiocqhttp 的群消息会话 id 变成「QQ_群号」，一个人一个会话
  - 唤醒词：waking_check/stage.py 里 message_str.startswith(唤醒词)，空字符串谁都对得上
  - 额外唤醒前缀：astr_main_agent._matches_provider_wake_prefix，开头对不上就不叫模型（重复的唤醒词前缀会先去掉）
  - @全体成员：waking_check 里 AtAll 不忽略就算唤醒
  - 群聊上下文：group_chat_context 每次叫模型把上次以后攒的（最多 group_message_max_cnt 条）都塞进去
  - @ 发送人 / 引用回复 / 分段回复：result_decorate/stage.py，发送前钩子之后才加 @、加引用、切段
"""

LEVELS = ("冲突", "建议")  # 冲突 = 会打架（重复回、功能失效、漏出不该发的）；建议 = 不打架，但改了更好
ICL_MANY = 200  # 群聊上下文一次带这么多条以上算太多


def _get(cfg, path, default=None):
    cur = cfg
    for p in str(path).split("."):
        try:
            cur = cur.get(p) if hasattr(cur, "get") else None
        except Exception:  # noqa: BLE001  配置不是 dict / 读出错：当没有
            cur = None
        if cur is None:
            return default
    return cur


def _on(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "on", "开")


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _prov_prefix(cfg):
    """AstrBot 实际用的「额外唤醒前缀」：它开头跟唤醒词重复的那段会先去掉（照 agent_request.py）"""
    p = str(_get(cfg, "provider_settings.wake_prefix", "") or "")
    for b in _get(cfg, "wake_prefix", []) or []:
        b = str(b)
        if p.startswith(b):
            p = p[len(b):]
    return p


# 每一项：等级、标题、在 AstrBot WebUI 哪里改、读了哪些键（单测拿 KEYS 去对 AstrBot 的默认配置）
RULES = {
    "active_reply": {
        "level": "冲突",
        "title": "AstrBot 自带的「主动回复」开着",
        "where": "AstrBot 配置 → 扩展功能 → 群聊上下文感知 →「主动回复」",
        "keys": (
            "provider_ltm_settings.active_reply.enable",
            "provider_ltm_settings.active_reply.possibility_reply",
            "provider_ltm_settings.active_reply.whitelist",
        ),
    },
    "streaming": {
        "level": "冲突",
        "title": "「流式输出」开着",
        "where": "AstrBot 配置 → AI → 其他配置 →「流式输出」",
        "keys": ("provider_settings.streaming_response", "provider_settings.unsupported_streaming_strategy"),
    },
    "unique_session": {
        "level": "冲突",
        "title": "「隔离对话」开着",
        "where": "AstrBot 配置 → 平台与消息 → 基本 →「隔离对话」",
        "keys": ("platform_settings.unique_session",),
    },
    "wake_empty": {
        "level": "冲突",
        "title": "「唤醒词」里有一项是空的",
        "where": "AstrBot 配置 → 平台与消息 → 基本 →「唤醒词」",
        "keys": ("wake_prefix",),
    },
    "provider_wake": {
        "level": "冲突",
        "title": "「额外唤醒前缀」填了",
        "where": "AstrBot 配置 → AI → 其他配置 →「额外唤醒前缀」",
        "keys": ("provider_settings.wake_prefix", "wake_prefix"),
    },
    "at_all": {
        "level": "建议",
        "title": "@全体成员 也算在叫它",
        "where": "AstrBot 配置 → 平台与消息 → 其他配置 →「是否忽略 @ 全体成员事件」",
        "keys": ("platform_settings.ignore_at_all",),
    },
    "icl_many": {
        "level": "建议",
        "title": "群聊上下文一次带太多条",
        "where": "AstrBot 配置 → 扩展功能 → 群聊上下文感知 →「注入上下文最大消息数量」",
        "keys": ("provider_ltm_settings.group_icl_enable", "provider_ltm_settings.group_message_max_cnt"),
    },
    "icl_off": {
        "level": "建议",
        "title": "「群聊消息记录注入上下文」没开",
        "where": "AstrBot 配置 → 扩展功能 → 群聊上下文感知 →「群聊消息记录注入上下文」",
        "keys": ("provider_ltm_settings.group_icl_enable",),
    },
    "mention": {
        "level": "建议",
        "title": "「回复时 @ 发送人」开着",
        "where": "AstrBot 配置 → 平台与消息 → 基本 →「回复时 @ 发送人」",
        "keys": ("platform_settings.reply_with_mention",),
    },
    "quote_raw": {
        "level": "建议",
        "title": "AstrBot 的「引用回复」开着，活人感没在管引用",
        "where": "活人感配置 → 发消息 →「群里引用哪条」",
        "keys": ("platform_settings.reply_with_quote",),
    },
    "seg_off": {
        "level": "建议",
        "title": "打字节奏 / 打字刹车开着，AstrBot 的「分段回复」没开",
        "where": "AstrBot 配置 → 扩展功能 → 分段回复 →「启用分段回复」",
        "keys": ("platform_settings.segmented_reply.enable",),
    },
    "seg_slow": {
        "level": "建议",
        "title": "分段间隔太长，打字节奏被盖住",
        "where": "AstrBot 配置 → 扩展功能 → 分段回复 →「随机间隔时间」",
        "keys": (
            "platform_settings.segmented_reply.enable",
            "platform_settings.segmented_reply.interval_method",
            "platform_settings.segmented_reply.interval",
        ),
    },
}
KEYS = sorted({k for r in RULES.values() for k in r["keys"]})


def _hit(rid, now, why, fix):
    r = RULES[rid]
    return {
        "id": rid,
        "level": r["level"],
        "title": r["title"],
        "where": r["where"],
        "now": now,
        "why": why,
        "fix": fix,
    }


def check(cfg, mods=None):
    """一份 AstrBot 配置 → 查出来的 [{id, level, title, where, now, why, fix}]（冲突在前）。
    mods：活人感这边哪些开着 {"rhythm", "gather", "send", "guard": bool, "typing_human", "typing_brake": bool, "quote_mode": str}
    —— 活人感那块关着的，跟它打架的那项就不报（比如群聊节奏关了，AstrBot 的主动回复就不算抢）"""
    m = dict(mods or {})
    rhythm, gather, send, guard = (bool(m.get(k, True)) for k in ("rhythm", "gather", "send", "guard"))
    out = []

    ar = _get(cfg, "provider_ltm_settings.active_reply", {}) or {}
    p = _num(_get(ar, "possibility_reply", 0.1), 0.1)
    if rhythm and _on(_get(ar, "enable")) and p > 0:
        wl = _get(ar, "whitelist", []) or []
        out.append(_hit(
            "active_reply",
            f"开，回复概率 {p:g}" + ("，只在白名单里的群" if wl else "，所有群"),
            "它对每条没 @ 的群消息掷骰子、掷中就直接叫模型，跟活人感的群聊节奏是两套在抢着回："
            "群聊节奏判断不该接的它照样回，两边都接时一句话回两遍。",
            "关掉。想让它没 @ 也多接话，把活人感的档位调成「话多」。",
        ))

    strat = str(_get(cfg, "provider_settings.unsupported_streaming_strategy", "realtime_segmenting") or "")
    if (guard or rhythm or send) and _on(_get(cfg, "provider_settings.streaming_response")) and strat != "turn_off":
        out.append(_hit(
            "streaming",
            "开（QQ 不支持流式，按「实时分段回复」边生成边发）",
            "边生成边直接发出去，不经过「发送前」那一步：兜底拦不到报错和英文草稿，"
            "群聊节奏「想了想不说」的 [[不说]] 会原样发到群里，发消息模块（引用 / 打字节奏 / 刹车 / 叫停）也都管不到。",
            "关掉；或者把「不支持流式回复的平台」选成「关闭流式回复」。",
        ))

    if (rhythm or send) and _on(_get(cfg, "platform_settings.unique_session")):
        out.append(_hit(
            "unique_session",
            "开：群里每个人一个会话",
            "活人感按「一个群一个会话」算：开着时群里引用看不到别人插过话、打字刹车数不到群里又来了几句、"
            "冷场试探 / 收尾那句记进的对话记录谁也看不到；AstrBot 的群聊上下文也变成每人一份，它接话时不知道别人在聊什么。",
            "关掉（群里大家共用一份对话记录，像一个人在群里聊天）。",
        ))

    wp = _get(cfg, "wake_prefix", []) or []
    if isinstance(wp, str):
        wp = [wp]
    if any(str(x) == "" for x in wp):
        shown = "、".join(f"「{x}」" for x in wp)
        out.append(_hit(
            "wake_empty",
            f"唤醒词是 {shown}",
            "空的唤醒词等于每句话都在叫它：群里每条消息都会叫模型、都回。"
            "群聊节奏「没 @ 才看接不接」用不上，省 token 也把每句都当成有人找它、一定回。",
            "把空的那一项删掉（默认只有「/」）。想让它没 @ 也接话，交给群聊节奏。",
        ))

    raw = str(_get(cfg, "provider_settings.wake_prefix", "") or "")
    if (rhythm or gather) and _prov_prefix(cfg):
        out.append(_hit(
            "provider_wake",
            f"「{raw[:20]}」",
            "填了以后，消息开头得是这个前缀 AstrBot 才叫模型：群聊节奏决定接的那句（没 @、也没这个前缀）"
            "AstrBot 不会叫模型，接话 / 挑着接等于没开；攒一攒也只等带前缀的那几条。",
            "清空；只想让指令带前缀，「唤醒词」就够了。",
        ))

    if not _on(_get(cfg, "platform_settings.ignore_at_all")):
        out.append(_hit(
            "at_all",
            "「是否忽略 @ 全体成员事件」：关",
            "群里发公告 @全体成员，它也会跟着回一句；省 token 把它当成有人找它、一定回。",
            "打开。",
        ))

    icl = _on(_get(cfg, "provider_ltm_settings.group_icl_enable"))
    cnt = int(_num(_get(cfg, "provider_ltm_settings.group_message_max_cnt", 1000), 1000))
    if icl and cnt > ICL_MANY:
        out.append(_hit(
            "icl_many",
            f"「注入上下文最大消息数量」：{cnt}",
            f"每次叫模型最多带上 {cnt} 条群聊（它上次说话以后攒的）。群一热闹，一次就是几千上万个 token："
            "花钱快、省 token 的意愿掉得快，太长的上下文也容易把模型带偏。",
            "改成 50～100。",
        ))
    if rhythm and not icl:
        out.append(_hit(
            "icl_off",
            "关",
            "群聊节奏接话、挑着接的时候，模型只看得到这一句和它自己的对话记录，看不到群里前面在聊什么，容易接得驴唇不对马嘴。",
            "打开，「注入上下文最大消息数量」填 50～100。",
        ))

    if rhythm and _on(_get(cfg, "platform_settings.reply_with_mention")):
        out.append(_hit(
            "mention",
            "开",
            "群里每句回话都 @ 那个人，连群聊节奏自己挑着接的那几句也 @，像在点名；活人感的发消息模块只管引用，管不到这个 @。",
            "关掉；分清回谁交给引用（活人感 → 发消息 →「群里引用哪条」，默认跟着 AstrBot 的「回复时引用发送人消息」）。",
        ))

    qm = str(m.get("quote_mode") or "smart")
    if gather and _on(_get(cfg, "platform_settings.reply_with_quote")) and (not send or qm == "off"):
        out.append(_hit(
            "quote_raw",
            "AstrBot「回复时引用发送人消息」开着；活人感" + ("发消息模块关着" if not send else "「群里引用哪条」是「不管」"),
            "AstrBot 引用的是触发这一轮的那条：攒一攒并了几条时，常常是他随手补的最后一句；私聊、一来一回也挂引用框。",
            "选「跟着 AstrBot 的引用回复」（默认；发消息模块关着的话先打开「开启发消息这一块」）。",
        ))

    typing, brake = bool(m.get("typing_human")), bool(m.get("typing_brake"))
    seg = _get(cfg, "platform_settings.segmented_reply", {}) or {}
    seg_on = _on(_get(seg, "enable"))
    if send and (typing or brake) and not seg_on:
        out.append(_hit(
            "seg_off",
            "「启用分段回复」：关；活人感的" + "、".join(x for x, y in (("打字节奏", typing), ("打字刹车", brake)) if y) + "：开",
            "这两样是按 AstrBot 分好的段来的：没开分段回复，整段回话就是一条 —— 打字刹车没有可刹的，打字节奏按整段字数等一次。",
            "打开，「随机间隔时间」调小（比如 0.3,0.8）。",
        ))
    if send and typing and seg_on and str(_get(seg, "interval_method", "random") or "random") == "random":
        iv = str(_get(seg, "interval", "1.5,3.5") or "")
        try:
            lo = min(float(x) for x in iv.replace("，", ",").split(",") if x.strip())
        except ValueError:
            lo = 0.0
        if lo >= 1.0:
            out.append(_hit(
                "seg_slow",
                f"「随机间隔时间」：{iv}",
                "AstrBot 每段前面先睡这么久，打字节奏只补差的：间隔比打字时间还长时，节奏就变成了 AstrBot 的随机间隔。",
                "改小，比如 0.3,0.8。",
            ))

    out.sort(key=lambda f: LEVELS.index(f["level"]))
    return out


def check_all(profiles, mods=None):
    """[(配置文件名, AstrBot 配置)] → {"findings": [...（多份配置时带 profile）], "profiles": [名字], "rules": 查了几项,
    "conflicts": 几项冲突, "suggests": 几项建议}"""
    profiles = list(profiles or [])
    many = len(profiles) > 1
    found = []
    for name, cfg in profiles:
        try:
            got = check(cfg, mods)
        except Exception as e:  # noqa: BLE001  一份配置坏了不连累别的
            got = [{"id": "error", "level": "建议", "title": "这份配置读不懂", "where": "", "now": repr(e)[:80],
                    "why": "自检出错，跳过了这份配置。", "fix": "看一眼 AstrBot 的配置文件是不是手改坏了。"}]
        for f in got:
            f["profile"] = str(name) if many else ""
            found.append(f)
    found.sort(key=lambda f: LEVELS.index(f["level"]) if f["level"] in LEVELS else 9)
    return {
        "findings": found,
        "profiles": [str(n) for n, _ in profiles],
        "rules": len(RULES),
        "conflicts": sum(1 for f in found if f["level"] == "冲突"),
        "suggests": sum(1 for f in found if f["level"] == "建议"),
    }


def line(f):
    """日志里的一行：等级 + 哪份配置 + 标题（现在怎样）→ 为什么。改：哪里、怎么改"""
    head = f"{f['level']}" + (f"（配置文件「{f['profile']}」）" if f.get("profile") else "")
    return f"{head}：{f['title']}（现在：{f['now']}）—— {f['why']} 改：{f['where']}，{f['fix']}"


def summary(res):
    n = res.get("rules", len(RULES))
    c, s = res.get("conflicts", 0), res.get("suggests", 0)
    many = len(res.get("profiles") or []) > 1
    head = f"查了 AstrBot 的 {n} 项设置" + (f"（{len(res['profiles'])} 份配置文件）" if many else "")
    if not c and not s:
        return head + "：没有跟活人感打架的"
    return head + f"：{c} 项冲突、{s} 项建议（只读，不替你改；面板「自检」能看，发「/活人感 自检」重新查）"


def report(res):
    """/活人感 自检 回的那段话"""
    lines = ["鱼力max · 活人感 自检（只读，不改 AstrBot 的配置）", summary(res)]
    for i, f in enumerate(res.get("findings") or [], 1):
        lines.append(
            f"{i}. 【{f['level']}】{f['title']}" + (f"（配置文件「{f['profile']}」）" if f.get("profile") else "")
        )
        lines.append(f"   现在：{f['now']}")
        lines.append(f"   为什么：{f['why']}")
        lines.append(f"   改：{f['where']}，{f['fix']}")
    return "\n".join(lines)
