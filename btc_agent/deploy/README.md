# Deploying btc-agent on EC2

This folder runs btc-agent on an EC2 instance so that:

- **API keys never go into git.** They're stored in AWS SSM Parameter Store and fetched onto the instance at every boot.
- **The portfolio survives the instance.** The database (`trading.db`) is backed up to S3 every hour and restored
  automatically on a replacement instance.
- **Everything starts and restarts by itself.** systemd runs the agent, the frontend and the backups.

**This deployment's AWS resources**

| Resource | Value |
|---|---|
| Region | `eu-west-2` (London) |
| Backup bucket | `btc-agent-934912969749-eu-west-2-an` |
| Backup location (`BACKUP_S3_URI`) | `s3://btc-agent-934912969749-eu-west-2-an/btc-agent` |
| Parameter Store path | `/btc-agent/` |

If you ever change the bucket, update it in three places: `iam-policy.json`, the `/btc-agent/BACKUP_S3_URI` parameter, and
this table. If you change the `btc-agent` folder name inside the bucket, update the `Prefix` in `s3-lifecycle.json` too.

## What's in this folder

| File | What it is |
|---|---|
| `fetch_secrets.sh` | Downloads the keys from Parameter Store into `/etc/btc-agent/env`. |
| `db_backup.sh` | `backup` uploads a snapshot of `trading.db` to S3; `restore` downloads the latest one if the database is missing. |
| `btc-agent-secrets.service` | Runs `fetch_secrets.sh` at boot (as root). |
| `btc-agent-db-restore.service` | Runs `db_backup.sh restore` at boot, and `db_backup.sh backup` at shutdown. |
| `btc-agent.service` | Runs the agent: `timer.py`, which starts `main.py` at the top of every hour. |
| `btc-agent-app.service` | Runs the Streamlit frontend (`app.py`) on port 8501. |
| `btc-agent-db-backup.timer` and `.service` | Run `db_backup.sh backup` at 15 minutes past every hour, after the agent's hourly run. |
| `iam-policy.json` | Permissions for the instance's IAM role: read the keys, and read/write the backups. |
| `s3-lifecycle.json` | Bucket rule that deletes timestamped backups after 30 days (already applied). |

## How it fits together

At boot, the units start in this order. Each step needs the one before it to succeed:

1. **Secrets:** `btc-agent-secrets` writes the keys to `/etc/btc-agent/env` (readable by root and the `btc-agent` user only).
2. **Database:** `btc-agent-db-restore` keeps `trading.db` if it's on disk; if not, it downloads the latest backup from S3.
3. **App:** `btc-agent` (the hourly agent) and `btc-agent-app` (the frontend) start, with the keys as environment variables.
4. **Backups:** `btc-agent-db-backup.timer` backs up every hour from then on.

All app units run as the unprivileged `btc-agent` user. Nothing stores AWS access keys: the instance's IAM role grants
access to Parameter Store and the bucket.

### What happens to the portfolio when the instance...

