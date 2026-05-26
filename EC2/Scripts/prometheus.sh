# --- BEGIN PROMETHEUS INSTALLATION ---
# Definir versão do Prometheus
PROM_VERSION="2.52.0"

# Criar usuário e diretórios necessários
useradd --no-create-home --shell /bin/false prometheus
mkdir -p /etc/prometheus /var/lib/prometheus
chown prometheus:prometheus /etc/prometheus /var/lib/prometheus

# Baixar e extrair o Prometheus
cd /tmp
wget https://github.com/prometheus/prometheus/releases/download/v$${PROM_VERSION}/prometheus-$${PROM_VERSION}.linux-amd64.tar.gz
tar -xvf prometheus-$${PROM_VERSION}.linux-amd64.tar.gz

# Mover binários e ajustar permissões
cp prometheus-$${PROM_VERSION}.linux-amd64/prometheus /usr/local/bin/
cp prometheus-$${PROM_VERSION}.linux-amd64/promtool /usr/local/bin/
chown prometheus:prometheus /usr/local/bin/prometheus /usr/local/bin/promtool

# Mover arquivos de configuração e diretórios de console
cp -r prometheus-$${PROM_VERSION}.linux-amd64/consoles /etc/prometheus
cp -r prometheus-$${PROM_VERSION}.linux-amd64/console_libraries /etc/prometheus
cp prometheus-$${PROM_VERSION}.linux-amd64/prometheus.yml /etc/prometheus/
chown -R prometheus:prometheus /etc/prometheus/consoles /etc/prometheus/console_libraries /etc/prometheus/prometheus.yml

# Limpar arquivos temporários
rm -rf /tmp/prometheus-$${PROM_VERSION}.linux-amd64.tar.gz /tmp/prometheus-$${PROM_VERSION}.linux-amd64

# Criar o arquivo de serviço do Systemd para o Prometheus
cat << 'EOF' > /etc/systemd/system/prometheus.service
[Unit]
Description=Prometheus Time Series Collection and Processing Server
Wants=network-online.target
After=network-online.target

[Service]
User=prometheus
Group=prometheus
Type=simple
ExecStart=/usr/local/bin/prometheus \
    --config.file /etc/prometheus/prometheus.yml \
    --storage.tsdb.path /var/lib/prometheus/ \
    --web.console.templates=/etc/prometheus/consoles \
    --web.console.libraries=/etc/prometheus/console_libraries

[Install]
WantedBy=multi-user.target
EOF

# Recarregar o daemon do systemd, habilitar no boot e iniciar o Prometheus
systemctl daemon-reload
systemctl enable prometheus
systemctl start prometheus
# --- END PROMETHEUS INSTALLATION ---
