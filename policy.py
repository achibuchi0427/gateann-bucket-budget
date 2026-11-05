"""按课程已有分桶分配 L；不是逐查询自适应，也不修改 GateANN 内核。"""

BUCKETS = ("low", "medium", "high")
POLICIES = ("fixed320", "fixed1280", "bucket")
BUDGETS = {
    "fixed320": {"low": 320, "medium": 320, "high": 320},
    "fixed1280": {"low": 1280, "medium": 1280, "high": 1280},
    "bucket": {"low": 1280, "medium": 640, "high": 320},
}


def choose_l(policy, bucket):
    if policy not in POLICIES or bucket not in BUCKETS:
        raise ValueError("未知策略或分桶")
    return BUDGETS[policy][bucket]


def schedule(repeats=3):
    """每轮轮换分桶与策略顺序；3 轮时每个策略在各位置恰好一次。"""
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
        raise ValueError("repeats 必须是正整数")
    result = []
    for repeat in range(1, repeats + 1):
        offset = (repeat - 1) % 3
        buckets = BUCKETS[offset:] + BUCKETS[:offset]
        policies = POLICIES[offset:] + POLICIES[:offset]
        for bucket in buckets:
            for policy in policies:
                result.append({"repeat": repeat, "bucket": bucket,
                               "policy": policy, "L": choose_l(policy, bucket)})
    return result
