'''Compile the actual C decoder/JSON emitter on the host using synthetic pins.

This does not exercise RP2040 PIO, DMA IRQ timing, USB CDC or real wiring.
'''
import json
from pathlib import Path
import shutil
import struct
import subprocess

import pytest
from analysis.capture_engine import CaptureEngine
from communication.protocol_parser import parse_line

ROOT = Path(__file__).resolve().parents[1]


def build_decoder(tmp_path, stream=False):
    """Compile the firmware's decoder + JSON emitters (extracted verbatim) for the host."""
    gcc = shutil.which('gcc')
    if not gcc:
        pytest.skip('Host GCC is not installed; Pico SDK build remains separate')
    firmware = (ROOT/'firmware/native_rp2040_analyzer/packet_analyzer.c').read_text(encoding='utf8')
    # SDK queue stand-in only; decoder and emitters below are extracted verbatim.
    stub = r'''
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
typedef struct { unsigned size, n; unsigned char data[128][4096]; } queue_t;
static bool queue_try_add(queue_t *q, const void *r) {
    if(q->n == 128) return false;
    memcpy(q->data[q->n++], r, q->size); return true;
}
'''
    macros = firmware[firmware.index('#define SDA_PIN'):firmware.index('// One output record')]
    records = firmware[firmware.index('// One output record'):firmware.index('static uint32_t sample_buffers')]
    decoder = firmware[firmware.index('static void enqueue_record'):firmware.index('// IRQ is installed')]
    emitters = firmware[firmware.index('static void print_hello'):firmware.index('static void handle_command')]
    driver = r'''
int main(int argc, char **argv) {
    if(argc != 2) return 2;
    output_queue.size = sizeof(Record);
    waveform_queue.size = sizeof(Waveform);
    FILE *f = fopen(argv[1], "rb");
    if(!f) return 3;
    uint32_t buffer[WORDS]; uint64_t base = 100000;
    print_hello();
    while(fread(buffer, sizeof(buffer), 1, f) == 1) {
        decode_buffer(buffer, base); base += BLOCK_SAMPLES;
        for(unsigned i=0; i<output_queue.n; i++) print_record((const Record *)output_queue.data[i]);
        for(unsigned i=0; i<waveform_queue.n; i++) print_waveform((const Waveform *)waveform_queue.data[i]);
        output_queue.n = waveform_queue.n = 0;
    }
    fclose(f);
    print_status();
    return 0;
}
'''
    stream_driver = r'''
int main(int argc, char **argv) {
    if(argc < 2) return 2;
    int gap_after = argc > 2 ? atoi(argv[2]) : -1, idx = 0;
    output_queue.size = sizeof(Record);
    waveform_queue.size = sizeof(Waveform);
    stream_queue.size = sizeof(StreamBatch);
    stream_enabled = true;                         // what begin_capture() does at START
    FILE *f = fopen(argv[1], "rb");
    if(!f) return 3;
    uint32_t buffer[WORDS]; uint64_t base = 100000;
    print_hello();
    while(fread(buffer, sizeof(buffer), 1, f) == 1) {
        decode_buffer(buffer, base); base += BLOCK_SAMPLES;
        for(unsigned i=0; i<output_queue.n; i++) print_record((const Record *)output_queue.data[i]);
        for(unsigned i=0; i<waveform_queue.n; i++) print_waveform((const Waveform *)waveform_queue.data[i]);
        for(unsigned i=0; i<stream_queue.n; i++) print_stream((const StreamBatch *)stream_queue.data[i]);
        output_queue.n = waveform_queue.n = stream_queue.n = 0;
        if(idx++ == gap_after) reset_after_capture_gap(base);    // a DMA overrun: samples not observed
    }
    fclose(f);
    print_status();
    return 0;
}
'''
    source = tmp_path/('decoder_stream.c' if stream else 'decoder.c')
    source.write_text(stub+macros+records+decoder+emitters+(stream_driver if stream else driver), encoding='utf8')
    exe = tmp_path/('decoder_stream.exe' if stream else 'decoder.exe')
    built = subprocess.run([gcc,'-std=c11','-Wall','-Wextra','-O2',str(source),'-o',str(exe)],
                           capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    return exe


class Pins(list):
    """Two-bit SDA/SCL sample stream (bit0 SDA, bit1 SCL) at the analyzer sample rate."""
    def hold(self, state, n=10): self.extend([state]*n)
    def start(self): self.hold(3); self.hold(2); self.hold(0)
    def bit(self, sda): self.hold(sda); self.hold(sda | 2)
    def byte(self, value, ack=True):
        for bit in range(7,-1,-1):
            self.bit((value >> bit) & 1)
        self.hold(0 if ack else 1); self.hold(2 if ack else 3); self.hold(0 if ack else 1)
    def restart(self): self.hold(1); self.hold(3); self.hold(2); self.hold(0)
    def stop(self): self.hold(0); self.hold(2); self.hold(3)
    def run(self, exe, tmp_path, name='stimulus.bin', extra=()):
        pins = list(self) + [3] * ((-len(self)) % (2048*16))
        words = [sum(pins[i+j] << (2*j) for j in range(16)) for i in range(0,len(pins),16)]
        f = tmp_path/name
        f.write_bytes(struct.pack('<'+'I'*len(words),*words))
        return subprocess.run([str(exe),str(f),*map(str, extra)],capture_output=True,check=True).stdout.splitlines()


def test_c_decoder_emitter_to_python_with_synthetic_pio_words(tmp_path):
    exe = build_decoder(tmp_path)
    pins = []
    def hold(state, n=10): pins.extend([state]*n)
    def start():
        hold(3); hold(2); hold(0)
    def byte(value, ack=True):
        for bit in range(7,-1,-1):
            sda = (value >> bit) & 1
            hold(sda); hold(sda | 2)
        hold(0 if ack else 1); hold(2 if ack else 3); hold(0 if ack else 1)
    def restart():
        hold(1); hold(3); hold(2); hold(0)
    def stop():
        hold(0); hold(2); hold(3)
    hold(3,40)
    start(); byte(0x68<<1); byte(0); restart(); byte((0x68<<1)|1); byte(0x42,False); stop()
    start(); byte(0x76<<1); byte(0xD0); restart(); byte((0x76<<1)|1); byte(0x58,False); stop()
    start(); byte(0x3C<<1); byte(0x40); byte(0xAA); stop()
    start(); byte(0x55<<1,False); stop()
    # A long OLED payload must stay bounded (MAX_DATA 255: the 251-byte 2.6 OLED text write fits) and report truncation.
    start(); byte(0x3C<<1)
    for n in range(260): byte(n & 0xFF)
    stop()
    # Pad to whole DMA buffers; idle closes any open measured window.
    pins.extend([3] * ((-len(pins)) % (2048*16)))
    words = [sum(pins[i+j] << (2*j) for j in range(16)) for i in range(0,len(pins),16)]
    stimulus = tmp_path/'synthetic-pio.bin'
    stimulus.write_bytes(struct.pack('<'+'I'*len(words),*words))
    run = subprocess.run([str(exe),str(stimulus)],capture_output=True,check=True)
    lines = run.stdout.splitlines()
    for line in lines: parse_line(line)
    emitted = [json.loads(line) for line in lines]
    segments = [parse_line(line).data for line in lines if json.loads(line).get('type') == 'segment']   # compact form expanded
    windows = [r for r in emitted if r.get('t') == 'edges']
    assert len(segments) == 7
    assert segments[0]['address'] == 0x68 and segments[0]['end'] == 'RESTART'
    assert segments[1]['direction'] == 'read' and segments[1]['acks'] == [False]
    assert segments[3]['data'] == [0x58]
    assert segments[4]['address'] == 0x3C and segments[4]['data'] == [0x40,0xAA]
    assert segments[5]['address_ack'] is False
    assert segments[6]['truncated'] is True and segments[6]['data'] == [n & 0xFF for n in range(255)]
    assert all(not r['incomplete'] for r in segments)
    assert all(90000 < r['scl_hz'] < 110000 for r in segments)
    assert windows and windows[0]['snapshot'] and len(windows[0]['e']) <= 768
    assert sum(dt for dt,state in windows[0]['e']) <= 4000
    # Exact observed states and ticks, including the pre-START level.
    t = windows[0]['t0']
    for dt,state in windows[0]['e']:
        t += dt
        assert pins[t-100000] == state
    engine = CaptureEngine(); engine.set_source('live'); engine.ingest_lines(lines)
    assert len(engine.packets) == 5 and engine.transport_errors == 0
    assert engine.waveform_snapshots == 1 and engine.edges
    assert engine.packets[0].operation == 'WRITE→READ'
    assert engine.packets[1].operation == 'WRITE→READ'
    # Exact 0.5 us sample indices are emitted and used unchanged (no us truncation).
    assert all('start_sample' in r and 'end_sample' in r for r in segments)
    assert all(r['start_us'] == r['start_sample'] // 2 for r in segments)
    assert engine.packets[0].t_start == segments[0]['start_sample'] * 500
    assert engine.packets[0].t_end == segments[1]['end_sample'] * 500
    # Waveform-to-decoder agreement: the raw window decodes to the same bytes/ACKs
    # as the firmware segments, so measured bit times are attached, no disagreement.
    assert not engine.decode_disagreements
    assert engine.packets[0].bytes[0].bit_times and len(engine.packets[0].bytes[0].bit_times) == 8
    (tmp_path/'synthetic-firmware-output.jsonl').write_bytes(run.stdout)

    # A passive capture of idle-high or stuck-low must return those exact
    # constant sampled levels without requiring a START or creating pulses.
    for level,word in [(3,0xFFFFFFFF),(0,0)]:
        idle = tmp_path/f'constant-{level}.bin'
        idle.write_bytes(struct.pack('<I',word)*2048)
        result = subprocess.run([str(exe),str(idle)],capture_output=True,check=True)
        records = [json.loads(line) for line in result.stdout.splitlines()]
        snapshots = [r for r in records if r.get('t')=='edges']
        assert len(snapshots)==1
        assert {state for dt,state in snapshots[0]['e']}=={level}
        assert sum(dt for dt,state in snapshots[0]['e'])==4000
        assert not any(r.get('type')=='segment' for r in records)

    # Exercise simultaneous changes and all two-bit states across word borders.
    # These random values exist only in this isolated host unit test.
    import random
    rng = random.Random(9283)
    random_pins = [rng.randrange(4) for _ in range(2048*16)]
    random_words = [sum(random_pins[i+j] << (2*j) for j in range(16))
                    for i in range(0,len(random_pins),16)]
    random_input = tmp_path/'isolated-random-samples.bin'
    random_input.write_bytes(struct.pack('<'+'I'*len(random_words),*random_words))
    result = subprocess.run([str(exe),str(random_input)],capture_output=True,check=True)
    snapshots = [r for line in result.stdout.splitlines()
                 if (r:=json.loads(line)).get('t')=='edges']
    assert snapshots
    t = snapshots[0]['t0']
    actual = []
    for dt,state in snapshots[0]['e']:
        t += dt
        assert random_pins[t-100000] == state
        actual.append((t-100000,state))
    expected = [(0,random_pins[0])]
    expected += [(i,random_pins[i]) for i in range(1,len(random_pins))
                 if random_pins[i]!=random_pins[i-1]]
    assert actual == expected[:768]


def test_multi_master_traffic_decodes_cleanly_and_flags_mid_byte_cuts(tmp_path):
    """Back-to-back transactions of two masters, a repeated START, and a collision.

    Arbitration losers are invisible on a wired-AND bus, so the winner's traffic must
    decode as ordinary complete segments.  A START inside a byte (a master starting on a
    busy bus) and a STOP inside a byte (an ABORT) are flagged, never silently accepted.
    """
    exe = build_decoder(tmp_path)
    p = Pins(); p.hold(3, 40)
    # Master 1: BMP280 burst read (S W F7 Sr R x2 P), then Master 2's RTC read after a short gap
    p.start(); p.byte(0x76<<1); p.byte(0xF7); p.restart(); p.byte((0x76<<1)|1); p.byte(0x51); p.byte(0x60,False); p.stop()
    p.hold(3, 12)                     # STOP->START: 10 + 12 + 10 idle samples = 32 x 0.5 us = 16 us
    p.start(); p.byte(0x68<<1); p.byte(0x00); p.restart(); p.byte((0x68<<1)|1); p.byte(0x30,False); p.stop()
    p.hold(3, 40)
    # collision: a master starts, 3 address bits are clocked, then another START appears mid-byte
    p.start(); p.bit(0); p.bit(1); p.bit(1)
    p.hold(3); p.hold(2); p.hold(0)                   # SDA falls while SCL HIGH: START inside a byte
    p.byte(0x3C<<1); p.byte(0x40); p.byte(0xAA); p.stop()
    p.hold(3, 40)
    # controller ABORT: STOP after 5 data bits
    p.start(); p.byte(0x76<<1); p.bit(1); p.bit(0); p.bit(1); p.bit(1); p.bit(0); p.stop()
    lines = p.run(exe, tmp_path)
    for line in lines: parse_line(line)
    records = [json.loads(l) for l in lines]
    segs = [parse_line(l).data for l in lines if json.loads(l).get('type') == 'segment']
    status = [r for r in records if r.get('type') == 'status'][-1]
    assert [s['address'] for s in segs[:4]] == [0x76, 0x76, 0x68, 0x68]
    assert all(not s['incomplete'] and not s['mid_byte_end'] for s in segs[:4])
    assert segs[2]['start'] == 'START' and segs[2]['bus_free_ns'] == 16000   # measured tBUF
    assert 'bus_free_ns' not in segs[0]                # no previous STOP observed: unknown, not 0
    cut, after = segs[4], segs[5]
    assert cut['mid_byte_end'] and cut['incomplete'] and cut['end'] == 'RESTART'
    assert after['start'] == 'RESTART' and after['address'] == 0x3C and after['data'] == [0x40, 0xAA]
    aborted = segs[6]
    assert aborted['mid_byte_end'] and aborted['incomplete'] and aborted['end'] == 'STOP'
    assert status['mid_byte_ends'] == 2
    engine = CaptureEngine(); engine.set_source('live'); engine.ingest_lines(lines)
    assert engine.transport_errors == 0
    ok = [t for t in engine.packets if t.status == 'ok']
    assert [t.address for t in ok[:2]] == [0x76, 0x68]
    flagged = [t for t in engine.packets if any('inside a byte' in n for n in t.notes)]
    assert len(flagged) == 2 and all(t.status != 'ok' for t in flagged)
    assert engine.packets[1].timing.get('bus_free_ns') == 16000
