from collections import defaultdict
from math import floor


def clocked_readings(snapshot):
    clocks = snapshot["tables"]["clocks"]
    result = []
    for row in snapshot["tables"]["readings"]:
        applicable = [clock for clock in clocks if clock["device"] == row["device"] and clock["raw_from"] <= row["raw_hour"] < clock["raw_to"]]
        if not applicable:
            raise ValueError("计数器缺少可用时钟校正")
        clock = max(applicable, key=lambda value: (value["released_at"], value["revision"]))
        result.append({**row, "corrected_hour": row["raw_hour"] + clock["correction_hours"], "applied_clock": clock["id"]})
    return sorted(result, key=lambda value: (value["device"], value["raw_hour"]))


def observed_increments(snapshot):
    previous = {}
    increments = []
    for row in clocked_readings(snapshot):
        prior = previous.get(row["device"])
        if prior is not None and not row["reset"]:
            delta = row["cumulative"] - prior["cumulative"]
            increments.append({"platform": row["platform"], "start_hour": prior["corrected_hour"], "end_hour": row["corrected_hour"], "count": max(0, delta), "counter_anomaly": delta < 0, "source": [prior["id"], row["id"]], "clock": row["applied_clock"]})
        previous[row["device"]] = row
    return increments


def summarize(snapshot):
    daily = defaultdict(float)
    for row in observed_increments(snapshot):
        daily[f"{row['platform']}:{floor(row['end_hour']/24)}"] += row["count"]
    return {"daily_end_assigned_count": dict(daily), "interpretation": "计数增量按区间终点归日，仅供原始用量浏览，不表达周期同步"}
