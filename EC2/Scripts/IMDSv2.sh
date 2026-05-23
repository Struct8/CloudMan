#!/bin/bash

# Load environment variables
source /home/ec2-user/.env

# Install Apache
dnf install -y httpd

# Start Apache and enable it to start on boot
systemctl start httpd
systemctl enable httpd

# Create a configuration file for mod_rewrite
cat <<EOF >/etc/httpd/conf.d/rewrite.conf
RewriteEngine on
RewriteRule ^/?.* /var/www/html/index.html [L]
EOF

# Restart Apache to apply the configuration
systemctl restart httpd

# Create a session token for IMDSv2
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")

CW_INSTALLED="No"
CW_BADGE_CLASS="bg-danger"

# Obtain metadata using the session token
AVAILABILITY_ZONE=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/availability-zone)
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
PRIVATE_IP_ADDRESS=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/local-ipv4)
PUBLIC_IP_ADDRESS=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4)

# Fix for nested IMDSv2 call
MAC_ADDRESS=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/mac)
IPV6_ADDRESS=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/network/interfaces/macs/${MAC_ADDRESS}/ipv6s)

# Create a custom logging script
cat <<EOF >/usr/local/bin/custom_logging.sh
#!/bin/bash
while true; do
  echo "\$(date) - Logging data from instance $INSTANCE_ID" >> /var/log/custom_log.log
  sleep 10
done
EOF

chmod +x /usr/local/bin/custom_logging.sh
nohup /usr/local/bin/custom_logging.sh &

# Check if the environment variable for the CloudWatch logs group ARN is defined and valid
if [ -n "$AWS_CLOUDWATCH_LOG_GROUP_TARGET_ARN" ]; then
  dnf install -y amazon-cloudwatch-agent
  CW_INSTALLED="Yes"
  CW_BADGE_CLASS="bg-success"
  
  LOG_GROUP_NAME=$(echo $AWS_CLOUDWATCH_LOG_GROUP_TARGET_ARN | awk -F':' '{print $7}')

  cat <<EOF >/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/httpd/access_log",
            "log_group_name": "$LOG_GROUP_NAME",
            "log_stream_name": "${INSTANCE_ID}-access_log"
          },
          {
            "file_path": "/var/log/httpd/error_log",
            "log_group_name": "$LOG_GROUP_NAME",
            "log_stream_name": "${INSTANCE_ID}-error_log"
          },
          {
            "file_path": "/var/log/custom_log.log",
            "log_group_name": "$LOG_GROUP_NAME",
            "log_stream_name": "${INSTANCE_ID}-custom_log"
          }
        ]
      }
    }
  }
}
EOF

  /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
fi

DISK_DEVICES=$(lsblk -o NAME,SIZE,TYPE,MOUNTPOINT | awk '{if(NR>1)print}')

# =====================================================================
# GERAÇÃO DA PÁGINA HTML COM BOOTSTRAP 5 E CSS CUSTOMIZADO
# =====================================================================

