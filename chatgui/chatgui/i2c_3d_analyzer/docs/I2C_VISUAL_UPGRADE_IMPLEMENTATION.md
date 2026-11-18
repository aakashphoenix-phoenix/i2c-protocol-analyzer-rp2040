# Visual upgrade implementation

Modified application source: gui/network_3d.py only.

Added assets/hardware/{pico,rtc,bmp,oled}.png. Both Pico nodes reuse the reference Pico artwork. RTC uses the requested battery face; OLED uses the newly supplied front face with blank screen. Original user files remain untouched.

Cached decoding and texture arrays are shared across network widgets. Photo planes retain camera rotation/zoom, and QPainter fallback preserves image proportions. Grid backgrounds and fabricated hardware geometry were removed for known devices. Unknown discoveries retain a neutral placeholder. Qt overlay labels track projected board bounds, with shorter compact labels. Selection and transaction highlights surround the boards. Fixed fallback final-frame repaint so fading highlights clear.

Existing decoded-event pulse mapping is preserved (read/write/NACK colours and bounded lifetime). This change does not introduce new ACK/START/STOP animations or change metrics, decoder, capture, health, registry, exports or DEMO logic.

Standard launch with a working Python environment: python main.py --demo.

This computer's supplied .venv points to a missing Python on another user's machine. Launch_Visual_Demo.cmd uses the installed Codex Python 3.12 runtime, existing .venv packages, and a local compatible PyQt6 SIP bridge in tools/visual_runtime. This convenience launcher is machine-specific; application code has no new runtime dependency. Do not move it to another PC without installing requirements.txt in a working environment.

Image generation prompt: remove background, hands/fingers, tabletop and long external wires; preserve photographed board face, components, proportions and labels; transparent PNG, clean antialiased edges, minimal margin; no invented screen content. Built-in imagegen mode was used. AI-assisted extraction can reconstruct fine details; these assets are visual aids, not evidence of exact chip markings.
