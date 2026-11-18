"""Application policy: rolling 60 s of capture time, never an electrical certification."""
from collections import deque
from dataclasses import dataclass
import time

WINDOW_NS = 60_000_000_000
RULE = "60 s capture window: >=1 complete transaction; zero unexpected NACKs = HEALTHY; any = DEGRADED; unreliable/insufficient = UNKNOWN"


def acknowledgment_counts(txn, addr=None):
    c = dict(acks=0, raw_nacks=0, expected_read_nacks=0, unexpected_nacks=0,
             address_nacks=0, write_data_nacks=0, other_nacks=0, incomplete=int(txn.status != 'ok'),
             complete=int(txn.status == 'ok'))
    for fi, f in enumerate(txn.frames):
        if addr is not None and f.addr != addr:
            continue
        # Byte records retain individual ACK slots for 10-bit addressing.
        slots = [b.ack for b in txn.bytes if b.frame == fi and b.kind.startswith('addr')]
        if not slots:
            slots = [f.addr_ack]
        for ack in slots:
            if ack is True: c['acks'] += 1
            elif ack is False:
                c['raw_nacks'] += 1; c['address_nacks'] += 1; c['unexpected_nacks'] += 1
        for i, ack in enumerate(f.acks):
            if ack is True:
                c['acks'] += 1
            elif ack is False:
                c['raw_nacks'] += 1
                boundary = fi + 1 < len(txn.frames) or any(k == 'P' for _, k in txn.events)
                expected = (txn.status == 'ok' and f.addr_ack is True and f.rw == 'R'
                            and i == len(f.data)-1 and boundary and all(a is True for a in f.acks[:i]))
                if expected: c['expected_read_nacks'] += 1
                else:
                    c['unexpected_nacks'] += 1
                    c['write_data_nacks' if f.rw == 'W' else 'other_nacks'] += 1
    c['opportunities'] = c['acks'] + c['raw_nacks']
    return c


class CommunicationEvidence:
    def __init__(self):
        self.last_transaction_wall = None
        self.devices = {}
        self.buckets = deque()
        self.history = deque(maxlen=300)

    def observe(self, txn, configured):
        self.last_transaction_wall = time.monotonic()
        now = txn.t_end if txn.t_end is not None else txn.t_start
        sec = int(now // 1e9)
        counts = acknowledgment_counts(txn)
        if not self.buckets or self.buckets[-1][0] != sec:
            self.buckets.append((sec, {}))
        b = self.buckets[-1][1]
        for k, v in counts.items(): b[k] = b.get(k, 0) + v
        for addr in {f.addr for f in txn.frames if f.addr is not None}:
            d = self.devices.setdefault(addr, DeviceEvidence())
            c = acknowledgment_counts(txn, addr)
            d.last_observed_wall = time.monotonic()
            d.unexpected_nacks += c['unexpected_nacks']
            d.eligible_ack_bits += c['opportunities']
            d.expected_read_nacks += c['expected_read_nacks']
            d.incomplete += c['incomplete']
            if c['unexpected_nacks']:
                d.failed += 1; d.last_failure = 'Unexpected NACK'
            elif not c['incomplete']:
                d.successful += 1; d.last_success_hw = now; d.last_success_wall = time.monotonic()
        snap = self.snapshot(now)
        point = (now / 1e9, snap['unexpected_nack_pct'])
        if self.history and int(self.history[-1][0]) == sec: self.history[-1] = point
        else: self.history.append(point)

    def snapshot(self, now):
        now = now or 0
        while self.buckets and self.buckets[0][0] < int(now // 1e9) - 59:
            self.buckets.popleft()
        keys = acknowledgment_counts(type('Empty', (), dict(status='ok', frames=[], bytes=[]))())
        out = {k: sum(b.get(k, 0) for _, b in self.buckets) for k in keys}
        n = out['opportunities']
        out['raw_nack_pct'] = 100 * out['raw_nacks'] / n if n else None
        out['unexpected_nack_pct'] = 100 * out['unexpected_nacks'] / n if n else None
        out['status'] = ('UNKNOWN' if not out['complete'] or out['incomplete'] or not n else
                         'DEGRADED' if out['unexpected_nacks'] else 'HEALTHY')
        return out

    def device(self, addr, trustworthy, reason='', now=None):
        now = time.monotonic() if now is None else now
        d = self.devices.get(addr, DeviceEvidence())
        age = None if d.last_observed_wall is None else max(0, now-d.last_observed_wall)
        fresh = age is not None and age < 3
        status = 'DEGRADED' if trustworthy and fresh and d.failed else 'UNKNOWN'
        return dict(status=status, detail=reason or 'Session evidence; bus policy uses rolling 60 s',
                    successful=d.successful, failed=d.failed, incomplete=d.incomplete,
                    unexpected_nacks=d.unexpected_nacks,
                    unexpected_nack_pct=100*d.unexpected_nacks/d.eligible_ack_bits if d.eligible_ack_bits else None,
                    eligible_ack_bits=d.eligible_ack_bits, expected_read_nacks=d.expected_read_nacks,
                    unverified_nacks=0, last_success_hw=d.last_success_hw,
                    last_success_age_s=None if d.last_success_wall is None else max(0, now-d.last_success_wall),
                    last_failure=d.last_failure or None, observation_age_s=age,
                    confidence='Fresh observed traffic' if fresh and trustworthy else 'Unverified / stale / unreliable')

@dataclass
class DeviceEvidence:
    successful: int = 0
    failed: int = 0
    incomplete: int = 0
    unexpected_nacks: int = 0
    eligible_ack_bits: int = 0
    expected_read_nacks: int = 0
    last_success_hw: float | None = None
    last_success_wall: float | None = None
    last_observed_wall: float | None = None
    last_failure: str = ''
