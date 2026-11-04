#!/usr/bin/env bash
# Pulls the btc-agent's API keys from AWS Systems Manager Parameter Store and writes them to a
# root-owned, group-readable env file that systemd loads via EnvironmentFile=. Runs on every boot
# (see btc-agent-secrets.service), so a replacement instance re-fetches the current keys on its
# own as long as the instance role is attached -- nothing is ever typed into the box by hand.
#
# Installed as a root-owned copy at /usr/local/sbin/btc-agent-fetch-secrets (see README step 5) and
# run as root by btc-agent-secrets.service.
#
# Requires: the AWS CLI, and an instance role allowed to ssm:GetParametersByPath on /btc-agent and
# /btc-agent/*, and kms:Decrypt via the ssm service (see iam-policy.json).

set -euo pipefail

SSM_PATH="/btc-agent"
ENV_FILE="/etc/btc-agent/env"
ENV_GROUP="btc-agent" # Matches the system user the services run as (see btc-agent.service)
IMDS="http://169.254.169.254/latest"
ATTEMPTS=5 # The network (or instance metadata service) may not be ready the moment the instance boots, so retry

# Region: use AWS_REGION if set, otherwise ask the instance metadata service. New instances require IMDSv2,
# which needs a session token first; a plain IMDSv1 request is rejected and would leave the region empty.
region_from_imds() {
    local token
    token="$(curl -sf -X PUT "${IMDS}/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")" || return 1
    curl -sf -H "X-aws-ec2-metadata-token: ${token}" "${IMDS}/meta-data/placement/region"
}

REGION="${AWS_REGION:-}"
for attempt in $(seq 1 "$ATTEMPTS"); do
    [ -n "$REGION" ] && break
    REGION="$(region_from_imds || true)"
    [ -n "$REGION" ] || sleep $((attempt * 3))
done
if [ -z "$REGION" ]; then
    echo "Couldn't determine the AWS region from the instance metadata service; set AWS_REGION in btc-agent-secrets.service." >&2
    exit 1
fi

mkdir -p "$(dirname "$ENV_FILE")"
umask 077 # Belt-and-braces: anything created from here on starts out root-only

TMP_FILE="$(mktemp)"
trap 'rm -f "$TMP_FILE"' EXIT

# SENSITIVE: decrypted secret values pass through this pipe into the temp file; they're never echoed or logged
fetch() {
    aws ssm get-parameters-by-path \
        --path "$SSM_PATH" \
        --with-decryption \
        --recursive \
        --region "$REGION" \
        --query "Parameters[].{Name:Name,Value:Value}" \
        --output text
}

for attempt in $(seq 1 "$ATTEMPTS"); do
    if PARAMETERS="$(fetch)"; then
        break
    fi
    if [ "$attempt" -eq "$ATTEMPTS" ]; then
        echo "Fetching parameters from SSM failed after ${ATTEMPTS} attempts -- check the instance role and network." >&2
        exit 1
    fi
    sleep $((attempt * 3))
done

while IFS=$'\t' read -r name value; do
    [ -n "$name" ] || continue
    key="${name#"${SSM_PATH}"/}"
    if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then # e.g. a nested /btc-agent/sub/NAME, which isn't a valid env var name
        echo "Skipping parameter ${name}: ${key} isn't a valid environment variable name." >&2
        continue
    fi
    printf '%s=%s\n' "$key" "$value"
done <<< "$PARAMETERS" > "$TMP_FILE"

if [ ! -s "$TMP_FILE" ]; then
    echo "No parameters found under ${SSM_PATH}/ in region ${REGION} -- check the path and the instance role." >&2
    exit 1
fi

# Pass the region on too (unless a parameter already set it), so the AWS CLI calls in db_backup.sh, run by the other
# units with this file as their environment, don't depend on the CLI finding the region by itself
grep -q '^AWS_REGION=' "$TMP_FILE" || printf 'AWS_REGION=%s\n' "$REGION" >> "$TMP_FILE"

mv "$TMP_FILE" "$ENV_FILE"
chown "root:${ENV_GROUP}" "$ENV_FILE"
chmod 640 "$ENV_FILE"
echo "Wrote $(wc -l < "$ENV_FILE" | tr -d " ") secret(s) to $ENV_FILE: $(cut -d= -f1 "$ENV_FILE" | paste -sd ' ' -)" # Names only, never values
