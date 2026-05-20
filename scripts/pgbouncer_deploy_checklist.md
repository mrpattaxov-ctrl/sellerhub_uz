# PgBouncer cut-over checklist (Step 2-POST)

Run on the VPS **only after** Step 2-PRE code lands in main and a deploy
has shipped the audit + `prepare_threshold=None` change.

## 1. Install
```bash
sudo apt update && sudo apt install -y pgbouncer
```

## 2. Config + userlist
Copy the template:
```bash
sudo cp scripts/pgbouncer.ini /etc/pgbouncer/pgbouncer.ini
sudo $EDITOR /etc/pgbouncer/pgbouncer.ini   # fill <DB_HOST>/<DB_NAME>/<DB_USER>/<DB_PASS>
```

Generate md5 userlist (PgBouncer format is `md5 + md5(password + user)`):
```bash
USER=<DB_USER>; PASS=<DB_PASS>
HASH="md5$(printf '%s%s' "$PASS" "$USER" | md5sum | awk '{print $1}')"
echo "\"$USER\" \"$HASH\"" | sudo tee /etc/pgbouncer/userlist.txt
sudo chown postgres:postgres /etc/pgbouncer/userlist.txt
sudo chmod 600 /etc/pgbouncer/userlist.txt
```

## 3. Start
```bash
sudo systemctl enable --now pgbouncer
sudo systemctl status pgbouncer --no-pager
```

## 4. Sanity check
```bash
psql "host=127.0.0.1 port=6432 user=<DB_USER> dbname=pgbouncer" -c "SHOW POOLS;"
psql "host=127.0.0.1 port=6432 user=<DB_USER> dbname=<DB_NAME>"  -c "SELECT 1;"
```

## 5. Flip the app
Edit `.env` on VPS:
```
DB_PORT=6432
```
(or update `DATABASE_URL` to point to `127.0.0.1:6432`). Then:
```bash
sudo systemctl restart gunicorn
sudo systemctl restart uzum-worker   # or whatever runs worker.py
```

## 6. Step 2-POST verification
From `project_scaling_testing_plan.md` — run each:
- `grep -E "prepared statement .* does not exist" /var/log/gunicorn/*.log` -> zero hits after 15 min
- Duplicate-snapshot check:
  ```sql
  SELECT shop_id, snap_hour, count(*)
  FROM finance_snapshots
  WHERE snap_hour > now() - interval '2 hours'
  GROUP BY 1,2 HAVING count(*) > 1;
  ```
  -> zero rows
- Pool burst test: trigger HH:00 hourly burst, watch `SHOW POOLS`; `cl_waiting`
  should drain within ~30 s, `sv_active` stays <= `default_pool_size`.

## 7. 24 h burn-in
Re-run the duplicate-snapshot query after 24 h. Still zero rows -> done.

## Rollback
`.env` -> `DB_PORT=5432`, restart Gunicorn + worker. No data migration needed;
PgBouncer is stateless.
