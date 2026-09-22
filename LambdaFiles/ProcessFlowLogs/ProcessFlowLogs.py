"""Aggregates VPC Flow Log records into Prometheus series and writes them to an
Amazon Managed Service for Prometheus workspace.

Sibling of ProcessCUR: same repository, same conventions -- configuration from
environment variables with built-in defaults, wired resources arriving through the
names the generator exports, plain print() for logging, and a handler that accepts
either an S3 event or an explicit key.

WHAT THIS SLICE DOES, AND WHAT IT LEAVES OUT. It reads plain-text flow log objects,
derives the field map from each file's own header, keeps only the egress direction,
names both ends, collapses everything outside the known CIDRs, accumulates into
60-second buckets and writes the result once. It does NOT yet write partials, run a
compactor, or read Parquet -- those come after the first run proves the path.

NO NEW DEPENDENCIES. Remote write is protobuf framed in snappy, and neither needs a
package here: the WriteRequest message is small enough to encode by hand, and the
snappy format permits an all-literal block, so a valid payload can be produced without
compressing anything. Signing uses botocore, which the runtime already carries.
"""

import gzip
import io
import ipaddress
import json
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

# --- configuration ---------------------------------------------------------------
#
# The wired resources arrive under the names the generator builds from each type's
# `exportEnvVar` plus the wire label: <TYPE>_<KEY>_<LABEL>. ProcessCUR reads its
# bucket the same way, and falls back the same way, because the label depends on
# which wire was drawn first.

WORKSPACE_ENDPOINT = (
    os.environ.get('AWS_PROMETHEUS_WORKSPACE_ENDPOINT_0')
    or os.environ.get('AWS_PROMETHEUS_WORKSPACE_TARGET_ENDPOINT_0')
    or os.environ.get('PROMETHEUS_ENDPOINT', '')
)

# `FLOW_LOG_BUCKET` comes FIRST, and the order is the point. A wire writes
# <TYPE>_NAME_<LABEL> holding the target's LOGICAL NAME -- the label on the canvas
# -- while a bucket whose namespace is account-regional is called
# `<logical>-<account>-<region>-an` in the cloud. Taking the wire's value as a
# bucket name asks S3 for a bucket that does not exist, and the two agree only
# when the bucket has no namespace. The endpoint above does not share the
# problem: the workspace declares `prometheus_endpoint` in its `exportEnvVar`, so
# what arrives there is the real address.
#
# The diagram therefore writes this one by hand, carrying a Terraform reference
# (`aws_s3_bucket.<label>.id`). The wire-built names stay as the fallback.
FLOW_BUCKET_NAME = (
    os.environ.get('FLOW_LOG_BUCKET')
    or os.environ.get('AWS_S3_BUCKET_NAME_0')
    or os.environ.get('AWS_S3_BUCKET_TARGET_NAME_0', '')
)

REGION = os.environ.get('AWS_REGION', 'us-east-1')

# Where the AWS delivery lands. The notification listens to this prefix only, and
# step 1 refuses anything else -- the trap that survives someone editing the
# notification by hand.
DELIVERY_PREFIX = os.environ.get('DELIVERY_PREFIX', 'AWSLogs/')
OUTPUT_PREFIX = os.environ.get('OUTPUT_PREFIX', 'struct8/')

# The time bucket, in seconds. 60 matches the finest aggregation the flow log can
# deliver, so a coarser value here only loses resolution.
BUCKET_SECONDS = int(os.environ.get('BUCKET_SECONDS', '60'))

# How long to wait before a time bucket is considered closed. Anything delivered
# after this is dropped and counted, because Prometheus refuses a second value for an
# instant it already holds. 30 minutes against a delivery that is documented as
# roughly 10 -- and the tail of that delay is the measurement this parameter is
# waiting for.
CUTOFF_SECONDS = int(os.environ.get('CUTOFF_SECONDS', '1800'))

# The highest number of pairs kept per bucket. What falls outside is summed into one
# `rest` row, so the total still closes.
TOP_N_PAIRS = int(os.environ.get('TOP_N_PAIRS', '200'))

