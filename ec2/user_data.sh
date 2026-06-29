#!/bin/bash
set -euxo pipefail

REGION="${AWS_REGION:-eu-north-1}"
APP_DIR="/opt/medallion"

dnf install -y docker
systemctl enable docker
systemctl start docker

curl -L "https://github.com/docker/compose/releases/download/v2.24.5/docker-compose-$(uname -s)-$(uname -m)" \
  -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose

mkdir -p "${APP_DIR}"
cd "${APP_DIR}"

# Swap za t2.micro (1 GB RAM)
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

POSTGRES_PASSWORD=$(aws ssm get-parameter \
  --name "/medallion/postgres/password" \
  --with-decryption \
  --region "${REGION}" \
  --query "Parameter.Value" \
  --output text)

SUPERSET_ADMIN_PASSWORD=$(aws ssm get-parameter \
  --name "/medallion/superset/admin-password" \
  --with-decryption \
  --region "${REGION}" \
  --query "Parameter.Value" \
  --output text)

PRIVATE_IP=$(curl -s http://169.254.169.254/latest/meta-data/local-ipv4)

aws ssm put-parameter \
  --name "/medallion/postgres/host" \
  --value "${PRIVATE_IP}" \
  --type String \
  --overwrite \
  --region "${REGION}"

export POSTGRES_DB=medallion
export POSTGRES_USER=medallion
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD}"
export SUPERSET_ADMIN_USER=admin
export SUPERSET_ADMIN_PASSWORD="${SUPERSET_ADMIN_PASSWORD}"
export SUPERSET_SECRET_KEY="$(openssl rand -hex 32)"

docker-compose up -d
