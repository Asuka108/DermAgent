# -*- coding: utf-8 -*-
"""记忆层单测假实现（不连真实 Redis；仅实现窗口用到的命令）。"""


class _FakePipe:
    def __init__(self, parent):
        self.parent = parent
        self.ops = []

    def rpush(self, key, value):
        self.ops.append(("rpush", key, value)); return self

    def ltrim(self, key, start, end):
        self.ops.append(("ltrim", key, start, end)); return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl)); return self

    def set(self, key, value, ex=None):
        self.ops.append(("set", key, value, ex)); return self

    def execute(self):
        ops, self.ops = self.ops, []
        for op in ops:
            getattr(self.parent, op[0])(*op[1:])
        return [True] * len(ops)


class FakeRedis:
    def __init__(self, fail=False):
        self.fail = fail
        self.lists = {}
        self.values = {}
        self.ttls = {}

    def ping(self):
        if self.fail:
            raise ConnectionError("down")
        return True

    def pipeline(self):
        return _FakePipe(self)

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def ltrim(self, key, start, end):
        items = self.lists.get(key, [])
        self.lists[key] = items[start:] if end == -1 else items[start:end + 1]

    def expire(self, key, ttl):
        self.ttls[key] = ttl

    def set(self, key, value, ex=None):
        self.values[key] = value
        if ex:
            self.ttls[key] = ex

    def get(self, key):
        return self.values.get(key)

    def lrange(self, key, start, end):
        items = self.lists.get(key, [])
        return items[start:] if end == -1 else items[start:end + 1]

    def delete(self, *keys):
        n = 0
        for key in keys:
            n += 1 if self.lists.pop(key, None) is not None else 0
            n += 1 if self.values.pop(key, None) is not None else 0
        return n
