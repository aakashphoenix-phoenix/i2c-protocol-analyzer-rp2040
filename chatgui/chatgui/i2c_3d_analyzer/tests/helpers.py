"""Builds protocol lines for exact, hand-specified I²C traffic (used by the tests)."""
import json

from sources.demo_source import EdgeSynth


def synth(build, t_start=1000, scl_hz=100_000):
    s = EdgeSynth(scl_hz=scl_hz)
    s.t = t_start
    s.out = [(t_start - 500, 3)]          # initial idle state
    s.last_s = 3
    build(s)
    s.wait(500)
    return s.out


def lines_for(edges, seq0=0, chunk=50):
    out = [json.dumps({"t": "hello", "proto": 1, "fw": "test", "sample_hz": 10_000_000, "tick_ns": 100,
                       "caps": ["edges", "seq"]})]
    seq = seq0
    for i in range(0, len(edges), chunk):
        c = edges[i:i + chunk]
        prev = c[0][0]
        e = []
        for t, st in c:
            e.append([t - prev, st])
            prev = t
        out.append(json.dumps({"t": "edges", "seq": seq, "t0": c[0][0], "e": e}))
        seq += 1
    return [ln.encode() for ln in out]


def write(addr, data, nack_addr=False):
    def b(s):
        s.start()
        s.byte(addr << 1, ack=not nack_addr)
        if not nack_addr:
            for x in data:
                s.byte(x)
        s.stop()
    return b


def write_read(addr, reg, values):
    def b(s):
        s.start(); s.byte(addr << 1); s.byte(reg)
        s.rstart(); s.byte(addr << 1 | 1)
        for i, v in enumerate(values):
            s.byte(v, ack=i < len(values) - 1)
        s.stop()
    return b


def seq_of(*builders, gap=2000):
    def b(s):
        for f in builders:
            f(s)
            s.wait(gap)
    return b
