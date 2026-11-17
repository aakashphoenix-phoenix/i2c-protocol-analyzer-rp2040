"""Measured communication evidence, separate from topology and capture health.

No HEALTHY/FAULTY or recovery thresholds are documented in the original project.
Consequently successful traffic is evidence, not a positive health classification.
Failures persist for the capture session; a successful retry never erases one.
The existing registry's 3 s activity bound is used ONLY to label freshness.
"""
from dataclasses import dataclass
import time

@dataclass
class DeviceEvidence:
    successful: int = 0
    failed: int = 0
    incomplete: int = 0
    unexpected_nacks: int = 0
    eligible_ack_bits: int = 0
    expected_read_nacks: int = 0
    unverified_nacks: int = 0
    last_success_hw: float | None = None
    last_success_wall: float | None = None
    last_observed_wall: float | None = None
    last_failure: str = ''

class CommunicationEvidence:
    def __init__(self):
        self.devices = {}
        self.acknowledged = set()

    def observe(self, txn, configured):
        now = time.monotonic()
        addresses = {f.addr for f in txn.frames if f.addr is not None}
        for addr in addresses:
            d = self.devices.setdefault(addr, DeviceEvidence())
            frames = [f for f in txn.frames if f.addr == addr]
            reasons = ['Malformed transaction'] if txn.status == 'malformed' else []
            unknown = txn.status != 'ok'
            for f in frames:
                if f.addr_ack is True:
                    self.acknowledged.add(addr)
                    d.eligible_ack_bits += 1
                elif f.addr_ack is False:
                    if addr in configured or addr in self.acknowledged:
                        d.unexpected_nacks += 1; d.eligible_ack_bits += 1
                        reasons.append('Address NACK')
                    else:
                        # An unconfigured never-ACKed probe is not a known
                        # required device. Preserve its ambiguity, not an error.
                        d.unverified_nacks += 1; unknown = True
                else:
                    unknown = True
                for i, ack in enumerate(f.acks):
                    if ack is True:
                        d.eligible_ack_bits += 1
                    elif f.rw == 'R' and i == len(f.acks)-1 and txn.status == 'ok':
                        d.expected_read_nacks += 1
                    elif f.rw == 'W' or i < len(f.acks)-1:
                        d.unexpected_nacks += 1; d.eligible_ack_bits += 1
                        reasons.append('Write data NACK' if f.rw == 'W' else 'Premature read NACK')
                    else:
                        d.unverified_nacks += 1; unknown = True
            d.last_observed_wall = now
            if reasons:
                d.failed += 1
                d.last_failure = '; '.join(dict.fromkeys(reasons))
            elif not unknown:
                d.successful += 1
                d.last_success_hw = txn.t_end
                d.last_success_wall = now
            if unknown:
                d.incomplete += 1

    def device(self, addr, trustworthy, reason='', now=None):
        now = time.monotonic() if now is None else now
        d = self.devices.get(addr, DeviceEvidence())
        age = None if d.last_observed_wall is None else max(0, now-d.last_observed_wall)
        fresh = age is not None and age < 3  # original activity boundary
        if not trustworthy:
            status, detail = 'UNKNOWN', reason
        elif not fresh:
            status, detail = 'UNKNOWN', 'No fresh transaction evidence (existing 3 s activity bound)'
        elif d.failed:
            status, detail = 'DEGRADED', 'Unexpected failures observed; no recovery criteria defined'
        else:
            status, detail = 'UNKNOWN', 'HEALTHY / FAULTY / recovery criteria are not defined'
        if d.incomplete and status != 'DEGRADED':
            detail += '; incomplete/unverified transactions observed'
        return dict(status=status, detail=detail, successful=d.successful, failed=d.failed,
                    incomplete=d.incomplete, unexpected_nacks=d.unexpected_nacks,
                    unexpected_nack_pct=(100*d.unexpected_nacks/d.eligible_ack_bits if d.eligible_ack_bits else None),
                    eligible_ack_bits=d.eligible_ack_bits, expected_read_nacks=d.expected_read_nacks,
                    unverified_nacks=d.unverified_nacks, last_success_hw=d.last_success_hw,
                    last_success_age_s=(None if d.last_success_wall is None else max(0,now-d.last_success_wall)),
                    last_failure=d.last_failure or None, observation_age_s=age,
                    confidence=('Fresh observed traffic' if fresh and trustworthy else 'Unverified / stale / unreliable'))
