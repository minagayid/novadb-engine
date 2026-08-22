from __future__ import annotations

import json
import random
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class LogEntry:
    term: int
    command: Any

    def to_dict(self) -> dict[str, Any]:
        return {"term": self.term, "command": self.command}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LogEntry":
        return cls(int(value["term"]), value["command"])


class NotLeaderError(RuntimeError):
    def __init__(self, leader_id: str | None):
        super().__init__(f"node is not leader; leader={leader_id!r}")
        self.leader_id = leader_id


class ConsensusSafetyError(RuntimeError):
    pass


class RaftStorage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.file = self.path / "raft-state.json"

    def load(self) -> dict[str, Any]:
        if not self.file.exists():
            return {"current_term": 0, "voted_for": None, "log": []}
        return json.loads(self.file.read_text())

    def save(self, current_term: int, voted_for: str | None, log: list[LogEntry]) -> None:
        payload = {"current_term": current_term, "voted_for": voted_for, "log": [entry.to_dict() for entry in log]}
        temporary = self.file.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        temporary.replace(self.file)


class RaftNode:
    """A deterministic Raft reference node for a small cluster.

    Transport is injected so the same node logic can be used with the in-memory
    test cluster or a future network transport. Commands are applied only after
    they reach the committed prefix; followers never acknowledge local append as
    a committed write.
    """

    def __init__(
        self,
        node_id: str,
        peer_ids: list[str],
        transport: "RaftCluster",
        apply_command: Callable[[Any], None] | None = None,
        storage: RaftStorage | None = None,
        election_timeout: int = 5,
        heartbeat_interval: int = 2,
        seed: int = 0,
    ):
        self.node_id = node_id
        self.peer_ids = list(peer_ids)
        self.transport = transport
        self.apply_command = apply_command or (lambda command: None)
        self.storage = storage
        persisted = storage.load() if storage else {"current_term": 0, "voted_for": None, "log": []}
        self.current_term = int(persisted.get("current_term", 0))
        self.voted_for = persisted.get("voted_for")
        self.log = [LogEntry.from_dict(entry) for entry in persisted.get("log", [])]
        self.state = "follower"
        self.leader_id: str | None = None
        self.commit_index = -1
        self.last_applied = -1
        self.next_index: dict[str, int] = {}
        self.match_index: dict[str, int] = {}
        self.votes_received: set[str] = set()
        self.clock = 0
        self.heartbeat_elapsed = 0
        self.election_timeout = election_timeout + random.Random(seed).randint(0, max(1, election_timeout // 2))
        self.heartbeat_interval = heartbeat_interval
        self._lock = threading.RLock()

    @property
    def majority(self) -> int:
        return (len(self.peer_ids) + 1) // 2 + 1

    @property
    def last_log_index(self) -> int:
        return len(self.log) - 1

    @property
    def last_log_term(self) -> int:
        return self.log[-1].term if self.log else 0

    def _persist(self) -> None:
        if self.storage:
            self.storage.save(self.current_term, self.voted_for, self.log)

    def _step_down(self, term: int, leader_id: str | None = None) -> None:
        if term > self.current_term:
            self.current_term = term
            self.voted_for = None
        self.state = "follower"
        self.leader_id = leader_id
        self.votes_received.clear()
        self.heartbeat_elapsed = 0
        self.clock = 0
        self._persist()

    def tick(self) -> None:
        with self._lock:
            self.clock += 1
            if self.state == "leader":
                self.heartbeat_elapsed += 1
                if self.heartbeat_elapsed >= self.heartbeat_interval:
                    self.heartbeat_elapsed = 0
                    self.broadcast_append_entries()
            elif self.clock >= self.election_timeout:
                self.start_election()

    def start_election(self) -> None:
        with self._lock:
            self.state = "candidate"
            self.current_term += 1
            self.voted_for = self.node_id
            self.votes_received = {self.node_id}
            self.leader_id = None
            self.clock = 0
            self._persist()
            request = {
                "type": "request_vote",
                "term": self.current_term,
                "candidate_id": self.node_id,
                "last_log_index": self.last_log_index,
                "last_log_term": self.last_log_term,
            }
        for peer_id in self.peer_ids:
            response = self.transport.send_request_vote(self.node_id, peer_id, request)
            if response:
                self.receive_vote_response(peer_id, response)

    def receive_vote_response(self, peer_id: str, response: dict[str, Any]) -> None:
        with self._lock:
            if response["term"] > self.current_term:
                self._step_down(response["term"])
                return
            if self.state != "candidate" or response["term"] != self.current_term:
                return
            if response.get("vote_granted"):
                self.votes_received.add(peer_id)
                if len(self.votes_received) >= self.majority:
                    self.become_leader()

    def become_leader(self) -> None:
        self.state = "leader"
        self.leader_id = self.node_id
        self.next_index = {peer_id: len(self.log) for peer_id in self.peer_ids}
        self.match_index = {peer_id: -1 for peer_id in self.peer_ids}
        self.match_index[self.node_id] = self.last_log_index
        self.heartbeat_elapsed = 0
        self.broadcast_append_entries()

    def handle_request_vote(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            term = int(request["term"])
            if term < self.current_term:
                return {"term": self.current_term, "vote_granted": False}
            if term > self.current_term:
                self._step_down(term)
            candidate_id = request["candidate_id"]
            candidate_is_current = self.voted_for in {None, candidate_id}
            candidate_is_up_to_date = (int(request["last_log_term"]), int(request["last_log_index"])) >= (self.last_log_term, self.last_log_index)
            grant = candidate_is_current and candidate_is_up_to_date
            if grant:
                self.voted_for = candidate_id
                self.clock = 0
                self._persist()
            return {"term": self.current_term, "vote_granted": grant}

    def _matches(self, previous_index: int, previous_term: int) -> bool:
        return previous_index == -1 or (previous_index < len(self.log) and self.log[previous_index].term == previous_term)

    def handle_append_entries(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            term = int(request["term"])
            if term < self.current_term:
                return {"term": self.current_term, "success": False, "match_index": self.last_log_index}
            if term > self.current_term or self.state != "follower":
                self._step_down(term, request["leader_id"])
            self.leader_id = request["leader_id"]
            self.clock = 0
            previous_index = int(request["prev_log_index"])
            previous_term = int(request["prev_log_term"])
            if not self._matches(previous_index, previous_term):
                return {"term": self.current_term, "success": False, "match_index": self.last_log_index}
            entries = [LogEntry.from_dict(entry) for entry in request.get("entries", [])]
            index = previous_index + 1
            for entry in entries:
                if index < len(self.log) and self.log[index].term != entry.term:
                    self.log = self.log[:index]
                if index >= len(self.log):
                    self.log.append(entry)
                index += 1
            leader_commit = int(request["leader_commit"])
            if leader_commit > self.commit_index:
                self.commit_index = min(leader_commit, self.last_log_index)
                self.apply_committed()
            self._persist()
            return {"term": self.current_term, "success": True, "match_index": self.last_log_index}

    def broadcast_append_entries(self) -> None:
        if self.state != "leader":
            return
        for peer_id in self.peer_ids:
            self.replicate_to(peer_id)
        self.advance_commit()
        # Send the new leader commit index in a follow-up heartbeat.
        for peer_id in self.peer_ids:
            self.replicate_to(peer_id)
        self.apply_committed()

    def replicate_to(self, peer_id: str) -> None:
        if self.state != "leader":
            return
        next_index = self.next_index.get(peer_id, len(self.log))
        previous_index = next_index - 1
        previous_term = self.log[previous_index].term if previous_index >= 0 else 0
        request = {
            "type": "append_entries",
            "term": self.current_term,
            "leader_id": self.node_id,
            "prev_log_index": previous_index,
            "prev_log_term": previous_term,
            "entries": [entry.to_dict() for entry in self.log[next_index:]],
            "leader_commit": self.commit_index,
        }
        response = self.transport.send_append_entries(self.node_id, peer_id, request)
        if not response:
            return
        if response["term"] > self.current_term:
            self._step_down(response["term"])
            return
        if self.state != "leader":
            return
        if response.get("success"):
            self.match_index[peer_id] = response["match_index"]
            self.next_index[peer_id] = response["match_index"] + 1
        else:
            self.next_index[peer_id] = max(0, next_index - 1)
            if self.next_index[peer_id] != next_index:
                self.replicate_to(peer_id)

    def advance_commit(self) -> None:
        if self.state != "leader":
            return
        for index in range(self.commit_index + 1, len(self.log)):
            if self.log[index].term != self.current_term:
                continue
            replicated = 1 + sum(1 for peer_id in self.peer_ids if self.match_index.get(peer_id, -1) >= index)
            if replicated >= self.majority:
                self.commit_index = index

    def apply_committed(self) -> None:
        while self.last_applied < self.commit_index:
            self.last_applied += 1
            self.apply_command(self.log[self.last_applied].command)

    def propose(self, command: Any) -> int:
        with self._lock:
            if self.state != "leader":
                raise NotLeaderError(self.leader_id)
            self.log.append(LogEntry(self.current_term, command))
            self.match_index[self.node_id] = self.last_log_index
            self._persist()
            self.broadcast_append_entries()
            if self.commit_index < self.last_log_index:
                raise ConsensusSafetyError("proposal is not committed by a quorum")
            return self.last_log_index

    def status(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "state": self.state,
            "term": self.current_term,
            "leader_id": self.leader_id,
            "log_length": len(self.log),
            "commit_index": self.commit_index,
            "last_applied": self.last_applied,
        }


class RaftCluster:
    """Deterministic in-memory transport and fault-injection harness."""

    def __init__(self, node_ids: list[str], apply_factories: dict[str, Callable[[Any], None]] | None = None, storage_root: str | Path | None = None, election_timeout: int = 5, heartbeat_interval: int = 2):
        if len(node_ids) < 3 or len(node_ids) % 2 == 0:
            raise ValueError("RaftCluster requires an odd-sized cluster of at least three nodes")
        self.node_ids = list(node_ids)
        self.nodes: dict[str, RaftNode] = {}
        self.blocked: set[frozenset[str]] = set()
        apply_factories = apply_factories or {}
        for index, node_id in enumerate(self.node_ids):
            peer_ids = [peer for peer in self.node_ids if peer != node_id]
            storage = RaftStorage(Path(storage_root) / node_id) if storage_root else None
            self.nodes[node_id] = RaftNode(node_id, peer_ids, self, apply_factories.get(node_id), storage, election_timeout, heartbeat_interval, index)

    def connected(self, source: str, target: str) -> bool:
        return frozenset({source, target}) not in self.blocked

    def send_request_vote(self, source: str, target: str, request: dict[str, Any]) -> dict[str, Any] | None:
        if not self.connected(source, target):
            return None
        return self.nodes[target].handle_request_vote(request)

    def send_append_entries(self, source: str, target: str, request: dict[str, Any]) -> dict[str, Any] | None:
        if not self.connected(source, target):
            return None
        return self.nodes[target].handle_append_entries(request)

    def tick(self, count: int = 1) -> None:
        for _ in range(count):
            for node in list(self.nodes.values()):
                node.tick()

    def leaders(self) -> list[RaftNode]:
        return [node for node in self.nodes.values() if node.state == "leader"]

    def leader(self) -> RaftNode | None:
        leaders = self.leaders()
        return leaders[0] if len(leaders) == 1 else None

    def partition(self, left: list[str], right: list[str]) -> None:
        for source in left:
            for target in right:
                self.blocked.add(frozenset({source, target}))

    def heal(self) -> None:
        self.blocked.clear()
        for node in self.leaders():
            node.broadcast_append_entries()

    def status(self) -> list[dict[str, Any]]:
        return [self.nodes[node_id].status() for node_id in self.node_ids]


class ReplicatedEngine:
    """Apply committed SQL commands to one NovaDB engine per Raft node."""

    def __init__(self, engines: dict[str, Any], election_timeout: int = 5):
        self.engines = engines
        factories = {node_id: engine.execute for node_id, engine in engines.items()}
        self.cluster = RaftCluster(list(engines), factories, election_timeout=election_timeout)

    def tick(self, count: int = 1) -> None:
        self.cluster.tick(count)

    def execute(self, sql: str) -> int:
        leader = self.cluster.leader()
        if leader is None:
            raise NotLeaderError(None)
        return leader.propose(sql)
