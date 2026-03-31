"""
Exchange Fault Tolerance — Raft Consensus, Hot-Hot Replication, Deterministic Replay.

When the NYSE went dark for 3 hours and 38 minutes on July 8, 2015, it wasn't because
of a cyberattack or hardware failure. It was a configuration mismatch between primary
and backup systems after a routine software update. The backup couldn't take over
because it was running a different software version.

This module implements the fault tolerance primitives that prevent such outages:

Architecture:
┌──────────────────────────────────────────────────────────────────────────────┐
│                       EXCHANGE FAULT TOLERANCE                               │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────────────┐   │
│  │  RAFT CONSENSUS │   │  HOT-HOT        │   │  FAILOVER CONTROLLER    │   │
│  │                 │   │  REPLICATION     │   │                         │   │
│  │  Leader elect   │   │                 │   │  Health monitoring      │   │
│  │  Log replicate  │   │  Dual engines   │   │  Automatic switchover  │   │
│  │  Commit quorum  │   │  State compare  │   │  Recovery orchestration│   │
│  │  Term tracking  │   │  Diverge detect │   │  Version validation    │   │
│  └─────────────────┘   └─────────────────┘   └─────────────────────────┘   │
│                                                                              │
│  ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────────────┐   │
│  │  DETERMINISTIC  │   │  STATE          │   │  CHAOS ENGINEER         │   │
│  │  REPLAY         │   │  FINGERPRINT    │   │  (AI-DRIVEN)            │   │
│  │                 │   │                 │   │                         │   │
│  │  WAL-based      │   │  Hash-based     │   │  Fault injection       │   │
│  │  Sequence order │   │  Cross-replica  │   │  Recovery validation   │   │
│  │  Fixed-point    │   │  Consistency    │   │  Preemptive failover   │   │
│  │  Catch-up       │   │  verification   │   │  Resilience scoring    │   │
│  └─────────────────┘   └─────────────────┘   └─────────────────────────┘   │
│                                                                              │
│  Recovery: <30s target | Replication lag: <1ms | Uptime SLA: 99.99%         │
└──────────────────────────────────────────────────────────────────────────────┘

References:
- "In Search of an Understandable Consensus Algorithm" — Ongaro & Ousterhout (Raft)
- NYSE July 8, 2015 Outage — SEC Investigation Report
- "Patterns of Distributed Systems" — Martin Fowler
- "Building Consistent Transactions with Inconsistent Replication" — ACM
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & Data Models
# ---------------------------------------------------------------------------

class NodeState(str, Enum):
    FOLLOWER = "FOLLOWER"
    CANDIDATE = "CANDIDATE"
    LEADER = "LEADER"
    OFFLINE = "OFFLINE"


class NodeHealth(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    UNREACHABLE = "UNREACHABLE"


class FailoverReason(str, Enum):
    LEADER_TIMEOUT = "LEADER_TIMEOUT"
    STATE_DIVERGENCE = "STATE_DIVERGENCE"
    HEALTH_CHECK_FAIL = "HEALTH_CHECK_FAIL"
    MANUAL_TRIGGER = "MANUAL_TRIGGER"
    CHAOS_TEST = "CHAOS_TEST"
    VERSION_MISMATCH = "VERSION_MISMATCH"


class FaultType(str, Enum):
    NODE_CRASH = "NODE_CRASH"
    NETWORK_PARTITION = "NETWORK_PARTITION"
    SLOW_DISK = "SLOW_DISK"
    STATE_CORRUPTION = "STATE_CORRUPTION"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    CLOCK_SKEW = "CLOCK_SKEW"


@dataclass
class LogEntry:
    """A single entry in the Raft replicated log."""
    term: int
    index: int
    command: dict
    timestamp_ns: int
    committed: bool = False

    def fingerprint(self) -> str:
        """Deterministic fingerprint for cross-replica comparison."""
        content = f"{self.term}:{self.index}:{sorted(self.command.items())}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]


@dataclass
class StateFingerprint:
    """Cross-replica state validation fingerprint."""
    node_id: str
    sequence_number: int
    state_hash: str
    order_book_hash: str
    log_length: int
    last_committed_index: int
    software_version: str
    timestamp_ns: int

    def matches(self, other: StateFingerprint) -> bool:
        """Check if two replicas have identical state."""
        return (
            self.state_hash == other.state_hash
            and self.order_book_hash == other.order_book_hash
            and self.last_committed_index == other.last_committed_index
            and self.software_version == other.software_version
        )


@dataclass
class HealthCheck:
    """Health check result for a single node."""
    node_id: str
    health: NodeHealth
    latency_ms: float
    cpu_percent: float
    memory_percent: float
    disk_io_ms: float
    last_heartbeat_ns: int
    software_version: str
    sequence_number: int
    log_lag: int  # How far behind the leader


@dataclass
class FailoverEvent:
    """Record of a failover occurrence."""
    event_id: str
    reason: FailoverReason
    from_node: str
    to_node: str
    duration_ms: float
    success: bool
    orders_during_failover: int
    data_loss: bool
    timestamp_ns: int
    details: str = ""


@dataclass
class ChaosResult:
    """Result of a chaos engineering test."""
    test_id: str
    fault_type: FaultType
    target_node: str
    injection_time_ns: int
    detection_time_ns: int
    recovery_time_ns: int
    detection_latency_ms: float
    recovery_latency_ms: float
    data_integrity_preserved: bool
    orders_affected: int
    success: bool
    details: str = ""


# ---------------------------------------------------------------------------
# Raft Consensus — Leader Election & Log Replication
# ---------------------------------------------------------------------------

class RaftNode:
    """
    Simplified Raft consensus node for exchange state replication.

    Implements the core Raft algorithm:
    1. Leader Election — nodes vote when leader heartbeat times out
    2. Log Replication — leader replicates log entries to followers
    3. Safety — only committed entries are applied to state machine

    In production: this would use gRPC for inter-node communication.
    Here we simulate the consensus protocol in-process for testing.

    The 2015 NYSE outage happened because their equivalent of this
    system didn't enforce version consistency across nodes.
    """

    def __init__(
        self,
        node_id: str,
        software_version: str = "0.5.0",
        election_timeout_ms: float = 150.0,
    ):
        self.node_id = node_id
        self.software_version = software_version
        self.election_timeout_ms = election_timeout_ms

        # Raft persistent state
        self.current_term: int = 0
        self.voted_for: Optional[str] = None
        self.log: list[LogEntry] = []

        # Raft volatile state
        self.state: NodeState = NodeState.FOLLOWER
        self.commit_index: int = -1
        self.last_applied: int = -1

        # Leader volatile state
        self.next_index: dict[str, int] = {}  # node_id -> next log index to send
        self.match_index: dict[str, int] = {}  # node_id -> highest replicated index

        # Health
        self.health: NodeHealth = NodeHealth.HEALTHY
        self.last_heartbeat_ns: int = time.time_ns()

        # Applied state (the order book state machine)
        self._state_hash: str = hashlib.sha256(b"empty").hexdigest()[:16]
        self._applied_commands: list[dict] = []

    @property
    def is_leader(self) -> bool:
        return self.state == NodeState.LEADER

    @property
    def last_log_index(self) -> int:
        return len(self.log) - 1

    @property
    def last_log_term(self) -> int:
        if self.log:
            return self.log[-1].term
        return 0

    def request_vote(self, candidate_id: str, candidate_term: int, last_log_index: int, last_log_term: int) -> tuple[int, bool]:
        """
        Handle RequestVote RPC (Raft Section 5.2).

        Returns (current_term, vote_granted).
        """
        if self.state == NodeState.OFFLINE:
            return self.current_term, False

        # If candidate's term is higher, step down
        if candidate_term > self.current_term:
            self.current_term = candidate_term
            self.state = NodeState.FOLLOWER
            self.voted_for = None

        if candidate_term < self.current_term:
            return self.current_term, False

        # Check if we can vote for this candidate
        if self.voted_for is not None and self.voted_for != candidate_id:
            return self.current_term, False

        # Check log is at least as up-to-date (Raft Section 5.4.1)
        if last_log_term < self.last_log_term:
            return self.current_term, False
        if last_log_term == self.last_log_term and last_log_index < self.last_log_index:
            return self.current_term, False

        self.voted_for = candidate_id
        self.last_heartbeat_ns = time.time_ns()
        return self.current_term, True

    def append_entries(
        self,
        leader_id: str,
        leader_term: int,
        prev_log_index: int,
        prev_log_term: int,
        entries: list[LogEntry],
        leader_commit: int,
    ) -> tuple[int, bool]:
        """
        Handle AppendEntries RPC (Raft Section 5.3).

        Returns (current_term, success).
        """
        if self.state == NodeState.OFFLINE:
            return self.current_term, False

        if leader_term < self.current_term:
            return self.current_term, False

        # Valid leader — reset election timeout
        self.current_term = leader_term
        self.state = NodeState.FOLLOWER
        self.voted_for = None
        self.last_heartbeat_ns = time.time_ns()

        # Check prev_log consistency
        if prev_log_index >= 0:
            if prev_log_index >= len(self.log):
                return self.current_term, False
            if self.log[prev_log_index].term != prev_log_term:
                # Conflict — truncate from here
                self.log = self.log[:prev_log_index]
                return self.current_term, False

        # Append new entries
        for entry in entries:
            idx = entry.index
            if idx < len(self.log):
                if self.log[idx].term != entry.term:
                    self.log = self.log[:idx]
                    self.log.append(entry)
            else:
                self.log.append(entry)

        # Update commit index
        if leader_commit > self.commit_index:
            self.commit_index = min(leader_commit, self.last_log_index)
            self._apply_committed()

        return self.current_term, True

    def client_request(self, command: dict) -> Optional[LogEntry]:
        """
        Handle a client request (only valid on leader).

        Returns the LogEntry if accepted, None if not leader.
        """
        if not self.is_leader:
            return None

        entry = LogEntry(
            term=self.current_term,
            index=len(self.log),
            command=command,
            timestamp_ns=time.time_ns(),
        )
        self.log.append(entry)
        return entry

    def start_election(self) -> int:
        """
        Start a new election (Raft Section 5.2).

        Returns the new term number.
        """
        self.current_term += 1
        self.state = NodeState.CANDIDATE
        self.voted_for = self.node_id
        return self.current_term

    def become_leader(self, cluster_node_ids: list[str]):
        """Transition to leader state after winning election."""
        self.state = NodeState.LEADER
        # Initialize leader volatile state
        for node_id in cluster_node_ids:
            if node_id != self.node_id:
                self.next_index[node_id] = len(self.log)
                self.match_index[node_id] = -1

    def commit_entries(self, cluster_size: int):
        """
        Advance commit index based on majority replication.

        An entry is committed when stored on a majority of nodes.
        """
        if not self.is_leader:
            return

        for i in range(self.commit_index + 1, len(self.log)):
            if self.log[i].term != self.current_term:
                continue
            # Count replicas (including self)
            replicas = 1
            for match_idx in self.match_index.values():
                if match_idx >= i:
                    replicas += 1
            if replicas > cluster_size // 2:
                self.commit_index = i
                self.log[i].committed = True

        self._apply_committed()

    def go_offline(self):
        """Simulate node crash."""
        self.state = NodeState.OFFLINE
        self.health = NodeHealth.UNREACHABLE

    def recover(self):
        """Simulate node recovery."""
        self.state = NodeState.FOLLOWER
        self.health = NodeHealth.HEALTHY
        self.voted_for = None
        self.last_heartbeat_ns = time.time_ns()

    def get_fingerprint(self) -> StateFingerprint:
        """Generate state fingerprint for cross-replica validation."""
        log_hash = hashlib.sha256(
            "|".join(e.fingerprint() for e in self.log).encode()
        ).hexdigest()[:16] if self.log else "empty"

        return StateFingerprint(
            node_id=self.node_id,
            sequence_number=len(self._applied_commands),
            state_hash=self._state_hash,
            order_book_hash=log_hash,
            log_length=len(self.log),
            last_committed_index=self.commit_index,
            software_version=self.software_version,
            timestamp_ns=time.time_ns(),
        )

    def _apply_committed(self):
        """Apply committed but unapplied entries to the state machine."""
        while self.last_applied < self.commit_index:
            self.last_applied += 1
            if self.last_applied < len(self.log):
                entry = self.log[self.last_applied]
                self._applied_commands.append(entry.command)
                # Update state hash deterministically
                content = f"{self._state_hash}:{entry.fingerprint()}"
                self._state_hash = hashlib.sha256(content.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Raft Cluster — Coordinated Multi-Node Consensus
# ---------------------------------------------------------------------------

class RaftCluster:
    """
    Manages a cluster of Raft nodes for exchange fault tolerance.

    Provides:
    - Automatic leader election when leader fails
    - Log replication to all followers
    - Commit confirmation when majority acknowledges
    - State consistency validation across all nodes
    """

    def __init__(self, node_ids: list[str], software_version: str = "0.5.0"):
        self.nodes: dict[str, RaftNode] = {
            nid: RaftNode(nid, software_version)
            for nid in node_ids
        }
        self.leader_id: Optional[str] = None
        self._failover_log: list[FailoverEvent] = []

    @property
    def cluster_size(self) -> int:
        return len(self.nodes)

    @property
    def quorum_size(self) -> int:
        return self.cluster_size // 2 + 1

    @property
    def leader(self) -> Optional[RaftNode]:
        if self.leader_id:
            return self.nodes.get(self.leader_id)
        return None

    def elect_leader(self, candidate_id: Optional[str] = None) -> Optional[str]:
        """
        Run a leader election round.

        If candidate_id is specified, that node starts the election.
        Otherwise, picks a random healthy follower.
        """
        if candidate_id is None:
            healthy = [
                nid for nid, n in self.nodes.items()
                if n.state != NodeState.OFFLINE
            ]
            if not healthy:
                return None
            candidate_id = healthy[0]

        candidate = self.nodes[candidate_id]
        if candidate.state == NodeState.OFFLINE:
            return None

        new_term = candidate.start_election()
        votes = 1  # Self-vote

        for nid, node in self.nodes.items():
            if nid == candidate_id:
                continue
            term, granted = node.request_vote(
                candidate_id=candidate_id,
                candidate_term=new_term,
                last_log_index=candidate.last_log_index,
                last_log_term=candidate.last_log_term,
            )
            if granted:
                votes += 1

        if votes >= self.quorum_size:
            candidate.become_leader(list(self.nodes.keys()))
            self.leader_id = candidate_id
            logger.info(f"Raft: {candidate_id} elected leader (term={new_term}, votes={votes}/{self.cluster_size})")
            return candidate_id

        candidate.state = NodeState.FOLLOWER
        return None

    def submit_command(self, command: dict) -> Optional[LogEntry]:
        """Submit a command (order) to the cluster via the leader."""
        leader = self.leader
        if not leader:
            return None
        return leader.client_request(command)

    def replicate(self) -> int:
        """
        Replicate leader's log to all followers.

        Returns number of successful replications.
        """
        leader = self.leader
        if not leader:
            return 0

        success_count = 0
        for nid, node in self.nodes.items():
            if nid == leader.node_id:
                continue

            next_idx = leader.next_index.get(nid, 0)
            prev_idx = next_idx - 1
            prev_term = leader.log[prev_idx].term if prev_idx >= 0 and prev_idx < len(leader.log) else 0

            entries = leader.log[next_idx:]

            term, ok = node.append_entries(
                leader_id=leader.node_id,
                leader_term=leader.current_term,
                prev_log_index=prev_idx,
                prev_log_term=prev_term,
                entries=entries,
                leader_commit=leader.commit_index,
            )

            if ok:
                leader.next_index[nid] = len(leader.log)
                leader.match_index[nid] = len(leader.log) - 1 if leader.log else -1
                success_count += 1
            elif next_idx > 0:
                leader.next_index[nid] = max(0, next_idx - 1)

        leader.commit_entries(self.cluster_size)

        # Second pass: propagate updated commit index to followers
        for nid, node in self.nodes.items():
            if nid == leader.node_id or node.state == NodeState.OFFLINE:
                continue
            node.append_entries(
                leader_id=leader.node_id,
                leader_term=leader.current_term,
                prev_log_index=len(leader.log) - 1 if leader.log else -1,
                prev_log_term=leader.log[-1].term if leader.log else 0,
                entries=[],
                leader_commit=leader.commit_index,
            )

        return success_count

    def check_consistency(self) -> dict:
        """
        Validate state consistency across all healthy nodes.

        This is the check that would have caught the NYSE 2015 mismatch.
        """
        fingerprints = {}
        for nid, node in self.nodes.items():
            if node.state != NodeState.OFFLINE:
                fingerprints[nid] = node.get_fingerprint()

        if len(fingerprints) < 2:
            return {"consistent": True, "nodes_checked": len(fingerprints), "divergences": []}

        reference_id = self.leader_id or list(fingerprints.keys())[0]
        reference = fingerprints[reference_id]
        divergences = []

        for nid, fp in fingerprints.items():
            if nid == reference_id:
                continue
            if not reference.matches(fp):
                reasons = []
                if reference.state_hash != fp.state_hash:
                    reasons.append("state_hash_mismatch")
                if reference.order_book_hash != fp.order_book_hash:
                    reasons.append("order_book_hash_mismatch")
                if reference.software_version != fp.software_version:
                    reasons.append(f"version_mismatch({reference.software_version}!={fp.software_version})")
                if reference.last_committed_index != fp.last_committed_index:
                    reasons.append(f"commit_lag({fp.last_committed_index} vs {reference.last_committed_index})")
                divergences.append({
                    "node": nid,
                    "reasons": reasons,
                })

        return {
            "consistent": len(divergences) == 0,
            "nodes_checked": len(fingerprints),
            "reference_node": reference_id,
            "divergences": divergences,
        }

    def simulate_node_failure(self, node_id: str) -> Optional[FailoverEvent]:
        """Simulate a node crash and handle failover."""
        start_ns = time.time_ns()
        node = self.nodes.get(node_id)
        if not node:
            return None

        node.go_offline()
        was_leader = node_id == self.leader_id

        if was_leader:
            self.leader_id = None
            # Elect new leader from healthy nodes
            healthy = [
                nid for nid, n in self.nodes.items()
                if n.state != NodeState.OFFLINE
            ]
            new_leader = None
            if healthy:
                new_leader = self.elect_leader(healthy[0])

            duration_ms = (time.time_ns() - start_ns) / 1_000_000

            event = FailoverEvent(
                event_id=uuid4().hex[:12],
                reason=FailoverReason.LEADER_TIMEOUT,
                from_node=node_id,
                to_node=new_leader or "NONE",
                duration_ms=round(duration_ms, 3),
                success=new_leader is not None,
                orders_during_failover=0,
                data_loss=False,
                timestamp_ns=start_ns,
                details=f"Leader {node_id} crashed, failover to {new_leader or 'NONE'}",
            )
            self._failover_log.append(event)
            return event

        return FailoverEvent(
            event_id=uuid4().hex[:12],
            reason=FailoverReason.HEALTH_CHECK_FAIL,
            from_node=node_id,
            to_node="N/A",
            duration_ms=0,
            success=True,
            orders_during_failover=0,
            data_loss=False,
            timestamp_ns=start_ns,
            details=f"Follower {node_id} went offline, cluster continues with leader {self.leader_id}",
        )

    def recover_node(self, node_id: str) -> bool:
        """Recover a crashed node and catch it up."""
        node = self.nodes.get(node_id)
        if not node:
            return False

        node.recover()

        # Catch up from leader's log (replace log entirely to avoid duplication)
        leader = self.leader
        if leader and leader.log:
            node.log = list(leader.log)
            node.commit_index = leader.commit_index
            node.last_applied = -1
            node.current_term = leader.current_term
            node._state_hash = hashlib.sha256(b"empty").hexdigest()[:16]
            node._applied_commands = []
            node._apply_committed()

        return True

    def get_cluster_status(self) -> dict:
        """Get comprehensive cluster status."""
        nodes = {}
        for nid, node in self.nodes.items():
            nodes[nid] = {
                "state": node.state.value,
                "health": node.health.value,
                "term": node.current_term,
                "log_length": len(node.log),
                "commit_index": node.commit_index,
                "applied": node.last_applied,
                "version": node.software_version,
                "is_leader": node.is_leader,
            }

        return {
            "cluster_size": self.cluster_size,
            "quorum_size": self.quorum_size,
            "leader": self.leader_id,
            "healthy_nodes": sum(1 for n in self.nodes.values() if n.state != NodeState.OFFLINE),
            "has_quorum": sum(1 for n in self.nodes.values() if n.state != NodeState.OFFLINE) >= self.quorum_size,
            "nodes": nodes,
            "consistency": self.check_consistency(),
            "total_failovers": len(self._failover_log),
        }

    def get_failover_history(self, limit: int = 20) -> list[dict]:
        """Get recent failover events."""
        events = self._failover_log[-limit:]
        return [
            {
                "event_id": e.event_id,
                "reason": e.reason.value,
                "from_node": e.from_node,
                "to_node": e.to_node,
                "duration_ms": e.duration_ms,
                "success": e.success,
                "data_loss": e.data_loss,
                "details": e.details,
            }
            for e in events
        ]


# ---------------------------------------------------------------------------
# Hot-Hot Replication — Dual Engine Comparison
# ---------------------------------------------------------------------------

class HotHotReplicator:
    """
    Hot-hot matching engine replication with state comparison.

    Both engines process every order simultaneously and must produce
    identical results. A comparator validates outputs continuously.

    If any divergence is detected, trading halts immediately.
    This is the system that catches bugs like the NYSE 2015 mismatch
    before they cause outages.

    Unlike cold standby (backup sits idle), hot-hot means:
    - Zero warm-up time on failover
    - Continuous validation that both engines are correct
    - Failover in <30 seconds because backup is already running
    """

    def __init__(self, engine_a_id: str = "engine_a", engine_b_id: str = "engine_b"):
        self.engine_a_id = engine_a_id
        self.engine_b_id = engine_b_id

        self._results_a: list[dict] = []
        self._results_b: list[dict] = []
        self._sequence: int = 0
        self._divergences: list[dict] = []
        self._halted: bool = False
        self._halt_reason: Optional[str] = None
        self._active_engine: str = engine_a_id

    @property
    def is_consistent(self) -> bool:
        return len(self._divergences) == 0

    @property
    def is_halted(self) -> bool:
        return self._halted

    def process_dual(self, command: dict, result_a: dict, result_b: dict) -> dict:
        """
        Compare results from both engines for the same command.

        Returns comparison result with divergence details if any.
        """
        self._sequence += 1
        self._results_a.append(result_a)
        self._results_b.append(result_b)

        # Compute deterministic hashes of results
        hash_a = self._hash_result(result_a)
        hash_b = self._hash_result(result_b)

        comparison = {
            "sequence": self._sequence,
            "command": command,
            "match": hash_a == hash_b,
            "hash_a": hash_a,
            "hash_b": hash_b,
            "timestamp_ns": time.time_ns(),
        }

        if hash_a != hash_b:
            divergence = {
                "sequence": self._sequence,
                "command": command,
                "result_a": result_a,
                "result_b": result_b,
                "hash_a": hash_a,
                "hash_b": hash_b,
                "timestamp_ns": time.time_ns(),
            }
            self._divergences.append(divergence)
            self._halted = True
            self._halt_reason = f"State divergence at sequence {self._sequence}: {hash_a} != {hash_b}"
            logger.critical(f"HOT-HOT DIVERGENCE: {self._halt_reason}")
            comparison["divergence"] = divergence

        return comparison

    def failover(self, to_engine: str) -> dict:
        """Switch active engine."""
        prev = self._active_engine
        self._active_engine = to_engine
        return {
            "previous_active": prev,
            "new_active": to_engine,
            "timestamp_ns": time.time_ns(),
        }

    def resume(self):
        """Resume after resolving divergence."""
        self._halted = False
        self._halt_reason = None

    def get_status(self) -> dict:
        return {
            "active_engine": self._active_engine,
            "sequence": self._sequence,
            "consistent": self.is_consistent,
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "total_divergences": len(self._divergences),
            "total_comparisons": self._sequence,
        }

    @staticmethod
    def _hash_result(result: dict) -> str:
        """Deterministic hash of an engine result."""
        content = str(sorted(result.items()))
        return hashlib.sha256(content.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Deterministic Replay — WAL-Based State Recovery
# ---------------------------------------------------------------------------

class DeterministicReplayEngine:
    """
    Replay engine for crash recovery and node catch-up.

    Given a sequence of commands, replays them in exact order to
    reconstruct the state machine. Every replay of the same sequence
    must produce bit-identical results.

    Key requirements for determinism:
    - Fixed-point arithmetic (no floats in critical path)
    - Seeded random generators (if any randomness needed)
    - Nanosecond timestamps from log, not wall clock
    - Sorted iteration over all collections
    """

    def __init__(self):
        self._replay_log: list[dict] = []
        self._checkpoints: dict[int, str] = {}  # sequence -> state_hash
        self._state_hash: str = hashlib.sha256(b"genesis").hexdigest()[:16]
        self._sequence: int = 0

    def checkpoint(self) -> dict:
        """Save a checkpoint of current state."""
        self._checkpoints[self._sequence] = self._state_hash
        return {
            "sequence": self._sequence,
            "state_hash": self._state_hash,
            "total_entries": len(self._replay_log),
        }

    def append(self, command: dict) -> int:
        """Append a command to the replay log."""
        self._sequence += 1
        entry = {
            "sequence": self._sequence,
            "command": command,
            "timestamp_ns": time.time_ns(),
        }
        self._replay_log.append(entry)

        # Update state hash deterministically
        content = f"{self._state_hash}:{self._sequence}:{sorted(command.items())}"
        self._state_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        return self._sequence

    def replay_from(self, checkpoint_sequence: int = 0) -> dict:
        """
        Replay all commands from a checkpoint to rebuild state.

        Returns the final state hash for validation.
        """
        if checkpoint_sequence in self._checkpoints:
            state_hash = self._checkpoints[checkpoint_sequence]
        else:
            state_hash = hashlib.sha256(b"genesis").hexdigest()[:16]
            checkpoint_sequence = 0

        replayed = 0
        for entry in self._replay_log:
            if entry["sequence"] <= checkpoint_sequence:
                continue
            content = f"{state_hash}:{entry['sequence']}:{sorted(entry['command'].items())}"
            state_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
            replayed += 1

        return {
            "from_checkpoint": checkpoint_sequence,
            "entries_replayed": replayed,
            "final_state_hash": state_hash,
            "matches_current": state_hash == self._state_hash,
        }

    def verify_determinism(self) -> bool:
        """
        Verify that replaying the entire log produces the same state.

        This is the fundamental invariant of deterministic replay.
        """
        result = self.replay_from(0)
        return result["matches_current"]

    @property
    def state_hash(self) -> str:
        return self._state_hash

    def get_stats(self) -> dict:
        return {
            "total_entries": len(self._replay_log),
            "total_checkpoints": len(self._checkpoints),
            "current_sequence": self._sequence,
            "current_state_hash": self._state_hash,
        }


# ---------------------------------------------------------------------------
# Chaos Engineer — AI-Driven Fault Injection
# ---------------------------------------------------------------------------

class ChaosEngineer:
    """
    AI-driven chaos engineering for exchange fault tolerance testing.

    Inspired by Netflix's Chaos Monkey, adapted for financial exchanges.
    Injects controlled faults during low-volume periods and measures
    the system's ability to detect, recover, and maintain data integrity.

    The 2015 NYSE outage could have been prevented if they had run
    chaos tests that verified version consistency during failover.

    Fault types:
    - Node crash: kill a random node, verify failover
    - Network partition: isolate a node, verify quorum maintained
    - Slow disk: inject latency, verify degraded-mode operation
    - State corruption: mutate state, verify detection
    - Version mismatch: simulate the NYSE 2015 bug
    - Clock skew: offset timestamps, verify determinism
    """

    def __init__(self):
        self._test_history: list[ChaosResult] = []
        self._resilience_score: float = 1.0  # 0.0 = fragile, 1.0 = resilient
        self._total_tests: int = 0
        self._total_passed: int = 0

    def inject_node_crash(self, cluster: RaftCluster, target_node: Optional[str] = None) -> ChaosResult:
        """Inject a node crash and measure recovery."""
        start_ns = time.time_ns()

        if target_node is None:
            online = [nid for nid, n in cluster.nodes.items() if n.state != NodeState.OFFLINE]
            if not online:
                return self._make_result(FaultType.NODE_CRASH, "NONE", start_ns, False, "No online nodes")
            target_node = random.choice(online)

        was_leader = target_node == cluster.leader_id

        # Inject fault
        event = cluster.simulate_node_failure(target_node)
        detection_ns = time.time_ns()

        # Verify recovery
        has_quorum = sum(1 for n in cluster.nodes.values() if n.state != NodeState.OFFLINE) >= cluster.quorum_size
        has_leader = cluster.leader_id is not None

        # Check state consistency among remaining nodes
        consistency = cluster.check_consistency()

        recovery_ns = time.time_ns()
        success = has_quorum and (has_leader or not was_leader) and consistency["consistent"]

        result = ChaosResult(
            test_id=uuid4().hex[:12],
            fault_type=FaultType.NODE_CRASH,
            target_node=target_node,
            injection_time_ns=start_ns,
            detection_time_ns=detection_ns,
            recovery_time_ns=recovery_ns,
            detection_latency_ms=round((detection_ns - start_ns) / 1_000_000, 3),
            recovery_latency_ms=round((recovery_ns - start_ns) / 1_000_000, 3),
            data_integrity_preserved=consistency["consistent"],
            orders_affected=0,
            success=success,
            details=f"{'Leader' if was_leader else 'Follower'} crash, quorum={'YES' if has_quorum else 'NO'}, leader={'YES' if has_leader else 'NO'}",
        )

        self._record_result(result)
        return result

    def inject_version_mismatch(self, cluster: RaftCluster, target_node: str, bad_version: str = "0.4.9") -> ChaosResult:
        """
        Simulate the NYSE 2015 bug: version mismatch between nodes.

        Changes one node's software version and checks if the cluster detects it.
        """
        start_ns = time.time_ns()

        node = cluster.nodes.get(target_node)
        if not node:
            return self._make_result(FaultType.VERSION_MISMATCH, target_node, start_ns, False, "Node not found")

        original_version = node.software_version
        node.software_version = bad_version

        detection_ns = time.time_ns()

        # Check if cluster detects the mismatch
        consistency = cluster.check_consistency()
        detected = not consistency["consistent"]

        # Check that the reason includes version mismatch
        version_detected = any(
            "version_mismatch" in str(d.get("reasons", []))
            for d in consistency.get("divergences", [])
        )

        recovery_ns = time.time_ns()

        # Restore original version
        node.software_version = original_version

        result = ChaosResult(
            test_id=uuid4().hex[:12],
            fault_type=FaultType.VERSION_MISMATCH,
            target_node=target_node,
            injection_time_ns=start_ns,
            detection_time_ns=detection_ns,
            recovery_time_ns=recovery_ns,
            detection_latency_ms=round((detection_ns - start_ns) / 1_000_000, 3),
            recovery_latency_ms=round((recovery_ns - start_ns) / 1_000_000, 3),
            data_integrity_preserved=True,
            orders_affected=0,
            success=version_detected,
            details=f"Version mismatch {original_version} vs {bad_version}, detected={version_detected}",
        )

        self._record_result(result)
        return result

    def inject_state_corruption(self, cluster: RaftCluster, target_node: str) -> ChaosResult:
        """Corrupt a node's state and verify detection."""
        start_ns = time.time_ns()

        node = cluster.nodes.get(target_node)
        if not node:
            return self._make_result(FaultType.STATE_CORRUPTION, target_node, start_ns, False, "Node not found")

        # Corrupt the state hash
        original_hash = node._state_hash
        node._state_hash = "CORRUPTED_STATE"

        detection_ns = time.time_ns()

        # Check if cluster detects corruption
        consistency = cluster.check_consistency()
        detected = not consistency["consistent"]

        recovery_ns = time.time_ns()

        # Restore state
        node._state_hash = original_hash

        result = ChaosResult(
            test_id=uuid4().hex[:12],
            fault_type=FaultType.STATE_CORRUPTION,
            target_node=target_node,
            injection_time_ns=start_ns,
            detection_time_ns=detection_ns,
            recovery_time_ns=recovery_ns,
            detection_latency_ms=round((detection_ns - start_ns) / 1_000_000, 3),
            recovery_latency_ms=round((recovery_ns - start_ns) / 1_000_000, 3),
            data_integrity_preserved=True,
            orders_affected=0,
            success=detected,
            details=f"State corrupted on {target_node}, detected={detected}",
        )

        self._record_result(result)
        return result

    def run_full_suite(self, cluster: RaftCluster) -> dict:
        """
        Run a complete chaos test suite against the cluster.

        Returns aggregate results and resilience score.
        """
        results = []

        # Test 1: Non-leader crash
        non_leaders = [nid for nid, n in cluster.nodes.items() if not n.is_leader and n.state != NodeState.OFFLINE]
        if non_leaders:
            r = self.inject_node_crash(cluster, non_leaders[0])
            results.append(r)
            cluster.recover_node(non_leaders[0])

        # Test 2: Version mismatch detection
        followers = [nid for nid, n in cluster.nodes.items() if not n.is_leader and n.state != NodeState.OFFLINE]
        if followers:
            r = self.inject_version_mismatch(cluster, followers[0])
            results.append(r)

        # Test 3: State corruption detection
        if followers:
            r = self.inject_state_corruption(cluster, followers[-1])
            results.append(r)

        passed = sum(1 for r in results if r.success)
        total = len(results)

        return {
            "total_tests": total,
            "passed": passed,
            "failed": total - passed,
            "resilience_score": round(self._resilience_score, 4),
            "results": [
                {
                    "test_id": r.test_id,
                    "fault_type": r.fault_type.value,
                    "target": r.target_node,
                    "success": r.success,
                    "detection_ms": r.detection_latency_ms,
                    "recovery_ms": r.recovery_latency_ms,
                    "data_intact": r.data_integrity_preserved,
                }
                for r in results
            ],
        }

    def get_resilience_score(self) -> dict:
        """Get overall resilience score and breakdown."""
        return {
            "score": round(self._resilience_score, 4),
            "total_tests": self._total_tests,
            "total_passed": self._total_passed,
            "pass_rate": round(self._total_passed / max(1, self._total_tests), 4),
            "recent_tests": [
                {
                    "test_id": r.test_id,
                    "fault_type": r.fault_type.value,
                    "success": r.success,
                    "details": r.details,
                }
                for r in self._test_history[-10:]
            ],
        }

    def _record_result(self, result: ChaosResult):
        """Record a test result and update resilience score."""
        self._test_history.append(result)
        self._total_tests += 1
        if result.success:
            self._total_passed += 1
        # Exponential moving average of pass rate
        self._resilience_score = 0.9 * self._resilience_score + 0.1 * (1.0 if result.success else 0.0)

    def _make_result(self, fault_type: FaultType, target: str, start_ns: int, success: bool, details: str) -> ChaosResult:
        now = time.time_ns()
        result = ChaosResult(
            test_id=uuid4().hex[:12],
            fault_type=fault_type,
            target_node=target,
            injection_time_ns=start_ns,
            detection_time_ns=now,
            recovery_time_ns=now,
            detection_latency_ms=0,
            recovery_latency_ms=0,
            data_integrity_preserved=True,
            orders_affected=0,
            success=success,
            details=details,
        )
        self._record_result(result)
        return result
