import unittest


# Mocking OpenWrtClient since it depends on many other things
class OpenWrtClientMock:
    def __init__(self):
        self._last_cpu_stats = None

    def _calculate_cpu_usage(self, proc_stat: str) -> float:
        """Copied logic from base.py for testing."""
        if not proc_stat:
            return 0.0

        try:
            line = proc_stat.splitlines()[0]
            parts = line.split()
            if len(parts) < 5:
                return 0.0

            user = int(parts[1])
            nice = int(parts[2])
            system = int(parts[3])
            idle = int(parts[4])
            iowait = int(parts[5]) if len(parts) > 5 else 0
            irq = int(parts[6]) if len(parts) > 6 else 0
            softirq = int(parts[7]) if len(parts) > 7 else 0
            steal = int(parts[8]) if len(parts) > 8 else 0

            idle_time = idle + iowait
            non_idle_time = user + nice + system + irq + softirq + steal
            total_time = idle_time + non_idle_time

            if self._last_cpu_stats is None:
                self._last_cpu_stats = (total_time, idle_time)
                return 0.0

            prev_total, prev_idle = self._last_cpu_stats
            self._last_cpu_stats = (total_time, idle_time)

            total_diff = total_time - prev_total
            idle_diff = idle_time - prev_idle

            if total_diff <= 0:
                return 0.0

            cpu_usage = (total_diff - idle_diff) / total_diff
            return round(max(0.0, min(100.0, cpu_usage * 100.0)), 1)
        except ValueError, IndexError:
            return 0.0


class TestCpuCalculation(unittest.TestCase):
    def test_cpu_calculation(self):
        client = OpenWrtClientMock()

        # First poll: stats saved, returns 0.0
        # cpu  user nice system idle iowait irq softirq steal guest guest_nice
        stat1 = "cpu  100 0 50 1000 0 0 0 0 0 0"
        # Total: 1150, Idle: 1000
        self.assertEqual(client._calculate_cpu_usage(stat1), 0.0)
        self.assertEqual(client._last_cpu_stats, (1150, 1000))

        # Second poll:
        # Increase user by 50, system by 50, idle by 100
        stat2 = "cpu  150 0 100 1100 0 0 0 0 0 0"
        # Total: 1350, Idle: 1100
        # DiffTotal: 200, DiffIdle: 100
        # Usage: (200 - 100) / 200 = 0.5 = 50.0%
        self.assertEqual(client._calculate_cpu_usage(stat2), 50.0)

        # Third poll: High usage
        # Increase user by 100, idle by 0
        stat3 = "cpu  250 0 100 1100 0 0 0 0 0 0"
        # Total: 1450, Idle: 1100
        # DiffTotal: 100, DiffIdle: 0
        # Usage: (100 - 0) / 100 = 100.0%
        self.assertEqual(client._calculate_cpu_usage(stat3), 100.0)

        # Fourth poll: Idle usage
        # Increase idle by 100
        stat4 = "cpu  250 0 100 1200 0 0 0 0 0 0"
        # Total: 1550, Idle: 1200
        # DiffTotal: 100, DiffIdle: 100
        # Usage: (100 - 100) / 100 = 0.0%
        self.assertEqual(client._calculate_cpu_usage(stat4), 0.0)


if __name__ == "__main__":
    unittest.main()
