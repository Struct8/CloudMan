#!/bin/bash
# EC2/Scripts/WordPressProfessional.sh
# === Script de Configuração do WordPress em EC2 com EFS, RDS, ProxySQL e X-Ray ===
# Versão: 3.2.0 (PHP 8.2 e WordPress Dinâmico)
# - ADICIONADO: Variável de ambiente 'WP_VERSION' para controlar a versão do WordPress. Se vazia ou "latest", instala a mais recente.
# - MANTIDO: PHP 8.2 e instrumentação manual robusta do X-Ray via UDP.

# --- Configurações Chave ---
readonly THIS_SCRIPT_TARGET_PATH="/usr/local/bin/wordpress_setup_v3.2.0.sh"
readonly APACHE_USER="apache"
readonly ENV_VARS_FILE="/etc/wordpress_setup_v3.2.0_env_vars.sh"

# --- Variáveis Globais ---
LOG_FILE="/var/log/wordpress_setup_v3.2.0.log"
MOUNT_POINT="/var/www/html"
WP_DOWNLOAD_DIR="/tmp/wp_download_temp"
WP_FINAL_CONTENT_DIR="/tmp/wp_final_efs_content"
ACTIVE_CONFIG_FILE_EFS="$MOUNT_POINT/wp-config.php"
CONFIG_SAMPLE_ON_EFS="$MOUNT_POINT/wp-config-sample.php"
HEALTH_CHECK_FILE_PATH_EFS="$MOUNT_POINT/healthcheck.php"
MARKER_LINE_SED_PATTERN='\/\* That'\''s all, stop editing! Happy publishing\. \*\/'

# --- Variáveis Essenciais (Esperadas do Ambiente) ---
essential_vars=(
    "AWS_EFS_FILE_SYSTEM_TARGET_ID_0"
    "AWS_SECRETSMANAGER_SECRET_VERSION_SOURCE_REGION_0"
    "AWS_SECRETSMANAGER_SECRET_VERSION_SOURCE_NAME_0"
    "AWS_DB_INSTANCE_TARGET_ENDPOINT_0"
    "AWS_DB_INSTANCE_TARGET_NAME_0"
    "WPDOMAIN"
    "ACCOUNT"
    "AWS_EFS_ACCESS_POINT_TARGET_ID_0"
    "AWS_S3_BUCKET_TARGET_NAME_0"
    "AWS_S3_BUCKET_TARGET_NAME_SCRIPT"
    "AWS_S3_BUCKET_TARGET_REGION_SCRIPT"
)

# --- Funções Auxiliares ---

mount_efs() {
    local efs_id=$1
    local mount_point_arg=$2
    local efs_ap_id="${AWS_EFS_ACCESS_POINT_TARGET_ID_0:-}"
    local max_retries=5
    local retry_delay_seconds=15
    local attempt_num=1

    echo "INFO: Tentando montar EFS '$efs_id' em '$mount_point_arg' via AP '$efs_ap_id' (até $max_retries tentativas)..."
    while [ $attempt_num -le $max_retries ]; do
        echo "INFO: Tentativa de montagem EFS: $attempt_num de $max_retries..."
        if mount | grep -q "on ${mount_point_arg} type efs"; then
            echo "INFO: EFS já está montado em '$mount_point_arg'."
            return 0
        fi

        sudo mkdir -p "$mount_point_arg"
        local mount_options="tls"
        local mount_source="$efs_id:/"
        if [ -n "$efs_ap_id" ]; then
            mount_options="tls,accesspoint=$efs_ap_id"
            mount_source="$efs_id"
            echo "INFO: Usando Access Point '$efs_ap_id'."
        else
            echo "INFO: Não usando Access Point."
        fi

        if sudo timeout 30 mount -t efs -o "$mount_options" "$mount_source" "$mount_point_arg" -v; then
            echo "INFO: EFS montado com sucesso em '$mount_point_arg' na tentativa $attempt_num."
            if ! grep -q "${mount_point_arg} efs" /etc/fstab; then
                local fstab_mount_options="_netdev,${mount_options}"
                local fstab_entry="$mount_source $mount_point_arg efs $fstab_mount_options 0 0"
                echo "$fstab_entry" | sudo tee -a /etc/fstab >/dev/null
                echo "INFO: Entrada adicionada ao /etc/fstab: '$fstab_entry'"
            fi
            return 0
        else
            echo "AVISO: Falha ao montar EFS na tentativa $attempt_num. Código de saída: $?"
            if [ $attempt_num -lt $max_retries ]; then
                echo "INFO: Aguardando $retry_delay_seconds segundos..."
                sleep $retry_delay_seconds
            fi
        fi
        attempt_num=$((attempt_num + 1))
    done

    echo "ERRO CRÍTICO: Falha ao montar EFS após $max_retries tentativas."
    ip addr
    dmesg | tail -n 20
    exit 1
}