| Event | Result |
|---|---|
| **Reboots, or is stopped and started** | The disk survives, so `trading.db` is kept as it is (it's newer than any backup). |
| **Is terminated and replaced** | The new instance has no `trading.db`, so the latest backup is restored from S3 before the app starts. A clean shutdown (stop or terminate from the console) takes a final backup first, so nothing is lost. |
| **Crashes or loses power** | No final backup runs, so up to an hour of trades since the last hourly backup can be lost on a replacement instance. |
| **Is deployed for the first time** | There's no backup yet, so the app starts a new portfolio (seeded with `STARTING_CASH_BALANCE`, default $10,000). |

**Safety rule:** if the restore can't tell whether a backup exists (e.g. access denied), or the backup it downloads is
damaged, it fails and **the app doesn't start**. Otherwise the app would begin a new portfolio, and the next hourly backup
would overwrite the real one in S3.

Every backup is stored twice: `btc-agent/trading.db` (always the latest, used by the restore, never deleted) and
`btc-agent/history/trading-<timestamp>.db` (kept for 30 days, so you can go back to an earlier state).

### Where things live on the instance

```
/opt/btc-agent/                  # the git clone (repo root), owned by root
├── .venv/                       # Python virtual environment, owned by root (about 2 GB)
└── btc_agent/                   # the app: working directory for the agent and frontend
    ├── models/direction/        # direction model + pipeline (in git)
    ├── models/volatility/       # volatility model + pipeline + test metrics (in git)
    ├── databases/trading.db     # writable by btc-agent; restored from S3 or created on the first run
    ├── logs/btc_agent.log       # writable by btc-agent; created on the first run
    └── deploy/                  # this folder
/etc/btc-agent/env               # the keys, written at every boot
/var/lib/btc-agent/              # the btc-agent user's home (Streamlit and Keras cache files)
```

The code is owned by root and the `btc-agent` user can only write `databases/` and `logs/`, so the running agent can't
modify its own code, and `git pull` always runs as root.

## Before you deploy (on your laptop)

The instance gets the code **and the trained models** from GitHub, so everything has to be committed and pushed first:

```bash
git status                     # btc_agent/models/volatility/ and any code changes must not be listed as untracked/modified
git add -A && git commit -m "..." && git push
```

Optional, but the cheapest way to catch a bad key or model name: do one local run first (see the main
[README](../../README.md#running-locally)). `MODEL` in `src/agent.py` is the OpenAI model the agent uses; a wrong name only
shows up as an error on the first real run.

## Before you start

- **Instance:** in `eu-west-2`, **x86_64** (this deployment uses a `t3.medium`, 4 GB; the dependencies were checked for x86_64 only), with at least
  2 GB of RAM (TensorFlow and Streamlit together don't fit in 1 GB) and **at least 16 GB of disk** (the virtual environment
  alone is about 2 GB, and the default 8 GB root volume fills up during `pip install`).
- **Operating system: Amazon Linux 2023 or Ubuntu 22.04/24.04.** TensorFlow 2.21 needs glibc 2.27 or newer, so
  **Amazon Linux 2 won't work** (pip fails with "No matching distribution found for tensorflow").
- **Python 3.12** (matches development). Amazon Linux 2023's default `python3` is 3.9, which is too old: install it with
  `sudo dnf install -y python3.12 python3.12-pip git`. Ubuntu 24.04's default `python3` is already 3.12
  (`sudo apt install -y python3-venv git`).
- **The AWS CLI** on the instance. Amazon Linux 2023 includes it; on Ubuntu install it first
  (see the [AWS CLI install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)).
- **Repo access:** the GitHub repo is public, so the instance can clone it over HTTPS without a key. (Public also means
  anyone can read the code and models; the keys and the database are never in git.)
- **The AWS CLI on your own Windows or Mac machine**, signed in with your own AWS credentials, for steps 1 and 2 and for
  checking backups.

## One-time setup in AWS

Run these from your own machine. They need your real AWS credentials and real key values, so do them yourself.

1. **Store the keys in Parameter Store**, in `eu-west-2` (replace the `...` values with your real keys):
   ```bash
   aws ssm put-parameter --region eu-west-2 --name /btc-agent/OPENAI_API_KEY     --type SecureString --value "sk-..."
   aws ssm put-parameter --region eu-west-2 --name /btc-agent/TELEGRAM_BOT_TOKEN --type SecureString --value "..."
   aws ssm put-parameter --region eu-west-2 --name /btc-agent/MAILTRAP_API_TOKEN --type SecureString --value "..."
   aws ssm put-parameter --region eu-west-2 --name /btc-agent/NEWS_API_KEY       --type SecureString --value "..."
   aws ssm put-parameter --region eu-west-2 --name /btc-agent/BACKUP_S3_URI      --type String       --value "s3://btc-agent-934912969749-eu-west-2-an/btc-agent"
   ```
   - Names must be directly under `/btc-agent/` and use only letters, digits and underscores; anything else is skipped.
   - `STARTING_CASH_BALANCE` (default 10000) can be changed by adding it the same way as a plain `String`.
   - `google_sheets.py` isn't used by the app yet. If it is later, add `/btc-agent/GOOGLE_CREDENTIALS_BASE64` the same way.

2. **Backup bucket and IAM role.**
   - The bucket `btc-agent-934912969749-eu-west-2-an` exists, and the lifecycle rule from `s3-lifecycle.json` is applied.
     To check the rule: `aws s3api get-bucket-lifecycle-configuration --bucket btc-agent-934912969749-eu-west-2-an`.
     Keep the bucket's default settings: public access blocked and encryption on.
   - Create an IAM role for EC2 with the policy in `iam-policy.json`, and attach it to the instance (EC2 console:
     Actions -> Security -> Modify IAM role, or set it at launch). If the role already exists with an older version of the
     policy, update the policy: the S3 permissions are needed for backups.

3. **Security group:** allow inbound SSH (22) and Streamlit (8501) **from your own IP only**. Streamlit has no login, so
   anyone who can reach port 8501 can see the portfolio.

## One-time setup on the instance

4. **Create the service user, clone the repo, and install the dependencies:**
   ```bash
   sudo useradd --system --create-home --home-dir /var/lib/btc-agent --shell /usr/sbin/nologin btc-agent
   sudo git clone https://github.com/cameroncochrane/vlXsnPPgKKnWqEJB.git /opt/btc-agent
   sudo python3.12 -m venv /opt/btc-agent/.venv   # or python3 on Ubuntu 24.04
   sudo /opt/btc-agent/.venv/bin/pip install --no-cache-dir -r /opt/btc-agent/btc_agent/requirements.txt   # takes a few minutes
   # The only places the services write to:
   sudo install -d -o btc-agent -g btc-agent /opt/btc-agent/btc_agent/databases /opt/btc-agent/btc_agent/logs
   ```
   **Check Yahoo Finance answers from this instance** before going further. Yahoo sometimes rate-limits or blocks cloud
   provider addresses, and every hourly run (and the frontend's chart) depends on it. This should print a price and a
   number of days, not an error:
   ```bash
   cd /opt/btc-agent/btc_agent && sudo -u btc-agent -H ../.venv/bin/python -c "from src.price_data import fetch_live_BTCUSD, fetch_historical_BTCUSD; print(fetch_live_BTCUSD()['Close'].iloc[0], len(fetch_historical_BTCUSD('6mo')))"
   ```
   If it fails with "Too Many Requests" or "returned no BTC-USD historical data", wait an hour and try again. If it keeps
   failing, the app can't run on this instance as it is: see Troubleshooting.

5. **Install the secrets script** to a fixed location outside the repo, so the secrets unit doesn't depend on where the
   repo is cloned, and only changes when you deliberately reinstall it:
   ```bash
   sudo install -o root -g root -m 755 /opt/btc-agent/btc_agent/deploy/fetch_secrets.sh /usr/local/sbin/btc-agent-fetch-secrets
   ```

6. **Install and start the systemd units:**
   ```bash
   sudo cp /opt/btc-agent/btc_agent/deploy/*.service /opt/btc-agent/btc_agent/deploy/*.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now btc-agent-secrets.service btc-agent-db-restore.service btc-agent.service btc-agent-app.service btc-agent-db-backup.timer
   ```
   `db_backup.sh` is run straight from the repo, so it must be executable. `ls -l /opt/btc-agent/btc_agent/deploy/db_backup.sh`
   should show `-rwxr-xr-x` (git keeps this). If it doesn't, run `sudo chmod +x` on it.

7. **Check the keys arrived.** This lists the names only, never the values:
   ```bash
   sudo cut -d= -f1 /etc/btc-agent/env
   ```
   You should see your keys plus `BACKUP_S3_URI` and `AWS_REGION` (added automatically).

   **Optional: set the timezone.** The weekly email goes out on Monday at 09:00 *instance time*, and EC2 instances use UTC
   by default (09:00 UTC is 09:00 in winter and 10:00 in summer in the UK). For UK time:
   ```bash
   sudo timedatectl set-timezone Europe/London && sudo systemctl restart btc-agent
   ```
   The database always stores times in UTC, whatever the instance's timezone.

8. **Do one run by hand** before trusting the hourly timer. The timer is already running (step 6), so **do this between
   about :05 and :55 past the hour**; otherwise two runs could overlap and both trade. This loads the keys and runs `main.py` once as the service user
   (it takes a minute or so: loading TensorFlow and both models is the slow part):
   ```bash
   sudo -u btc-agent -H bash -c 'set -a; . /etc/btc-agent/env; set +a; cd /opt/btc-agent/btc_agent && ../.venv/bin/python main.py'
   ```
   Then check the log for errors. A healthy run logs a line like
   `Model predictions: {'direction': 0.2457, 'volatility': 0.1441}` (a number for each model; `'error'` instead of a number
   means that model failed, and the traceback is just above it) followed by the agent's summary:
   ```bash
   tail -n 50 /opt/btc-agent/btc_agent/logs/btc_agent.log
   ```
   Open `http://<instance-public-ip>:8501` to see the frontend. If the agent traded, a Telegram message should have arrived.

   Things to know about the services the app uses:
   - **Telegram** only delivers to a chat that has messaged the bot first (send it `/start` once from your account). Besides
     trade messages, it sends an alert whenever a Yahoo Finance request still fails after its retries, and whenever an
     hourly run is still going when the next hour starts (the scheduler stops it first, so runs never pile up).
   - **Yahoo Finance** is only asked for prices by the hourly run and when someone uses the frontend: the frontend doesn't
     refresh the price by itself, so press **Refresh price** for a new one (a page load reuses a price under 5 minutes old).
   - **Mailtrap's demo sender** (`hello@demomailtrap.co`) can only send to the email address your Mailtrap account was set
     up with, so the weekly email must go to that address.
   - **NewsAPI's free plan** allows 100 requests a day (the agent uses 24, the frontend up to 48) and its articles are
     about 24 hours old.
   - **Logs:** the agent, the scheduler and the frontend all write to `btc_agent.log`. When it rotates at 1 MB, a few lines
     can land in `btc_agent.log.1` instead. `journalctl` always has every line.

9. **Check backups work.** Take one now on the instance, then list the bucket from your own machine:
   ```bash
   sudo systemctl start btc-agent-db-backup.service && sudo journalctl -u btc-agent-db-backup -n 5
   ```
   ```bash
   aws s3 ls s3://btc-agent-934912969749-eu-west-2-an/btc-agent/ --recursive
   ```
   You should see `btc-agent/trading.db` and one `btc-agent/history/trading-<timestamp>.db`.

## Day-to-day

Commands marked **(laptop)** run on your own machine with the AWS CLI; the rest run on the instance.

| Task | Command |
|---|---|
| Is everything running? | `systemctl status btc-agent btc-agent-app btc-agent-db-restore` |
| Follow the agent's logs | `sudo journalctl -u btc-agent -f` (or `tail -f /opt/btc-agent/btc_agent/logs/btc_agent.log`) |
| Follow the frontend's logs | `sudo journalctl -u btc-agent-app -f` |
| Did the secrets fetch work? | `sudo journalctl -u btc-agent-secrets` |
| Did the database restore at boot? | `sudo journalctl -u btc-agent-db-restore` |
| When did backups last run, and when's the next? | `sudo journalctl -u btc-agent-db-backup -n 20` and `systemctl list-timers btc-agent-db-backup` |
| Back up right now | `sudo systemctl start btc-agent-db-backup.service` |
| List backups **(laptop)** | `aws s3 ls s3://btc-agent-934912969749-eu-west-2-an/btc-agent/ --recursive` |
| Download the latest backup to inspect **(laptop)** | `aws s3 cp s3://btc-agent-934912969749-eu-west-2-an/btc-agent/trading.db ./trading.db` |
| Stop trading | `sudo systemctl stop btc-agent` |

### Deploying code changes
Commit and push from your laptop first. Then, on the instance (`git fetch` alone doesn't change any files; `git pull` does):
```bash
sudo git -C /opt/btc-agent pull
sudo /opt/btc-agent/.venv/bin/pip install --no-cache-dir -r /opt/btc-agent/btc_agent/requirements.txt
sudo systemctl restart btc-agent btc-agent-app
```
- **Do it between about :20 and :55 past the hour.** The agent runs on the hour, and restarting `btc-agent` stops a run in
  progress. Each trade is saved all-or-nothing, so nothing is half-written, but that hour's run is lost.
- `main.py` starts as a fresh process every hour, so changes to `main.py`, `src/` or the models are picked up by the next
  run even without a restart. `timer.py` and `app.py` only change after the restart above.
- If the pull changed anything in `deploy/`, repeat steps 5 and 6 (they include `daemon-reload`) before restarting.

### Updating a model after retraining
1. Save the new files from the notebook with **the same filenames** into `btc_agent/models/direction/`
   (`model_simple.keras`, `process_pipeline.joblib`) or `btc_agent/models/volatility/` (`vol_model_simple.keras`,
   `vol_pipeline.joblib`, `vol_model_evaluation.json`). A different filename means changing the names in `models_predict()`
   in `main.py` too.
2. The volatility model's test metrics are read from `vol_model_evaluation.json` on every run, so they update by
   themselves. The direction model's metrics (`DIRECTION_MODEL_INFO` in `src/predictions.py`) and both models' descriptions
   and limitations (also there, and in the agent instructions in `src/agent.py`) are written by hand: update them if the
   model's behaviour changes, e.g. if the volatility model starts beating its persistence baseline.
3. A model that needs more history or new feature columns may need changes in `main.py` (the price fetch is 6 months, and
   the volatility model needs about 109 completed days).
4. Run `python main.py` locally once, then commit, push and deploy as above.

### Changing a key
Update it in Parameter Store from your laptop, then re-fetch the keys on the instance:
```bash
aws ssm put-parameter --region eu-west-2 --name /btc-agent/NAME --value "..." --overwrite
```
```bash
sudo systemctl restart btc-agent-secrets && sudo systemctl restart btc-agent btc-agent-app
```

### Restoring an earlier backup
For example, to undo a run of bad trades. Stop everything that writes to the database, put the chosen copy in place, and
start again:
```bash
aws s3 ls s3://btc-agent-934912969749-eu-west-2-an/btc-agent/history/     # (laptop) pick a timestamp
```
```bash
sudo systemctl stop btc-agent btc-agent-app btc-agent-db-backup.timer
sudo -u btc-agent -H bash -c 'set -a; . /etc/btc-agent/env; set +a; aws s3 cp "$BACKUP_S3_URI/history/trading-<timestamp>.db" /opt/btc-agent/btc_agent/databases/trading.db'
sudo systemctl start btc-agent btc-agent-app btc-agent-db-backup.timer
```
The next hourly backup then makes that copy the latest one in S3.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `btc-agent-secrets` failed: "Couldn't determine the AWS region" | The instance metadata service isn't reachable. Add `Environment=AWS_REGION=eu-west-2` under `[Service]` in `btc-agent-secrets.service`, then repeat step 6. |
| "No parameters found" or AccessDenied from `btc-agent-secrets` | The IAM role isn't attached, its policy doesn't match `iam-policy.json`, or the parameters aren't in `eu-west-2`. |
| The app won't start, and `sudo journalctl -u btc-agent-db-restore` says "Couldn't check for a backup" | The instance can't reach the bucket: the role isn't attached, its policy is out of date, `BACKUP_S3_URI` has the wrong bucket, or there's no network. Fix it, then run `sudo systemctl restart btc-agent-db-restore btc-agent btc-agent-app`. This is deliberate (see the safety rule above). |
| "Database check failed" | The backup in S3 is damaged. Restore an earlier copy from `history/` (see above). |
| "BACKUP_S3_URI isn't set" | Add the `/btc-agent/BACKUP_S3_URI` parameter (step 1), then run `sudo systemctl restart btc-agent-secrets` and restart the other units. |
| "Permission denied" writing `trading.db` or `btc_agent.log` | Re-run the `install -d` line from step 4. |
| `pip install` fails: "No matching distribution found for tensorflow" | The OS is too old (e.g. Amazon Linux 2) or the instance isn't x86_64. Use Amazon Linux 2023 or Ubuntu 22.04+ on x86_64. |
| `pip install` fails: "No space left on device" | The root volume is too small. Increase it to 16 GB or more (EC2 console: Volumes -> Modify), extend the filesystem, and re-run step 4's `pip install`. |
| The run is killed, or `dmesg` shows "Out of memory" | 1 GB of RAM isn't enough for TensorFlow plus Streamlit. Use an instance with 2 GB or more. |
| The log shows `'volatility': 'error'` or `'direction': 'error'` | That model failed; the traceback just above says why (e.g. a missing model file: check it was committed and pulled). The agent still runs and ignores that model. |
| The weekly email arrives at the wrong hour | The instance's timezone (see step 7). |
| Telegram alert "Yahoo Finance request failed after N attempts", or the frontend says it couldn't get the price or history | Yahoo Finance is rate-limiting or blocking the instance's address. Each fetch is already retried (after 5, 15 and 45 seconds in the hourly run; once after 3 seconds in the frontend) before the alert is sent, and that hour's run is skipped without trading. Occasional alerts need nothing doing: the next hourly run tries again. If they keep coming, stopping and starting the instance gives it a new public IP (unless it has an Elastic IP); otherwise the price data needs another source. |
| Telegram alert "the hourly run started at ... was still running an hour later, so it was stopped" | A run hung (e.g. a request that never returned), so the scheduler stopped it before starting the next one; only one run ever exists at a time. Nothing needs doing if it's a one-off. `sudo journalctl -u btc-agent --since "<that hour>"` shows the last thing the run logged, i.e. where it got stuck. |
| The agent or frontend won't start for another reason | `sudo journalctl -u btc-agent -n 50` shows the error. A missing key (e.g. `TELEGRAM_BOT_TOKEN is not set`) means it isn't in Parameter Store under `/btc-agent/`. |
