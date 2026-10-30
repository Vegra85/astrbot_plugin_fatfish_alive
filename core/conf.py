# -*- coding: utf-8 -*-
"""配置分组：_conf_schema.json 按模块分了组（alive / gather / rhythm / budget / guard / compat）。
老代码里写的是 self.config.get(key, default) / key in self.config，这里给每个分组包一层，老代码几乎不用改。"""


class Sub:
    """总配置里的一个分组，当成扁平的配置字典用（只读）。总配置是 AstrBotConfig（dict）或者普通 dict，缺了分组就当空的"""

    def __init__(self, root, name):
        self._root = root if root is not None else {}
        self._name = name

    def _d(self):
        try:
            v = self._root.get(self._name)
        except Exception:
            v = None
        return v if isinstance(v, dict) else {}

    def get(self, key, default=None):
        return self._d().get(key, default)

    def __contains__(self, key):
        return key in self._d()

    def __getitem__(self, key):
        return self._d()[key]

    def __iter__(self):
        return iter(self._d())

    def keys(self):
        return self._d().keys()

    def items(self):
        return self._d().items()

    def __len__(self):
        return len(self._d())

    def __bool__(self):
        return True


def truthy(v, default=True):
    """配置里的开关：True/False，也认「开 / on / 1 / true」这些字符串；没填用默认"""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "开")
