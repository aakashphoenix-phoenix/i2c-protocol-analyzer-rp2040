import pytest

from communication.protocol_parser import (LineAssembler, ProtocolError, SequenceTracker, capabilities_from_hello,
                                           parse_line)


def test_hello_and_caps():
    rec = parse_line(b'{"t":"hello","proto":1,"fw":"x","sample_hz":10000000,"caps":["edges","seq"]}')
    caps = capabilities_from_hello(rec)
    assert caps.proto == 1 and caps.tick_ns == 100 and caps.has("edges") and not caps.has("lossless")


def test_edges_valid():
    rec = parse_line('{"t":"edges","seq":3,"t0":100,"e":[[0,3],[25,2]]}')
    assert rec.seq == 3 and rec.data["e"][1] == [25, 2]


@pytest.mark.parametrize("line", [
    b"", b"hello world", b"{not json", b'{"t":"nope"}', b'["t","edges"]',
    b'{"t":"edges","seq":1,"t0":5,"e":[[1,7]]}',          # state out of range
    b'{"t":"edges","seq":1,"t0":-5,"e":[]}',              # negative time
    b'{"t":"edges","seq":1.5,"t0":5,"e":[]}',             # non-integer seq
    b'{"t":"evt","kind":"explode","ts":1}', b"\xff\xfe",
])
def test_malformed_rejected(line):
    with pytest.raises(ProtocolError):
        parse_line(line)


def test_line_assembler_partial_reads():
    a = LineAssembler()
    assert a.feed(b'{"t":"log","msg":"a"}\n{"t":"lo') == [b'{"t":"log","msg":"a"}']
    assert a.feed(b'g","msg":"b"}\n') == [b'{"t":"log","msg":"b"}']


def test_line_assembler_resync_on_garbage():
    a = LineAssembler()
    a.feed(b"x" * 70000)
    assert a.overlong == 1
    # The suffix of the oversized frame must be discarded through its newline.
    assert a.feed(b'garbage suffix\n{"t":"log","msg":"ok"}\n') == [b'{"t":"log","msg":"ok"}']


def test_sequence_gap_detection():
    s = SequenceTracker()
    assert [s.check(i) for i in (0, 1, 2)] == [0, 0, 0]
    assert s.check(5) == 2 and s.lost == 2
    assert s.check(6) == 0
    assert s.check(0) == 0          # wrap/restart backwards is not a loss
