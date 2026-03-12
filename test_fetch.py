import os
import time
from dotenv import load_dotenv

load_dotenv("/Users/walterworley/Documents/villages-golf-app/.env")
from golf_service import GolfService

def test():
    service = GolfService()
    print("Fetching buddies...")
    start = time.time()
    buddies = service.fetch_buddy_list(
        tvn_username=os.environ["TVN_USERNAME"],
        tvn_password=os.environ["TVN_PASSWORD"],
        golf_password=os.environ["GOLF_PASSWORD"],
    )
    print(f"Buddies ({time.time() - start:.2f}s): {buddies}")

    print("Fetching times...")
    start = time.time()
    result = service.get_available_times(
        tvn_username=os.environ["TVN_USERNAME"],
        tvn_password=os.environ["TVN_PASSWORD"],
        golf_password=os.environ["GOLF_PASSWORD"],
        date_str="20260314",
        date_label="Sat, Mar 14, 2026",
        course_type="Executive",
        golfer_ids=["483204"],
        num_golfers=1,
        has_guests=False,
        time_filter="all"
    )
    print(f"Times Result ({time.time() - start:.2f}s): {result['success']}")

if __name__ == "__main__":
    os.environ["HEADLESS"] = "true"
    test()
