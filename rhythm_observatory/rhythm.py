"""不规则观测下的周期与跨平台同步分析。

设计原则（对应排期端的使用约束）：

* 不做等距重采样、不补零；观测时间使用时钟校正后的真实时间，每个派生点保留
  原始读数 id、时钟规则 id 与停测标记，结果可以回溯到原始时间与校正版本。
* 周期检测用 Lomb-Scargle 周期图（适用于任意采样时刻），并单独计算采样窗口
  谱，把采样节律产生的峰与内容信号区分开；显著性由保持采样时刻不变的时序
  置换代理分布给出，不依赖等距或独立同分布假设。
* 相位与共同周期只在两地覆盖时段的交叠窗口内比较；相位差不确定性用成对
  bootstrap 给出，并显式区分"各自存在周期"与"足以判断同步"两类结论。
* 停测窗口与短时突发会被标记并做剔除复算，结论按最保守口径降级。
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np
from scipy.signal import lombscargle
from scipy.stats import f as f_distribution

from .product import clocked_readings

METHOD = "rhythm-sync-v1"
MIN_PERIOD_HOURS = 24.0
MAX_PERIOD_FRACTION = 0.5
SURROGATE_RUNS = 200
BOOTSTRAP_RUNS = 500
SIGNIFICANCE_LEVEL = 0.05
# 业务先验：内容排期的候选节奏落在 3-14 天窗口；显著性只在该频带内做
# 多频点校正，避免对全频段做"找最大值"检验导致弱但真实的周期被淹没。
PERIOD_BAND_HOURS = (72.0, 336.0)
# 候选周期视为共同周期时，各自周期近似置信区间允许的最大失配（小时）。
COMMON_PERIOD_TOLERANCE_HOURS = 6.0
# 少于该数量的有效周期覆盖数时，不输出周期结论。
MIN_CYCLES = 3.0
# 少于该数量的交叠观测点时，不输出同步结论。
MIN_OVERLAP_POINTS = 30
# 相位差 95% 区间宽度（占周期比例）超过该阈值，视为精度不足，只能给"未决"。
# 取 0.12 而非更宽松的 0.15，使蒙特卡洛误差下边界样本不会在两档间来回翻转。
PHASE_PRECISION_LIMIT = 0.12


def _finite(values):
    array = np.asarray(values, dtype=float)
    if not np.all(np.isfinite(array)):
        raise ValueError("分析输入必须全部为有限数")
    return array


def rate_series(snapshot):
    """把累计计数增量换算为区间中点的单位时间计数率。

    返回按平台分组的记录列表；每条记录携带原始/校正时间、来源读数与时钟
    规则，保证后续任何结论都能回溯。停测区间（outages，左闭右开）在原始
    时间轴上标记，并把横跨停测的增量标为 gap_spanning。
    """
    readings = clocked_readings(snapshot)
    outages = defaultdict(list)
    for row in snapshot["tables"]["outages"]:
        outages[row["platform"]].append((row["from_hour"], row["to_hour"], row["id"]))

    previous = {}
    series = defaultdict(list)
    for row in readings:
        prior = previous.get(row["device"])
        if prior is not None and not row["reset"]:
            delta = row["cumulative"] - prior["cumulative"]
            duration = row["corrected_hour"] - prior["corrected_hour"]
            raw_duration = row["raw_hour"] - prior["raw_hour"]
            if duration <= 0 or raw_duration <= 0:
                raise ValueError("校正后时间序列出现非递增，无法建立增量区间")
            spans = [
                outage_id
                for from_hour, to_hour, outage_id in outages[row["platform"]]
                if prior["raw_hour"] < to_hour and row["raw_hour"] > from_hour
            ]
            midpoint = (prior["corrected_hour"] + row["corrected_hour"]) / 2.0
            series[row["platform"]].append(
                {
                    "platform": row["platform"],
                    "time_hour": midpoint,
                    "raw_time_hour": (prior["raw_hour"] + row["raw_hour"]) / 2.0,
                    "start_hour": prior["corrected_hour"],
                    "end_hour": row["corrected_hour"],
                    "raw_start_hour": prior["raw_hour"],
                    "raw_end_hour": row["raw_hour"],
                    "duration_hours": duration,
                    "nominal_interval_hours": row["interval_hours"],
                    "rate": max(0.0, delta) / duration,
                    "count": max(0.0, delta),
                    "counter_anomaly": delta < 0,
                    "gap_spanning": bool(spans),
                    "outage_ids": spans,
                    "source_readings": [prior["id"], row["id"]],
                    "clock_records": sorted({prior["applied_clock"], row["applied_clock"]}),
                    "clock_versions": sorted({prior["clock_version"], row["clock_version"]}),
                }
            )
        previous[row["device"]] = row
    return {platform: sorted(rows, key=lambda item: item["time_hour"]) for platform, rows in series.items()}


def _period_grid(times):
    span = float(times.max() - times.min())
    upper = min(span * MAX_PERIOD_FRACTION, span)
    if upper <= MIN_PERIOD_HOURS:
        upper = span
    # 均匀频率网格，避免长周期一侧分辨率过粗；规模控制在可重复计算的范围。
    frequencies = np.linspace(2.0 * np.pi / upper, 2.0 * np.pi / MIN_PERIOD_HOURS, 2400)
    return 2.0 * np.pi / frequencies, frequencies


def _periodogram(times, values, frequencies):
    centered = values - values.mean()
    return lombscargle(times, centered, frequencies, normalize=True, precenter=True)


def _sampling_window_power(times, frequencies):
    """采样窗口谱：观测时刻放单位值，揭示采样节律自身制造的谱峰。

    用非归一化傅里叶幅度并按观测数标准化，谱峰位置与经典均匀采样的
    1/采样间隔及其谐波一致；对近似规则采样会在相应频率处接近 1，
    而对抖动的不规则采样保持低值。
    """
    angular = np.atleast_1d(frequencies)
    cosine = np.cos(np.outer(angular, times)).sum(axis=1)
    sine = np.sin(np.outer(angular, times)).sum(axis=1)
    power = (cosine ** 2 + sine ** 2) / (len(times) ** 2)
    peak = power.max()
    return power / peak if peak > 0 else power


def _surrogate_pvalue(times, values, frequencies, observed_power, runs, rng):
    """保持采样时刻不变、置换取值，模拟无时间结构的零假设。

    检验在给定频带内取代理谱的最大值，等价于对该频带做族系校正，
    避免固定频点检验的择优偏差，也避免全频段检验过度保守。
    """
    exceed = 0
    for _ in range(runs):
        shuffled = rng.permutation(values)
        null_power = _periodogram(times, shuffled, frequencies).max()
        if null_power >= observed_power:
            exceed += 1
    return (exceed + 1) / (runs + 1)


def _band_frequencies():
    return np.linspace(
        2.0 * np.pi / PERIOD_BAND_HOURS[1],
        2.0 * np.pi / PERIOD_BAND_HOURS[0],
        1200,
    )


def fit_harmonic(times, values, period):
    """在指定周期上拟合 A*cos(wt)+B*sin(wt)+C，返回振幅、相位（小时）与残差。"""
    angular = 2.0 * np.pi / period
    design = np.column_stack(
        [np.cos(angular * times), np.sin(angular * times), np.ones_like(times)]
    )
    coefficients, _, _, _ = np.linalg.lstsq(design, values, rcond=None)
    cosine, sine, intercept = coefficients
    amplitude = float(np.hypot(cosine, sine))
    phase_hours = float((np.arctan2(-sine, cosine) * period / (2.0 * np.pi)) % period)
    residual = values - design @ coefficients
    return amplitude, phase_hours, float(residual @ residual), intercept


def _period_refinement(times, values, coarse_period):
    """在粗峰附近以残差平方和最小化细化周期，并给近似 95% 置信区间。"""
    grid = np.linspace(coarse_period * 0.94, coarse_period * 1.06, 241)
    rss = np.array([fit_harmonic(times, values, period)[2] for period in grid])
    best_index = int(np.argmin(rss))
    best = float(grid[best_index])
    threshold = rss.min() * (1.0 + 3.84 / max(len(times) - 3, 1))
    accepted = grid[rss <= threshold]
    if len(accepted) == 0:
        interval = (best, best)
    else:
        interval = (float(accepted.min()), float(accepted.max()))
    return best, interval


def _bootstrap_phase(times, values, period, runs, rng):
    phases = np.empty(runs)
    for index in range(runs):
        picked = rng.integers(0, len(times), len(times))
        _, phase, _, _ = fit_harmonic(times[picked], values[picked], period)
        phases[index] = phase / period * 2.0 * np.pi
    return phases


def _robust_burst_mask(values):
    """基于中位数与 MAD 的突发标记；返回布尔掩码与阈值。"""
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    scale = 1.4826 * mad if mad > 0 else float(np.std(values))
    if scale <= 0:
        return np.zeros_like(values, dtype=bool), median, None
    threshold = median + 4.0 * scale
    return values > threshold, median, threshold


def analyze_platform(platform, points, rng, surrogate_runs=SURROGATE_RUNS, bootstrap_runs=BOOTSTRAP_RUNS):
    times = _finite([point["time_hour"] for point in points])
    rates = _finite([point["rate"] for point in points])
    periods, frequencies = _period_grid(times)
    signal_power = _periodogram(times, rates, frequencies)
    window_power = _sampling_window_power(times, frequencies)

    peak_index = int(np.argmax(signal_power))
    coarse_period = float(periods[peak_index])
    peak_power = float(signal_power[peak_index])
    # 显著性检验限定在内容节奏先验频带内，并以该频带的最大谱功率为观测统计量。
    band_frequencies = _band_frequencies()
    band_indices = np.flatnonzero(
        (periods >= PERIOD_BAND_HOURS[0]) & (periods <= PERIOD_BAND_HOURS[1])
    )
    band_peak_index = int(band_indices[np.argmax(signal_power[band_indices])])
    band_period = float(periods[band_peak_index])
    band_power = float(signal_power[band_peak_index])
    p_value = _surrogate_pvalue(
        times, rates, band_frequencies, band_power, surrogate_runs, rng
    )

    burst_mask, rate_median, burst_threshold = _robust_burst_mask(rates)
    gap_mask = np.array([point["gap_spanning"] for point in points], dtype=bool)
    excluded_mask = burst_mask | gap_mask

    refined_period, period_interval = _period_refinement(times, rates, band_period)
    amplitude, phase_hours, rss, _ = fit_harmonic(times, rates, refined_period)
    total = float(((rates - rates.mean()) ** 2).sum())
    r_squared = 1.0 - rss / total if total > 0 else 0.0

    phase_angles = _bootstrap_phase(times, rates, refined_period, bootstrap_runs, rng)
    observed_angle = phase_hours / refined_period * 2.0 * np.pi
    deviation = np.angle(np.exp(1j * (phase_angles - observed_angle)))
    low_delta, high_delta = np.quantile(deviation, [0.025, 0.975])
    phase_ci = (
        float((observed_angle + low_delta) * refined_period / (2.0 * np.pi) % refined_period),
        float((observed_angle + high_delta) * refined_period / (2.0 * np.pi) % refined_period),
    )

    span = float(times.max() - times.min())
    cycles = span / refined_period
    # 采样窗口谱在候选峰处若同样高，说明该峰可能由采样节律制造，不能算内容信号。
    window_at_band_peak = float(window_power[band_peak_index])
    sampling_induced = window_at_band_peak >= 0.5
    significant = (
        p_value < SIGNIFICANCE_LEVEL
        and cycles >= MIN_CYCLES
        and not sampling_induced
        and amplitude > 0.0
    )

    # 剔除突发/跨停测增量后的复算，用于稳健性判断。
    kept = ~excluded_mask
    robustness = {"excluded_points": int(excluded_mask.sum()), "status": "not_needed"}
    if excluded_mask.any() and kept.sum() >= MIN_OVERLAP_POINTS:
        power_trimmed = float(_periodogram(times[kept], rates[kept], frequencies).max())
        period_trimmed, _ = _period_refinement(
            times[kept], rates[kept], refined_period
        )
        robustness = {
            "status": "stable"
            if abs(period_trimmed - refined_period) <= COMMON_PERIOD_TOLERANCE_HOURS
            and power_trimmed >= 0.5 * peak_power
            else "sensitive",
            "excluded_points": int(excluded_mask.sum()),
            "retained_points": int(kept.sum()),
            "period_without_bursts_hours": float(period_trimmed),
            "peak_power_without_bursts": power_trimmed,
        }

    supporting = [
        {
            "source_readings": point["source_readings"],
            "clock_records": point["clock_records"],
            "clock_versions": point["clock_versions"],
            "time_hour": point["time_hour"],
            "raw_time_hour": point["raw_time_hour"],
            "rate": point["rate"],
            "gap_spanning": point["gap_spanning"],
            "outage_ids": point["outage_ids"],
        }
        for point in points
    ]
    return {
        "platform": platform,
        "observation_count": len(points),
        "coverage": {
            "start_hour": float(times.min()),
            "end_hour": float(times.max()),
            "span_hours": span,
        },
        "candidate_period": {
            "period_hours": refined_period,
            "period_95ci_hours": list(period_interval),
            "search_peak_hours": coarse_period,
            "band_peak_hours": band_period,
            "test_band_hours": list(PERIOD_BAND_HOURS),
            "peak_power": peak_power,
            "band_peak_power": band_power,
            "sampling_window_power": window_at_band_peak,
            "sampling_induced": sampling_induced,
            "surrogate_p_value": p_value,
            "cycles_observed": cycles,
            "amplitude": amplitude,
            "r_squared": float(r_squared),
            "phase_hours": phase_hours,
            "phase_95ci_hours": list(phase_ci),
            "significant": significant,
            "status": "period_detected"
            if significant
            else "period_not_established",
        },
        "sampling_spectrum_note": (
            "候选峰在采样窗口谱上同样显著，应视为采样节律，不能作为内容周期"
            if sampling_induced
            else "候选峰处采样窗口功率很低，谱峰来自取值变化而非采样时刻排布"
        ),
        "burst_detection": {
            "method": "median_plus_4_mad",
            "rate_median": rate_median,
            "threshold": burst_threshold,
            "burst_count": int(burst_mask.sum()),
            "gap_spanning_count": int(gap_mask.sum()),
            "burst_sources": [point["source_readings"] for point, mark in zip(points, burst_mask) if mark],
            "gap_sources": [point["source_readings"] for point, mark in zip(points, gap_mask) if mark],
            "gap_outage_ids": sorted({oid for point in points if point["gap_spanning"] for oid in point["outage_ids"]}),
        },
        "robustness": robustness,
        "supporting_records": supporting,
    }


def _shared_phase_test(pair_a, pair_b, period):
    """共同相位模型 vs 各自相位模型的 F 检验（平台各自保留截距）。"""
    separated_rss = 0.0
    blocks = []
    targets = []
    for index, (times, values) in enumerate((pair_a, pair_b)):
        _, _, rss, _ = fit_harmonic(times, values, period)
        separated_rss += rss
        angular = 2.0 * np.pi / period
        basis = np.column_stack([np.cos(angular * times), np.sin(angular * times)])
        intercept_block = np.zeros((len(times), 2))
        intercept_block[:, index] = 1.0
        blocks.append(np.hstack([basis, intercept_block]))
        targets.append(values)
    design = np.vstack(blocks)
    target = np.concatenate(targets)
    coefficients, _, _, _ = np.linalg.lstsq(design, target, rcond=None)
    shared_rss = float(((target - design @ coefficients) ** 2).sum())
    degrees = len(target) - 6
    if degrees <= 0 or separated_rss <= 0:
        return None, None, None
    f_statistic = ((shared_rss - separated_rss) / 1.0) / (separated_rss / degrees)
    p_value = float(1.0 - f_distribution.cdf(f_statistic, 1, degrees))
    shared_phase = float(
        (np.arctan2(-coefficients[1], coefficients[0]) * period / (2.0 * np.pi)) % period
    )
    return float(f_statistic), p_value, shared_phase


def analyze_pair(left, right, platform_points, rng, bootstrap_runs=BOOTSTRAP_RUNS):
    """两个平台在共同覆盖窗口内的相位同步分析。"""
    points_a = platform_points[left]
    points_b = platform_points[right]
    times_a = _finite([point["time_hour"] for point in points_a])
    times_b = _finite([point["time_hour"] for point in points_b])
    overlap_start = max(times_a.min(), times_b.min())
    overlap_end = min(times_a.max(), times_b.max())
    mask_a = (times_a >= overlap_start) & (times_a <= overlap_end)
    mask_b = (times_b >= overlap_start) & (times_b <= overlap_end)
    rates_a = _finite([point["rate"] for point in points_a])[mask_a]
    rates_b = _finite([point["rate"] for point in points_b])[mask_b]
    window_a = times_a[mask_a]
    window_b = times_b[mask_b]

    result = {
        "platforms": [left, right],
        "overlap_window_hours": [float(overlap_start), float(overlap_end)],
        "overlap_point_counts": [int(mask_a.sum()), int(mask_b.sum())],
        "synchronization_status": "not_evaluable",
        "reason": None,
    }
    if min(mask_a.sum(), mask_b.sum()) < MIN_OVERLAP_POINTS or overlap_end <= overlap_start:
        result["reason"] = "交叠窗口内观测点不足，同步比较不可评估"
        return result

    period_a, interval_a = _period_refinement(window_a, rates_a, 168.0)
    period_b, interval_b = _period_refinement(window_b, rates_b, 168.0)
    # 交叠窗口内重新检验显著性，避免用窗口外的周期冒充同步证据。
    band_frequencies = _band_frequencies()
    power_a = float(_periodogram(window_a, rates_a, band_frequencies).max())
    power_b = float(_periodogram(window_b, rates_b, band_frequencies).max())
    p_a = _surrogate_pvalue(window_a, rates_a, band_frequencies, power_a, SURROGATE_RUNS, rng)
    p_b = _surrogate_pvalue(window_b, rates_b, band_frequencies, power_b, SURROGATE_RUNS, rng)
    result["within_overlap"] = {
        left: {"period_hours": period_a, "period_95ci_hours": interval_a,
               "peak_power": power_a, "surrogate_p_value": p_a},
        right: {"period_hours": period_b, "period_95ci_hours": interval_b,
                "peak_power": power_b, "surrogate_p_value": p_b},
    }
    both_periodic = p_a < SIGNIFICANCE_LEVEL and p_b < SIGNIFICANCE_LEVEL
    intervals_touch = not (interval_a[1] < interval_b[0] or interval_b[1] < interval_a[0])
    period_gap = abs(period_a - period_b)
    result["common_period"] = {
        "period_gap_hours": float(period_gap),
        "confidence_intervals_overlap": bool(intervals_touch),
        "tolerance_hours": COMMON_PERIOD_TOLERANCE_HOURS,
        "established": bool(both_periodic and intervals_touch and period_gap <= COMMON_PERIOD_TOLERANCE_HOURS),
    }
    if not result["common_period"]["established"]:
        result["synchronization_status"] = "periods_not_comparable"
        result["reason"] = "交叠窗口内未能同时确认相容周期，不能比较相位同步"
        return result

    shared_period = (period_a + period_b) / 2.0
    _, phase_a, _, _ = fit_harmonic(window_a, rates_a, shared_period)
    _, phase_b, _, _ = fit_harmonic(window_b, rates_b, shared_period)
    point_diff = float(np.angle(np.exp(1j * ((phase_b - phase_a) / shared_period * 2 * np.pi)))
                       * shared_period / (2 * np.pi))

    # 子流由平台对确定性派生，使同一平台对的结论不受调用顺序影响、可复现。
    # 不用内置 hash()：字符串哈希带进程随机化，会破坏跨进程复现。
    identity = (left + "|" + right).encode("utf-8")
    pair_seed = np.random.SeedSequence(list(identity))
    pair_rng = np.random.default_rng(pair_seed.spawn(1)[0])
    boot_a = _bootstrap_phase(window_a, rates_a, shared_period, bootstrap_runs, pair_rng)
    boot_b = _bootstrap_phase(window_b, rates_b, shared_period, bootstrap_runs, pair_rng)
    diff_angles = np.angle(np.exp(1j * (boot_b - boot_a)))
    point_angle = np.angle(np.exp(1j * ((phase_b - phase_a) / shared_period * 2 * np.pi)))
    # 以点估计为中心展开环形角差，使分位数在同一支上计算，避免 ±π 处的环绕错误。
    unwrapped = np.angle(np.exp(1j * (diff_angles - point_angle))) + point_angle
    diffs = unwrapped * shared_period / (2 * np.pi)
    low, median, high = (float(value) for value in np.quantile(diffs, [0.025, 0.5, 0.975]))
    f_statistic, f_p_value, shared_phase = _shared_phase_test(
        (window_a, rates_a), (window_b, rates_b), shared_period
    )
    ci_contains_zero = low <= 0.0 <= high
    circular_width = float(high - low)
    precise_enough = circular_width / shared_period <= PHASE_PRECISION_LIMIT
    result["phase_synchrony"] = {
        "shared_period_hours": float(shared_period),
        "phase_difference_hours": point_diff,
        "bootstrap_median_hours": median,
        "bootstrap_95ci_hours": [low, high],
        "ci_contains_zero": bool(ci_contains_zero),
        "ci_width_hours": circular_width,
        "ci_width_fraction_of_period": circular_width / shared_period,
        "precision_limit_fraction": PHASE_PRECISION_LIMIT,
        "shared_phase_f_statistic": f_statistic,
        "shared_phase_p_value": f_p_value,
        "shared_phase_hours": shared_phase,
    }
    # 三档结论：支持同步 / 同步未决（证据不足，不能据此合并档期）/ 相位不同。
    # CI 跨 0 但过宽时显式降级为未决，让排期端保留各自节奏，而不是误判共同节奏。
    if not precise_enough:
        result["synchronization_status"] = "synchrony_inconclusive"
        result["reason"] = "共同周期成立，但相位差 95% 区间过宽，同步判断精度不足"
    elif ci_contains_zero and (f_p_value is None or f_p_value >= SIGNIFICANCE_LEVEL):
        result["synchronization_status"] = "phase_consistent"
        result["reason"] = "相位差置信区间包含零且宽度达标，同相位模型未被拒绝"
    else:
        result["synchronization_status"] = "phase_distinct"
        result["reason"] = "共同周期成立，但同相位假设被拒绝或零不在相位差置信区间内"
    return result


def trace_period(snapshot, platform, start_hour, end_hour, period_hours=None):
    """追查指定时期内候选周期由哪些观测记录支持。

    窗口按校正后时间过滤；返回窗口内每条派生记录及其对周期拟合的逐点
    贡献（余弦/正弦项与残差），以及窗口内重新拟合得到的周期与显著性，
    使任何发布结论都能回到原始读数 id、时钟规则与校正版本。
    period_hours 为空时先在窗口内重新搜索候选周期。
    """
    platform_points = rate_series(snapshot)
    if platform not in platform_points:
        raise ValueError(f"平台 {platform} 不存在")
    points = [
        point
        for point in platform_points[platform]
        if start_hour <= point["time_hour"] <= end_hour
    ]
    if len(points) < 8:
        raise ValueError("窗口内观测点不足，无法追查周期")
    times = _finite([point["time_hour"] for point in points])
    rates = _finite([point["rate"] for point in points])
    frequencies = _band_frequencies()
    power = _periodogram(times, rates, frequencies)
    searched_period = float((2.0 * np.pi / frequencies)[int(np.argmax(power))])
    period = float(period_hours) if period_hours is not None else searched_period
    amplitude, phase_hours, rss, intercept = fit_harmonic(times, rates, period)
    angular = 2.0 * np.pi / period
    fitted = intercept + amplitude * np.cos(angular * (times - phase_hours))
    contributions = []
    for point, time_value, rate_value, fitted_value in zip(points, times, rates, fitted):
        contributions.append({
            "source_readings": point["source_readings"],
            "clock_records": point["clock_records"],
            "clock_versions": point["clock_versions"],
            "time_hour": float(time_value),
            "raw_time_hour": point["raw_time_hour"],
            "observed_rate": float(rate_value),
            "fitted_rate": float(fitted_value),
            "residual": float(rate_value - fitted_value),
            "gap_spanning": point["gap_spanning"],
            "outage_ids": point["outage_ids"],
        })
    return {
        "method": METHOD,
        "platform": platform,
        "window_hours": [float(start_hour), float(end_hour)],
        "requested_period_hours": (float(period_hours) if period_hours is not None else None),
        "searched_period_hours": searched_period,
        "fitted_period_hours": period,
        "amplitude": amplitude,
        "phase_hours": phase_hours,
        "record_count": len(points),
        "cycle_count": float((times.max() - times.min()) / period),
        "supporting_records": contributions,
    }


def analyze_rhythm(snapshot, seed=20240501):
    """完整分析入口：单平台周期 + 全部平台对的交叠同步。"""
    rng = np.random.default_rng(seed)
    platform_points = rate_series(snapshot)
    platform_results = [
        analyze_platform(platform, points, rng) for platform, points in sorted(platform_points.items())
    ]
    pair_results = []
    platforms = sorted(platform_points)
    for index in range(len(platforms)):
        for other in range(index + 1, len(platforms)):
            pair_results.append(
                analyze_pair(platforms[index], platforms[other], platform_points, rng)
            )

    scheduling = []
    periodic_platforms = {
        item["platform"]
        for item in platform_results
        if item["candidate_period"]["significant"]
    }
    for pair in pair_results:
        left, right = pair["platforms"]
        status = pair["synchronization_status"]
        if status == "phase_consistent":
            guidance = "synchronized_schedule_supported"
            message = "交叠窗口内共同周期与同相位均获支持，可按共同节奏排期"
        elif status == "phase_distinct":
            guidance = "separate_phase_schedules"
            message = "共同周期存在但相位不同，应按各自相位分别排期，不要同时段投放"
        elif left in periodic_platforms and right in periodic_platforms:
            guidance = "separate_rhythms_only"
            message = "两地各自周期成立，但同步证据不足或不可评估；排期保留各自节奏，不要合并为单一档期"
        else:
            guidance = "no_shared_rhythm_claim"
            message = "至少一方周期尚未确立，不能主张共同节奏"
        scheduling.append({"platforms": [left, right], "guidance": guidance, "message": message,
                           "synchronization_status": status})

    return {
        "method": METHOD,
        "seed": seed,
        "surrogate_runs": SURROGATE_RUNS,
        "bootstrap_runs": BOOTSTRAP_RUNS,
        "significance_level": SIGNIFICANCE_LEVEL,
        "method_notes": [
            "Lomb-Scargle 周期图直接作用于不规则观测时刻，未做等距重采样或补零",
            "采样窗口谱用于识别采样节律自身制造的谱峰，sampling_induced=true 的峰不可作为内容周期",
            "时序置换代理保持观测时刻不变，在 72-336 小时内容节奏频带内做族系校正后给出周期峰显著性",
            "跨平台相位只在共同覆盖窗口内比较，相位差不确定性为成对 bootstrap 95% 区间",
            "每条派生记录保留 source_readings、clock_records、clock_versions 与原始/校正时间，可回溯资料版本",
            "停测横跨增量与短时突发被标记并参与剔除复算；结论按稳健性口径保守解释",
        ],
        "platforms": platform_results,
        "pairwise_synchrony": pair_results,
        "scheduling_guidance": scheduling,
    }
