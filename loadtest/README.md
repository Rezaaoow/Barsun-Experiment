# BarSan staging load tests

These tests are intended for a **separate staging deployment and disposable PostgreSQL database**. They create synthetic participants and behavioral rows. Never run them against `my-experiment.ir`, the main study code, or real participants.

## 1. Prepare an isolated environment

Deploy the exact candidate commit to staging with:
- the same Python version and Passenger/ASGI topology planned for the event;
- the same PostgreSQL major version and comparable database resource limits;
- a separate `STUDY_CODE`, separate database, and staging-only `ADMIN_TOKEN`;
- the same domain/proxy/compression settings where feasible.

Set `BASE_URL` to the staging app URL including `/lifetrade`. Set `ADMIN_TOKEN` in the shell environment; never place it in a command, file, script argument or Git.

Create a new batch of invitations for each independent test run:

```bash
export BASE_URL="https://YOUR-STAGING-HOST/lifetrade"
export ADMIN_TOKEN="(set this in the shell without echoing it)"
python loadtest/create_participants.py --count 600 --output loadtest/participants.json
```

The generator creates a custom workflow with the same three modules (questionnaire, Life-Trade, Ultimatum), creates assignments in batches of at most 500, writes the invitation tokens to a mode-0600 ignored file and never prints them. It refuses the known production hostname.

Install Grafana k6 on a separate load-generator machine, not on the cPanel application host. Keep the k6 machine's CPU/network below saturation so it does not become the bottleneck.

## 2. Full participant journey

The full test simulates consent, identity, 25 questionnaire answers plus draft persistence, 60 Life-Trade records in bounded batches, two Ultimatum practice trials, all 20 main trials, result-event persistence, stage completion and time-tracking events.

**Sudden 600-user start:**

```bash
k6 run loadtest/full_journey.js \
  -e BASE_URL="$BASE_URL" \
  -e PARTICIPANTS_FILE="loadtest/participants.json" \
  -e VUS=600 \
  -e PROFILE=burst
```

**Gradual ramp to 600 and 10-minute steady state:**

```bash
k6 run loadtest/full_journey.js \
  -e BASE_URL="$BASE_URL" \
  -e PARTICIPANTS_FILE="loadtest/participants.json" \
  -e VUS=600 \
  -e PROFILE=ramp \
  -e THINK_TIME_SECONDS=1.5
```

The default think time is deliberately low to stress the API. Increasing it models more realistic participant pacing. Create a new set of 600 invitations for every run because a completed invitation is not a fresh participant on the next run.

Initial thresholds are `http_req_failed < 0.1%`, p95 below 1.5 seconds, p99 below 4 seconds and successful checks above 99.9%. These are test gates, not evidence that the host can meet them. Report actual results and adjust thresholds only with an explicit rationale.

## 3. Steady 600-user heartbeat test

This isolates the 15-second active-time heartbeat path and holds 600 synthetic participants active for ten minutes (about 40 heartbeat requests/second, staggered across the 15-second window):

```bash
k6 run loadtest/heartbeat_steady.js \
  -e BASE_URL="$BASE_URL" \
  -e PARTICIPANTS_FILE="loadtest/participants.json" \
  -e VUS=600 \
  -e DURATION_SECONDS=600
```

Use a fresh invitation batch for this test as well. It records consent for synthetic test accounts and tracks the `load-questionnaire` step only.

## 4. What to monitor

Capture these alongside the k6 summary:
- k6: request rate, p50/p95/p99, failed requests, checks and dropped iterations;
- Passenger: active/spawned process count, process limit, queue depth and 503s;
- cPanel host: CPU, RSS/memory, entry processes, process limits, I/O and network;
- PostgreSQL: `pg_stat_activity`, active/idle connections, connection waits, CPU/RAM, locks and slow statements (`pg_stat_statements` if enabled);
- application: 5xx responses and logs without invitation tokens or secrets.

A 600 VU test on a different machine, plan, proxy or process limit does not establish 600-user capacity for production. Do not increase Passenger processes or PostgreSQL pool size until memory, process limits and database connection limits are measured. The time pool is per process.

After the test, securely remove the local participant file and discard the staging database. Do not export synthetic rows into the primary study.
