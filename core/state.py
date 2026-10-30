# -*- coding: utf-8 -*-
"""状态：原来三个插件各存一份 state.json，现在合成一份 plugin_data/astrbot_plugin_fatfish_alive/state.json：

    {"schema": 1, "gather": {...}, "rhythm": {...}, "budget": {...}, "guard": {...}, "meta": {"migrated": {...}}}

每一块的内容跟原来那个插件存的一样（所以旧存档整块搬过来就能用）。
首次加载时如果旁边有旧三个插件的 plugin_data，自动迁移一次（只读，不删旧目录），迁完写 meta.migrated 标记。

另外 Shared 是三个模块共用的几样东西：最近的群聊（含她自己说的）、她刚回了谁、每个群多吵。
"""

import json
import time
from collections import defaultdict, deque
from pathlib import Path

SCHEMA = 1
OLD_PLUGINS = {
    "gather": "astrbot_plugin_fatfish_gather",
    "rhythm": "astrbot_plugin_fatfish_rhythm",
    "budget": "astrbot_plugin_fatfish_budget",
}


def read_json(path):
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return d
    except Exception:
        return None


class StateStore:
    def __init__(self, data_dir, warn=None, clock=time.time):
        self.dir = Path(data_dir)
        self.file = self.dir / "state.json"
        self.warn = warn or (lambda msg: None)
        self.clock = clock
        self.dumpers = {}
        d = read_json(self.file) if self.file.exists() else None
        self.data = d if isinstance(d, dict) else {}
        self.data.setdefault("schema", SCHEMA)
        self._dirty = False
        self._saved_at = 0.0
        self.migrated = self._migrate()  # 这次迁移了哪几块（[] = 没迁 / 之前迁过了）

    # ───────── 迁移 ─────────
    def _migrate(self):
        meta = self.data.setdefault("meta", {})
        if not isinstance(meta, dict):
            meta = self.data["meta"] = {}
        if meta.get("migrated"):
            return []
        got, bad = [], []
        root = self.dir.parent
        for name, old in OLD_PLUGINS.items():
            if isinstance(self.data.get(name), dict) and self.data[name]:
                continue  # 新插件自己已经有这一块了：不覆盖
            p = root / old / "state.json"
            if not p.is_file():
                continue
            d = read_json(p)
            if isinstance(d, dict):
                self.data[name] = d
                got.append(name)
            else:
                bad.append(name)  # 旧存档坏了：这一块从零开始（旧文件原样留着）
        meta["migrated"] = {"at": int(self.clock()), "from": got, "failed": bad}
        self._dirty = True
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._write()
        except Exception as e:
            self.warn(f"迁移后写存档失败：{e!r}")
        return got

    # ───────── 读 / 存 ─────────
    def get(self, name):
        d = self.data.get(name)
        return d if isinstance(d, dict) else {}

    def register(self, name, dump):
        """存档时调 dump() 拿这一块的内容"""
        self.dumpers[name] = dump

    def _write(self):
        for n, fn in self.dumpers.items():
            self.data[n] = fn()
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.file)
        self._dirty = False

    def save(self, force=False):
        """AstrBot 被直接关掉时不会调 terminate()，所以有变化就尽快存：最多 3 秒存一次，剩下的由后台每 5 秒补上"""
        self._dirty = True
        now = self.clock()
        if not force and now - self._saved_at < 3:
            return False
        self._saved_at = now
        try:
            self._write()
            return True
        except Exception as e:
            self.warn(f"存档失败：{e!r}")
            return False

    @property
    def dirty(self):
        return self._dirty


class Shared:
    """三个模块共用的几样东西（都只在内存里）"""

    def __init__(self):
        self.recent = defaultdict(
            lambda: deque(maxlen=40)
        )  # 群 -> 最近的群聊（含机器人自己说的）
        self.said_to = {}  # 群 -> {"t": 她什么时候说的, "uids": 她在回谁}

    def note_said(self, gk, uids, t):
        self.said_to[gk] = {
            "t": float(t),
            "uids": sorted({str(u) for u in uids or () if str(u)}),
        }

    def noise(self, gk, now=None, window=300.0):
        """这个群最近 window 秒里每分钟有几条别人（不是她）的消息"""
        now = float(now or time.time())
        n = sum(
            1
            for h in self.recent.get(gk, ())
            if h.get("role") == "user" and now - float(h.get("ts") or 0) <= window
        )
        return round(n / (window / 60.0), 2)


def migrate_config(config, cfg_dir, schema, groups=None):
    """旧三个插件各自的配置（data/config/astrbot_plugin_fatfish_<x>_config.json）→ 搬进新插件对应的分组。
    只搬「新插件里还是默认值、旧配置里改过」的项（新插件里你已经改过的不动）。→ {分组: {项: 值}}（搬了什么）
    config：总配置（AstrBotConfig 或 dict，分组是里面的子 dict，直接就地改）；schema：_conf_schema.json 读出来的东西"""
    moved = {}
    for grp, old in (groups or OLD_PLUGINS).items():
        p = Path(cfg_dir) / f"{old}_config.json"
        if not p.is_file():
            continue
        d = read_json(p)
        sub = config.get(grp) if hasattr(config, "get") else None
        items = ((schema.get(grp) or {}).get("items")) or {}
        if not isinstance(d, dict) or not isinstance(sub, dict) or not items:
            continue
        for k, v in d.items():
            spec = items.get(k)
            if (
                not spec
                or v == spec.get("default")
                or sub.get(k, spec.get("default")) != spec.get("default")
            ):
                continue
            sub[k] = v
            moved.setdefault(grp, {})[k] = v
    return moved
