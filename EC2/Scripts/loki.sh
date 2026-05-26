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

# Gerar arquivo de configuração otimizado para os componentes do Cluster
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
  replication_factor: 3
  ring:
    kvstore:
      store: memberlist

# --- CONFIGURAÇÃO ESPECÍFICA DO DISTRIBUTOR ---
distributor:
  ring:
    kvstore:
      store: memberlist

# --- CONFIGURAÇÃO ESPECÍFICA DO INGESTER ---
ingester:
  lifecycler:
    ring:
      kvstore:
        store: memberlist
      replication_factor: 3
    final_sleep: 0s
  chunk_idle_period: 1h
  max_chunk_age: 1h
  chunk_target_size: 1572864
  chunk_retain_period: 30s

# --- CONFIGURAÇÃO ESPECÍFICA DO QUERY FRONTEND ---
query_frontend:
  max_outstanding_per_tenant: 2048
  compress_responses: true

# --- CONFIGURAÇÃO ESPECÍFICA DO QUERIER ---
querier:
  query_timeout: 5m
  max_concurrent: 8

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

# Script de descoberta dinâmica do Memberlist para os 3 nós
cat << 'EOF_RUN' > /usr/local/bin/run-loki.sh
#!/bin/bash
source /etc/profile.d/struct8_vars.sh

# Busca IPs de outros Lokis no ASG
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

# Criar serviço Systemd
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
