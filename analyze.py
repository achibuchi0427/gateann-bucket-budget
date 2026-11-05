#!/usr/bin/env python3
"""复核原始证据，按策略和分桶汇总三次测量；仅 --plots 需要 matplotlib。"""
import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

from policy import BUCKETS, POLICIES
from run import validate_result

METRICS = {"recall_percent": 1, "mean_latency_us": 1, "qps": 1, "mean_ios": 1,
           "peak_rss_mib": 1/1024, "filter_skips": 1, "io_time_us": 1,
           "tunnel_time_us": 1, "process_time_us": 1}


def summarize(exp):
    settings = json.loads((exp/"settings.json").read_text())
    plan = json.loads((exp/"plan.json").read_text())
    status = json.loads((exp/"status.json").read_text())
    if status.get("status") != "complete":
        raise ValueError("实验尚未完成")
    with (exp/"results.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != len(plan) or status["validated_rows"] != len(plan):
        raise ValueError("结果行数与完整实验计划不一致")
    by_order = {int(row["order"]): row for row in rows}
    if len(by_order) != len(rows):
        raise ValueError("重复结果序号")
    groups = defaultdict(list)
    for item in plan:
        row = by_order[item["order"]]
        for key in ("policy", "bucket", "L", "repeat", "run_id"):
            if row[key] != str(item[key]):
                raise ValueError(f"结果被修改或与计划不符：{key}")
        evidence = (exp/row["evidence_dir"]).resolve()
        if exp.resolve() not in evidence.parents:
            raise ValueError("证据路径超出实验目录")
        original = validate_result(evidence, item, settings["tier"], settings["integration"]["gateann_commit"])
        if any(row.get(key) != value for key, value in original.items()):
            raise ValueError("汇总行与原始 CSV 不一致")
        groups[row["policy"], row["bucket"]].append(row)
    summary = []
    for bucket in BUCKETS:
        for policy in POLICIES:
            values = groups[policy, bucket]
            if len(values) != settings["repeats"] or len({v["L"] for v in values}) != 1:
                raise ValueError("分组缺少重复测量或 L 不一致")
            result = {"policy": policy, "bucket": bucket, "L": values[0]["L"], "repeats": len(values)}
            for metric, scale in METRICS.items():
                key = "peak_rss_kb" if metric == "peak_rss_mib" else metric
                x = [float(row[key])*scale for row in values]
                result.update({metric+"_median": statistics.median(x),
                               metric+"_min": min(x), metric+"_max": max(x)})
            summary.append(result)
    return summary


def plot(summary, exp):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError("CSV 已生成。可安装 python3-matplotlib 后重新加 --plots 绘图") from error
    fig, axes = plt.subplots(2, 2, figsize=(10, 7))
    metrics = [("recall_percent", "Recall@10 (%)"), ("mean_latency_us", "Mean query latency (us)"),
               ("mean_ios", "Mean logical I/O / query"), ("peak_rss_mib", "Process peak RSS (MiB)")]
    colors = ("#8796A5", "#C66A35", "#18798C")
    lookup = {(r["policy"], r["bucket"]): r for r in summary}
    for ax, (metric, label) in zip(axes.flat, metrics):
        for i, (policy, color) in enumerate(zip(POLICIES, colors)):
            ds = [lookup[policy, b] for b in BUCKETS]
            y = [d[metric+"_median"] for d in ds]
            err = [[yy-d[metric+"_min"] for d, yy in zip(ds, y)],
                   [d[metric+"_max"]-yy for d, yy in zip(ds, y)]]
            ax.bar([j+(i-1)*.25 for j in range(3)], y, width=.24, yerr=err,
                   color=color, label=policy, capsize=3)
        ax.set_xticks(range(3), [b.title() for b in BUCKETS])
        ax.set_ylabel(label);ax.set_ylim(bottom=0)
        ax.spines[["right", "top"]].set_visible(False)
        if metric == "recall_percent":ax.set_ylim(0, 105)
    fig.legend(*axes.flat[0].get_legend_handles_labels(), loc="upper center", ncol=3, frameon=False)
    fig.tight_layout(rect=(0, 0, 1, .94))
    fig.savefig(exp/"policy_comparison.png", dpi=220)
    fig.savefig(exp/"policy_comparison.pdf")
    plt.close(fig)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("experiment_dir", type=Path)
    p.add_argument("--plots", action="store_true")
    args = p.parse_args()
    exp = args.experiment_dir.resolve()
    summary = summarize(exp)
    with (exp/"summary.csv").open("w", newline="", encoding="utf-8") as handle:
        w = csv.DictWriter(handle, fieldnames=list(summary[0]));w.writeheader();w.writerows(summary)
    lines = ["# GateANN 分桶预算补充实验结果", "", "各项为独立重复测量的中位数 [最小值, 最大值]。", "",
             "|分桶|策略|L|Recall (%)|查询延迟 (us)|逻辑 I/O/查询|峰值 RSS (MiB)|",
             "|---|---|---:|---:|---:|---:|---:|"]
    for row in summary:
        fields = [row["bucket"], row["policy"], row["L"]]
        for m in ("recall_percent", "mean_latency_us", "mean_ios", "peak_rss_mib"):
            fields.append(f'{row[m+"_median"]:.2f} [{row[m+"_min"]:.2f}, {row[m+"_max"]:.2f}]')
        lines.append("|"+"|".join(map(str, fields))+"|")
    lines.extend(["", "注意：L 相同的策略在同一分桶上的差别属于重复运行差异，不能归因于新的搜索算法。",
        "Recall 不同的结果不能直接解释为等质量加速。没有预设优化一定有效。",
        "查询延迟沿用课程适配器的 1000000/QPS 换算；QPS 原始输出可能已取整。",
        "I/O 是程序逻辑计数；RSS 是每个独立查询进程全生命周期峰值。",
        "三轮的最小—最大范围不是置信区间。不要对三个分桶的 QPS 直接取平均。"])
    (exp/"result_table.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    if args.plots:plot(summary, exp)
    print(f"已复核全部原始证据并生成：{exp/'summary.csv'} 和 result_table.md")


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, RuntimeError) as error:
        raise SystemExit(f"ERROR: {error}")
