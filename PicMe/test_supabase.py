"""A1 test script: prove the Supabase connection works.

Run:
    venv\\Scripts\\python.exe test_supabase.py

It checks the three .env values, connects to Postgres, and reads an
empty table (run schema.sql first, in Supabase's SQL Editor).
"""
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()  # reads .env into environment variables


def mask(v):
    return (v[:10] + "..." + v[-4:]) if v and len(v) > 18 else ("set" if v else "MISSING")


url = os.environ.get("SUPABASE_URL", "")
key = os.environ.get("SUPABASE_SERVICE_KEY", "")
dburl = os.environ.get("DATABASE_URL", "")

print("SUPABASE_URL       :", mask(url))
print("SUPABASE_SERVICE_KEY:", mask(key))
print("DATABASE_URL       :", mask(dburl))

if not (url and key and dburl):
    sys.exit("Fill all three values in .env first (copy them from .env.example).")

if "sslmode" not in dburl:
    dburl += ("&" if "?" in dburl else "?") + "sslmode=require"

conn = psycopg2.connect(dburl)
cur = conn.cursor()
cur.execute("select version()")
print("Postgres says:", cur.fetchone()[0][:60], "...")

for table in ("photos", "faces", "sessions", "exports"):
    try:
        cur.execute(f"select count(*) from {table}")
        print(f"  table {table:9s}: exists, {cur.fetchone()[0]} rows")
    except psycopg2.Error as e:
        print(f"  table {table:9s}: MISSING -> {e.pgerror.strip().splitlines()[0]}")
        conn.rollback()

cur.close()
conn.close()
print("A1 connection test OK")