create_wp_config_template() {
    local target_file_on_efs="$1"
    local primary_wpdomain_for_fallback="$2"
    local db_name="$3"
    local db_user="$4"
    local db_password="$5"
    local db_host="$6"
    local temp_config_file

    temp_config_file=$(mktemp /tmp/wp-config.XXXXXX.php)
    sudo chmod 644 "$temp_config_file"
    trap 'rm -f "$temp_config_file"' RETURN

    echo "INFO: Criando wp-config.php em '$temp_config_file' para EFS '$target_file_on_efs' com DB_HOST: '$db_host'..."
    if [ ! -f "$CONFIG_SAMPLE_ON_EFS" ]; then
        echo "ERRO: Arquivo de exemplo '$CONFIG_SAMPLE_ON_EFS' não encontrado."
        exit 1
    fi

    sudo cp "$CONFIG_SAMPLE_ON_EFS" "$temp_config_file"

    SAFE_DB_NAME=$(echo "$db_name" | sed -e 's/[&\\/]/\\&/g' -e "s/'/\\'/g")
    SAFE_DB_USER=$(echo "$db_user" | sed -e 's/[&\\/]/\\&/g' -e "s/'/\\'/g")
    SAFE_DB_PASSWORD=$(echo "$db_password" | sed -e 's/[&\\/]/\\&/g' -e "s/'/\\'/g")
    SAFE_DB_HOST=$(echo "$db_host" | sed -e 's/[&\\/]/\\&/g' -e "s/'/\\'/g")

    sed -i "s/database_name_here/$SAFE_DB_NAME/g" "$temp_config_file"
    sed -i "s/username_here/$SAFE_DB_USER/g" "$temp_config_file"
    sed -i "s/password_here/$SAFE_DB_PASSWORD/g" "$temp_config_file"
    sed -i "s/localhost/$SAFE_DB_HOST/g" "$temp_config_file"

    SALT=$(curl -sL https://api.wordpress.org/secret-key/1.1/salt/)
    if [ -n "$SALT" ]; then
        TEMP_SALT_FILE_INNER=$(mktemp /tmp/salts.XXXXXX)
        echo "$SALT" >"$TEMP_SALT_FILE_INNER"
        sed -i -e '/^define( *'\''AUTH_KEY'\''/d' -e '/^define( *'\''SECURE_AUTH_KEY'\''/d' -e '/^define( *'\''LOGGED_IN_KEY'\''/d' -e '/^define( *'\''NONCE_KEY'\''/d' -e '/^define( *'\''AUTH_SALT'\''/d' -e '/^define( *'\''SECURE_AUTH_SALT'\''/d' -e '/^define( *'\''LOGGED_IN_SALT'\''/d' -e '/^define( *'\''NONCE_SALT'\''/d' "$temp_config_file"
        sed -i "/$MARKER_LINE_SED_PATTERN/r $TEMP_SALT_FILE_INNER" "$temp_config_file"
        rm -f "$TEMP_SALT_FILE_INNER"
        echo "INFO: SALTs de segurança configurados."
    else
        echo "ERRO: Falha ao obter SALTs de segurança do WordPress.org."
    fi

    PHP_DEFINES_BLOCK_CONTENT=$(cat <<EOPHP
// --- Configurações Adicionadas pelo Script de Setup ---
\$site_scheme = 'https';
\$site_host = '$primary_wpdomain_for_fallback'; // Fallback
if (!empty(\$_SERVER['HTTP_X_FORWARDED_HOST'])) {
    \$hosts = explode(',', \$_SERVER['HTTP_X_FORWARDED_HOST']);
    \$site_host = trim(\$hosts[0]);
} elseif (!empty(\$_SERVER['HTTP_HOST'])) {
    \$site_host = \$_SERVER['HTTP_HOST'];
}

if (isset(\$_SERVER['HTTP_X_FORWARDED_PROTO']) && strtolower(\$_SERVER['HTTP_X_FORWARDED_PROTO']) === 'https') {
    \$_SERVER['HTTPS'] = 'on';
}

define('WP_HOME', \$site_scheme . '://' . \$site_host);
define('WP_SITEURL', \$site_scheme . '://' . \$site_host);
define('FS_METHOD', 'direct');
// --- Fim das Configurações Adicionadas ---
EOPHP
)
    TEMP_DEFINES_FILE_INNER=$(mktemp /tmp/defines.XXXXXX)
    echo -e "\n$PHP_DEFINES_BLOCK_CONTENT" >"$TEMP_DEFINES_FILE_INNER"
    sed -i "/$MARKER_LINE_SED_PATTERN/r $TEMP_DEFINES_FILE_INNER" "$temp_config_file"
    rm -f "$TEMP_DEFINES_FILE_INNER"
    echo "INFO: Definições de WP_HOME e WP_SITEURL configuradas."

    if sudo -u "$APACHE_USER" cp "$temp_config_file" "$target_file_on_efs"; then
        echo "INFO: Arquivo '$target_file_on_efs' criado com sucesso."
    else
        echo "ERRO CRÍTICO: Falha ao copiar para '$target_file_on_efs' como '$APACHE_USER'."
        exit 1
    fi
}

setup_and_configure_proxysql() {
    local rds_host="$1"
    local rds_port="$2"
    local db_user="$3"
    local db_pass="$4"
    
    echo "INFO (ProxySQL): Iniciando configuração idempotente do ProxySQL..."
    run_proxysql_admin() { mysql -u admin -padmin -h 127.0.0.1 -P 6032 -e "$1"; }
    
    if ! sudo systemctl start proxysql; then
        echo "ERRO CRÍTICO (ProxySQL): Falha ao iniciar o serviço ProxySQL para configuração."
        exit 1
    fi
    sleep 5

    run_proxysql_admin "DELETE FROM mysql_servers WHERE hostgroup_id = 10;"
    run_proxysql_admin "DELETE FROM mysql_users WHERE username = '${db_user}';"
    run_proxysql_admin "DELETE FROM mysql_query_rules WHERE rule_id = 1;"

    run_proxysql_admin "INSERT INTO mysql_servers (hostgroup_id, hostname, port) VALUES (10, '${rds_host}', ${rds_port});"
    run_proxysql_admin "INSERT INTO mysql_users (username, password, default_hostgroup) VALUES ('${db_user}', '${db_pass}', 10);"
    run_proxysql_admin "INSERT INTO mysql_query_rules (rule_id, active, username, destination_hostgroup, apply) VALUES (1, 1, '${db_user}', 10, 1);"
    
    run_proxysql_admin "UPDATE global_variables SET variable_value='${db_user}' WHERE variable_name='mysql-monitor_username';"
    run_proxysql_admin "UPDATE global_variables SET variable_value='${db_pass}' WHERE variable_name='mysql-monitor_password';"
    
    run_proxysql_admin "LOAD MYSQL VARIABLES TO RUNTIME;"
    run_proxysql_admin "LOAD MYSQL SERVERS TO RUNTIME;"
    run_proxysql_admin "LOAD MYSQL USERS TO RUNTIME;"
    run_proxysql_admin "LOAD MYSQL QUERY RULES TO RUNTIME;"
    run_proxysql_admin "SAVE MYSQL VARIABLES TO DISK;"
    run_proxysql_admin "SAVE MYSQL SERVERS TO DISK;"
    run_proxysql_admin "SAVE MYSQL USERS TO DISK;"
    run_proxysql_admin "SAVE MYSQL QUERY RULES TO DISK;"
    
    echo "INFO (ProxySQL): Configuração do ProxySQL concluída."
}

setup_xray_instrumentation() {
    local wp_path="$1"
    echo "INFO (X-Ray): Iniciando a instrumentação manual e robusta do WordPress em '$wp_path'..."

    local db_dropin_path="$wp_path/wp-content/db.php"
    
    sudo rm -f "$db_dropin_path"

    echo "INFO (X-Ray): Criando DB Drop-in com lógica de instrumentação manual via UDP."
    sudo -u "$APACHE_USER" tee "$db_dropin_path" > /dev/null <<'EOPHP'
<?php
/**
 * WordPress DB Drop-in for AWS X-Ray Tracing via Manual UDP Packet Submission.
 */

if (!class_exists('wpdb')) {
    require_once(ABSPATH . WPINC . '/wp-db.php');
}

class XRayManualTracer {
    private $is_sampled = false;
    private $trace_id;
    private $parent_id;
    private $daemon_address = '127.0.0.1';
    private $daemon_port = 2000;
    const UDP_HEADER = "{\"format\": \"json\", \"version\": 1}\n";

    public function __construct() {
        if (empty($_SERVER['HTTP_X_AMZN_TRACE_ID'])) {
            return;
        }

        $trace_header = $_SERVER['HTTP_X_AMZN_TRACE_ID'];
        
        foreach (explode(';', $trace_header) as $part) {
            @list($key, $value) = explode('=', $part, 2);
            if ($key === 'Root') $this->trace_id = $value;
            if ($key === 'Parent') $this->parent_id = $value;
            if ($key === 'Sampled') $this->is_sampled = ($value === '1');
        }
    }

    public function isValid() {
        return $this->is_sampled && $this->trace_id && $this->parent_id;
    }

    private function sendToDaemon(array $subsegment) {
        $socket = socket_create(AF_INET, SOCK_DGRAM, SOL_UDP);
        if ($socket) {
            $json_payload = json_encode($subsegment);
            $udp_message = self::UDP_HEADER . $json_payload;
            @socket_sendto($socket, $udp_message, strlen($udp_message), 0, $this->daemon_address, $this->daemon_port);
            socket_close($socket);
        }
    }

    public function traceFunction(string $name, callable $func) {
        $start_time = microtime(true);
        try {
            return $func();
        } finally {
            $end_time = microtime(true);
            $subsegment = [
                'id'         => bin2hex(random_bytes(8)),
                'trace_id'   => $this->trace_id,
                'parent_id'  => $this->parent_id,
                'name'       => $name,
                'start_time' => $start_time,
                'end_time'   => $end_time,
                'namespace'  => 'remote',
            ];
            
            if ($name === 'RDS') {
                $subsegment['sql'] = ['database_type' => 'MySQL', 'user' => DB_USER];
            }

            $this->sendToDaemon($subsegment);
        }
    }
}

class XRay_wpdb extends wpdb {
    private $tracer;

    public function __construct($dbuser, $dbpassword, $dbname, $dbhost) {
        parent::__construct($dbuser, $dbpassword, $dbname, $dbhost);
        $this->tracer = new XRayManualTracer();
    }

    public function query($query) {
        if ($this->tracer->isValid()) {
            return $this->tracer->traceFunction('RDS', function() use ($query) {
                return parent::query($query);
            });
        }
        return parent::query($query);
    }
}

if (defined('WP_INSTALLING') && WP_INSTALLING) {
    $wpdb = new wpdb(DB_USER, DB_PASSWORD, DB_NAME, DB_HOST);
} else {
    $wpdb = new XRay_wpdb(DB_USER, DB_PASSWORD, DB_NAME, DB_HOST);
}
EOPHP
    echo "INFO (X-Ray): Instrumentação manual do WordPress concluída com sucesso."
}

tune_apache_and_phpfpm() {
    echo "INFO (Performance Tuning): Otimizando Apache, PHP-FPM e limite de memória do PHP..."
    
    local APACHE_MPM_TUNING_CONF="/etc/httpd/conf.d/mpm_tuning.conf"
    sudo tee "$APACHE_MPM_TUNING_CONF" >/dev/null <<EOF_APACHE_MPM
<IfModule mpm_event_module>
    StartServers             3
    MinSpareThreads          25
    MaxSpareThreads          75
    ThreadsPerChild          25
    ServerLimit              16
    MaxRequestWorkers        400
    MaxConnectionsPerChild   1000
</IfModule>
EOF_APACHE_MPM

    local PHP_FPM_POOL_CONF="/etc/php-fpm.d/www.conf"
    if [ -f "$PHP_FPM_POOL_CONF" ]; then
        sudo sed -i 's/^pm.max_children = .*/pm.max_children = 50/' "$PHP_FPM_POOL_CONF"
        sudo sed -i 's/^pm.start_servers = .*/pm.start_servers = 10/' "$PHP_FPM_POOL_CONF"
        sudo sed -i 's/^pm.min_spare_servers = .*/pm.min_spare_servers = 10/' "$PHP_FPM_POOL_CONF"
        sudo sed -i 's/^pm.max_spare_servers = .*/pm.max_spare_servers = 30/' "$PHP_FPM_POOL_CONF"
    fi

    local PHP_INI_FILE="/etc/php.ini"
    if [ -f "$PHP_INI_FILE" ]; then
        echo "INFO: Ajustando memory_limit do PHP para 512M em $PHP_INI_FILE..."
        sudo sed -i 's/^;? *memory_limit *=.*/memory_limit = 512M/' "$PHP_INI_FILE"
    fi
}

# --- Lógica Principal de Execução ---
exec > >(tee -a "${LOG_FILE}") 2>&1
echo "=================================================="
echo "--- Iniciando Script WordPress Setup (v3.2.0 - PHP 8.2, WP Dinâmico) ($(date)) ---"
echo "=================================================="

if [ "$(id -u)" -ne 0 ]; then
    echo "ERRO: Execução inicial deve ser como root."
    exit 1
fi

echo "INFO: Verificando variáveis de ambiente essenciais..."
if [ -z "${ACCOUNT:-}" ]; then ACCOUNT_STS=$(aws sts get-caller-identity --query Account --output text 2>/dev/null); ACCOUNT="${ACCOUNT_STS:-}"; fi
error_found=0
for var_name in "${essential_vars[@]}"; do
    if [ "$var_name" != "AWS_EFS_ACCESS_POINT_TARGET_ID_0" ] && [ -z "${!var_name:-}" ]; then
        echo "ERRO: Variável essencial '$var_name' está vazia."
        error_found=1
    fi
done
if [ "$error_found" -eq 1 ]; then echo "ERRO CRÍTICO: Uma ou mais variáveis essenciais não foram definidas. Abortando."; exit 1; fi
echo "INFO: Verificação de variáveis concluída."

### INÍCIO DA SEÇÃO DE INSTALAÇÃO DE PACOTES E CONFIGURAÇÃO ###
echo "INFO: Iniciando instalação de pacotes..."
sudo yum update -y -q

echo "INFO: Instalando dependências básicas e habilitando PHP 8.2..."
sudo yum install -y amazon-efs-utils jq mysql
sudo amazon-linux-extras enable php8.2 -y

echo "INFO: Instalando X-Ray Daemon manualmente via RPM para máxima compatibilidade..."
if ! rpm -q xray > /dev/null; then
  curl -o /tmp/xray.rpm https://s3.us-east-2.amazonaws.com/aws-xray-assets.us-east-2/xray-daemon/aws-xray-daemon-3.x.rpm
  sudo yum install -y /tmp/xray.rpm
  rm /tmp/xray.rpm
fi

echo "INFO: Configurando repositório YUM para o ProxySQL v2.6..."
sudo tee /etc/yum.repos.d/proxysql.repo > /dev/null <<'EOF'
[proxysql]
name=ProxySQL YUM repository for v2.6
baseurl=https://repo.proxysql.com/ProxySQL/proxysql-2.6.x/centos/7/
gpgcheck=1
gpgkey=https://repo.proxysql.com/ProxySQL/proxysql-2.6.x/repo_pub_key
EOF
sudo yum clean all

echo "INFO: Instalando pacotes restantes (PHP, ProxySQL)..."
sudo yum install -y httpd php php-common php-fpm php-mysqlnd php-json php-cli php-xml php-zip php-gd php-mbstring php-soap php-opcache proxysql
if [ $? -ne 0 ]; then echo "ERRO CRÍTICO: Falha durante o 'yum install' dos pacotes restantes."; exit 1; fi

echo "INFO: Verificando instalação do X-Ray Daemon..."
if ! rpm -q xray > /dev/null; then echo "ERRO CRÍTICO: O pacote 'xray' (daemon) não foi instalado com sucesso. Abortando."; exit 1; fi
echo "INFO: Todos os pacotes e ferramentas foram instalados com sucesso."

AWS_SECRETSMANAGER_SECRET_VERSION_SOURCE_ARN_0="arn:aws:secretsmanager:${AWS_SECRETSMANAGER_SECRET_VERSION_SOURCE_REGION_0}:${ACCOUNT}:secret:${AWS_SECRETSMANAGER_SECRET_VERSION_SOURCE_NAME_0}"

mount_efs "$AWS_EFS_FILE_SYSTEM_TARGET_ID_0" "$MOUNT_POINT"

echo "INFO: Obtendo credenciais do RDS do Secrets Manager..."
SECRET_STRING_VALUE=$(aws secretsmanager get-secret-value --secret-id "$AWS_SECRETSMANAGER_SECRET_VERSION_SOURCE_ARN_0" --query 'SecretString' --output text --region "$AWS_SECRETSMANAGER_SECRET_VERSION_SOURCE_REGION_0")
DB_USER=$(echo "$SECRET_STRING_VALUE" | jq -r .username)
DB_PASSWORD=$(echo "$SECRET_STRING_VALUE" | jq -r .password)
DB_NAME_TO_USE="$AWS_DB_INSTANCE_TARGET_NAME_0"
RDS_ACTUAL_HOST_ENDPOINT=$(echo "$AWS_DB_INSTANCE_TARGET_ENDPOINT_0" | cut -d: -f1)
RDS_ACTUAL_PORT=$(echo "$AWS_DB_INSTANCE_TARGET_ENDPOINT_0" | cut -d: -f2); [ -z "$RDS_ACTUAL_PORT" ] && RDS_ACTUAL_PORT=3306
DB_HOST_FOR_WP_CONFIG="127.0.0.1:6033"
setup_and_configure_proxysql "$RDS_ACTUAL_HOST_ENDPOINT" "$RDS_ACTUAL_PORT" "$DB_USER" "$DB_PASSWORD"

if [ ! -d "$MOUNT_POINT/wp-includes" ]; then
    # --- INÍCIO DA LÓGICA DE VERSÃO DINÂMICA DO WORDPRESS ---
    # Determina a versão a ser instalada com base na variável de ambiente WP_VERSION.
    # Se WP_VERSION estiver vazia ou for "latest", usa a versão mais recente.
    # Caso contrário, usa a versão específica fornecida.
    if [ -z "${WP_VERSION:-}" ] || [ "${WP_VERSION}" == "latest" ]; then
        TARGET_WP_VERSION="latest"
    else
        TARGET_WP_VERSION="${WP_VERSION}"
    fi

    echo "INFO: WordPress versão '$TARGET_WP_VERSION' não encontrado no EFS. Baixando e instalando..."

    # Constrói a URL de download e o nome do arquivo com base na versão alvo.
    if [ "$TARGET_WP_VERSION" == "latest" ]; then
        WP_DOWNLOAD_URL="https://wordpress.org/latest.tar.gz"
        WP_TARBALL_FILENAME="latest.tar.gz"
    else
        WP_DOWNLOAD_URL="https://wordpress.org/wordpress-${TARGET_WP_VERSION}.tar.gz"
        WP_TARBALL_FILENAME="wordpress-${TARGET_WP_VERSION}.tar.gz"
    fi
    # --- FIM DA LÓGICA DE VERSÃO DINÂMICA DO WORDPRESS ---

    sudo mkdir -p "$WP_DOWNLOAD_DIR" "$WP_FINAL_CONTENT_DIR"
    sudo chown "$(id -u):$(id -g)" "$WP_DOWNLOAD_DIR" "$WP_FINAL_CONTENT_DIR"
    
    # Baixa e extrai a versão do WordPress determinada dinamicamente.
    (cd "$WP_DOWNLOAD_DIR" && curl -f -sLO "$WP_DOWNLOAD_URL" && tar -xzf "$WP_TARBALL_FILENAME" -C "$WP_FINAL_CONTENT_DIR" --strip-components=1)
    
    if [ $? -ne 0 ]; then 
        echo "ERRO CRÍTICO: Falha ao baixar ou extrair o WordPress versão '$TARGET_WP_VERSION'. Verifique se a versão existe e a URL '$WP_DOWNLOAD_URL' é válida."
        exit 1
    fi
    
    sudo -u "$APACHE_USER" cp -aT "$WP_FINAL_CONTENT_DIR/" "$MOUNT_POINT/"
    sudo rm -rf "$WP_DOWNLOAD_DIR" "$WP_FINAL_CONTENT_DIR"
fi

setup_xray_instrumentation "$MOUNT_POINT"

if [ ! -f "$ACTIVE_CONFIG_FILE_EFS" ]; then
    create_wp_config_template "$ACTIVE_CONFIG_FILE_EFS" "$WPDOMAIN" "$DB_NAME_TO_USE" "$DB_USER" "$DB_PASSWORD" "$DB_HOST_FOR_WP_CONFIG"
fi

sudo -u "$APACHE_USER" tee "$HEALTH_CHECK_FILE_PATH_EFS" >/dev/null <<< '<?php http_response_code(200); echo "OK"; ?>'

echo "INFO: Ajustando permissões finais no EFS..."
sudo chown -R "$APACHE_USER:$APACHE_USER" "$MOUNT_POINT"
sudo find "$MOUNT_POINT" -type d -exec chmod 775 {} \;
sudo find "$MOUNT_POINT" -type f -exec chmod 664 {} \;

echo "INFO: Configurando Apache..."
sudo sed -i "s#^DocumentRoot \"/var/www/html\"#DocumentRoot \"$MOUNT_POINT\"#" /etc/httpd/conf/httpd.conf
sudo sed -i "s#^<Directory \"/var/www/html\">#<Directory \"$MOUNT_POINT\">#" /etc/httpd/conf/httpd.conf

sudo tee "/etc/httpd/conf.d/wordpress.conf" >/dev/null <<EOF
<Directory "$MOUNT_POINT">
    AllowOverride All
    Require all granted
</Directory>
EOF

tune_apache_and_phpfpm

### INÍCIO DA SEÇÃO DE INICIALIZAÇÃO DE SERVIÇOS ###
PHP_FPM_SERVICE_NAME="php-fpm.service"
if ! sudo systemctl list-unit-files | grep -q -w "$PHP_FPM_SERVICE_NAME"; then
    echo "ERRO CRÍTICO: Serviço PHP-FPM ('$PHP_FPM_SERVICE_NAME') não encontrado."
    exit 1
fi

echo "INFO: Habilitando serviços para iniciar no boot (httpd, php-fpm, proxysql, xray)..."
sudo systemctl enable httpd
sudo systemctl enable "$PHP_FPM_SERVICE_NAME"
sudo systemctl enable proxysql
sudo systemctl enable xray

echo "INFO: Reiniciando serviços na ordem correta para aplicar todas as mudanças..."
sudo systemctl restart xray
sudo systemctl restart proxysql
sudo systemctl restart "$PHP_FPM_SERVICE_NAME"
sudo systemctl restart httpd

sleep 3
if systemctl is-active --quiet httpd && systemctl is-active --quiet "$PHP_FPM_SERVICE_NAME" && systemctl is-active --quiet proxysql && systemctl is-active --quiet xray; then
    echo "INFO: Todos os serviços essenciais estão ativos."
else
    echo "ERRO CRÍTICO: Um ou mais serviços essenciais não estão ativos após o reinício final."
    sudo systemctl status httpd --no-pager
    sudo systemctl status "$PHP_FPM_SERVICE_NAME" --no-pager
    sudo systemctl status proxysql --no-pager
    sudo systemctl status xray --no-pager
    exit 1
fi
### FIM DA SEÇÃO DE INICIALIZAÇÃO DE SERVIÇOS ###

echo "=================================================="
echo "--- Script WordPress Setup (v3.2.0) concluído! ---"
echo "=================================================="
exit 0
