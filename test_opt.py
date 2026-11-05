"""策略边界与实验计划检查；不需要数据、索引、GateANN 或第三方包。"""
import unittest
from collections import Counter
from policy import BUCKETS, POLICIES, choose_l, schedule


class PolicyTests(unittest.TestCase):
    def test_budgets(self):
        self.assertEqual([choose_l("bucket", b) for b in BUCKETS], [1280, 640, 320])
        self.assertTrue(all(choose_l("fixed320", b) == 320 for b in BUCKETS))
        self.assertTrue(all(choose_l("fixed1280", b) == 1280 for b in BUCKETS))

    def test_reject_invalid_names(self):
        for policy, bucket in [("fake", "low"), ("bucket", "LOW"), ("bucket", "unknown")]:
            with self.assertRaises(ValueError):choose_l(policy, bucket)

    def test_complete_unique_plan(self):
        plan = schedule()
        self.assertEqual(len(plan), 27)
        self.assertEqual(len({(x['policy'], x['bucket'], x['repeat']) for x in plan}), 27)
        self.assertEqual(set(Counter((x['policy'], x['bucket']) for x in plan).values()), {3})

    def test_order_balancing(self):
        plan = schedule()
        for bucket in BUCKETS:
            for position in range(3):
                actual = []
                for repeat in (1, 2, 3):
                    group = [x for x in plan if x['repeat'] == repeat and x['bucket'] == bucket]
                    actual.append(group[position]['policy'])
                self.assertEqual(set(actual), set(POLICIES))

    def test_invalid_repeats(self):
        for value in (0, -1, 1.5, True):
            with self.assertRaises(ValueError):schedule(value)


if __name__ == '__main__':unittest.main()
