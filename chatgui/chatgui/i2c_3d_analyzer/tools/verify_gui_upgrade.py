"""Render real Qt/OpenGL pages using deterministic demo records; no serial connection."""
import os, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/".runtime_dependencies"))
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer
from gui.main_window import MainWindow
from sources.demo_source import DemoGenerator
app=QApplication([])
from gui.theme import STYLESHEET
app.setStyleSheet(STYLESHEET)
w=MainWindow();w.resize(1910,1050);w.show()
w.engine.set_source('demo')
g=DemoGenerator(seed=17)
w.engine.ingest_lines([g.hello()])
for k in range(1,61): w.engine.ingest_lines(g.lines_until(k*0.05))
w.packet_model.sync()
t=next(t for t in reversed(w.engine.packets) if t.has_read and t.byte_count < 10)
w._set_state()
w.select_transaction(t)
w.timer.stop()
root=Path('work/verification')
steps=iter([(3,'waveform'),(0,'dashboard'),(1,'network'),(4,'performance')])
def render():
 try: idx,name=next(steps)
 except StopIteration:
  print('OpenGL renderer:',w.p_net.net.is_3d,'context valid:',w.p_net.net.view.isValid() if w.p_net.net.is_3d else False,flush=True)
  w.close();app.quit();return
 w.nav.setCurrentRow(idx)
 w.pages[idx].refresh(w.engine)
 w.playback.position=min(10,len(w.playback.events)-1);w._sync_playback()
 if name == 'performance':
  from PyQt6.QtWidgets import QScrollArea
  scroll=w.p_perf.findChild(QScrollArea) if hasattr(w,'p_perf') else w.pages[4].findChild(QScrollArea)
  scroll.verticalScrollBar().setValue(500)
 QTimer.singleShot(600,lambda:(w.grab().save(str(root/(name+'.png'))),render()))
QTimer.singleShot(700,render)
app.exec()
