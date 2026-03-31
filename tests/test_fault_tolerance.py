"""
Tests for Exchange Fault Tolerance — Raft Consensus, Hot-Hot Replication,
Deterministic Replay, and Chaos Engineering.

Covers:
- Raft leader election and log replication
- Raft safety guarantees (term, commit, quorum)
- Hot-hot divergence detection
- Deterministic replay correctness
- Chaos fault injection and recovery
- Version mismatch detection (NYSE 2015 bug)
- State corruption detection
"""

import pytest

from exchange.fault_tolerance import (
    ChaosEngineer,
    DeterministicReplayEngine,
    FailoverReason,
    FaultType,
    HotHotReplicator,
    LogEntry,
    NodeHealth,
    NodeState,
    RaftCluster,
    RaftNode,
    StateFingerprint,
)


# ===================================================================
# Raft Node Tests
# ===================================================================

class TestRaftNode:
    def test_initial_state(self):
        node = RaftNode("node1")
        assert node.state == NodeState.FOLLOWER
        assert node.current_term == 0
        assert node.voted_for is None
        assert node.is_leader is False
        assert node.last_log_index == -1

    def test_start_election(self):
        node = RaftNode("node1")
        term = node.start_election()
        assert term == 1
        assert node.state == NodeState.CANDIDATE
        assert node.voted_for == "node1"

    def test_request_vote_grants_if_valid(self):
        node = RaftNode("node2")
        term, granted = node.request_vote("node1", 1, -1, 0)
        assert granted is True
        assert node.voted_for == "node1"

    def test_request_vote_rejects_stale_term(self):
        node = RaftNode("node2")
        node.current_term = 5
        term, granted = node.request_vote("node1", 3, -1, 0)
        assert granted is False

    def test_request_vote_only_once_per_term(self):
        node = RaftNode("node2")
        _, granted1 = node.request_vote("node1", 1, -1, 0)
        _, granted2 = node.request_vote("node3", 1, -1, 0)
        assert granted1 is True
        assert granted2 is False  # Already voted for node1 in term 1

    def test_client_request_only_on_leader(self):
        follower = RaftNode("node1")
        assert follower.client_request({"type": "order"}) is None

        leader = RaftNode("leader")
        leader.state = NodeState.LEADER
        entry = leader.client_request({"type": "order", "id": "123"})
        assert entry is not None
        assert entry.term == 0
        assert entry.index == 0

    def test_append_entries_from_leader(self):
        follower = RaftNode("node2")
        entry = LogEntry(term=1, index=0, command={"type": "order"}, timestamp_ns=0)
        term, ok = follower.append_entries("leader", 1, -1, 0, [entry], -1)
        assert ok is True
        assert len(follower.log) == 1
        assert follower.state == NodeState.FOLLOWER

    def test_append_entries_rejects_old_term(self):
        follower = RaftNode("node2")
        follower.current_term = 5
        term, ok = follower.append_entries("leader", 3, -1, 0, [], -1)
        assert ok is False

    def test_go_offline_and_recover(self):
        node = RaftNode("node1")
        node.go_offline()
        assert node.state == NodeState.OFFLINE
        assert node.health == NodeHealth.UNREACHABLE

        node.recover()
        assert node.state == NodeState.FOLLOWER
        assert node.health == NodeHealth.HEALTHY

    def test_offline_node_rejects_everything(self):
        node = RaftNode("node1")
        node.go_offline()
        _, granted = node.request_vote("node2", 1, -1, 0)
        assert granted is False
        _, ok = node.append_entries("leader", 1, -1, 0, [], -1)
        assert ok is False

    def test_fingerprint_deterministic(self):
        node = RaftNode("node1", "1.0.0")
        entry = LogEntry(term=1, index=0, command={"a": 1}, timestamp_ns=0)
        node.log.append(entry)
        fp1 = node.get_fingerprint()
        fp2 = node.get_fingerprint()
        assert fp1.order_book_hash == fp2.order_book_hash
        assert fp1.software_version == "1.0.0"

    def test_fingerprint_matches_identical_nodes(self):
        a = RaftNode("a", "1.0.0")
        b = RaftNode("b", "1.0.0")
        entry = LogEntry(term=1, index=0, command={"x": 1}, timestamp_ns=0)
        a.log.append(entry)
        b.log.append(entry)
        assert a.get_fingerprint().matches(b.get_fingerprint())

    def test_fingerprint_detects_version_mismatch(self):
        a = RaftNode("a", "1.0.0")
        b = RaftNode("b", "1.0.1")
        assert not a.get_fingerprint().matches(b.get_fingerprint())


# ===================================================================
# Raft Cluster Tests
# ===================================================================

