# test_adapter.py
import sys
import time
from pathlib import Path
ADAPTERS_DIR = Path(__file__).resolve().parent.parent / "adapters"
sys.path.insert(0, str(ADAPTERS_DIR))

import rclpy
from ros2_adapter import TATAdapter
from concurrent.futures import as_completed

def main():
    rclpy.init()
    adapter = TATAdapter(pose_table_path="../adapters/poses.yaml")

    # TAT1 dispatch — 즉시 리턴
    print("→ TAT1 dispatch")
    f1 = adapter.send_command("TAT1", "ToPP", "dock")

    # 결과 안 기다리고 다른 일
    time.sleep(3)

    # TAT2 dispatch
    print("→ TAT2 dispatch")
    f2 = adapter.send_command("TAT2", "ToINSP", "dock")


    labels = {f1: "TAT1", f2: "TAT2"}
    for fut in as_completed(labels, timeout=120):
        print(f"[{labels[fut]}] {fut.result()}")

    adapter.shutdown()
    rclpy.shutdown()


if __name__ == "__main__":
    main()