# Only consulted when a file carries no header line. Never inferred from position:
# a file read with the wrong map does not fail, it attributes bytes to the wrong pair
# with a plausible number, which is the worst defect available here.
FALLBACK_FIELD_ORDER = os.environ.get('FALLBACK_FIELD_ORDER', '').split()

METRIC_BYTES = 'struct8_edge_bytes'
METRIC_PACKETS = 'struct8_edge_packets'

s3 = boto3.client('s3')
ec2 = boto3.client('ec2')


# --- protobuf, by hand ------------------------------------------------------------
#
# Only four messages are needed, and they are all length-delimited or scalar:
#
#   WriteRequest { repeated TimeSeries timeseries = 1 }
#   TimeSeries   { repeated Label labels = 1; repeated Sample samples = 2 }
#   Label        { string name = 1; string value = 2 }
#   Sample       { double value = 1; int64 timestamp = 2 }

def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _key(field, wire):
    return _varint((field << 3) | wire)


def _len_delimited(field, payload):
    return _key(field, 2) + _varint(len(payload)) + payload


def _string_field(field, value):
    return _len_delimited(field, value.encode('utf-8'))


def _double_field(field, value):
    # Wire type 1: eight bytes, little-endian IEEE 754.
    return _key(field, 1) + struct.pack('<d', float(value))


def _int64_field(field, value):
    return _key(field, 0) + _varint(int(value))


def encode_write_request(series):
    """`series` is a list of (labels dict, list of (timestamp_ms, value))."""
    out = bytearray()
    for labels, samples in series:
        ts = bytearray()
        # __name__ has to come first only by convention; Prometheus sorts on receipt.
        for name, value in labels.items():
            label = _string_field(1, name) + _string_field(2, value)
            ts += _len_delimited(1, label)
        for timestamp_ms, value in samples:
            sample = _double_field(1, value) + _int64_field(2, timestamp_ms)
            ts += _len_delimited(2, sample)
        out += _len_delimited(1, bytes(ts))
    return bytes(out)


# --- snappy, without compressing --------------------------------------------------

def snappy_literal_only(data):
    """A valid snappy block that stores `data` verbatim.

    The format is a varint of the uncompressed length followed by elements, and a
    literal is an element. Emitting one literal for the whole payload is legal and
    decompresses to the input, which is all remote write asks for. It costs
    bandwidth on a payload that is already small, and saves a compiled dependency.
    """
    n = len(data)
    out = bytearray(_varint(n))
    if n == 0:
        return bytes(out)
    if n <= 60:
        out.append((n - 1) << 2)
    elif n <= 1 << 8:
        out.append(60 << 2)
        out += (n - 1).to_bytes(1, 'little')
    elif n <= 1 << 16:
        out.append(61 << 2)
        out += (n - 1).to_bytes(2, 'little')
    elif n <= 1 << 24:
        out.append(62 << 2)
        out += (n - 1).to_bytes(3, 'little')
    else:
        out.append(63 << 2)
        out += (n - 1).to_bytes(4, 'little')
    out += data
    return bytes(out)


# --- remote write -----------------------------------------------------------------

class RemoteWriteRefused(Exception):
    """AMP answered with a status other than 2xx, and the body says why."""

    def __init__(self, status, detail):
        super().__init__('remote_write refused with ' + str(status) + ': ' + detail)
        self.status = status
        self.detail = detail


