from copy import deepcopy
from pathlib import Path
import json
import unittest

import numpy as np

from rhythm_observatory.data import load_bundle, visible_snapshot
from rhythm_observatory.rhythm import (
    METHOD,
    analyze_platform,
    analyze_rhythm,
    rate_series,
    trace_period,
)


ROOT = Path(__file__).resolve().parents[1]


class RhythmFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bundle = load_bundle(ROOT / "fixtures" / "bundle.json")
        cls.snapshot = visible_snapshot(bundle, "2026-01-01")
        cls.result = analyze_rhythm(cls.snapshot)

    def test_result_is_json_safe_and_finite(self):
        encoded = json.dumps(self.result, ensure_ascii=False, allow_nan=False)
        self.assertIn("rhythm-sync-v1", encoded)

    def test_series_keeps_traceability_and_uses_corrected_time(self):
        series = rate_series(self.snapshot)
        self.assertEqual(set(series), {"p0", "p1", "p2"})
        for platform, points in series.items():
            self.assertTrue(all(point["end_hour"] > point["start_hour"] for point in points))
            self.assertTrue(all(len(point["source_readings"]) == 2 for point in points))
            self.assertTrue(all(point["clock_records"] for point in points))
        # d2 有 +1 小时时钟校正：校正时间必须整体晚于原始时间。
        p2 = series["p2"]
        self.assertTrue(all(point["time_hour"] == point["raw_time_hour"] + 1.0 for point in p2))
        # 停测横跨增量被标记并保留停测记录 id。
        p1_gaps = [point for point in series["p1"] if point["gap_spanning"]]
        self.assertEqual(len(p1_gaps), 1)
        self.assertEqual(p1_gaps[0]["outage_ids"], ["gap-p1"])

    def test_weekly_period_is_content_signal_not_sampling_peak(self):
        for platform in self.result["platforms"]:
            candidate = platform["candidate_period"]
            self.assertEqual(candidate["status"], "period_detected")
            self.assertAlmostEqual(candidate["period_hours"], 168.0, delta=6.0)
            self.assertLess(candidate["surrogate_p_value"], 0.05)
            self.assertFalse(candidate["sampling_induced"])
            self.assertLess(candidate["sampling_window_power"], 0.5)
            self.assertGreaterEqual(candidate["cycles_observed"], 3.0)

    def test_supporting_records_trace_back_to_readings(self):
        visible_ids = {
            row["id"] for row in self.snapshot["tables"]["readings"]
        }
        for platform in self.result["platforms"]:
            self.assertTrue(platform["supporting_records"])
            for record in platform["supporting_records"]:
                self.assertTrue(set(record["source_readings"]).issubset(visible_ids))
                self.assertIn("raw_time_hour", record)
                self.assertIn("time_hour", record)

    def test_synchrony_is_downgraded_when_phase_uncertainty_is_wide(self):
        for pair in self.result["pairwise_synchrony"]:
            self.assertTrue(pair["common_period"]["established"])
            # 现有资料共同周期成立但相位精度不足以宣称同步。
            self.assertEqual(pair["synchronization_status"], "synchrony_inconclusive")
            self.assertGreater(
                pair["phase_synchrony"]["ci_width_fraction_of_period"],
                pair["phase_synchrony"]["precision_limit_fraction"],
            )
        guidance = {tuple(item["platforms"]): item["guidance"] for item in self.result["scheduling_guidance"]}
        self.assertTrue(all(value == "separate_rhythms_only" for value in guidance.values()))

    def test_overlap_window_is_actual_intersection(self):
        series = rate_series(self.snapshot)
        for pair in self.result["pairwise_synchrony"]:
            left, right = pair["platforms"]
            starts = [min(point["time_hour"] for point in series[name]) for name in (left, right)]
            ends = [max(point["time_hour"] for point in series[name]) for name in (left, right)]
            self.assertEqual(pair["overlap_window_hours"][0], max(starts))
            self.assertEqual(pair["overlap_window_hours"][1], min(ends))

    def test_snapshot_is_not_mutated(self):
        before = deepcopy(self.snapshot)
        analyze_rhythm(self.snapshot)
        self.assertEqual(self.snapshot, before)

    def test_period_trace_returns_source_records_in_window(self):
        trace = trace_period(self.snapshot, "p1", 300.0, 1000.0, period_hours=168.0)
        self.assertEqual(trace["method"], METHOD)
        self.assertTrue(trace["supporting_records"])
        self.assertTrue(all(300.0 <= record["time_hour"] <= 1000.0
                            for record in trace["supporting_records"]))
        visible_readings = {row["id"] for row in self.snapshot["tables"]["readings"]}
        visible_clocks = {row["id"] for row in self.snapshot["tables"]["clocks"]}
        for record in trace["supporting_records"]:
            self.assertTrue(set(record["source_readings"]).issubset(visible_readings))
            self.assertTrue(set(record["clock_records"]).issubset(visible_clocks))
        gap_records = [record for record in trace["supporting_records"] if record["gap_spanning"]]
        self.assertEqual(gap_records[0]["outage_ids"], ["gap-p1"])
        json.dumps(trace, allow_nan=False)

    def test_period_trace_searches_when_period_not_given(self):
        trace = trace_period(self.snapshot, "p2", 0.0, 1200.0)
        self.assertAlmostEqual(trace["fitted_period_hours"], 168.0, delta=8.0)
        self.assertGreaterEqual(trace["cycle_count"], 3.0)

    def test_period_trace_rejects_unknown_platform_and_tiny_window(self):
        with self.assertRaises(ValueError):
            trace_period(self.snapshot, "nope", 0.0, 1000.0)
        with self.assertRaises(ValueError):
            trace_period(self.snapshot, "p0", 0.0, 10.0)