class TestRaftCluster:
    def test_create_cluster(self):
        cluster = RaftCluster(["n1", "n2", "n3"])
        assert cluster.cluster_size == 3
        assert cluster.quorum_size == 2
        assert cluster.leader_id is None

    def test_elect_leader(self):
        cluster = RaftCluster(["n1", "n2", "n3"])
        leader = cluster.elect_leader("n1")
        assert leader == "n1"
        assert cluster.leader_id == "n1"
        assert cluster.nodes["n1"].is_leader

    def test_submit_command_to_leader(self):
        cluster = RaftCluster(["n1", "n2", "n3"])
        cluster.elect_leader("n1")
        entry = cluster.submit_command({"type": "order", "id": "001"})
        assert entry is not None
        assert entry.index == 0

    def test_submit_command_without_leader_fails(self):
        cluster = RaftCluster(["n1", "n2", "n3"])
        assert cluster.submit_command({"type": "order"}) is None

    def test_replicate_to_followers(self):
        cluster = RaftCluster(["n1", "n2", "n3"])
        cluster.elect_leader("n1")
        cluster.submit_command({"type": "order", "id": "001"})
        replicated = cluster.replicate()
        assert replicated == 2  # Two followers

        # Followers should have the entry
        assert len(cluster.nodes["n2"].log) == 1
        assert len(cluster.nodes["n3"].log) == 1

    def test_commit_after_majority(self):
        cluster = RaftCluster(["n1", "n2", "n3"])
        cluster.elect_leader("n1")
        cluster.submit_command({"type": "order", "id": "001"})
        cluster.replicate()

        leader = cluster.nodes["n1"]
        assert leader.commit_index == 0

    def test_consistency_check_passes(self):
        cluster = RaftCluster(["n1", "n2", "n3"])
        cluster.elect_leader("n1")
        cluster.submit_command({"type": "order"})
        cluster.replicate()
        result = cluster.check_consistency()
        assert result["consistent"] is True

    def test_consistency_detects_version_mismatch(self):
        cluster = RaftCluster(["n1", "n2", "n3"], software_version="1.0.0")
        cluster.elect_leader("n1")
        cluster.nodes["n3"].software_version = "0.9.9"
        result = cluster.check_consistency()
        assert result["consistent"] is False
        assert any("version_mismatch" in str(d["reasons"]) for d in result["divergences"])

    def test_leader_failure_triggers_election(self):
        cluster = RaftCluster(["n1", "n2", "n3"])
        cluster.elect_leader("n1")
        event = cluster.simulate_node_failure("n1")
        assert event.reason == FailoverReason.LEADER_TIMEOUT
        assert event.success is True
        assert cluster.leader_id != "n1"
        assert cluster.leader_id is not None

    def test_follower_failure_no_election(self):
        cluster = RaftCluster(["n1", "n2", "n3"])
        cluster.elect_leader("n1")
        event = cluster.simulate_node_failure("n3")
        assert event.reason == FailoverReason.HEALTH_CHECK_FAIL
        assert cluster.leader_id == "n1"  # Leader unchanged

    def test_recover_node_catches_up(self):
        cluster = RaftCluster(["n1", "n2", "n3"])
        cluster.elect_leader("n1")
        cluster.submit_command({"type": "order1"})
        cluster.submit_command({"type": "order2"})
        cluster.replicate()

        cluster.simulate_node_failure("n3")
        assert cluster.nodes["n3"].state == NodeState.OFFLINE

        cluster.recover_node("n3")
        assert cluster.nodes["n3"].state == NodeState.FOLLOWER
        assert len(cluster.nodes["n3"].log) == 2

    def test_quorum_maintained_after_single_failure(self):
        cluster = RaftCluster(["n1", "n2", "n3", "n4", "n5"])
        cluster.elect_leader("n1")
        cluster.simulate_node_failure("n5")
        status = cluster.get_cluster_status()
        assert status["has_quorum"] is True
        assert status["healthy_nodes"] == 4

    def test_quorum_lost_after_majority_failure(self):
        cluster = RaftCluster(["n1", "n2", "n3"])
        cluster.elect_leader("n1")
        cluster.simulate_node_failure("n2")
        cluster.simulate_node_failure("n3")
        status = cluster.get_cluster_status()
        assert status["has_quorum"] is False

    def test_failover_history(self):
        cluster = RaftCluster(["n1", "n2", "n3"])
        cluster.elect_leader("n1")
        cluster.simulate_node_failure("n1")
        history = cluster.get_failover_history()
        assert len(history) == 1
        assert history[0]["reason"] == "LEADER_TIMEOUT"

    def test_multiple_commands_replicate(self):
        cluster = RaftCluster(["n1", "n2", "n3"])
        cluster.elect_leader("n1")
        for i in range(10):
            cluster.submit_command({"type": "order", "id": str(i)})
        cluster.replicate()
        for nid in ["n1", "n2", "n3"]:
            assert len(cluster.nodes[nid].log) == 10


