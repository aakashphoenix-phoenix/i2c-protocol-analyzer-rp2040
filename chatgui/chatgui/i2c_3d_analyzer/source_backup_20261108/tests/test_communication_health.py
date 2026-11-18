import time
from types import SimpleNamespace
from unittest.mock import patch
from analysis.capture_engine import CaptureEngine
from analysis.i2c_decoder import Transaction, Frame
from tests.test_hardware_pipeline import qt_app

def engine():
    e=CaptureEngine();e.set_source('live');e.stream_connected=True;e.capture_active=True
    e.last_record_wall=time.monotonic()
    return e

def txn(acks, rw='R', addr=0x76, address_ack=True, status='ok'):
    return Transaction(id=1,t_start=100,t_end=1000,status=status,
                       frames=[Frame('S',100,addr=addr,rw=rw,addr_ack=address_ack,data=[1]*len(acks),acks=acks)])

def test_expected_final_read_nack_is_not_failure_or_healthy():
    e=engine();e._store(txn([True,False]));h=e.device_health(0x76)
    assert h['successful']==1 and h['failed']==0 and h['unexpected_nacks']==0
    assert h['expected_read_nacks']==1 and h['status']=='UNKNOWN'
    assert e.communication_health()[0]=='UNKNOWN'

def test_write_nack_persists_after_successful_retry_and_degrades_bus():
    e=engine();e._store(txn([False],rw='W'));e._store(txn([True],rw='W'))
    h=e.device_health(0x76)
    assert h['status']=='DEGRADED' and h['successful']==1 and h['failed']==1
    assert h['unexpected_nacks']==1 and h['unexpected_nack_pct']==25
    assert e.communication_health()[0]=='DEGRADED'

def test_unconfigured_probe_is_not_unexpected_but_known_missing_device_is():
    e=engine();e._store(txn([],addr=0x25,address_ack=False))
    assert e.device_health(0x25)['unexpected_nacks']==0
    e._store(txn([],addr=0x68,address_ack=False))
    assert e.device_health(0x68)['unexpected_nacks']==1
    assert e.communication_health()[0]=='DEGRADED'

def test_premature_read_nack_is_unexpected():
    e=engine();e._store(txn([False,False]))
    h=e.device_health(0x76)
    assert h['unexpected_nacks']==1 and h['expected_read_nacks']==1 and h['failed']==1

def test_unreliable_disconnected_stale_and_incomplete_never_positive():
    e=engine();e._store(txn([True],rw='W',status='gap'))
    assert e.device_health(0x76)['incomplete']==1
    e.fw_stats['overflows']=1
    assert e.device_health(0x76)['status']=='UNKNOWN'
    assert e.communication_health()[0]=='DEGRADED'
    e.stream_connected=False
    assert e.communication_health()[0]=='UNKNOWN'
    assert e.device_health(0x76)['status']=='UNKNOWN'
    e=engine();e._store(txn([True],rw='W'))
    e.communication.devices[0x76].last_observed_wall-=4
    assert e.device_health(0x76)['status']=='UNKNOWN'
    e.capture_active=False
    assert e.communication_health()[0]=='UNKNOWN'

def test_gui_shows_missing_devices_and_negative_health_even_paused(qt_app):
    import gui.network_3d as network
    from gui.main_window import MainWindow
    with patch.object(network,'HAVE_GL',False),patch('gui.main_window.list_ports',return_value=[]):
        w=MainWindow();w.engine=engine();w.engine._store(txn([False],rw='W'))
        # Packet model keeps its own engine reference; this test only checks health.
        w.display_paused=True;w._refresh()
        assert 'DEGRADED' in w.p_dash.device_health_text.text()
        assert w.engine.communication_health()[0]=='DEGRADED'
        w.p_dev.refresh(w.engine)
        assert w.p_dev.tbl.rowCount()>=3
        assert 'HEALTHY' not in [w.p_dev.tbl.item(r,11).text() for r in range(w.p_dev.tbl.rowCount())]
        w.close()
