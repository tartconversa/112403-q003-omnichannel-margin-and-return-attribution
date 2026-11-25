#!/usr/bin/env python3
"""隔离模式测试入口：python3 -I tests/run_tests.py [verbosity]"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(
        os.path.dirname(os.path.abspath(__file__)), pattern="test_*.py")
    verbosity = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