class SyntheticControlTests(unittest.TestCase):
    def _snapshot(self, specs, nominal=(11, 13, 15)):
        """构造带真实时钟校正、抖动采样时刻的合成累计计数器。

        累计增量按区间真实积分生成，使区间中点的平均率近似瞬时率；
        两个平台在同相位设定下采样网格不同，用于检验方法不依赖共同网格。
        """
        rng = np.random.default_rng(17)
        readings, clocks = [], []

        def instantaneous(corrected, period, phase, amplitude):
            return 6.0 + amplitude * np.cos(2 * np.pi * (corrected - phase) / period)

        for index, (platform, device, period, phase, amplitude, correction) in enumerate(specs):
            clocks.append({
                "id": f"clock-{index}", "revision": 1, "released_at": "2024-01-01",
                "device": device, "raw_from": 0, "raw_to": 20000,
                "correction_hours": correction, "clock_version": "cv-1",
            })
            raw_time, cumulative, row_id, first = 2.0, 0.0, 0, True
            while raw_time < 2400:
                step = nominal[index % 3] + int(rng.integers(-2, 3))
                previous_time = raw_time
                raw_time += step
                # 对区间内若干子点做梯形积分，减少区间平均带来的相位偏移。
                grid = np.linspace(previous_time, raw_time, 7) + correction
                rates = np.array([
                    max(0.1, instantaneous(moment, period, phase, amplitude) + rng.normal(0, 0.4))
                    for moment in grid
                ])
                cumulative += np.trapezoid(rates, grid)
                readings.append({
                    "id": f"{platform}-{row_id}", "revision": 1, "released_at": "2024-01-02",
                    "platform": platform, "device": device, "raw_hour": raw_time,
                    "interval_hours": nominal[index % 3], "cumulative": round(cumulative, 3),
                    "reset": first, "clock_version": "cv-1",
                })
                first, row_id = False, row_id + 1
        return {
            "schema_version": 1, "product": "synthetic", "as_of": "2026-01-01",
            "snapshot_id": "synthetic",
            "tables": {"readings": readings, "clocks": clocks, "outages": []},
        }

    def test_identical_phase_is_declared_synchronized(self):
        snapshot = self._snapshot([
            ("a", "da", 168.0, 40.0, 1.6, 0.0),
            ("b", "db", 168.0, 40.0, 1.6, 0.0),
        ])
        result = analyze_rhythm(snapshot)
        pair = result["pairwise_synchrony"][0]
        self.assertEqual(pair["synchronization_status"], "phase_consistent")
        self.assertAlmostEqual(pair["phase_synchrony"]["phase_difference_hours"], 0.0, delta=6.0)

    def test_opposite_phase_is_declared_distinct(self):
        snapshot = self._snapshot([
            ("a", "da", 168.0, 10.0, 1.6, 0.0),
            ("b", "db", 168.0, 94.0, 1.6, 0.0),
        ])
        result = analyze_rhythm(snapshot)
        pair = result["pairwise_synchrony"][0]
        self.assertEqual(pair["synchronization_status"], "phase_distinct")
        self.assertFalse(pair["phase_synchrony"]["ci_contains_zero"])

    def test_pure_noise_has_no_period_and_no_synchrony_claim(self):
        snapshot = self._snapshot([
            ("a", "da", 168.0, 0.0, 0.0, 0.0),
            ("b", "db", 168.0, 0.0, 0.0, 0.0),
        ])
        result = analyze_rhythm(snapshot)
        self.assertTrue(all(
            platform["candidate_period"]["status"] == "period_not_established"
            for platform in result["platforms"]
        ))
        self.assertEqual(
            result["pairwise_synchrony"][0]["synchronization_status"], "periods_not_comparable"
        )
        self.assertEqual(
            result["scheduling_guidance"][0]["guidance"], "no_shared_rhythm_claim"
        )

    def test_regular_sampling_harmonic_is_visible_in_window_spectrum(self):
        rng = np.random.default_rng(31)
        readings, clocks, raw_time, cumulative = [], [], 0.0, 0.0
        clocks.append({
            "id": "clock-0", "revision": 1, "released_at": "2024-01-01", "device": "da",
            "raw_from": 0, "raw_to": 20000, "correction_hours": 0.0, "clock_version": "cv-1",
        })
        for row_id in range(120):
            raw_time += 96.0
            cumulative += max(0.1, 6.0 + rng.normal(0, 1.0)) * 96
            readings.append({
                "id": f"a-{row_id}", "revision": 1, "released_at": "2024-01-02",
                "platform": "a", "device": "da", "raw_hour": raw_time,
                "interval_hours": 96, "cumulative": round(cumulative, 3),
                "reset": row_id == 0, "clock_version": "cv-1",
            })
        snapshot = {
            "schema_version": 1, "product": "synthetic", "as_of": "2026-01-01",
            "snapshot_id": "synthetic",
            "tables": {"readings": readings, "clocks": clocks, "outages": []},
        }
        points = rate_series(snapshot)["a"]
        times = np.array([point["time_hour"] for point in points])
        rates = np.array([point["rate"] for point in points])
        platform = analyze_platform("a", points, np.random.default_rng(1), surrogate_runs=100)
        # 无信号的规则采样噪声不能被判为内容周期。
        self.assertEqual(platform["candidate_period"]["status"], "period_not_established")
        self.assertGreaterEqual(platform["candidate_period"]["surrogate_p_value"], 0.05)


if __name__ == "__main__":
    unittest.main()
