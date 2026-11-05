"""Run as postgres: stream a consistent dump; emit aggregate manifest on stderr."""
import json,subprocess,sys
import psycopg
from psycopg import sql

with psycopg.connect(dbname='barsan_production',user='postgres',host='/var/run/postgresql',port=5432) as connection:
 connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
 assert connection.execute('SELECT current_database(),current_user').fetchone()==('barsan_production','postgres')
 connection.execute("SET statement_timeout='45s'")
 snapshot=connection.execute('SELECT pg_export_snapshot()').fetchone()[0]
 tables=connection.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall()
 counts={name:connection.execute(sql.SQL('SELECT count(*) FROM public.{}').format(sql.Identifier(name))).fetchone()[0] for name, in tables}
 result=subprocess.run(['pg_dump','-h','/var/run/postgresql','-U','postgres','-d','barsan_production','-Fc','--lock-wait-timeout=3s','--snapshot='+snapshot],stdout=sys.stdout.buffer,stderr=subprocess.PIPE,timeout=90)
 if result.returncode:raise SystemExit('database_capture_failed')
 print(json.dumps({'database':'barsan_production','tables':counts,'transaction_consistent_counts':True}),file=sys.stderr)
