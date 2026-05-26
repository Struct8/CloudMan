# --- BEGIN LOKI CLUSTER INSTALLATION ---
source /etc/profile.d/struct8_vars.sh

LOKI_VERSION="2.9.8"
useradd --no-create-home --shell /bin/false loki
mkdir -p /etc/loki /var/lib/loki/tsdb-index /var/lib/loki/tsdb-cache
chown -R loki:loki /etc/loki /var/lib/loki

dnf update -y
dnf install -y unzip wget jq

# Baixar e instalar binário do Loki
cd /tmp
wget "https://github.com/grafana/loki/releases/download/v$${LOKI_VERSION}/loki-linux-amd64.zip"
unzip loki-linux-amd64.zip
mv loki-linux-amd64 /usr/local/bin/loki
chown loki:loki /usr/local/bin/loki
chmod +x /usr/local/bin/loki

# Resgatar IP local para Configuração
PRIVATE_IP=\$(curl -s http://169.254.169.254/latest/meta-data/local-ipv4)

# Gerar arquivo de configuração com S3 Backend
cat << EOF_CONF > /etc/loki/loki-config.yaml
auth_enabled: false

server:
  http_listen_port: 3100
  grpc_listen_port: 9096

common:
  instance_addr: \$PRIVATE_IP
  path_prefix: /var/lib/loki
  storage:
    s3:
      region: \$REGION
      bucketnames: \$AWS_S3_BUCKET_NAME_0
  replication_factor: 1
  ring:
    kvstore:
      store: memberlist

memberlist:
  bind_port: 7946

schema_config:
  configs:
    - from: "2024-01-01"
      store: tsdb
      object_store: s3
      schema: v13
      index:
        prefix: index_
        period: 24h

storage_config:
  aws:
    s3: s3://\$REGION/\$AWS_S3_BUCKET_NAME_0
  tsdb_shipper:
    active_index_directory: /var/lib/loki/tsdb-index
    cache_location: /var/lib/loki/tsdb-cache
    shared_store: s3
EOF_CONF
chown loki:loki /etc/loki/loki-config.yaml

# Script que descobre os outros Lokis no Auto Scaling para formar o anel (cluster)
cat << 'EOF_RUN' > /usr/local/bin/run-loki.sh
#!/bin/bash
source /etc/profile.d/struct8_vars.sh

# Busca IPs de outros Lokis subindo no mesmo cluster
LOKI_IPS=\$(aws ec2 describe-instances --region \$REGION \
  --filters "Name=tag:Name,Values=loki" "Name=instance-state-name,Values=running" \
  --query 'Reservations[*].Instances[*].PrivateIpAddress' --output text | tr '\t' ',')

JOIN_ARGS=""
if [ -n "\$LOKI_IPS" ]; then
    JOIN_ARGS="-memberlist.join=\$LOKI_IPS"
fi

exec /usr/local/bin/loki -config.file=/etc/loki/loki-config.yaml \$JOIN_ARGS
EOF_RUN
chmod +x /usr/local/bin/run-loki.sh

# Criar e iniciar o serviço
cat << 'EOF_SVC' > /etc/systemd/system/loki.service
[Unit]
Description=Loki service
After=network.target

[Service]
Type=simple
User=loki
ExecStart=/usr/local/bin/run-loki.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF_SVC

systemctl daemon-reload
systemctl enable --now loki
# --- END LOKI CLUSTER INSTALLATION ---
