# --- BEGIN GRAFANA & HAPROXY INSTALLATION ---
# Carregar as variáveis de ambiente exportadas acima
source /etc/profile.d/struct8_vars.sh

# Atualizar e instalar HAProxy
dnf update -y
dnf install -y haproxy

# Criar script de atualização dinâmica do HAProxy (Monitorando Loki e Prometheus)
cat << 'EOF_SCRIPT' > /usr/local/bin/update-haproxy-monitoring.sh
#!/bin/bash
source /etc/profile.d/struct8_vars.sh

# 1. Buscar os IPs do Loki
LOKI_IPS=$(aws ec2 describe-instances --region $REGION \
  --filters "Name=tag:Name,Values=loki" "Name=instance-state-name,Values=running" \
  --query 'Reservations[*].Instances[*].PrivateIpAddress' --output text)

# 2. Buscar o IP do Prometheus
PROM_IP=$(aws ec2 describe-instances --region $REGION \
  --filters "Name=tag:Name,Values=prometheus" "Name=instance-state-name,Values=running" \
  --query 'Reservations[*].Instances[*].PrivateIpAddress' --output text)

# 3. Gerar arquivo base do HAProxy
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

# --- FRONTEND/BACKEND LOKI ---
frontend loki_front
    bind *:3100
    default_backend loki_back

backend loki_back
    balance roundrobin

# --- FRONTEND/BACKEND PROMETHEUS ---
frontend prometheus_front
    bind *:9090
    default_backend prometheus_back

backend prometheus_back
    balance roundrobin
EOFCONF

# 4. Injetar instâncias do Loki no backend
INDEX=1
for IP in $LOKI_IPS; do
    echo "    server loki$INDEX $IP:3100 check" >> /etc/haproxy/haproxy.cfg
    INDEX=$((INDEX+1))
done

# 5. Injetar instância do Prometheus no backend (se existir IP válido)
if [ -n "$PROM_IP" ]; then
    echo "    server prometheus1 $PROM_IP:9090 check" >> /etc/haproxy/haproxy.cfg
fi

# 6. Aplicar nova configuração ao HAProxy
systemctl reload haproxy || systemctl restart haproxy
EOF_SCRIPT

# Dar permissão de execução ao script
chmod +x /usr/local/bin/update-haproxy-monitoring.sh

# Executar a primeira vez e habilitar serviço
/usr/local/bin/update-haproxy-monitoring.sh
systemctl enable --now haproxy

# Configurar Cron Job (roda a cada minuto para manter IPs atualizados)
echo "* * * * * root /usr/local/bin/update-haproxy-monitoring.sh > /dev/null 2>&1" > /etc/cron.d/haproxy-monitoring

# Configurar Repositório e Instalar o Grafana
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

# Configurar o Provisionamento Automático de Datasources no Grafana
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

# Iniciar o Grafana
systemctl daemon-reload
systemctl enable --now grafana-server
# --- END GRAFANA & HAPROXY INSTALLATION ---
