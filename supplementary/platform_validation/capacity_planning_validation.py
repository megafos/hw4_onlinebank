import unittest
from capacity_planning import calculate
class CapacityTests(unittest.TestCase):
    def test_dau_and_mau(self):
        a,b=calculate(),calculate(10_000_000)
        self.assertEqual(a['total_daily'],30_000_000)
        self.assertEqual(b['total_daily'],100_000_000)
        self.assertAlmostEqual(a['critical_write_operations_daily_base'],38_880_000)
        self.assertAlmostEqual(a['critical_raw_history_gb_30d'],280.8)
        self.assertAlmostEqual(a['illustrative_external_cost_daily'],1_201_200)
        self.assertAlmostEqual(b['critical_history_tb_index2_copies3']/a['critical_history_tb_index2_copies3'],10/3)

if __name__ == '__main__':
    import io
    import sys
    from pathlib import Path
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    report = stream.getvalue()
    target = Path(__file__).resolve().with_name('capacity_planning_output.txt')
    target.write_text(report, encoding='utf-8')
    print(report, end='')
    sys.exit(0 if result.wasSuccessful() else 1)
