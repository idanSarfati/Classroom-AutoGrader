# Running the portal on its own server

The portal is a normal Python web application with a SQLite database. Moving it
to a small Linux server gives it a stable HTTPS address that works when the
laptop is off, asleep or at home. The laptop keeps its Cloudflare tunnel until
the server has been verified, so there is always a fallback.

Total cost with the recommendation below: about €4 per month (plus VAT).
Setup time: 20–30 minutes, most of it waiting for DNS and the certificate.

## 0. The live server

The portal runs on an Oracle Cloud Always Free instance (no monthly cost).

- Address: <https://idan-python.duckdns.org> (the name students use)
- Fallback: <https://158.178.250.254.sslip.io> (same server, same certificate
  authority; both names are served from one block in `/etc/caddy/Caddyfile`)
- Instance `portal-server`, Ubuntu 24.04 aarch64, `VM.Standard.A1.Flex`
  (2 OCPU / 12 GB), region `il-jerusalem-1`, availability domain AD-1.
- Reserved public IP `158.178.250.254`, so the address survives a
  stop/start of the instance.
- SSH: `ssh -i portal/data/deploy_key ubuntu@158.178.250.254` (the deploy
  key stays on the laptop, never on the server).
- Code: `/opt/clasroom-autograder`; services `portal` and `caddy`.

The name comes from DuckDNS (free): the subdomain `idan-python` in the
teacher's DuckDNS account points at the reserved IP. The `sslip.io` name
derives from the IP itself and needs no account. Both are listed on the first
line of `/etc/caddy/Caddyfile`, so either one works and both get certificates.

DuckDNS deletes a subdomain that has not been updated for 30 days, and a
successful update counts as activity. The server therefore refreshes the name
itself, once a day at 06:17 (`/etc/cron.d/duckdns-update` →
`/usr/local/bin/duckdns-update`, log in `/var/log/duckdns-update.log`). The
call sends an empty `ip=`, so DuckDNS records the address the request came
from — the server's own reserved IP — and there is nothing to keep in sync.

The token that authorises the update sits in `/etc/duckdns/duckdns.conf`
(root, mode 600) and is deliberately not part of the bundle or this repo. To
install it, run on the laptop and paste the token from <https://www.duckdns.org>
(there is no other step, and no need to repeat it):

```powershell
powershell -ExecutionPolicy Bypass -File portal\scripts\install_duckdns_token.ps1
```

It prints `OK` when DuckDNS accepted the update. The installer strips Windows
line endings and blank lines and rejects a config without both keys, so
pasting from PowerShell cannot leave a half-written file. Check the daily run
later with `ssh -i portal/data/deploy_key ubuntu@158.178.250.254 \
'tail -n 5 /var/log/duckdns-update.log'`.

Two firewalls must both allow traffic:

1. the OCI security list of `vcn-portal` (ingress TCP 22, 80, 443);
2. the host's iptables — the Ubuntu image ends `INPUT` with `REJECT`, so
   the rules for 80/443 were added and saved with `netfilter-persistent save`
   into `/etc/iptables/rules.v4`. Check them with
   `sudo iptables -S INPUT` after any image rebuild.

## 1. Choose the server

| Option | Cost | Notes |
| --- | --- | --- |
| **Hetzner Cloud, CX22** (recommended) | ≈€3.79/month + VAT | 2 vCPU, 4 GB RAM, 40 GB disk. Pick **Debian 13** (Python 3.13). Account needs a card and sometimes an ID check. |
| DigitalOcean, Basic 2 GB | $12/month | Slightly more expensive, very simple signup. Ubuntu 24.04 works; install Python 3.12 or newer. |
| Oracle Cloud Always Free | 0 | Free ARM instance, but signup needs card verification and "out of capacity" errors are common. Only worth it if the account already exists. |

The 1 GB tiers are tight: the app, the grading worker and a sandbox run share
memory. 2 GB is the minimum, 4 GB is comfortable.

Managed platforms such as Vercel, Railway or Render are a poor fit: their
filesystems are ephemeral, so the SQLite database, uploaded slides and the
background grading worker would all need to be rebuilt on external services.
A plain server runs the existing code unchanged.

## 2. Create the free address

DuckDNS gives a hostname such as `idan-python.duckdns.org` for free:

1. Sign in at <https://www.duckdns.org> (GitHub or Google).
2. Create a subdomain, for example `idan-python`.
3. Put the server's IPv4 address in **current ip** and save.
4. Copy the token shown next to the subdomain; it goes into the server once
   with `portal/scripts/install_duckdns_token.ps1` (§0). After that the daily
   refresh keeps the name alive and the token is never needed again.

