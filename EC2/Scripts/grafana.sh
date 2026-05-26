# --- BEGIN GRAFANA & HAPROXY INSTALLATION ---
# Carregar as variáveis de ambiente exportadas acima (ex: $REGION)
source /etc/profile.d/struct8_vars.sh

# Atualizar e instalar pacotes necessários (HAProxy)
dnf update -y
dnf install -y haproxy

# Criar script de atualização dinâmica do HAProxy para apontar para as instâncias do Loki
# Este script buscará as instâncias baseadas na Tag 'Name=loki' via AWS CLI
cat << 'EOF_SCRIPT' > /usr/local/bin/update-haproxy-loki.sh
#!/bin/bash
source /etc/profile.d/struct8_vars.sh

# Obter IPs privados do ASG do Loki usando a tag Name=loki
LOKI_IPS=$(aws ec2 describe-instances --region $REGION \
  --filters "Name=tag:Name,Values=loki" "Name=instance-state-name,Values=running" \
  --query 'Reservations[*].Instances[*].PrivateIpAddress' --output text)

# Gerar arquivo de configuração base do HAProxy
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
EOFCONF

# Adicionar os IPs retornados como servidores de backend do HAProxy
INDEX=1
for IP in $LOKI_IPS; do
    echo "    server loki$INDEX $IP:3100 check" >> /etc/haproxy/haproxy.cfg
    INDEX=$((INDEX+1))
done

# Recarregar o HAProxy para aplicar mudanças (ou iniciar se estiver parado)
systemctl reload haproxy || systemctl restart haproxy
EOF_SCRIPT
chmod +x /usr/local/bin/update-haproxy-loki.sh

# Executar o script pela primeira vez e habilitar o HAProxy no boot
/usr/local/bin/update-haproxy-loki.sh
systemctl enable --now haproxy

# Configurar um cron job para manter a lista de IPs do Loki sempre atualizada (a cada 1 minuto)
echo "* * * * * root /usr/local/bin/update-haproxy-loki.sh > /dev/null 2>&1" > /etc/cron.d/haproxy-loki

# Configurar Repositório do Grafana (última versão)
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

# Instalar o Grafana
dnf install -y grafana

# Fazer o provisionamento do Datasource do Loki no Grafana
# Ele irá se comunicar em localhost:3100, apontando pro HAProxy instalado na mesma máquina
mkdir -p /etc/grafana/provisioning/datasources/
cat << 'EOF_DS' > /etc/grafana/provisioning/datasources/loki.yaml
apiVersion: 1
datasources:
  - name: Loki
    type: loki
    access: proxy
    url: http://localhost:3100
    isDefault: true
EOF_DS

# Habilitar e iniciar o serviço do Grafana
systemctl daemon-reload
systemctl enable --now grafana-server
# --- END GRAFANA & HAPROXY INSTALLATION ---
