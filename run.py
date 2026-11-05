#!/usr/bin/env python3
"""调用原课程适配器；每个配置独立启动查询进程，保留证据并支持断点续跑。"""
import argparse
import csv
import hashlib
import importlib.util
import json
import math
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from policy import BUDGETS, schedule

HERE = Path(__file__).resolve().parent


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot():
    """虚拟机内的瞬时状态，不代表宿主机状态，也不代替持续监控。"""
    result = {"time": datetime.now().astimezone().isoformat()}
    for name in ("loadavg", "meminfo"):
        path = Path("/proc") / name
        result[name] = path.read_text() if path.exists() else "unavailable"
    commands = {"processes": ["ps", "-eo", "pid,comm,pcpu,pmem", "--sort=-pcpu"],
                "uname": ["uname", "-a"]}
    for name, command in commands.items():
        try:
            p = subprocess.run(command, text=True, capture_output=True, check=False)
            result[name] = "\n".join(p.stdout.splitlines()[:21])
        except OSError as e:
            result[name] = str(e)
    return result


def logged(command, log):
    print("$ " + shlex.join(command), flush=True)
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            output.write(line)
            print(line, end="", flush=True)
        code = process.wait()
    if code:
        raise RuntimeError(f"命令退出码 {code}；请查看 {log}")


def validate_result(raw_dir, item, tier, pin):
    record = json.loads((raw_dir / "command.json").read_text())
    info = record["dataset"]
    expected_n = 1000000 if tier == "formal_1000000" else 100000
    if record.get("exit_code") != 0 or info["base_size"] != expected_n:
        raise ValueError("退出状态或基础数据规模异常")
    if info["query_count"] != 200 or info["dimension"] != 192:
        raise ValueError("查询数量或向量维数异常")
    if record.get("system") != "gateann" or record.get("tier") != tier or record.get("bucket") != item["bucket"]:
        raise ValueError("原始命令记录与计划不一致")
    with (raw_dir / "results.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 1:
        raise ValueError("每次调用必须恰好产生一条结果")
    row = rows[0]
    for key, value in {"system": "gateann", "tier": tier, "bucket": item["bucket"],
                       "L": item["L"], "k": 10, "threads": 1, "beamwidth": 8,
                       "queries": 200, "system_commit": pin}.items():
        if row.get(key) != str(value):
            raise ValueError(f"结果字段不符合计划：{key}")
    for key in ("recall_percent", "mean_latency_us", "qps", "mean_ios",
                "peak_rss_kb", "index_bytes", "filter_skips", "io_time_us",
                "tunnel_time_us", "process_time_us"):
        value = float(row[key])
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"结果包含无效数值：{key}")
    if not 0 <= float(row["recall_percent"]) <= 100:
        raise ValueError("Recall 超出百分比范围")
    if any(float(row[key]) <= 0 for key in ("qps", "mean_latency_us", "peak_rss_kb", "index_bytes")):
        raise ValueError("时延、QPS、内存和索引字节数必须为正")
    values = []
    for line in (raw_dir / "run.log").read_text().splitlines():
        try:
            nums = [float(s) for s in line.split()]
        except ValueError:
            continue
        if len(nums) == 8 and nums[0] == item["L"]:
            values.append(nums)
    if len(values) != 1:
        raise ValueError("原始查询日志缺少唯一对应结果")
    v = values[0]
    expected = {"qps": v[1], "recall_percent": v[2]*100, "mean_ios": v[3],
                "filter_skips": v[4], "io_time_us": v[5], "tunnel_time_us": v[6],
                "process_time_us": v[7], "mean_latency_us": 1e6/v[1],
                "peak_rss_kb": record["peak_rss_kb"]}
    for key, value in expected.items():
        if not math.isclose(float(row[key]), value, rel_tol=1e-9, abs_tol=1e-8):
            raise ValueError(f"CSV 与原始证据不一致：{key}")
    return row


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--course-root", type=Path, default=HERE.parent)
    p.add_argument("--dataset-root", type=Path)
    p.add_argument("--run-root", type=Path)
    p.add_argument("--repo", type=Path)
    p.add_argument("--tier", choices=("formal", "debug"), default="formal")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--experiment-id", default="bucket-opt-"+datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    p.add_argument("--host-note", default="宿主机后台负载未记录")
    p.add_argument("--dry-run", action="store_true", help="检查配置并打印命令，不启动查询，不写实验结果")
    p.add_argument("--resume", action="store_true", help="验证并跳过已完成项，保留失败目录后重试")
    return p


