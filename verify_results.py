"""只读核验已保存的BTC结果；仅使用Python标准库，不读取密钥或调用模型。"""

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def close(left, right):
    return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-10)


def verify(root):
    base = root / "results/formal/baseline"
    manifest = read_json(base / "experiment.json")["fingerprint"]
    for name, checksum in manifest["source_sha256"].items():
        require(sha256(root / name) == checksum, f"实验源码指纹变化：{name}")
    for name, checksum in manifest["data_sha256"].items():
        require(sha256(root / "benchmark/btc" / name) == checksum, f"官方数据变化：{name}")

    scores = {}
    patterns = [0, 0, 0]
    trends = [0, 0, 0]
    fields = ("indicator_report", "pattern_report", "trend_report", "final_trade_decision")
    for repeat in range(1, 4):
        for sample in range(1, 101):
            folder = base / f"repeat_{repeat:02d}" / f"BTC_4h_{sample}"
            marker = read_json(folder / "completed.json")
            attempt = folder / marker["attempt"]
            require(not (attempt / "error.json").exists(), f"成功尝试存在错误记录：{attempt}")
            for name in ("history.csv", "future.csv", "reports.json"):
                require(sha256(attempt / name) == marker["artifact_sha256"][name], f"原始结果被更改：{attempt / name}")

            history, future = read_csv(attempt / "history.csv"), read_csv(attempt / "future.csv")
            original = read_csv(root / "benchmark/btc" / f"BTC_4h_{sample}.csv")
            require((len(history), len(future), len(original)) == (97, 3, 100), f"数据行数不符：{folder}")
            for actual, expected in zip(history + future, original):
                require(actual["Datetime"] == expected["Datetime"], f"日期不一致：{folder}")
                for field in ("Open", "High", "Low", "Close", "Volume"):
                    require(close(actual[field], expected[field]), f"价格/成交量不一致：{folder} {field}")

            reports = read_json(attempt / "reports.json")
            require(all(isinstance(reports.get(field), str) and reports[field].strip() for field in fields), f"缺少报告：{attempt}")
            text = reports["final_trade_decision"]
            decision = json.loads(text[text.find("{"):text.rfind("}") + 1])["decision"].strip().upper()
            require(decision in ("LONG", "SHORT"), f"决策无效：{attempt}")
            reference = float(history[-1]["Close"])
            prices = [float(row["Close"]) for row in future]
            hits = sum(price > reference if decision == "LONG" else price < reference for price in prices)
            saved = marker["score"]
            require(saved["prediction"] == decision and saved["hits"] == hits and saved["comparisons"] == 3, f"方向/命中不一致：{folder}")
            require(close(saved["reference_close"], reference), f"参考价不一致：{folder}")
            require(len(saved["future_closes"]) == 3 and all(close(a, b) for a, b in zip(saved["future_closes"], prices)), f"验证价不一致：{folder}")
            require(close(saved["accuracy_percent"], hits / 3 * 100), f"样本准确率不一致：{folder}")
            require((attempt / "decision_prompt.txt").is_file(), f"缺少决策提示词：{attempt}")
            scores[repeat, sample] = (decision, hits)
            for name, counts in (("kline_chart.png", patterns), ("trend_graph.png", trends)):
                image = attempt / name
                if image.is_file():
                    with image.open("rb") as stream:
                        require(stream.read(8) == b"\x89PNG\r\n\x1a\n", f"PNG格式无效：{image}")
                    counts[repeat - 1] += 1

    details = read_csv(base / "scores.csv")
    require(len(details) == 300, "scores.csv必须有300行")
    seen = set()
    for row in details:
        key = (int(row["轮次"]), int(row["样本"]))
        require(key in scores and key not in seen, f"明细样本重复或无效：{key}")
        seen.add(key)
        decision, hits = scores[key]
        require(row["预测"] == decision and int(row["命中数"]) == hits and int(row["比较次数"]) == 3, f"明细计分不一致：{key}")
        require(abs(float(row["准确率(%)"]) - hits / 3 * 100) < 0.0001, f"明细准确率不一致：{key}")

    hits_by_round = [sum(hits for (r, sample), (decision, hits) in scores.items() if r == repeat) for repeat in range(1, 4)]
    accuracies = [hits / 300 * 100 for hits in hits_by_round]
    summary = read_json(base / "summary.json")
    require(summary["complete"] is True and summary["completed_samples"] == 300 and summary["expected_samples"] == 300, "汇总没有完成300次")
    round_csv = read_csv(base / "round_summary.csv")
    require(len(summary["rounds"]) == len(round_csv) == 3, "轮次汇总数量不符")
    for i, (item, row) in enumerate(zip(summary["rounds"], round_csv)):
        require(item["轮次"] == int(row["轮次"]) == i + 1, "轮次编号不一致")
        require(item["已完成样本"] == int(row["已完成样本"]) == 100, "轮次完成数不一致")
        require(item["命中数"] == int(row["命中数"]) == hits_by_round[i], "轮次命中不一致")
        require(abs(item["正式准确率(%)"] - accuracies[i]) < 0.0001 and abs(float(row["正式准确率(%)"]) - accuracies[i]) < 0.0001, "轮次准确率不一致")
    for name, value in (("mean_accuracy_percent", statistics.mean(accuracies)), ("std_accuracy_percentage_points", statistics.stdev(accuracies)), ("variance_percentage_points_squared", statistics.variance(accuracies))):
        require(close(summary[name], value), f"三轮统计不一致：{name}")

    print("核验通过：300/300；源码、数据、报告hash、评分与汇总一致。")
    for i, (hits, accuracy) in enumerate(zip(hits_by_round, accuracies), 1):
        print(f"第{i}轮：{hits}/300，准确率 {accuracy:.4f}%")
    print(f"三轮平均：{statistics.mean(accuracies):.4f}%")
    print(f"样本标准差：{statistics.stdev(accuracies):.4f} 个百分点")
    print(f"样本方差：{statistics.variance(accuracies):.4f} 平方百分点")
    print(f"成功结果中的形态图：{sum(patterns)}/300；趋势图：{sum(trends)}/300")
    print("此核验确认保存与计分一致，不保证全部报告完成看图或预测范围一致。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    try:
        verify(args.root.resolve())
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"核验失败：{error}", file=sys.stderr)
        sys.exit(1)