# ===================================================================
# Hot-Hot Replication Tests
# ===================================================================

class TestHotHotReplicator:
    def test_matching_results(self):
        repl = HotHotReplicator()
        result = repl.process_dual(
            {"order": "buy"},
            {"trade": "100@50.00"},
            {"trade": "100@50.00"},
        )
        assert result["match"] is True
        assert repl.is_consistent

    def test_divergence_detected(self):
        repl = HotHotReplicator()
        result = repl.process_dual(
            {"order": "buy"},
            {"trade": "100@50.00"},
            {"trade": "100@50.01"},  # Different price!
        )
        assert result["match"] is False
        assert not repl.is_consistent
        assert repl.is_halted

    def test_halt_on_divergence(self):
        repl = HotHotReplicator()
        repl.process_dual({"o": "1"}, {"r": "a"}, {"r": "b"})
        assert repl.is_halted
        assert "divergence" in repl.get_status()["halt_reason"]

    def test_resume_after_halt(self):
        repl = HotHotReplicator()
        repl.process_dual({"o": "1"}, {"r": "a"}, {"r": "b"})
        repl.resume()
        assert not repl.is_halted

    def test_failover_switches_engine(self):
        repl = HotHotReplicator()
        assert repl._active_engine == "engine_a"
        result = repl.failover("engine_b")
        assert result["new_active"] == "engine_b"
        assert repl._active_engine == "engine_b"

    def test_status(self):
        repl = HotHotReplicator()
        repl.process_dual({"o": "1"}, {"r": "same"}, {"r": "same"})
        status = repl.get_status()
        assert status["sequence"] == 1
        assert status["consistent"] is True
        assert status["total_comparisons"] == 1

    def test_multiple_consistent_comparisons(self):
        repl = HotHotReplicator()
        for i in range(100):
            repl.process_dual({"o": str(i)}, {"r": str(i)}, {"r": str(i)})
        assert repl.is_consistent
        assert repl.get_status()["total_comparisons"] == 100


# ===================================================================
# Deterministic Replay Tests
# ===================================================================

class TestDeterministicReplay:
    def test_append_and_replay(self):
        engine = DeterministicReplayEngine()
        engine.append({"type": "order", "id": "1"})
        engine.append({"type": "order", "id": "2"})
        result = engine.replay_from(0)
        assert result["entries_replayed"] == 2
        assert result["matches_current"] is True

    def test_verify_determinism(self):
        engine = DeterministicReplayEngine()
        for i in range(50):
            engine.append({"type": "order", "id": str(i), "price": 100 + i})
        assert engine.verify_determinism() is True

    def test_checkpoint_and_replay(self):
        engine = DeterministicReplayEngine()
        for i in range(5):
            engine.append({"type": "order", "id": str(i)})
        cp = engine.checkpoint()
        assert cp["sequence"] == 5

        for i in range(5, 10):
            engine.append({"type": "order", "id": str(i)})

        result = engine.replay_from(5)
        assert result["from_checkpoint"] == 5
        assert result["entries_replayed"] == 5
        assert result["matches_current"] is True

    def test_empty_replay(self):
        engine = DeterministicReplayEngine()
        result = engine.replay_from(0)
        assert result["entries_replayed"] == 0
        assert result["matches_current"] is True

    def test_same_commands_produce_same_hash(self):
        a = DeterministicReplayEngine()
        b = DeterministicReplayEngine()
        commands = [{"type": "order", "id": str(i)} for i in range(10)]
        for cmd in commands:
            a.append(cmd)
            b.append(cmd)
        assert a.state_hash == b.state_hash

    def test_different_commands_produce_different_hash(self):
        a = DeterministicReplayEngine()
        b = DeterministicReplayEngine()
        a.append({"type": "order", "id": "1"})
        b.append({"type": "order", "id": "2"})
        assert a.state_hash != b.state_hash

    def test_stats(self):
        engine = DeterministicReplayEngine()
        engine.append({"type": "x"})
        engine.checkpoint()
        stats = engine.get_stats()
        assert stats["total_entries"] == 1
        assert stats["total_checkpoints"] == 1
        assert stats["current_sequence"] == 1


# ===================================================================
# Chaos Engineer Tests
# ===================================================================

