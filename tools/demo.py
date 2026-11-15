from pathlib import Path
import json
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rhythm_observatory.client import ProductClient
from rhythm_observatory.data import load_bundle
from rhythm_observatory.service import running_service
from rhythm_observatory.store import ArtifactStore
from rhythm_observatory.workflow import publish_overview, publish_rhythm


def main():
    root = Path(__file__).resolve().parents[1]
    bundle = load_bundle(root / "fixtures" / "bundle.json")
    with tempfile.TemporaryDirectory() as directory:
        store = ArtifactStore(str(Path(directory) / "state.sqlite3"), bundle.get("policy"))
        try:
            with running_service(bundle, store) as base:
                client = ProductClient(base)
                published = publish_overview(client, "2026-01-01", "overview-demo")
                restored = client.artifact(published["artifact_id"])
                assert restored == published["artifact"]
                rhythm = publish_rhythm(client, "2026-01-01", "rhythm-demo")
                rhythm_restored = client.artifact(rhythm["artifact_id"])
                assert rhythm_restored == rhythm["artifact"]
                summary = {
                    "产品": bundle["product"],
                    "概览发布编号": published["artifact_id"],
                    "节奏分析发布编号": rhythm["artifact_id"],
                    "方法": rhythm["artifact"]["method"],
                    "平台周期": [
                        {
                            "平台": item["platform"],
                            "状态": item["candidate_period"]["status"],
                            "候选周期小时": round(item["candidate_period"]["period_hours"], 2),
                            "代理检验p值": item["candidate_period"]["surrogate_p_value"],
                            "采样诱导": item["candidate_period"]["sampling_induced"],
                        }
                        for item in rhythm["artifact"]["result"]["platforms"]
                    ],
                    "跨平台结论": [
                        {"平台对": item["platforms"], "状态": item["synchronization_status"], "原因": item["reason"]}
                        for item in rhythm["artifact"]["result"]["pairwise_synchrony"]
                    ],
                    "排期建议": [
                        {"平台对": item["platforms"], "建议": item["guidance"]}
                        for item in rhythm["artifact"]["result"]["scheduling_guidance"]
                    ],
                }
                print(json.dumps(summary, ensure_ascii=False, indent=2))
        finally:
            store.close()


if __name__ == "__main__":
    main()
