"""A7 test: the rate limiter counts every client IP separately.

    venv\\Scripts\\python.exe test_forwarded_ip.py

Needs the server running with PROXY_HOPS >= 1 (so the real client IP is read
from the X-Forwarded-For header).

What it does:
  1. sends MAX_TRIES selfies pretending to come from 1.1.1.1 -> the next
     one must be rejected with 429
  2. sends one selfie pretending to come from 9.9.9.9 -> it must NOT be
     rejected (400 = it got past the rate limiter and only failed to decode
     the fake image), which proves the two IPs are counted separately
"""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.getcwd(), ".env"))

BASE = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8000")
MAX = int(os.environ.get("MAX_TRIES", "300"))
FAILED = []


def check(label, ok, extra=""):
    print(("  ok   " if ok else "  FAIL ") + label + ((" -> " + str(extra)) if extra else ""))
    if not ok:
        FAILED.append(label)


def selfie(ip):
    """One fake selfie attempt, pretending to come from `ip`."""
    return requests.post(f"{BASE}/api/match",
                         files={"file": ("x.jpg", b"not-an-image", "image/jpeg")},
                         headers={"X-Forwarded-For": ip}, timeout=15)


# 1) burn through the limit for 1.1.1.1
last = None
for i in range(MAX + 5):
    last = selfie("1.1.1.1")
    if last.status_code == 429:
        break
check(f"IP 1.1.1.1 is blocked with 429 after {MAX} tries", last.status_code == 429,
      f"last status {last.status_code}")

# 2) a different IP must not be affected
other = selfie("9.9.9.9")
check("IP 9.9.9.9 is NOT blocked (counted separately)", other.status_code != 429,
      f"status {other.status_code} (400 = passed the limiter, only the fake image failed)")
check("...and it really did pass the limiter (400, not 429)", other.status_code == 400,
      other.status_code)

print()
if FAILED:
    print(f"FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("A7 forwarded-IP rate limiting OK")
