"""Small durable-engine-friendly B+ tree implementation.

The tree is intentionally in-memory: the page log remains the durable source
of truth while table indexes provide bounded lookup acceleration.  Keeping the
index separate makes rebuilds after recovery deterministic.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class _Node:
    leaf: bool
    keys: list[Any] = field(default_factory=list)
    values: list[list[int]] = field(default_factory=list)
    children: list["_Node"] = field(default_factory=list)
    parent: "_Node | None" = None
    next: "_Node | None" = None


class BPlusTree:
    """An ordered B+ tree mapping keys to unique integer row positions."""

    def __init__(self, max_keys: int = 32) -> None:
        if max_keys < 3:
            raise ValueError("max_keys must be at least 3")
        self.max_keys = max_keys
        self.root = _Node(leaf=True)
        self._first_leaf = self.root
        self._size = 0

    def clear(self) -> None:
        self.root = _Node(leaf=True)
        self._first_leaf = self.root
        self._size = 0

    def __len__(self) -> int:
        return self._size

    def _leaf_for(self, key: Any) -> _Node:
        node = self.root
        while not node.leaf:
            node = node.children[bisect_right(node.keys, key)]
        return node

    def insert(self, key: Any, value: int) -> None:
        leaf = self._leaf_for(key)
        index = bisect_left(leaf.keys, key)
        if index < len(leaf.keys) and leaf.keys[index] == key:
            if value not in leaf.values[index]:
                leaf.values[index].append(value)
                leaf.values[index].sort()
                self._size += 1
            return
        leaf.keys.insert(index, key)
        leaf.values.insert(index, [value])
        self._size += 1
        if len(leaf.keys) > self.max_keys:
            self._split_leaf(leaf)

    def find(self, key: Any) -> list[int]:
        leaf = self._leaf_for(key)
        index = bisect_left(leaf.keys, key)
        if index >= len(leaf.keys) or leaf.keys[index] != key:
            return []
        return list(leaf.values[index])

    def items(self) -> Iterator[tuple[Any, list[int]]]:
        leaf = self._first_leaf
        while leaf is not None:
            yield from ((key, list(values)) for key, values in zip(leaf.keys, leaf.values))
            leaf = leaf.next

    def range(self, start: Any | None = None, end: Any | None = None) -> Iterator[tuple[Any, list[int]]]:
        leaf = self._first_leaf if start is None else self._leaf_for(start)
        while leaf is not None:
            for key, values in zip(leaf.keys, leaf.values):
                if start is not None and key < start:
                    continue
                if end is not None and key >= end:
                    return
                yield key, list(values)
            leaf = leaf.next

    def _split_leaf(self, leaf: _Node) -> None:
        midpoint = len(leaf.keys) // 2
        right = _Node(leaf=True, parent=leaf.parent)
        right.keys = leaf.keys[midpoint:]
        right.values = leaf.values[midpoint:]
        leaf.keys = leaf.keys[:midpoint]
        leaf.values = leaf.values[:midpoint]
        right.next = leaf.next
        leaf.next = right
        if leaf is self._first_leaf and right.keys[0] < leaf.keys[0]:
            self._first_leaf = right
        self._insert_in_parent(leaf, right.keys[0], right)

    def _split_internal(self, node: _Node) -> None:
        midpoint = len(node.keys) // 2
        separator = node.keys[midpoint]
        right = _Node(leaf=False, parent=node.parent)
        right.keys = node.keys[midpoint + 1:]
        right.children = node.children[midpoint + 1:]
        for child in right.children:
            child.parent = right
        node.keys = node.keys[:midpoint]
        node.children = node.children[:midpoint + 1]
        self._insert_in_parent(node, separator, right)

    def _insert_in_parent(self, left: _Node, separator: Any, right: _Node) -> None:
        parent = left.parent
        if parent is None:
            self.root = _Node(leaf=False, keys=[separator], children=[left, right])
            left.parent = self.root
            right.parent = self.root
            return
        index = parent.children.index(left)
        parent.keys.insert(index, separator)
        parent.children.insert(index + 1, right)
        right.parent = parent
        if len(parent.keys) > self.max_keys:
            self._split_internal(parent)
