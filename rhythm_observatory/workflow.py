from __future__ import annotations

from .product import summarize
from .rhythm import METHOD as RHYTHM_METHOD
from .rhythm import analyze_rhythm


def publish_overview(client, as_of, request_key):
    snapshot = client.snapshot(as_of)
    policy = client.policy()
    artifact = {
        "as_of": as_of,
        "snapshot_id": snapshot["snapshot_id"],
        "method": "observed-overview-v1",
        "result": summarize(snapshot),
    }
    return client.publish(request_key, artifact, policy["revision"])


def publish_rhythm(client, as_of, request_key, seed=20240501):
    """发布不规则观测周期与跨平台同步分析成果。"""
    snapshot = client.snapshot(as_of)
    policy = client.policy()
    result = analyze_rhythm(snapshot, seed=seed)
    artifact = {
        "as_of": as_of,
        "snapshot_id": snapshot["snapshot_id"],
        "method": RHYTHM_METHOD,
        "result": result,
        "config": {"seed": seed, "time_basis": "clock_corrected_hours_since_2024-01-01"},
    }
    return client.publish(request_key, artifact, policy["revision"])
