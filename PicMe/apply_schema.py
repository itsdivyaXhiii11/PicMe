"""A2 - apply schema.sql to Supabase from the command line.

    venv\\Scripts\\python.exe apply_schema.py

Same effect as pasting schema.sql into Supabase's SQL Editor, but it is
repeatable and it prints what happened. Safe to run any time.
"""
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()
HERE = os.path.dirname(os.path.abspath(__file__))

url = os.environ.get("DATABASE_URL", "")
if not url:
    sys.exit("DATABASE_URL missing - fill it in .env (see .env.example)")
if "sslmode" not in url:
    url += ("&" if "?" in url else "?") + "sslmode=require"

sql = open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read()

con = psycopg2.connect(url)
con.autocommit = True
with con.cursor() as cur:
    cur.execute(sql)
    print("schema.sql applied")
    for table in ("photos", "faces", "sessions", "exports"):
        cur.execute(f"select count(*) from {table}")
        print(f"  {table:9s}: {cur.fetchone()[0]} rows")
con.close()
print("done")
