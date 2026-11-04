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
├── .venv/                       # Python virtual environment, owned by root
└── btc_agent/                   # the app: working directory for the agent and frontend
    ├── databases/trading.db     # writable by btc-agent; restored from S3 or created on the first run
    ├── logs/btc_agent.log       # writable by btc-agent; created on the first run
    └── deploy/                  # this folder
/etc/btc-agent/env               # the keys, written at every boot
/var/lib/btc-agent/              # the btc-agent user's home (Streamlit and Keras cache files)
```

The code is owned by root and the `btc-agent` user can only write `databases/` and `logs/`, so the running agent can't
modify its own code, and `git pull` always runs as root.

## Before you start

- **Instance:** in `eu-west-2`, with at least 2 GB of RAM (e.g. `t3.small`). TensorFlow and Streamlit together don't fit
  in 1 GB.
- **Python 3.10 or newer** (3.12 matches development). Amazon Linux 2023's default `python3` is 3.9, which is too old:
  install `python3.12` with `sudo dnf install -y python3.12`. Ubuntu 24.04's default `python3` is already 3.12.
- **Git and the AWS CLI** on the instance. Amazon Linux 2023 includes the AWS CLI; on Ubuntu install it first
  (see the [AWS CLI install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)).
- **Repo access:** if the GitHub repo is private, the instance needs a deploy key or token to clone and pull it.
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
   sudo git clone <your-repo-url> /opt/btc-agent
   sudo python3.12 -m venv /opt/btc-agent/.venv   # or python3 on Ubuntu 24.04
   sudo /opt/btc-agent/.venv/bin/pip install -r /opt/btc-agent/btc_agent/requirements.txt
   # The only places the services write to:
   sudo install -d -o btc-agent -g btc-agent /opt/btc-agent/btc_agent/databases /opt/btc-agent/btc_agent/logs
   ```

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

8. **Do one run by hand** before trusting the hourly timer. This loads the keys and runs `main.py` once as the service user:
   ```bash
   sudo -u btc-agent -H bash -c 'set -a; . /etc/btc-agent/env; set +a; cd /opt/btc-agent/btc_agent && ../.venv/bin/python main.py'
   ```
   Then check `/opt/btc-agent/btc_agent/logs/btc_agent.log` for errors, and open `http://<instance-public-ip>:8501` to see
   the frontend.

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
`git fetch` alone doesn't change any files; `git pull` does:
```bash
sudo git -C /opt/btc-agent pull
sudo /opt/btc-agent/.venv/bin/pip install -r /opt/btc-agent/btc_agent/requirements.txt
sudo systemctl restart btc-agent btc-agent-app
```
If the pull changed anything in `deploy/`, repeat steps 5 and 6 (they include `daemon-reload`) before restarting.

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
| The agent or frontend won't start for another reason | `sudo journalctl -u btc-agent -n 50` shows the error. A missing key (e.g. `TELEGRAM_BOT_TOKEN is not set`) means it isn't in Parameter Store under `/btc-agent/`. |