def remote_write(series):
    """Sends one batch. Returns the HTTP status, or raises with what AMP said.

    A 400 here is not a transport failure, and reading it as one throws away the
    only thing that explains the run. Prometheus answers 400 for a sample it
    REFUSES -- a repeated instant carrying a different value, or one older than
    the out-of-order window -- and the reason is in the response body. `urlopen`
    raises before anyone reads that body, so a refusal used to reach the log as a
    traceback with nothing in it about the cause.
    """
    if not WORKSPACE_ENDPOINT:
        raise RuntimeError('No workspace endpoint: wire the Lambda to the workspace.')

    url = WORKSPACE_ENDPOINT.rstrip('/') + '/api/v1/remote_write'
    body = snappy_literal_only(encode_write_request(series))

    headers = {
        'Content-Encoding': 'snappy',
        'Content-Type': 'application/x-protobuf',
        'X-Prometheus-Remote-Write-Version': '0.1.0',
    }

    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    request = AWSRequest(method='POST', url=url, data=body, headers=headers)
    # `aps` is the signing name of Amazon Managed Service for Prometheus.
    SigV4Auth(credentials, 'aps', REGION).add_auth(request)

    sent = urllib.request.Request(url, data=body, headers=dict(request.headers), method='POST')
    try:
        with urllib.request.urlopen(sent, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as error:
        raise RemoteWriteRefused(error.code,
                                 error.read().decode('utf-8', 'replace')[:500]) from None


def write_series(series, diagnostics):
    """Writes a batch, and on a refusal falls back to one request per series.

    AMP refuses the WHOLE request over one bad sample. Without this fallback a
    single conflicting instant loses every other series in the batch, and the
    object goes with them -- the two asynchronous retries send identical bytes
    and are refused identically.

    THE CONFLICT IS REAL AND IT IS NOT RARE. Measured on 2026-09-22, ten minutes
    into the first run, with ONE network interface: two objects of the same
    delivery both carried bucket 1790117040 for the same pair, one saying 2778
    bytes and the other 1401, because AWS had split that capture window across
    two files. Whichever arrives second is refused.

    One request per series costs N requests on a bad batch and nothing on a good
    one, and it names the series that conflicted instead of losing the evidence.
    A 5xx is re-raised: there the bytes are fine and a retry is what helps.
    """
    if not series:
        return
    try:
        status = remote_write(series)
        print('remote_write status ' + str(status) + ', series ' + str(len(series)))
        return
    except RemoteWriteRefused as refusal:
        if refusal.status >= 500:
            raise
        print('Batch of ' + str(len(series)) + ' refused: ' + str(refusal))
        diagnostics['batches_refused'] += 1

    for labels, samples in series:
        try:
            remote_write([(labels, samples)])
            diagnostics['series_written_singly'] += 1
        except RemoteWriteRefused as single:
            diagnostics['series_refused'] += 1
            print(json.dumps({'metric': 'struct8_series_refused',
                              'labels': labels, 'detail': single.detail[:200]}))


# --- reading the flow log ---------------------------------------------------------

# Without these four a record cannot be used at all. Everything else missing removes
# one specific capability, and only that one.
REQUIRED_FIELDS = ('srcaddr', 'dstaddr', 'bytes', 'start')


def field_map_from_header(first_line):
    """The map comes from the file, never from a position we assumed."""
    names = first_line.strip().split()
    if not names or 'srcaddr' not in names:
        return None
    return {name: index for index, name in enumerate(names)}


def read_text_object(bucket, key):
    """Yields dicts of field name to raw string. Gzipped plain text only."""
    body = s3.get_object(Bucket=bucket, Key=key)['Body'].read()
    if key.endswith('.gz'):
        body = gzip.decompress(body)
    text = io.StringIO(body.decode('utf-8', errors='replace'))

    first = text.readline()
    mapping = field_map_from_header(first)
    if mapping is None:
        if not FALLBACK_FIELD_ORDER:
            raise ValueError('no header line and no FALLBACK_FIELD_ORDER declared')
        mapping = {name: i for i, name in enumerate(FALLBACK_FIELD_ORDER)}
        text.seek(0)

    for line in text:
        parts = line.split()
        if len(parts) < len(mapping):
            continue
        yield {name: parts[index] for name, index in mapping.items()}


def value_of(record, name):
    """`-` is how the flow log writes an absent field."""
    raw = record.get(name, '-')
    return None if raw in ('-', '') else raw


# --- naming the two ends ----------------------------------------------------------

def known_cidrs():
    """The CIDRs of the VPCs this account can describe, with the customer's role."""
    blocks = []
    paginator = ec2.get_paginator('describe_vpcs')
    for page in paginator.paginate():
        for vpc in page.get('Vpcs', []):
            for association in vpc.get('CidrBlockAssociationSet', []):
                block = association.get('CidrBlock')
                if block:
                    blocks.append((ipaddress.ip_network(block), vpc['VpcId']))
            # A dual-stack VPC keeps its IPv6 ranges in a SEPARATE list, and
            # leaving it out is the kind of gap that never looks like one: every
            # IPv6 address then falls outside every known CIDR, so name_endpoint
            # collapses the whole v6 half of the traffic to `internet` and the
            # picture reads as complete.
            for association in vpc.get('Ipv6CidrBlockAssociationSet', []):
                block = association.get('Ipv6CidrBlock')
                if block:
                    blocks.append((ipaddress.ip_network(block), vpc['VpcId']))
    return blocks


def scope_of_address(address, cidrs):
    """The VPC id an address belongs to, or None when it is outside all of them."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return None
    for network, vpc_id in cidrs:
        if parsed.version == network.version and parsed in network:
            return vpc_id
    return None


def is_private(address):
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    # 100.64/10 is carrier-grade NAT, which AWS uses in places and which is not the
    # internet either.
    return parsed.is_private or parsed in ipaddress.ip_network('100.64.0.0/10')


def name_endpoint(record, side, cidrs):
    """Returns the four labels for one end: id, address, scope and type.

    Identity before address, because an address is reassigned and an id is not. Both
    are emitted when both exist: a disagreement between them is the only free signal
    that the address map has gone stale.
    """
    address = value_of(record, 'pkt_' + side + 'addr') or value_of(record, side + 'addr') or ''
    scope = scope_of_address(address, cidrs)

    if scope:
        identifier = ''
        kind = 'address'
        if side == 'src':
            identifier = value_of(record, 'instance-id') or ''
            if identifier:
                kind = 'instance'
            service_name = value_of(record, 'ecs-service-name')
            if service_name:
                identifier, kind = service_name, 'ecs_service'
        interface_type = value_of(record, 'interface-type')
        if interface_type and side == 'src':
            kind = interface_type
        return {
            side + '_id': identifier,
            side + '_addr': address,
            side + '_scope': scope,
            side + '_type': kind,
        }

    # Outside every known CIDR the canvas has no box to draw, so the end collapses.
    # Which of the four it collapses to is read from the record, never guessed.
    service = value_of(record, 'pkt-' + side + '-aws-service')
    path = value_of(record, 'traffic-path')
    if service:
        collapsed, kind = service, 'aws_service'
    elif path in ('3', '6'):
        collapsed, kind = 'on-premises', 'on_premises'
    elif path in ('2', '8'):
        collapsed, kind = 'internet', 'internet'
    elif is_private(address):
        # Private and matching no CIDR means the map is incomplete, which is a
        # different statement from "the internet" and the one that is true.
        collapsed, kind = 'unmapped-network', 'unmapped'
    else:
        collapsed, kind = 'internet', 'internet'

    return {
        side + '_id': collapsed,
        side + '_addr': '',
        side + '_scope': 'external',
        side + '_type': kind,
    }


# --- aggregation ------------------------------------------------------------------

def accumulate(records, cidrs, diagnostics):
    """(bucket, labels) -> [bytes, packets], keeping only the egress direction."""
    totals = defaultdict(lambda: [0, 0])

    for record in records:
        diagnostics['records_seen'] += 1

        status = value_of(record, 'log-status')
        if status in ('NODATA', 'SKIPDATA'):
            diagnostics['records_' + str(status).lower()] += 1
            continue

        if any(value_of(record, name) is None for name in REQUIRED_FIELDS):
            diagnostics['records_missing_required'] += 1
            continue

        # Deduplication. A VPC flow log captures every interface, so an internal flow
        # is written twice -- egress at the sender, ingress at the receiver. Keeping
        # the egress side counts it once. What must NOT be done instead is dividing
        # by two: that assumes both sides were captured, and it is wrong at the edge.
        direction = value_of(record, 'flow-direction')
        if direction is None:
            diagnostics['records_without_direction'] += 1
        elif direction != 'egress':
            continue

        try:
            start = int(value_of(record, 'start'))
            byte_count = int(value_of(record, 'bytes'))
            packet_count = int(value_of(record, 'packets') or 0)
        except (TypeError, ValueError):
            diagnostics['records_unparsable'] += 1
            continue

        bucket = (start // BUCKET_SECONDS) * BUCKET_SECONDS

        labels = {}
        labels.update(name_endpoint(record, 'src', cidrs))
        labels.update(name_endpoint(record, 'dst', cidrs))

        port = value_of(record, 'dstport')
        if port:
            labels['dstport'] = port

        key = (bucket, tuple(sorted(labels.items())))
        totals[key][0] += byte_count
        totals[key][1] += packet_count

    return totals


def cut_to_top_n(totals):
    """Top N pairs per bucket, plus one `rest` row so the total still closes."""
    by_bucket = defaultdict(list)
    for (bucket, labels), values in totals.items():
        by_bucket[bucket].append((labels, values))

    kept = {}
    for bucket, rows in by_bucket.items():
        rows.sort(key=lambda row: row[1][0], reverse=True)
        for labels, values in rows[:TOP_N_PAIRS]:
            kept[(bucket, labels)] = values
        overflow = rows[TOP_N_PAIRS:]
        if overflow:
            rest = [sum(v[1][0] for v in overflow), sum(v[1][1] for v in overflow)]
            rest_labels = (
                ('src_id', 'rest'), ('src_addr', ''), ('src_scope', 'aggregate'),
                ('src_type', 'rest'),
                ('dst_id', 'rest'), ('dst_addr', ''), ('dst_scope', 'aggregate'),
                ('dst_type', 'rest'),
            )
            kept[(bucket, rest_labels)] = rest
    return kept


def to_series(totals, diagnostics):
    """One Prometheus series per (labels, metric), samples ordered by instant."""
    grouped = defaultdict(list)
    now = int(time.time())
    closed_before = now - CUTOFF_SECONDS

    for (bucket, labels), (byte_count, packet_count) in sorted(totals.items()):
        if bucket > closed_before:
            diagnostics['buckets_still_open'] += 1
            continue
        timestamp_ms = bucket * 1000
        grouped[(labels, METRIC_BYTES)].append((timestamp_ms, byte_count))
        grouped[(labels, METRIC_PACKETS)].append((timestamp_ms, packet_count))

    series = []
    for (labels, metric), samples in grouped.items():
        full = {'__name__': metric}
        full.update(dict(labels))
        series.append((full, sorted(samples)))
    return series


def diagnostic_series(diagnostics):
    """The diagnosis travels with the number, and as series it gains history.

    THE INSTANT IS IN MILLISECONDS, and under the S3 notification that is not a
    detail. One delivery drops several objects at once, so several invocations
    run at the same moment, each holding counts of its own. At second precision
    two of them land on the same instant with different values; Prometheus
    refuses that for the WHOLE request, so a single collision loses every edge
    series in the batch, not just the counter -- and the object is gone after the
    two asynchronous retries fail the same way.

    Milliseconds make the collision rare. They do not make it impossible: a
    counter written by every invocation has no instant that is correct for all of
    them. What removes it is the single writer of the study's §12.
    """
    timestamp_ms = int(time.time() * 1000)
    out = []
    for name, value in diagnostics.items():
        out.append((
            {'__name__': 'struct8_flowlog_' + name + '_total'},
            [(timestamp_ms, value)],
        ))
    return out


def report_delivery_delay(key, records, now):
    """Logs how long after its window closed the newest record in a file arrived.

    This is the measurement the study calls blocking. The cut has to sit past the
    tail of this distribution, and a cut set short drops data with nothing but a
    counter to say so -- so until the tail is measured, the cut is a guess.

    It goes to the LOG and not to a series, on purpose. A percentile wants every
    observation, and a gauge written once per invocation would both lose most of
    them and collide with the next invocation's instant, the same way the
    diagnostics above do. The line is JSON so Logs Insights discovers the fields:

      filter metric = "struct8_delivery_delay"
      | stats count(*), avg(delay_seconds), max(delay_seconds),
              pct(delay_seconds, 50), pct(delay_seconds, 95), pct(delay_seconds, 99)
    """
    ends = []
    for record in records:
        raw = value_of(record, 'end')
        if raw is None:
            continue
        try:
            ends.append(int(raw))
        except ValueError:
            continue
    if not ends:
        return
    newest = max(ends)
    print(json.dumps({
        'metric': 'struct8_delivery_delay',
        'key': key,
        'records': len(records),
        'window_end': newest,
        'delay_seconds': now - newest,
    }))


# --- handler ----------------------------------------------------------------------

def keys_from_event(event, bucket):
    """An S3 notification, an explicit key, or a sweep of the delivery prefix."""
    if 'Records' in event and isinstance(event.get('Records'), list):
        keys = []
        for record in event['Records']:
            if 's3' not in record:
                continue
            key = urllib.parse.unquote_plus(record['s3']['object']['key'], encoding='utf-8')
            # Step 1: refuse our own output, whatever the notification filter says.
            if key.startswith(OUTPUT_PREFIX):
                continue
            keys.append(key)
        return keys

    if isinstance(event, dict) and event.get('object_key'):
        return [event['object_key']]

    keys = []
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=DELIVERY_PREFIX):
        for item in page.get('Contents', []):
            keys.append(item['Key'])
    return keys


def lambda_handler(event, context):
    print('Lambda execution started. Received event: ' + json.dumps(event)[:500])

    bucket = FLOW_BUCKET_NAME
    if not bucket:
        return {'statusCode': 500, 'body': 'Configuration Error: no flow log bucket.'}

    diagnostics = defaultdict(int)
    keys = keys_from_event(event, bucket)
    print('Objects to read: ' + str(len(keys)))

    cidrs = known_cidrs()
    print('Known CIDRs: ' + str([str(network) for network, _ in cidrs]))

    records = []
    now = int(time.time())
    for key in keys:
        try:
            from_this_file = list(read_text_object(bucket, key))
            records.extend(from_this_file)
            diagnostics['files_processed'] += 1
            report_delivery_delay(key, from_this_file, now)
        except Exception as error:  # noqa: BLE001 -- one bad file must not stop the run
            diagnostics['files_skipped'] += 1
            print('Skipped ' + key + ': ' + str(error))

    totals = cut_to_top_n(accumulate(records, cidrs, diagnostics))
    edge_series = to_series(totals, diagnostics)

    if not edge_series:
        print('Nothing to write. Diagnostics: ' + json.dumps(dict(diagnostics)))
        return {'statusCode': 200, 'body': 'no closed buckets'}

    write_series(edge_series, diagnostics)
    # The diagnostics go in a request of their own, AFTER the edges, so a refused
    # edge batch does not take with it the numbers that explain the refusal.
    write_series(diagnostic_series(diagnostics), diagnostics)

    print('Diagnostics: ' + json.dumps(dict(diagnostics)))

    return {
        'statusCode': 200,
        'body': json.dumps({'series': len(edge_series), 'diagnostics': dict(diagnostics)}),
    }


# --- bench ------------------------------------------------------------------------
#
# Reading a real file by hand is what proves the part that decides everything: whether
# an address becomes a resource. It needs no pipeline and no workspace.
#
#   python ProcessFlowLogs.py <a local flow log file>

if __name__ == '__main__':
    import sys

    path = sys.argv[1]
    raw = open(path, 'rb').read()
    if path.endswith('.gz'):
        raw = gzip.decompress(raw)
    text = io.StringIO(raw.decode('utf-8', errors='replace'))
    mapping = field_map_from_header(text.readline())
    print('field map: ' + json.dumps(mapping))

    rows = []
    for line in text:
        parts = line.split()
        if len(parts) >= len(mapping):
            rows.append({name: parts[index] for name, index in mapping.items()})

    bench_diagnostics = defaultdict(int)
    totals = accumulate(rows, [], bench_diagnostics)
    print('buckets x pairs: ' + str(len(totals)))
    print('diagnostics: ' + json.dumps(dict(bench_diagnostics)))
    for (bucket, labels), values in sorted(totals.items())[:20]:
        print(str(bucket) + '  ' + str(values) + '  ' + json.dumps(dict(labels)))
