#!/bin/bash
# Boot script for the VPC Flow Logs + Managed Prometheus lab.
#
# The instance exists only to put packets on the wire: with no traffic the flow
# log delivers records whose log-status is NODATA, ProcessFlowLogs writes no
# series, and an empty graph looks exactly like a broken pipeline.
#
# THREE DESTINATIONS, ON PURPOSE. They exercise three different branches of the
# endpoint collapse that ProcessFlowLogs performs:
#
#   s3.<region>.amazonaws.com -- inside an AWS range, so the record fills
#                             pkt-dst-aws-service and the far end is named `S3`
#   checkip.amazonaws.com  -- also AWS-owned, and it lands on the generic
#                             `AMAZON` range rather than a named service
#   www.google.com         -- outside every AWS range, so no service name is on
#                             the record and the far end collapses to `internet`
#
# THE THIRD ONE IS NOT DECORATION. The first run of this lab, on 2026-09-22, had
# only the two Amazon destinations, and the workspace came back with dst_id in
# {AMAZON, EC2, S3} and no `internet` at all: the branch that matters most for
# cardinality had no traffic to prove it. An address being public is not what
# makes it the internet here -- the record naming no service is.
#
# A run every 20 seconds keeps every 60-second aggregation window populated, so
# no bucket is skipped for lack of traffic.

REGION=$(curl -s --max-time 5 -H "X-aws-ec2-metadata-token: $(curl -s --max-time 5 -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')" http://169.254.169.254/latest/meta-data/placement/region)
if [ -z "$REGION" ]; then
  REGION="us-east-1"
fi

cat >/etc/systemd/system/struct8-traffic.service <<UNIT
[Unit]
Description=Traffic generator for the VPC flow log lab
After=network-online.target
Wants=network-online.target

[Service]
Restart=always
RestartSec=5
ExecStart=/bin/bash -c 'while true; do curl -s -o /dev/null --max-time 5 https://checkip.amazonaws.com/; curl -s -o /dev/null --max-time 5 https://s3.$REGION.amazonaws.com/; curl -s -o /dev/null --max-time 5 https://www.google.com/; sleep 20; done'

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now struct8-traffic.service
