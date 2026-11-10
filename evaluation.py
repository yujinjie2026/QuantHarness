import json
from pathlib import Path

import pandas as pd


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    rows = []

    for folder in sorted((root / "results").glob("BTC_4h_*")):
        if not folder.is_dir():
            continue

        row = {"运行目录": folder.name}

        try:
            reports = json.loads(
                (folder / "reports.json").read_text(encoding="utf-8")
            )

            # 从模型输出中提取最终预测
            text = reports["final_trade_decision"]
            start = text.find("{")
            end = text.rfind("}") + 1
            decision = json.loads(text[start:end])["decision"].strip().upper()

            history = pd.read_csv(folder / "history.csv")
            future = pd.read_csv(folder / "future.csv")

            if len(history) != 97 or len(future) != 3:
                raise ValueError("历史数据应为97行，验证数据应为3行")

            reference = history.iloc[-1]["Close"]

            if decision == "LONG":
                hits = int((future["Close"] > reference).sum())
            elif decision == "SHORT":
                hits = int((future["Close"] < reference).sum())
            else:
                raise ValueError("预测必须是 LONG 或 SHORT")

            row.update({
                "预测": decision,
                "命中数": hits,
                "比较次数": 3,
                "准确率(%)": round(hits / 3 * 100, 2),
                "状态": "评分完成",
            })

        except Exception as error:
            row["状态"] = f"未评分：{error}"

        rows.append(row)

    summary = pd.DataFrame(rows)
    summary.to_csv(
        root / "results" / "scores.csv",
        index=False,
        encoding="utf-8-sig",
    )

    print(summary.to_string(index=False))
    print("\n评分表已保存到 results/scores.csv")