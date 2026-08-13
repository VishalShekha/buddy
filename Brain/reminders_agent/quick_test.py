import time
# quick_test.py (separate file, same folder)
import server
import datetime

when = (datetime.datetime.now() + datetime.timedelta(minutes=1)).isoformat(timespec="seconds")
print(server.add_reminder("test reminder", when))

print("Waiting...")
time.sleep(180)   # wait 3 minutes
