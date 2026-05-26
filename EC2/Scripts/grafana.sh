source /etc/profile.d/struct8_vars.sh

# Instalar utilitários essenciais e pacote de banco (mariadb105 fornece mysql client no Amazon Linux 2023)
dnf install -y jq mariadb105

# Obter o secret do RDS no Secrets Manager via AWS CLI
DB_SECRET=$(aws secretsmanager get-secret-value --secret-id "$AWS_DB_INSTANCE_SECRET_ARN_0" --region "$REGION" --query SecretString --output text)

# Extrair credenciais e informações do banco
DB_USER=$(echo $DB_SECRET | jq -r .username)
DB_PASS=$(echo $DB_SECRET | jq -r .password)
DB_HOST=$(echo $AWS_DB_INSTANCE_ENDPOINT_0 | cut -d':' -f1)

# Conectar ao banco RDS e criar o schema 'grafana'
mysql -h "$DB_HOST" -u "$DB_USER" -p"$DB_PASS" -e "CREATE DATABASE IF NOT EXISTS grafana;"

# Adicionar repositório oficial do Grafana
cat << 'EOFREPO' > /etc/yum.repos.d/grafana.repo
[grafana]
name=grafana
baseurl=https://rpm.grafana.com
repo_gpgcheck=1
enabled=1
gpgcheck=1
gpgkey=https://rpm.grafana.com/gpg.key
sslverify=1
sslcacert=/etc/pki/tls/certs/ca-bundle.crt
EOFREPO

# Instalar a última versão do Grafana disponível pelo repositório oficial
dnf install -y grafana

# Injetar as credenciais nas variáveis de inicialização do Grafana (substitui o SQLite padrão pelo MySQL do RDS)
cat << EOFCONF > /etc/sysconfig/grafana-server
GF_DATABASE_TYPE=mysql
GF_DATABASE_HOST=$DB_HOST:3306
GF_DATABASE_NAME=grafana
GF_DATABASE_USER=$DB_USER
GF_DATABASE_PASSWORD=$DB_PASS
EOFCONF

# Habilitar o Grafana para iniciar no boot e subir o serviço
systemctl daemon-reload
systemctl enable grafana-server
systemctl start grafana-server
