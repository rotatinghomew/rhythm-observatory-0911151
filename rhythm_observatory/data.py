from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path


TABLES = ['readings', 'clocks', 'outages']


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load_bundle(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or set(value["tables"]) != set(TABLES):
        raise ValueError("业务资料版本或表集合不匹配")
    for name, rows in value["tables"].items():
        if not isinstance(rows, list):
            raise ValueError("资料表必须是记录数组")
        identities = set()
        for row in rows:
            identity = (row["id"], row["revision"])
            if identity in identities or row["revision"] < 1:
                raise ValueError("同表记录代次重复或无效")
            identities.add(identity)
            if len(row["released_at"]) != 10:
                raise ValueError("公开日期应使用 ISO 日期")
            for cell_value in row.values():
                if isinstance(cell_value, float) and not math.isfinite(cell_value):
                    raise ValueError("资料不能含非有限值")
    return value


def visible_snapshot(bundle, as_of):
    tables = {}
    for name, rows in bundle["tables"].items():
        latest = {}
        for row in rows:
            if row["released_at"] > as_of:
                continue
            prior = latest.get(row["id"])
            if prior is None or (row["released_at"], row["revision"]) > (prior["released_at"], prior["revision"]):
                latest[row["id"]] = row
        tables[name] = [deepcopy(latest[key]) for key in sorted(latest) if not latest[key].get("withdrawn", False)]
    payload = {"schema_version": 1, "product": bundle["product"], "as_of": as_of, "tables": tables}
    return {**payload, "snapshot_id": fingerprint(payload)}
