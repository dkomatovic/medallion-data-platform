#!/bin/bash
set -euxo pipefail

REGION="${AWS_REGION:-eu-north-1}"
APP_DIR="/opt/medallion"

# Instaliraj Docker
dnf install -y docker
systemctl enable docker
systemctl start docker

# Čekaj da Docker bude spreman
sleep 10

# Instaliraj docker-compose
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

# Uzmi lozinke iz SSM
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

# Uzmi private IP i upiši u SSM
PRIVATE_IP=$(curl -s http://169.254.169.254/latest/meta-data/local-ipv4)

aws ssm put-parameter \
  --name "/medallion/postgres/host" \
  --value "${PRIVATE_IP}" \
  --type String \
  --overwrite \
  --region "${REGION}"

# Napravi .env fajl za docker-compose
cat > "${APP_DIR}/.env" <<EOF
POSTGRES_DB=medallion
POSTGRES_USER=medallion
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
SUPERSET_ADMIN_USER=admin
SUPERSET_ADMIN_PASSWORD=${SUPERSET_ADMIN_PASSWORD}
SUPERSET_SECRET_KEY=$(openssl rand -hex 32)
EOF

# Pokreni kontejnere
docker-compose up -d

# Loguj status
docker-compose ps