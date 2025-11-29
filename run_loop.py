import time
from main import main

while True:
    print("🟢 Starting trade evaluation cycle...")
    try:
        main()
    except Exception as e:
        print(f"Error in run_loop.py: {e}")
    print("⏳ Sleeping for 4 hours...")
    time.sleep(4 * 60 * 60)
