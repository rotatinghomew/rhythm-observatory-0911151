from __future__ import annotations

import json
import sqlite3
import threading

from .data import canonical, fingerprint


class Conflict(ValueError):
    pass


class ArtifactStore:
    def __init__(self, path=":memory:", policy=None):
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
            digest TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS policy (
            slot INTEGER PRIMARY KEY CHECK (slot=1), revision INTEGER NOT NULL, payload TEXT NOT NULL
        );
        """)
        self.connection.execute("INSERT OR IGNORE INTO policy VALUES (1,1,?)", (canonical(policy or {}),))
        self.connection.commit()

    def current_policy(self):
        with self.lock:
            row = self.connection.execute("SELECT revision,payload FROM policy WHERE slot=1").fetchone()
            return {**json.loads(row["payload"]), "revision": row["revision"]}

    def update_policy(self, expected_revision, value):
        payload = {key: val for key, val in value.items() if key != "revision"}
        with self.lock, self.connection:
            result = self.connection.execute("UPDATE policy SET revision=revision+1,payload=? WHERE slot=1 AND revision=?", (canonical(payload), expected_revision))
            if result.rowcount != 1:
                raise Conflict("消费约束版本已变化")
        return self.current_policy()

    def publish(self, request_key, artifact, policy_revision):
        if not request_key or not isinstance(artifact, dict):
            raise ValueError("发布请求不完整")
        if not artifact.get("snapshot_id") or not artifact.get("method") or "result" not in artifact:
            raise ValueError("成果缺少资料或方法来源")
        encoded = canonical(artifact)
        digest = fingerprint(artifact)
        with self.lock, self.connection:
            previous = self.connection.execute("SELECT digest,payload,artifact_id FROM artifacts WHERE request_key=?", (request_key,)).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise Conflict("同一发布键不可用于不同内容")
                return {"artifact_id": previous["artifact_id"], "artifact": json.loads(previous["payload"])}
            policy = self.current_policy()
            if policy_revision != policy["revision"]:
                raise Conflict("提交时消费约束已更新")
            blocked = set(policy.get("blocked_items", []))
            if blocked.intersection(artifact.get("items", [])):
                raise Conflict("部分内容已经不再允许使用")
            identifier = "artifact-" + fingerprint({"request_key": request_key, "digest": digest})[:24]
            self.connection.execute("INSERT INTO artifacts VALUES (?,?,?,?)", (identifier, request_key, digest, encoded))
            return {"artifact_id": identifier, "artifact": json.loads(encoded)}

    def get(self, identifier):
        with self.lock:
            row = self.connection.execute("SELECT payload FROM artifacts WHERE artifact_id=?", (identifier,)).fetchone()
            return None if row is None else json.loads(row["payload"])

    def close(self):
        with self.lock:
            self.connection.close()
