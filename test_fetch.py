import os
import time
import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from golf_service import GolfService

def main():
    parser = argparse.ArgumentParser(description="Opt-in live diagnostic; never part of the automated tests.")
    parser.add_argument("--live", action="store_true", required=True)
    parser.parse_args()
    load_dotenv()
    play_date = datetime.now(ZoneInfo("America/New_York")).date() + timedelta(days=1)
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
        date_str=play_date.strftime("%Y%m%d"),
        date_label=play_date.isoformat(),
        course_type="Executive",
        region_filter="all",
        golfer_ids=[buddies["primary"]["id"]],
        num_golfers=1,
        has_guests=False,
        time_filter="all"
    )
    print(f"Times Result ({time.time() - start:.2f}s): {result['success']}")

if __name__ == "__main__":
    os.environ["HEADLESS"] = "true"
    main()