cat <<EOF >/var/www/html/index.html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>EC2 Dashboard - $INSTANCE_ID</title>
  <!-- Bootstrap 5 CSS via CDN -->
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body { background-color: #f4f6f8; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #333; padding-bottom: 2rem; }
    .header-banner { background-color: #232f3e; color: white; padding: 2rem 0; border-bottom: 4px solid #ff9900; margin-bottom: 2rem; }
    .card { border: none; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); margin-bottom: 1.5rem; }
    .card-header { background-color: #ffffff; border-bottom: 2px solid #f0f2f5; font-weight: 600; font-size: 1.1rem; border-radius: 10px 10px 0 0 !important; color: #232f3e; }
    .property-name { font-weight: 600; color: #5a6872; width: 150px; display: inline-block; }
    .list-group-item { padding: 1rem 1.25rem; border-color: #f0f2f5; }
    pre.terminal { background-color: #1e1e1e; color: #00ff00; padding: 1.5rem; border-radius: 8px; font-size: 0.9rem; overflow-x: auto; margin: 0; border: 1px solid #333; }
    .aws-orange { color: #ff9900; }
  </style>
</head>
<body>

  <div class="header-banner text-center">
    <h1 class="fw-bold">AWS EC2 <span class="aws-orange">Dashboard</span></h1>
    <p class="mb-0 text-light">Generated on: <span class="badge bg-light text-dark">$(date '+%Y-%m-%d %H:%M:%S')</span></p>
  </div>

  <div class="container">
    <div class="row">
      
      <!-- Coluna da Esquerda: Info da Instância -->
      <div class="col-lg-7">
        <div class="card h-100">
          <div class="card-header">
            💻 Instance Details
          </div>
          <div class="card-body p-0">
            <ul class="list-group list-group-flush">
              <li class="list-group-item"><span class="property-name">Instance ID:</span> <span class="fw-bold">$INSTANCE_ID</span></li>
              <li class="list-group-item"><span class="property-name">Availability Zone:</span> $AVAILABILITY_ZONE</li>
EOF

if [ -n "$PUBLIC_IP_ADDRESS" ]; then
  echo "              <li class=\"list-group-item\"><span class=\"property-name\">Public IP:</span> <span class=\"text-primary\">$PUBLIC_IP_ADDRESS</span></li>" >>/var/www/html/index.html
fi
if [ -n "$PRIVATE_IP_ADDRESS" ]; then
  echo "              <li class=\"list-group-item\"><span class=\"property-name\">Private IP:</span> $PRIVATE_IP_ADDRESS</li>" >>/var/www/html/index.html
fi
if [ -n "$IPV6_ADDRESS" ]; then
  echo "              <li class=\"list-group-item\"><span class=\"property-name\">IPv6:</span> $IPV6_ADDRESS</li>" >>/var/www/html/index.html
fi

cat <<EOF >>/var/www/html/index.html
            </ul>
          </div>
        </div>
      </div>

      <!-- Coluna da Direita: CloudWatch e CI/CD -->
      <div class="col-lg-5">
        
        <!-- CloudWatch Card -->
        <div class="card mb-4">
          <div class="card-header">
            📊 CloudWatch Monitoring
          </div>
          <div class="card-body p-0">
            <ul class="list-group list-group-flush">
              <li class="list-group-item"><span class="property-name">Installed:</span> <span class="badge $CW_BADGE_CLASS">$CW_INSTALLED</span></li>
              <li class="list-group-item"><span class="property-name">Log Group:</span> ${LOG_GROUP_NAME:-Not Configured}</li>
            </ul>
          </div>
        </div>

        <!-- CI/CD Card -->
        <div class="card">
          <div class="card-header">
            🚀 CloudMan CI/CD
          </div>
          <div class="card-body p-0">
            <ul class="list-group list-group-flush">
              <li class="list-group-item"><span class="property-name">App Name:</span> <span class="fw-bold">${CLOUDMAN_CICD_APPNAME:-N/A}</span></li>
              <li class="list-group-item"><span class="property-name">Stage:</span> <span class="badge bg-info text-dark">${CLOUDMAN_CICD_STAGE:-N/A}</span></li>
              <li class="list-group-item"><span class="property-name">Version:</span> <span class="badge bg-secondary">${CLOUDMAN_CICD_VERSION:-N/A}</span></li>
            </ul>
          </div>
        </div>

      </div>
    </div>

    <!-- Linha Inferior: Discos -->
    <div class="row mt-3">
      <div class="col-12">
        <div class="card">
          <div class="card-header">
            💾 Storage Devices (lsblk)
          </div>
          <div class="card-body p-3 bg-dark" style="border-radius: 0 0 10px 10px;">
            <pre class="terminal"><code>$DISK_DEVICES</code></pre>
          </div>
        </div>
      </div>
    </div>

  </div>

</body>
</html>
EOF
