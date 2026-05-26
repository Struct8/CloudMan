source /etc/profile.d/struct8_vars.sh

dnf update -y
dnf install -y haproxy jq mariadb105

# 1. Script Dinâmico HAProxy
cat << 'EOF_SCRIPT' > /usr/local/bin/update-haproxy-monitoring.sh
#!/bin/bash
source /etc/profile.d/struct8_vars.sh

LOKI_IPS=\$(aws ec2 describe-instances --region \$REGION --filters "Name=tag:Name,Values=loki" "Name=instance-state-name,Values=running" --query 'Reservations[*].Instances[*].PrivateIpAddress' --output text)
PROM_IP=\$(aws ec2 describe-instances --region \$REGION --filters "Name=tag:Name,Values=prometheus" "Name=instance-state-name,Values=running" --query 'Reservations[*].Instances[*].PrivateIpAddress' --output text)

cat << 'EOFCONF' > /etc/haproxy/haproxy.cfg
global
    log /dev/log local0
    maxconn 4096
    user haproxy
    group haproxy

defaults
    log global
    mode http
    option httplog
    option dontlognull
    retries 3
    timeout connect 5000
    timeout client  50000
    timeout server  50000

frontend loki_front
    bind *:3100
    default_backend loki_back

backend loki_back
    balance roundrobin

frontend prometheus_front
    bind *:9090
    default_backend prometheus_back

backend prometheus_back
    balance roundrobin
EOFCONF

INDEX=1
for IP in \$LOKI_IPS; do
    echo "    server loki\$INDEX \$IP:3100 check" >> /etc/haproxy/haproxy.cfg
    INDEX=\$((INDEX+1))
done

if [ -n "\$PROM_IP" ]; then
    echo "    server prometheus1 \$PROM_IP:9090 check" >> /etc/haproxy/haproxy.cfg
fi

systemctl reload haproxy || systemctl restart haproxy
EOF_SCRIPT

chmod +x /usr/local/bin/update-haproxy-monitoring.sh
/usr/local/bin/update-haproxy-monitoring.sh
systemctl enable --now haproxy
echo "* * * * * root /usr/local/bin/update-haproxy-monitoring.sh > /dev/null 2>&1" > /etc/cron.d/haproxy-monitoring

# 2. Instalação Grafana
cat << 'EOF_REPO' > /etc/yum.repos.d/grafana.repo
[grafana]
name=grafana
baseurl=https://rpm.grafana.com
repo_gpgcheck=1
enabled=1
gpgcheck=1
gpgkey=https://rpm.grafana.com/gpg.key
sslverify=1
sslcacert=/etc/pki/tls/certs/ca-bundle.crt
EOF_REPO
dnf install -y grafana

# 3. Integrando com o AWS RDS (Extrair segredo e criar schema)
DB_SECRET=\$(aws secretsmanager get-secret-value --secret-id "\$AWS_DB_INSTANCE_SECRET_ARN_0" --region "\$REGION" --query SecretString --output text)
DB_USER=\$(echo \$DB_SECRET | jq -r .username)
DB_PASS=\$(echo \$DB_SECRET | jq -r .password)
DB_HOST_ONLY=\$(echo \$AWS_DB_INSTANCE_ENDPOINT_0 | cut -d: -f1)

# Conectar no RDS e criar a database grafana
mysql -h "\$DB_HOST_ONLY" -u "\$DB_USER" -p"\$DB_PASS" -e "CREATE DATABASE IF NOT EXISTS grafana;"

cat << EOF_INI > /etc/grafana/grafana.ini
[database]
type = mysql
host = \$AWS_DB_INSTANCE_ENDPOINT_0
name = grafana
user = \$DB_USER
password = \$DB_PASS
EOF_INI
chown root:grafana /etc/grafana/grafana.ini

# 4. Configurar Datasources
mkdir -p /etc/grafana/provisioning/datasources/
cat << 'EOF_DS' > /etc/grafana/provisioning/datasources/monitoring.yaml
apiVersion: 1
datasources:
  - name: Loki
    type: loki
    access: proxy
    url: http://localhost:3100
    isDefault: true
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://localhost:9090
    isDefault: false
EOF_DS

systemctl daemon-reload
systemctl enable --now grafana-server
# --- END GRAFANA & HAPROXY INSTALLATION ---
