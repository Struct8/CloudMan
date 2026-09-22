"""Proves the risky parts of ProcessFlowLogs without touching AWS.

Run it before deploying anything:

    python test_ProcessFlowLogs.py

Two of the checks matter more than the rest. The protobuf is decoded back by a
wire-level reader written from the format, not from the encoder, so a wrong field
number or wire type shows up instead of being agreed on twice. The snappy block is
fed to a decompressor that implements the copy elements this encoder never emits,
so a malformed literal header cannot pass by being read the same way it was written.

Exit code is 1 on any failure, so this can gate a pipeline.
"""

import importlib.util
import ipaddress
import os
import struct
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, 'ProcessFlowLogs.py')


# The module builds boto3 clients at import time. Stub them: nothing here talks to AWS.
class _NoAws:
    def __getattr__(self, _):
        raise RuntimeError('no AWS on the bench')


sys.modules.setdefault('boto3', type(sys)('boto3'))
sys.modules['boto3'].client = lambda *a, **k: _NoAws()
sys.modules['boto3'].Session = lambda *a, **k: _NoAws()
for name in ('botocore', 'botocore.auth', 'botocore.awsrequest'):
    sys.modules.setdefault(name, type(sys)(name))
sys.modules['botocore.auth'].SigV4Auth = object
sys.modules['botocore.awsrequest'].AWSRequest = object

spec = importlib.util.spec_from_file_location('process_flow_logs', TARGET)
pfl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pfl)

failures = []


def check(name, ok, detail=''):
    print(('  ok    ' if ok else '  FAIL  ') + name + (('   ' + detail) if detail else ''))
    if not ok:
        failures.append(name)


# --- a wire-level protobuf reader, written from the format ------------------------

