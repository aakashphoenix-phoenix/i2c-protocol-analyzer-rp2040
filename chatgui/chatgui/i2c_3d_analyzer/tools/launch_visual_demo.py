"""Local DEMO launcher for this computer's bundled Python 3.12 runtime."""
from pathlib import Path
import sys
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.append(str(root / '.venv' / 'Lib' / 'site-packages'))
import PyQt6
PyQt6.__path__.insert(0, str(root / 'tools' / 'visual_runtime'))
import main
sys.argv = [str(root / 'main.py'), '--demo']
main.main()