Check from the server once it exists:

```bash
getent hosts idan-python.duckdns.org
```

The address is stable as long as the subdomain stays in the account. Caddy
will obtain and renew a Let's Encrypt certificate for it automatically.

## 3. Prepare the server

Create the server in the provider's console and add your SSH public key
(`ssh-keygen -t ed25519` creates one on Windows at `~/.ssh/id_ed25519.pub`).
Then:

```bash
ssh root@SERVER_IP

apt-get update
apt-get install -y python3 python3-venv caddy curl rsync ufw sqlite3
python3 --version          # must be 3.12 or newer

ufw allow OpenSSH
ufw allow 80,443/tcp
ufw --force enable
```

`caddy` from the distribution is fine. If `apt-get` cannot find it, add
Caddy's official repository (see <https://caddyserver.com/docs/install>) and
install again.

## 4. Build and upload the bundle

On the laptop, from the project root:

```powershell
.venv\Scripts\python -m portal.scripts.build_deploy_bundle
```

This writes `portal\data\_tmp\portal-deploy-<date>.tar.gz` (about 5 MB). It
copies the database with `VACUUM INTO` while the local server keeps running,
rewrites the Windows rubric paths to `assignments/...` / `configs/...` on the
**snapshot only**, checks every rubric hash, and leaves out backups, logs and
student mapping files.

The archive contains `.env` (your API keys) and the portal's Google token.
Treat it as a secret and delete it from the server after installing.

```powershell
scp "portal\data\_tmp\portal-deploy-2026-11-09.tar.gz" root@SERVER_IP:/srv/
```

## 5. Install and start

```bash
mkdir -p /opt/clasroom-autograder
tar -xzf /srv/portal-deploy-2026-11-09.tar.gz \
    -C /opt/clasroom-autograder --strip-components=1
cd /opt/clasroom-autograder
chmod +x portal/deploy/setup_server.sh portal/deploy/backup.sh
./portal/deploy/setup_server.sh \
    "idan-python.duckdns.org, 158.178.250.254.sslip.io"
rm /srv/portal-deploy-2026-11-09.tar.gz
```

`setup_server.sh` is idempotent. It creates the unprivileged `portal` account,
builds `.venv` from the pinned requirements, gives that account write access
only to `portal/data` and `portal/content`, installs the systemd unit and the
Caddy reverse proxy, installs the nightly backup and the daily DuckDNS refresh.
Pass the names as one comma-separated argument (Caddy needs `, ` between them);
passing a single name would leave only that one address. It is also the update
command (see §8).

## 6. Verify

Run these in order and stop at the first failure:

```bash
curl -fsS http://127.0.0.1:8000/healthz        # local service
curl -fsS https://idan-python.duckdns.org/healthz   # certificate + proxy
systemctl status portal --no-pager
# the public answer must say "status":"ok" and "environment":"production"
journalctl -u portal -n 40 --no-pager
```

The certificate appears once DNS points here and ports 80/443 are open; Caddy
retries automatically. Check with `journalctl -u caddy -n 40`.

Then inside the app, on a phone or another computer:

1. Sign in as the teacher. Both classes open, and the dashboard lists the
   pending gradebook.
2. Open 3.2 and 3.3; the worksheet preview shows every widget (code boxes,
   tables, choices) exactly as on the laptop.
3. Run one real grade through the engine without writing anything:

   ```bash
   cd /opt/clasroom-autograder
   .venv/bin/python -m portal.scripts.check_engine
   ```

   `ok: score …, model …, rubric …, audit yes` proves the rubrics are found,
   the API keys work and the audit runs. It makes one real model call.
4. Sign in as `demo.student` (the password is in `portal\data\student_credentials.csv`
   on the laptop) and open 3.3. Type a few characters into one answer, wait two
   seconds for "נשמר אוטומטית", reload the page: the answer is still there.
   Do not submit.
5. Open a lesson with the code playground and run a two-line program. This is
   the only part whose implementation differs between Windows and Linux
   (`RLIMIT_AS` instead of a Job Object).

