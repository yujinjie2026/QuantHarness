"""BTC 4小时正式实验：100个样本 × 3轮，自动评分、汇总和断点续跑。"""

import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
import platform
import statistics
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
SAMPLES = 100
REPEATS = 3
FIELDS = (
    "indicator_report", "pattern_report", "trend_report", "final_trade_decision"
)
PROMPT_POLICY = "official agent source unchanged; no additional prompt instructions"


def stamp():
    return datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat(timespec="seconds")


def save_json(path, data):
    """先写临时文件，再替换，减少中断造成的文件损坏。"""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


@contextmanager
def project_lock():
    """运行与清理使用同一个项目锁，防止同时写入或清空结果。"""
    with (ROOT / ".run_btc.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("项目正在运行实验或清理结果，请勿重复启动")
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_sample(sample_id):
    path = ROOT / "benchmark" / "btc" / f"BTC_4h_{sample_id}.csv"
    data = pd.read_csv(path)
    columns = ["Open", "High", "Low", "Close", "Volume"]
    if len(data) != 100 or not set(columns + ["Datetime"]).issubset(data.columns):
        raise ValueError(f"{path.name} 必须包含100行及Datetime、OHLCV列")
    for column in columns:
        data[column] = pd.to_numeric(data[column], errors="raise")
        if not data[column].map(math.isfinite).all():
            raise ValueError(f"{path.name} 的 {column} 存在空值或无效数值")
    dates = pd.to_datetime(data["Datetime"], errors="raise")
    # 官方CSV的少量历史时间戳间隔是3或5小时；保留原始行序，不重采样。
    if dates.isna().any() or dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise ValueError(f"{path.name} 存在无效、重复或倒序的时间戳")
    return data.iloc[:97].copy(), data.iloc[97:].copy()


def fingerprint():
    """只记录公开配置和文件指纹，不保存.env及API密钥。"""
    from default_config import DEFAULT_CONFIG, DEEPSEEK_RUNTIME_CONFIG

    source_names = (
        "run_btc.py", "trading_graph.py", "default_config.py", "graph_setup.py",
        "agent_state.py", "indicator_agent.py", "pattern_agent.py",
        "trend_agent.py", "decision_agent.py", "graph_util.py",
        "static_util.py", "color_style.py", "deepseek_adapter.py",
    )
    packages = {}
    for name in ("pandas", "langchain", "langchain-core", "langchain-openai",
                 "langgraph", "openai", "TA-Lib"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = "not-installed"
    return {
        "samples": SAMPLES,
        "repeats": REPEATS,
        "history_bars": 97,
        "future_bars": 3,
        "timestamp_policy": "preserve official rows; no resampling or timezone conversion",
        "prompt_policy": PROMPT_POLICY,
        "scoring": "compare each future close with last historical close; ties miss",
        "model": os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "config": {key: DEFAULT_CONFIG.get(key) for key in (
            "agent_llm_provider", "graph_llm_provider", "agent_llm_model",
            "graph_llm_model", "agent_llm_temperature", "graph_llm_temperature"
        )},
        "deepseek_runtime": dict(DEEPSEEK_RUNTIME_CONFIG),
        "temperature_sent": None,
        "temperature_note": "not supported in thinking mode; original nominal 0.1 is not sent",
        "top_p_sent": None,
        "top_p_note": "provider default (deepseek-flash thinking mode: 1.0)",
        "python": platform.python_version(),
        "packages": packages,
        "source_sha256": {name: digest(ROOT / name) for name in source_names},
        "data_sha256": {
            f"BTC_4h_{i}.csv": digest(ROOT / "benchmark/btc" / f"BTC_4h_{i}.csv")
            for i in range(1, SAMPLES + 1)
        },
    }


def build_graph():
    """直接构造官方四代理流程，不包装或追加任何提示词。"""
    from trading_graph import TradingGraph
    return TradingGraph()


def score(reports, history, future):
    for field in FIELDS:
        if not isinstance(reports.get(field), str) or not reports[field].strip():
            raise ValueError(f"缺少有效的 {field}")
    text = reports["final_trade_decision"]
    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError("最终决策没有JSON对象")
    prediction = json.loads(text[start:end])["decision"].strip().upper()
    reference = float(history.iloc[-1]["Close"])
    future_closes = [float(value) for value in future["Close"]]
    if prediction == "LONG":
        hits = sum(value > reference for value in future_closes)
    elif prediction == "SHORT":
        hits = sum(value < reference for value in future_closes)
    else:
        raise ValueError("最终预测必须是LONG或SHORT")
    return {
        "prediction": prediction, "reference_close": reference,
        "future_closes": future_closes, "hits": hits, "comparisons": 3,
        "accuracy_percent": hits / 3 * 100,
    }


def read_completed(folder, history, future):
    """完成标记存在且原始结果重新评分一致，才允许跳过。"""
    marker = folder / "completed.json"
    if not marker.exists():
        return None
    completed = json.loads(marker.read_text(encoding="utf-8"))
    attempt = folder / completed["attempt"]
    for name in ("history.csv", "future.csv", "reports.json"):
        if digest(attempt / name) != completed["artifact_sha256"][name]:
            raise ValueError(f"已完成结果被修改：{attempt / name}")
    reports = json.loads((attempt / "reports.json").read_text(encoding="utf-8"))
    result = score(reports, history, future)
    if result != completed["score"]:
        raise ValueError(f"完成标记与重新评分不一致：{folder}")
    return result


def run_sample(folder, sample_id, repeat, history, future):
    folder.mkdir(parents=True, exist_ok=True)
    number = 1
    while (folder / f"attempt_{number:03d}").exists():
        number += 1
    attempt = folder / f"attempt_{number:03d}"
    attempt.mkdir()
    history.to_csv(attempt / "history.csv", index=False)
    future.to_csv(attempt / "future.csv", index=False)
    started = time.monotonic()
    reports = {}
    graph = None
    save_json(attempt / "metadata.json", {
        "sample": sample_id, "repeat": repeat, "started_at": stamp(),
        "history_bars": 97, "future_bars": 3,
        "prompt_policy": PROMPT_POLICY,
    })
    try:
        with working_directory(attempt):
            graph = build_graph()
            # 验证数据future不会传给模型或任何代理。
            state = {
                "kline_data": history.to_dict(orient="list"),
                "messages": [], "time_frame": "4hour", "stock_name": "BTC",
            }
            for update in graph.graph.stream(state, stream_mode="updates"):
                for agent_name, result in update.items():
                    print(f"    已完成：{agent_name}", flush=True)
                    for field in FIELDS:
                        if field in result:
                            reports[field] = result[field]
                    save_json(attempt / "reports.json", reports)
                    if result.get("decision_prompt"):
                        (attempt / "decision_prompt.txt").write_text(
                            result["decision_prompt"],
                            encoding="utf-8",
                        )
        result = score(reports, history, future)
        save_json(folder / "completed.json", {
            "attempt": attempt.name, "finished_at": stamp(),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "score": result,
            "artifact_sha256": {
                name: digest(attempt / name)
                for name in ("history.csv", "future.csv", "reports.json")
            },
        })
        return result
    except BaseException as error:
        message = str(error)
        key = os.getenv("DEEPSEEK_API_KEY", "")
        if key:
            message = message.replace(key, "[密钥已隐藏]")
        save_json(attempt / "error.json", {
            "time": stamp(), "type": type(error).__name__, "message": message,
            "traceback": [{"file": f.filename, "line": f.lineno, "function": f.name}
                          for f in traceback.extract_tb(error.__traceback__)],
        })
        raise
    finally:
        seen = set()
        for name in ("agent_llm", "graph_llm"):
            llm = getattr(graph, name, None)
            if llm is None or id(llm) in seen:
                continue
            seen.add(id(llm))
            close = getattr(llm, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as error:
                    save_json(attempt / "cleanup_error.json", {
                        "time": stamp(), "type": type(error).__name__,
                    })


def summarize(output, scores):
    """未完成的轮次显示进度；只为完整轮次计算正式准确率。"""
    details = []
    rounds = []
    accuracies = []
    for repeat in range(1, REPEATS + 1):
        items = [(sample, result) for (r, sample), result in sorted(scores.items())
                 if r == repeat]
        hits = sum(result["hits"] for _, result in items)
        complete = len(items) == SAMPLES
        accuracy = hits / (SAMPLES * 3) * 100 if complete else None
        if complete:
            accuracies.append(accuracy)
        rounds.append({
            "轮次": repeat, "已完成样本": len(items), "应完成样本": SAMPLES,
            "命中数": hits, "已完成比较次数": len(items) * 3,
            "正式准确率(%)": round(accuracy, 4) if complete else "",
            "状态": "完成" if complete else "未完成",
        })
        for sample, result in items:
            details.append({
                "轮次": repeat, "样本": sample, "预测": result["prediction"],
                "命中数": result["hits"], "比较次数": 3,
                "准确率(%)": round(result["accuracy_percent"], 4),
            })
    for name, rows, columns in (
        ("scores.csv", details, ["轮次", "样本", "预测", "命中数", "比较次数", "准确率(%)"]),
        ("round_summary.csv", rounds, list(rounds[0])),
    ):
        path = output / name
        temporary = path.with_name(name + ".tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(path)
    summary = {"completed_samples": len(scores), "expected_samples": SAMPLES * REPEATS,
               "rounds": rounds, "complete": len(accuracies) == REPEATS}
    if summary["complete"]:
        summary.update({
            "mean_accuracy_percent": statistics.mean(accuracies),
            "std_accuracy_percentage_points": statistics.stdev(accuracies),
            "variance_percentage_points_squared": statistics.variance(accuracies),
            "standard_deviation_definition": "sample standard deviation, ddof=1",
        })
    save_json(output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", default="baseline", choices=["baseline"],
                        help="baseline是本次DeepSeek完整系统实验的保存名称")
    parser.add_argument("--resume", action="store_true", help="继续同一个实验，跳过成功样本")
    args = parser.parse_args()
    with project_lock():
        run_experiment(args)


def run_experiment(args):
    load_dotenv(ROOT / ".env")
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        raise ValueError("请先在项目的.env中填写DEEPSEEK_API_KEY")

    # 付费调用前，先完整检查全部100个样本。
    samples = {i: load_sample(i) for i in range(1, SAMPLES + 1)}
    current = fingerprint()
    output = ROOT / "results" / "formal" / args.experiment
    manifest_path = output / "experiment.json"
    # 防止同时启动两份程序，重复付费或写入同一个实验目录。
    output.mkdir(parents=True, exist_ok=True)
    lock = (output / ".running.lock").open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise ValueError("这个实验已经在另一个终端运行，请等待它完成")
    scores = {}
    try:
        if manifest_path.exists():
            if not args.resume:
                raise ValueError("这个实验已经存在。继续运行请在命令末尾添加 --resume")
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            if saved["fingerprint"] != current:
                raise ValueError("代码、数据、模型或环境已变化，不能混入原实验。请核对保存的experiment.json；不同配置需要分开记录")
        else:
            if any(path.name != ".running.lock" for path in output.iterdir()):
                raise ValueError(f"目录已有文件但缺少实验记录，请先检查：{output}")
            save_json(manifest_path, {"experiment": args.experiment,
                                     "created_at": stamp(), "fingerprint": current})
        # 先核验并读回以前完成的结果，不调用API。
        for repeat in range(1, REPEATS + 1):
            for sample_id, (history, future) in samples.items():
                folder = output / f"repeat_{repeat:02d}" / f"BTC_4h_{sample_id}"
                previous = read_completed(folder, history, future)
                if previous is not None:
                    scores[repeat, sample_id] = previous
        summarize(output, scores)
        print(f"实验：{args.experiment}；100个样本 × 3轮", flush=True)
        print(f"已完成：{len(scores)}/300；结果：{output}", flush=True)
        for repeat in range(1, REPEATS + 1):
            for sample_id, (history, future) in samples.items():
                if (repeat, sample_id) in scores:
                    continue
                print(f"\n第{repeat}/3轮，第{sample_id}/100个样本", flush=True)
                folder = output / f"repeat_{repeat:02d}" / f"BTC_4h_{sample_id}"
                result = run_sample(folder, sample_id, repeat, history, future)
                scores[repeat, sample_id] = result
                summarize(output, scores)
                print(f"    样本完成：{result['prediction']}，命中{result['hits']}/3；"
                      f"总进度{len(scores)}/300", flush=True)
        final = summarize(output, scores)
        print("\n正式实验全部完成。", flush=True)
        for row in final["rounds"]:
            print(f"第{row['轮次']}轮准确率：{row['正式准确率(%)']:.4f}%")
        print(f"三轮平均准确率：{final['mean_accuracy_percent']:.4f}%")
        print(f"样本标准差：{final['std_accuracy_percentage_points']:.4f}个百分点")
        print(f"结果目录：{output}")
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止；成功样本已保留。重新执行带 --resume 的相同命令即可继续。")
        sys.exit(130)
    except Exception as error:
        message = str(error)
        key = os.getenv("DEEPSEEK_API_KEY", "")
        if key:
            message = message.replace(key, "[密钥已隐藏]")
        print(f"\n运行停止：{message}\n已完成结果保留；解决问题后用 --resume 继续。",
              file=sys.stderr)
        sys.exit(1)
