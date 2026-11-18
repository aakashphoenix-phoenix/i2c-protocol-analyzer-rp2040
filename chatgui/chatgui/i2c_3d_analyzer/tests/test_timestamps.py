"""Software-only timestamp display regressions."""
from types import SimpleNamespace
from analysis.playback import Playback, capture_clock_text
from tests.test_upgrade import capture
from tests.test_hardware_pipeline import qt_app


def test_read_write_times_are_captured_byte_times_and_clock_advances():
    e=capture();t=e.packets[0]
    reads=[b for b in t.bytes if b.kind=='data' and t.frames[b.frame].rw=='R']
    writes=[b for b in t.bytes if b.kind=='data' and t.frames[b.frame].rw=='W']
    assert e.last_transfer_timestamps['READ']==(reads[-1].t_first,True,t.id)
    assert e.last_transfer_timestamps['WRITE']==(writes[-1].t_first,True,t.id)
    original=dict(e.last_transfer_timestamps)
    old=capture_clock_text(e)
    e.hw_now+=1000000000
    assert capture_clock_text(e)!=old
    assert e.last_transfer_timestamps==original
    assert 'Simulation time:' in old and 'Last READ:' in old and 'Last WRITE:' in old
    e.clear()
    assert not e.last_transfer_timestamps and 'UNKNOWN' in capture_clock_text(e)


def test_both_views_keep_clock_visible_without_selected_packet(qt_app):
    from gui.network_3d import Network3D
    e=capture();p=Playback()
    first=Network3D(prefer_3d=False,compact=True)
    second=Network3D(prefer_3d=False)
    first.show_stage(p,e);second.show_stage(p,e)
    assert first.capture_clock.text()==second.capture_clock.text()==capture_clock_text(e)
    previous=first.capture_clock.text()
    e.hw_now+=1000000000
    first.playback_paused=True
    first.show_stage(p,e)
    assert first.capture_clock.text()!=previous
    first.close();second.close()