To clear the draft left by step 4, remove the row (the student and assignment
ids are visible on the teacher's grading page for that assignment):

```bash
sqlite3 /opt/clasroom-autograder/portal/data/portal.db \
  "delete from portal_answer_drafts where student_id = <id> and assignment_id = <id>;"
```

If you want to prove the whole path, including the queue and the worker,
submit as `demo.student` and then restore the backup you take first — the
restore drill in §7 does exactly that.

## 7. Backups and restore

Every night at 03:17 (server time, set to Asia/Jerusalem) `portal-backup` writes a compressed, consistent copy
to `/var/backups/portal/` and keeps 30 days. Run it by hand once:

```bash
/usr/local/bin/portal-backup
ls -lh /var/backups/portal/
```

Restore (portal stopped, then the newest backup):

```bash
systemctl stop portal
cd /opt/clasroom-autograder/portal/data
cp portal.db portal.db.before-restore
gunzip -c /var/backups/portal/portal-<stamp>.db.gz > portal.db
chown portal:portal portal.db
rm -f portal.db-wal portal.db-shm
systemctl start portal
curl -fsS http://127.0.0.1:8000/healthz
```

Copy a backup to the laptop now and then (`scp root@SERVER_IP:/var/backups/portal/...`):
a provider outage should not be the only place the grades live.

## 8. Updating the site

A normal update must **not** overwrite the server's database. Build a
code-only bundle, which contains no `portal/data` files:

```powershell
.venv\Scripts\python -m portal.scripts.build_deploy_bundle --code-only
scp "portal\data\_tmp\portal-code-2026-11-09.tar.gz" root@SERVER_IP:/srv/
```

```bash
tar -xzf /srv/portal-code-2026-11-09.tar.gz \
    -C /opt/clasroom-autograder --strip-components=1
/opt/clasroom-autograder/portal/deploy/setup_server.sh \
    "idan-python.duckdns.org, 158.178.250.254.sslip.io"
rm /srv/portal-code-2026-11-09.tar.gz
```

Re-running the setup script reinstalls dependencies, fixes permissions and
restarts the service. Schema changes are applied by the app itself
(`init_db` → `create_all` → `portal.migrations`), as on the laptop. Take a
backup first if the update touches the database schema, and check what is
pending before restarting:

```bash
sudo -u portal /opt/clasroom-autograder/.venv/bin/python -m portal.scripts.migrate
```

That command is read-only. A migration that fails rolls back and stops startup,
so the service will not come up with a half-changed schema — read
`journalctl -u portal -n 100`, fix the migration, and restart.

## 9. Cutover and rollback

1. Keep the laptop tunnel running while you verify the server.
2. When §6 passes, send the new `https://idan-python.duckdns.org` address to
   the classes. Cookies are scoped to the old domain, so every student signs in
   once on the new address; their saved drafts and grades are in the database.
3. Stop the tunnel on the laptop; it is no longer the entry point. Keep the
   laptop database as a dated archive, but from then on the server's database
   is the real one.
4. Rollback at any point before step 3 is simply "use the old tunnel address
   again". After step 3, rollback means restoring a server backup, not
   returning to the laptop.

## 10. Troubleshooting

| Symptom | Check |
| --- | --- |
| `https://…` does not load | DNS: `getent hosts NAME`. Firewall: `ufw status`. Caddy: `journalctl -u caddy -n 60`. |
| Certificate never issued | Port 80 must be reachable from the internet. The Let's Encrypt logs are in the Caddy journal. |
| `502` from Caddy | `systemctl status portal`; the app listens on `127.0.0.1:8000` only. |
| App restarts in a loop | `journalctl -u portal -n 100`. Usually a missing file or a permission on `portal/data`. |
| Startup stops with `Migration … failed` | The change rolled back; the database is as it was. Fix the migration in `portal/migrations`, then restart. `python -m portal.scripts.migrate` shows what is still pending. |
| Grades stay "בתור" | `journalctl -u portal -f` while a submission arrives. `check_engine` separates rubric/API problems from queue problems. |
| Disk usage grows | `df -h`, `du -sh /var/backups/portal`, `du -sh /opt/clasroom-autograder/portal/content`. Prune backups or raise `PORTAL_BACKUP_KEEP_DAYS`. |

Logs: `journalctl -u portal -f` and `journalctl -u caddy -f`.

## 11. What lives where

- `/opt/clasroom-autograder` — code, `.venv`, `assignments/`, `configs/`
- `/opt/clasroom-autograder/portal/data/portal.db` — the database
- `/opt/clasroom-autograder/portal/content` — imported slide images and files
- `/etc/systemd/system/portal.service` — the unit (`User=portal`)
- `/etc/caddy/Caddyfile` — HTTPS and reverse proxy
- `/var/backups/portal` — nightly database copies
- `/etc/portal/portal.env` — optional extra environment variables
