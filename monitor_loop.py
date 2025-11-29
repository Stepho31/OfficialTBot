import time
from monitor import monitor_open_trades  # You may need to define this in monitor.py

while True:
    try:
        monitor_open_trades()
    except Exception as e:
        print(f"Error in monitor_loop.py: {e}")
    time.sleep(60)