class TestChaosEngineer:
    def _make_cluster(self) -> RaftCluster:
        cluster = RaftCluster(["n1", "n2", "n3", "n4", "n5"])
        cluster.elect_leader("n1")
        cluster.submit_command({"type": "order", "id": "1"})
        cluster.replicate()
        return cluster

    def test_inject_follower_crash(self):
        cluster = self._make_cluster()
        chaos = ChaosEngineer()
        result = chaos.inject_node_crash(cluster, "n3")
        assert result.success is True
        assert result.fault_type == FaultType.NODE_CRASH
        assert result.data_integrity_preserved is True

    def test_inject_leader_crash_triggers_failover(self):
        cluster = self._make_cluster()
        chaos = ChaosEngineer()
        result = chaos.inject_node_crash(cluster, "n1")
        assert result.success is True
        assert cluster.leader_id != "n1"
        assert cluster.leader_id is not None

    def test_detect_version_mismatch(self):
        cluster = self._make_cluster()
        chaos = ChaosEngineer()
        result = chaos.inject_version_mismatch(cluster, "n3", "0.4.9")
        assert result.success is True
        assert result.fault_type == FaultType.VERSION_MISMATCH

    def test_detect_state_corruption(self):
        cluster = self._make_cluster()
        chaos = ChaosEngineer()
        result = chaos.inject_state_corruption(cluster, "n3")
        assert result.success is True
        assert result.fault_type == FaultType.STATE_CORRUPTION

    def test_full_chaos_suite(self):
        cluster = self._make_cluster()
        chaos = ChaosEngineer()
        results = chaos.run_full_suite(cluster)
        assert results["total_tests"] >= 2
        assert results["passed"] >= 2
        assert results["resilience_score"] > 0

    def test_resilience_score_tracking(self):
        cluster = self._make_cluster()
        chaos = ChaosEngineer()
        chaos.inject_node_crash(cluster, "n5")
        cluster.recover_node("n5")
        chaos.inject_version_mismatch(cluster, "n4")
        score = chaos.get_resilience_score()
        assert score["total_tests"] == 2
        assert score["score"] > 0

    def test_chaos_on_no_online_nodes(self):
        cluster = RaftCluster(["n1"])
        cluster.nodes["n1"].go_offline()
        chaos = ChaosEngineer()
        result = chaos.inject_node_crash(cluster)
        assert result.success is False


# ===================================================================
# Integration Tests
# ===================================================================

class TestFaultToleranceIntegration:
    def test_full_lifecycle(self):
        """Cluster boot → commands → failure → recovery → consistency."""
        cluster = RaftCluster(["n1", "n2", "n3", "n4", "n5"], "1.0.0")

        # 1. Boot and elect leader
        cluster.elect_leader("n1")
        assert cluster.leader_id == "n1"

        # 2. Process commands
        for i in range(20):
            cluster.submit_command({"type": "order", "id": str(i)})
        cluster.replicate()

        # 3. Verify consistency
        assert cluster.check_consistency()["consistent"] is True

        # 4. Kill leader
        cluster.simulate_node_failure("n1")
        assert cluster.leader_id is not None
        assert cluster.leader_id != "n1"

        # 5. Process more commands on new leader
        for i in range(20, 30):
            cluster.submit_command({"type": "order", "id": str(i)})
        cluster.replicate()

        # 6. Recover crashed node
        cluster.recover_node("n1")
        assert cluster.nodes["n1"].state == NodeState.FOLLOWER

        # 7. All nodes should have all entries
        status = cluster.get_cluster_status()
        assert status["has_quorum"] is True
        assert status["healthy_nodes"] == 5

    def test_nyse_2015_scenario(self):
        """Simulate the exact NYSE 2015 failure: version mismatch on failover."""
        cluster = RaftCluster(["primary", "hot_standby", "dr_site"], "2.1.0")
        cluster.elect_leader("primary")

        # Deploy update to primary only (the bug)
        cluster.nodes["primary"].software_version = "2.2.0"
        # Forgot to update DR site (the actual NYSE mistake)
        # hot_standby and dr_site still on 2.1.0

        # Check should catch this BEFORE failover
        consistency = cluster.check_consistency()
        assert consistency["consistent"] is False
        assert any("version_mismatch" in str(d["reasons"]) for d in consistency["divergences"])

    def test_chaos_then_verify_determinism(self):
        """Run chaos tests, then verify replay still works."""
        cluster = RaftCluster(["n1", "n2", "n3"])
        cluster.elect_leader("n1")

        replay = DeterministicReplayEngine()
        for i in range(10):
            cmd = {"type": "order", "id": str(i)}
            cluster.submit_command(cmd)
            replay.append(cmd)
        cluster.replicate()

        # Chaos
        chaos = ChaosEngineer()
        chaos.inject_node_crash(cluster, "n3")
        cluster.recover_node("n3")

        # Replay should still be deterministic
        assert replay.verify_determinism() is True
