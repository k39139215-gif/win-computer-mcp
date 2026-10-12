import unittest

class TestSafetyGuardrails(unittest.TestCase):
    def is_coordinate_safe(self, x, y, bounds):
        min_x, min_y, max_x, max_y = bounds
        return min_x <= x <= max_x and min_y <= y <= max_y

    def test_within_bounds(self):
        bounds = (0, 0, 1920, 1080)
        self.assertTrue(self.is_coordinate_safe(500, 400, bounds))

    def test_out_of_bounds_blocked(self):
        bounds = (0, 0, 1920, 1080)
        self.assertFalse(self.is_coordinate_safe(2100, 500, bounds))
        self.assertFalse(self.is_coordinate_safe(-5, 500, bounds))

if __name__ == '__main__':
    unittest.main()
