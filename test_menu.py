import os
import time
import argparse
from dotenv import load_dotenv

from golf_service import GolfService

def main():
    parser = argparse.ArgumentParser(description="Opt-in live menu diagnostic; never part of automated tests.")
    parser.add_argument("--live", action="store_true", required=True)
    parser.parse_args()
    load_dotenv()
    service = GolfService()
    print("Fetching buddies...")
    buddies = service.fetch_buddy_list(
        tvn_username=os.environ["TVN_USERNAME"],
        tvn_password=os.environ["TVN_PASSWORD"],
        golf_password=os.environ["GOLF_PASSWORD"],
    )
    
    _, page = service._get_or_create_session(os.environ["TVN_USERNAME"])
    try:
        btn = page.locator("input[name='Menu']")
        if btn.count() > 0:
            btn.first.click(timeout=5000)
            page.wait_for_load_state("networkidle")
            print("Page URL after click:", page.url)
            print("Content:", page.inner_text('body'))
    except Exception as e:
        print("Click exception:", e)

if __name__ == "__main__":
    os.environ["HEADLESS"] = "true"
    main()
