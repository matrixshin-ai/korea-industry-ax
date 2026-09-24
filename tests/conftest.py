import os
import sys

JOBS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "jobs")
if JOBS_DIR not in sys.path:
    sys.path.insert(0, JOBS_DIR)