def main():
    args = parser().parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", args.experiment_id):
        raise ValueError("experiment-id 只能包含字母、数字、点、横线和下划线")
    root = args.course_root.resolve()
    dataset = (args.dataset_root or root/"datasets/yfcc_course_v1").resolve()
    runs = (args.run_root or root/"course_runs").resolve()
    repo = (args.repo or root/"systems/GateANN-public").resolve()
    integration = json.loads((HERE/"integration.json").read_text())
    for relative, expected_hash in integration["course_files_sha256"].items():
        if digest(root/relative) != expected_hash:
            raise ValueError(f"课程文件与上传版本不同：{relative}；请先核对版本，不要直接覆盖课程文件")
    spec = importlib.util.spec_from_file_location("_course_common", root/"adapters/_common.py")
    common = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = common
    spec.loader.exec_module(common)
    tier = common.resolve_tier(dataset, args.tier)
    if tier.name not in ("formal_1000000", "debug_100000"):
        raise ValueError("仅支持课程指定的两个数据 Tier")
    for bucket in ("low", "medium", "high"):
        info = common.validate_dataset(tier, bucket)
        if info["base_size"] != (1000000 if args.tier == "formal" else 100000) or info["query_count"] != 200 or info["dimension"] != 192:
            raise ValueError("数据规模与课程配置不一致")
    adapter = root/"adapters/gateann_adapter.py"
    plan = schedule(args.repeats)
    for i, item in enumerate(plan, 1):
        item["order"] = i
        item["run_id"] = f'{args.experiment_id}-{item["policy"]}-r{item["repeat"]}'
        item["command"] = [sys.executable, str(adapter), "search", "--repo", str(repo),
            "--dataset-root", str(dataset), "--tier", args.tier, "--run-root", str(runs),
            "--bucket", item["bucket"], "--L", str(item["L"]), "--threads", "1",
            "--beamwidth", "8", "--k", "10", "--full-adj-neighbors", "32",
            "--mem-L", "0", "--cache-budget", "0", "--run-id", item["run_id"]]
    print(f"计划：{len(plan)} 次独立查询进程；Tier={tier.name}", flush=True)
    if args.dry_run:
        for item in plan:
            print(shlex.join(item["command"]))
        return
    exp = runs/"optimization"/args.experiment_id
    settings = {"course_root": str(root), "dataset_root": str(dataset), "run_root": str(runs),
        "repo": str(repo), "tier": tier.name, "repeats": args.repeats, "budgets": BUDGETS,
        "experiment_id": args.experiment_id, "integration": integration,
        "implementation_sha256": {p.name: digest(p) for p in (HERE/"policy.py", HERE/"run.py", HERE/"integration.json")}}
    if exp.exists():
        if not args.resume:
            raise FileExistsError(f"实验已存在：{exp}；使用 --resume 或更换 experiment-id")
        if json.loads((exp/"settings.json").read_text()) != settings or json.loads((exp/"plan.json").read_text()) != plan:
            raise ValueError("续跑的代码、路径或实验计划发生变化；请更换 experiment-id")
    else:
        exp.mkdir(parents=True)
        dump(exp/"settings.json", settings)
        dump(exp/"plan.json", plan)
        dump(exp/"environment.json", {"initial": snapshot(), "host_note": args.host_note,
            "cache_policy": "mem-L=0; cache-budget=0; 不主动清理系统或宿主缓存；每组同样处理",
            "monitoring": "每次调用前后记录虚拟机快照，不是持续监控；宿主状态仅来自人工说明"})
        shutil.copytree(HERE, exp/"implementation", ignore=shutil.ignore_patterns("__pycache__", ".git"))
    logged([sys.executable, str(adapter), "doctor", "--repo", str(repo),
            "--dataset-root", str(dataset), "--tier", args.tier], exp/"doctor.log")
    rows = []
    for item in plan:
        print(f'[{item["order"]}/{len(plan)}] {item["policy"]} {item["bucket"]} L={item["L"]} r={item["repeat"]}', flush=True)
        raw = runs/"results/gateann"/tier.name/item["bucket"]/item["run_id"]
        evidence = exp/"raw"/item["run_id"]/item["bucket"]
        completed = None
        if raw.exists():
            if not args.resume:
                raise FileExistsError(f"原始结果目录已存在：{raw}")
            try:
                completed = validate_result(raw, item, tier.name, integration["gateann_commit"])
            except (ValueError, KeyError, OSError, ZeroDivisionError):
                preserved = raw.with_name(raw.name+".failed-"+datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
                raw.rename(preserved)
                print(f"保留失败目录：{preserved}", flush=True)
        if completed is None:
            dump(exp/f'state-before-{item["order"]:02d}.json', snapshot())
            try:
                logged(item["command"], exp/f'adapter-{item["order"]:02d}.log')
                completed = validate_result(raw, item, tier.name, integration["gateann_commit"])
            finally:
                dump(exp/f'state-after-{item["order"]:02d}.json', snapshot())
                if raw.exists():
                    shutil.copytree(raw, evidence, dirs_exist_ok=True)
        else:
            print("已核对原始证据，跳过已完成项", flush=True)
            shutil.copytree(raw, evidence, dirs_exist_ok=True)
        row = dict(completed, experiment_id=args.experiment_id, policy=item["policy"],
                   repeat=item["repeat"], order=item["order"], run_id=item["run_id"],
                   evidence_dir=str(evidence.relative_to(exp)))
        rows.append(row)
        with (exp/"results.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            writer.writeheader();writer.writerows(rows)
    dump(exp/"status.json", {"status": "complete", "validated_rows": len(rows),
        "completed_at": datetime.now().astimezone().isoformat()})
    print(f"完成：{exp}\n下一步：python3 {HERE/'analyze.py'} {exp}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
