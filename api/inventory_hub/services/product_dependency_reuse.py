"""Bounded immutable results, owned by one stable dependency inspection only."""
from collections import OrderedDict
import json


class ResolutionMemo:
    """An optimization, never an eligibility limit or cross-request cache.

    Callers supply the complete resolver key within a single RR snapshot or
    apply's held input fences. Serialized values prevent a proof from mutating
    a later proof's answer. Every logical query still contributes its own paths
    and full result to the proof manifest.
    """

    def __init__(self, limit):
        self.limit = limit
        self.entries = OrderedDict()
        self.bytes = self.peak_bytes = 0
        self.hits = self.misses = self.evictions = self.skipped = 0
        self.allowance = limit

    def _evict(self):
        _, (_, size) = self.entries.popitem(last=False)
        self.bytes -= size
        self.evictions += 1

    def reserve(self, retained, maximum):
        self.allowance = min(self.limit, max(0, maximum - retained))
        while self.entries and self.bytes > self.allowance:
            self._evict()

    def get(self, key):
        found = self.entries.get(key)
        if found is None:
            self.misses += 1
            return None
        self.entries.move_to_end(key)
        self.hits += 1
        return json.loads(found[0])

    def put(self, key, value):
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        size = len(raw) + sum(len(part.encode('utf-8')) for part in key)
        if size > self.allowance:
            self.skipped += 1
            return
        previous = self.entries.pop(key, None)
        if previous is not None:
            self.bytes -= previous[1]
        while self.entries and self.bytes + size > self.allowance:
            self._evict()
        self.entries[key] = raw, size
        self.bytes += size
        self.peak_bytes = max(self.peak_bytes, self.bytes)

    def result(self):
        return {'limit_bytes': self.limit, 'retained_bytes': self.bytes,
            'peak_retained_bytes': self.peak_bytes, 'entries': len(self.entries),
            'hits': self.hits, 'misses': self.misses, 'evictions': self.evictions,
            'uncached_oversize_results': self.skipped}