def read_varint(buffer, i):
    value = 0
    shift = 0
    while True:
        byte = buffer[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7


def decode(buffer):
    """[(field_number, wire_type, value)] at one level."""
    out = []
    i = 0
    while i < len(buffer):
        key, i = read_varint(buffer, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = read_varint(buffer, i)
        elif wire == 1:
            value = struct.unpack('<d', buffer[i:i + 8])[0]
            i += 8
        elif wire == 2:
            length, i = read_varint(buffer, i)
            value = buffer[i:i + length]
            i += length
        else:
            raise ValueError('unexpected wire type ' + str(wire))
        out.append((field, wire, value))
    return out


print('\n=== protobuf ===')
sample_series = [
    ({'__name__': 'struct8_edge_bytes', 'src_id': 'i-0abc', 'dst_id': 'internet'},
     [(1758549780000, 41230.0), (1758549840000, 900.0)]),
]
encoded = pfl.encode_write_request(sample_series)

top = decode(encoded)
check('WriteRequest carries timeseries in field 1',
      len(top) == 1 and top[0][0] == 1 and top[0][1] == 2)

timeseries = decode(top[0][2])
labels = [v for (f, w, v) in timeseries if f == 1]
samples = [v for (f, w, v) in timeseries if f == 2]
check('TimeSeries: three labels in field 1', len(labels) == 3, str(len(labels)))
check('TimeSeries: two samples in field 2', len(samples) == 2, str(len(samples)))

pairs = []
for raw in labels:
    fields = decode(raw)
    pairs.append((
        [v for (f, w, v) in fields if f == 1][0].decode(),
        [v for (f, w, v) in fields if f == 2][0].decode(),
    ))
check('Label: name in field 1, value in field 2',
      ('__name__', 'struct8_edge_bytes') in pairs, str(pairs))

first = decode(samples[0])
check('Sample: value is a double in field 1',
      [v for (f, w, v) in first if f == 1 and w == 1] == [41230.0])
check('Sample: timestamp is a varint in field 2',
      [v for (f, w, v) in first if f == 2 and w == 0] == [1758549780000])


# --- a snappy decompressor, including the copies we never emit --------------------

def snappy_decompress(buffer):
    declared, i = read_varint(buffer, 0)
    out = bytearray()
    while i < len(buffer):
        tag = buffer[i]
        i += 1
        kind = tag & 0x03
        if kind == 0:
            n = tag >> 2
            if n < 60:
                length = n + 1
            else:
                extra = n - 59
                length = int.from_bytes(buffer[i:i + extra], 'little') + 1
                i += extra
            out += buffer[i:i + length]
            i += length
        elif kind == 1:
            length = 4 + ((tag >> 2) & 0x07)
            offset = ((tag >> 5) << 8) | buffer[i]
            i += 1
            for _ in range(length):
                out.append(out[-offset])
        else:
            width = 2 if kind == 2 else 4
            length = (tag >> 2) + 1
            offset = int.from_bytes(buffer[i:i + width], 'little')
            i += width
            for _ in range(length):
                out.append(out[-offset])
    return bytes(out), declared


print('\n=== snappy, literal-only block ===')
# The sizes are the boundaries of the literal header: 60 is the last inline length,
# then one, two, three and four extra bytes.
for n in (0, 1, 59, 60, 61, 255, 256, 257, 4096, 70000):
    data = bytes((i * 7 + n) % 251 for i in range(n))
    block = pfl.snappy_literal_only(data)
    back, declared = snappy_decompress(block)
    check('round-trip of ' + str(n) + ' bytes', back == data and declared == n,
          'declared=' + str(declared) + ' got=' + str(len(back)))

back, _ = snappy_decompress(pfl.snappy_literal_only(encoded))
check('the WriteRequest survives the snappy framing', back == encoded)


# --- reading, deduplication and the collapse --------------------------------------

print('\n=== reading, deduplication and collapse ===')
HEADER = ('version vpc-id subnet-id interface-id instance-id srcaddr dstaddr '
          'pkt-srcaddr pkt-dstaddr srcport dstport protocol packets bytes start '
          'end action log-status flow-direction traffic-path pkt-src-aws-service '
          'pkt-dst-aws-service interface-type')

LINES = [
    # internal, egress: both ends resolve inside the VPC
    '11 vpc-1 sub-1 eni-1 i-0abc 10.0.1.5 10.0.2.9 10.0.1.5 10.0.2.9 4444 443 6 10 1500 1758549780 1758549840 ACCEPT OK egress 1 - - -',
    # the SAME conversation seen at the other interface: ingress, must disappear
    '11 vpc-1 sub-2 eni-2 i-0def 10.0.1.5 10.0.2.9 10.0.1.5 10.0.2.9 4444 443 6 10 1500 1758549780 1758549840 ACCEPT OK ingress 1 - - -',
    # out to the internet through an internet gateway
    '11 vpc-1 sub-1 eni-1 i-0abc 10.0.1.5 140.82.121.4 10.0.1.5 140.82.121.4 5555 443 6 4 800 1758549780 1758549840 ACCEPT OK egress 8 - - -',
    # to S3, which the record names
    '11 vpc-1 sub-1 eni-1 i-0abc 10.0.1.5 52.216.1.1 10.0.1.5 52.216.1.1 5556 443 6 2 300 1758549780 1758549840 ACCEPT OK egress 2 - S3 -',
    # to on-prem through a virtual private gateway: NOT the internet
    '11 vpc-1 sub-1 eni-1 i-0abc 10.0.1.5 10.99.0.7 10.0.1.5 10.99.0.7 5557 22 6 1 100 1758549780 1758549840 ACCEPT OK egress 3 - - -',
    # a window with no traffic: discarded
    '11 vpc-1 sub-1 eni-1 - - - - - - - - - - 1758549780 1758549840 - NODATA - - - - -',
]

cidrs = [(ipaddress.ip_network('10.0.0.0/16'), 'vpc-1')]
field_map = pfl.field_map_from_header(HEADER)
records = [{n: line.split()[i] for n, i in field_map.items()} for line in LINES]

diagnostics = defaultdict(int)
totals = pfl.accumulate(records, cidrs, diagnostics)

destinations = {}
for (_, label_tuple), values in totals.items():
    as_dict = dict(label_tuple)
    destinations[as_dict['dst_id'] or as_dict['dst_addr']] = (as_dict['dst_type'], values[0])

check('the ingress copy was deduplicated (four edges, not five)',
      len(totals) == 4, str(len(totals)))
check('NODATA discarded', diagnostics['records_nodata'] == 1, str(dict(diagnostics)))
check('an internal destination resolves by CIDR',
      destinations.get('10.0.2.9', ('', 0))[0] == 'address')
check('public through an internet gateway becomes internet',
      destinations.get('internet', ('', 0))[0] == 'internet')
check('a named service becomes S3',
      destinations.get('S3', ('', 0))[0] == 'aws_service')
check('10.99 through a VGW becomes on-premises, NOT internet',
      destinations.get('on-premises', ('', 0))[0] == 'on_premises')

check('a bucket from last year is closed and goes out',
      len(pfl.to_series(totals, defaultdict(int))) == len(totals) * 2)

# The cutoff only proves itself against a RECENT instant: the sample lines are from
# 2025 and have been closed for a year, so they would pass under any cutoff.
now = int(time.time())
recent = dict(records[0])
recent['start'] = str(now - 60)
recent['end'] = str(now)
recent_diagnostics = defaultdict(int)
recent_series = pfl.to_series(
    pfl.accumulate([recent], cidrs, recent_diagnostics), recent_diagnostics)
check('what just arrived is held back by the cutoff',
      recent_series == [] and recent_diagnostics['buckets_still_open'] == 1,
      'series=' + str(len(recent_series)))

original_cutoff = pfl.CUTOFF_SECONDS
pfl.CUTOFF_SECONDS = -10 ** 9
series = pfl.to_series(totals, defaultdict(int))
pfl.CUTOFF_SECONDS = original_cutoff
check('one bytes series and one packets series per edge',
      len(series) == len(totals) * 2, str(len(series)))
check('every series carries __name__', all('__name__' in l for l, _ in series))

print('\n' + ('all checks passed' if not failures else 'FAILED: ' + ', '.join(failures)))
sys.exit(1 if failures else 0)
