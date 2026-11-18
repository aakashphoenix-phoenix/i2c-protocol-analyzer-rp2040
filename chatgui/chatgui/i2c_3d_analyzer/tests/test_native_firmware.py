"""Regression tests for C bundle JSON Lines compatibility."""
import json
from analysis.capture_engine import CaptureEngine
from communication.protocol_parser import parse_line, ProtocolError, capabilities_from_hello
import pytest

HELLO = {"type":"hello","protocol":1,"role":"analyzer","board":"RP2040", "sample_rate":2000000,
         "sample_resolution_us":0.5,"capabilities":["segment","loss","status","7bit","10bit","ack","timing"],"waveform":False}

def seg(seq,start,end,kind0,kind1,addr,dir,bytes_,acks,**extra):
    return dict(type="segment",seq=seq,start_us=start,end_us=end,duration_us=end-start,
                start=kind0,end=kind1,address=addr,address_bits=7,address_known=True,direction=dir,
                address_ack=True,scl_hz=100000,scl_high_avg_ns=5000,scl_low_avg_ns=5000,
                scl_low_over_50us=0,truncated=False,incomplete=False,data=bytes_,acks=acks,**extra)

def test_native_hello():
    c=capabilities_from_hello(parse_line(json.dumps(HELLO)))
    assert c.sample_hz == 2000000 and c.tick_ns == 500 and c.has('segment') and not c.has('edges')

def test_native_rtc_repeated_start_and_final_nack():
    e=CaptureEngine();e.set_source('live')
    e.ingest_lines([json.dumps(HELLO).encode()])
    a=seg(1,100,250,'START','RESTART',0x68,'write',[0],[True])
    b=seg(2,250,650,'RESTART','STOP',0x68,'read',[0x22,0x18],[True,False])
    e.ingest_lines([json.dumps(a).encode(),json.dumps(b).encode()])
    assert len(e.packets)==1
    t=e.packets[0]
    assert len(t.frames)==2 and t.operation=='WRITE→READ'
    assert t.frames[0].start_kind=='S' and t.frames[1].start_kind=='Sr'
    assert t.status=='ok' and t.data_hex()=='W: 00 | R: 22 18'
    assert e.perf.compute().freq_avg_hz == 100000
    assert e.perf.compute().read_end_nacks == 1
    assert not e.edges
    assert e.caps.has('segment')

def test_native_loss_marks_gap():
    e=CaptureEngine();e.set_source('live');e.ingest_lines([json.dumps(HELLO).encode()])
    e.ingest_lines([json.dumps(seg(9,0,50,'START','RESTART',0x76,'write',[0xF7],[True])).encode()])
    e.ingest_lines([json.dumps({"type":"loss","seq":10,"t_us":75,"lost_buffers":2}).encode()])
    assert len(e.packets)==1 and e.packets[0].status=='gap'
    assert e.health()[0]=='Degraded'
    assert e.fw_stats['overflows'] == 2

def test_native_lifetime_status_not_miscounted():
    e=CaptureEngine();e.set_source('live');e.ingest_lines([json.dumps(HELLO).encode()])
    e.ingest_lines([json.dumps({"type":"status","capturing":True,"dma_lost_buffers":10,"dropped_records":4}).encode()])
    assert e.fw_stats['overflows']==0 and e.fw_stats['dropped']==0
    e.ingest_lines([json.dumps({"type":"status","capturing":True,"dma_lost_buffers":11,"dropped_records":6}).encode()])
    assert e.fw_stats['overflows']==1 and e.fw_stats['dropped']==2

@pytest.mark.parametrize('changes', [
    {'acks':[True,True,True]}, {'address':300}, {'start':'BROKEN'},
    {'data':[256]}, {'address_ack':'true'}, {'scl_hz':-1}
])
def test_native_invalid_segment_rejected(changes):
    item=seg(1,0,20,'START','STOP',0x68,'write',[0],[True]);item.update(changes)
    with pytest.raises(ProtocolError): parse_line(json.dumps(item))
