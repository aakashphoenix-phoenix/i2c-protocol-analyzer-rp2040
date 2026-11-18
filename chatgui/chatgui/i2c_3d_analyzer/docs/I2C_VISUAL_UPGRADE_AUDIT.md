# Visual upgrade audit

PASS: staged original application launches with DEMO generator; original 65 regression tests pass (6.49 seconds final run). No source changes outside shared network renderer. All four assets have transparent backgrounds and were visually reviewed; no hands/tabletop visible. Requested RTC battery face and OLED front display are present in full and compact views.

PASS: executable smoke test exercised 5 fallback nodes, cached texture identity, node hit testing, 3D orbit/reset API, decoded DEMO activity, advancing pulse positions, and expiration of pulses/highlights. Final DEMO smoke run produced 84 transactions at first screenshot. Screenshots are from the running application, not mockups.

Fixes found in review: missing OpenGL text resolved using mouse-transparent QLabel overlays; labels moved outside projected board bounds; compact labels shortened; fallback final fade frame now repaints.

Commands executed with bundled Python 3.12 and the compatible SIP bridge:
- python -X utf8 work/run.py pytest (65 passed)
- python -X utf8 work/run.py work/smoke.py (PASS)

Initial test attempts failed due to sandbox temp access and Windows text encoding, corrected by explicit workspace --basetemp and -X utf8. No test logic was changed.

NOT TESTED: real hardware capture, physical wiring, electrical characteristics, sustained FPS/CPU benchmarks, every camera angle and every display size. Camera exercised programmatically, not a manual drag/scroll audit. Existing START/STOP-specific and new ACK/NACK animation enhancements from the broader reference brief were not implemented in this focused artwork update.

UNVERIFIED: exact purple-board chip identity. Configured/unconfirmed network names use BME280/BMP280 (Unverified); existing simulated DEMO identification may say Confirmed BMP280, which is simulation only. The cutouts are generated photographic derivatives and may reconstruct fine labels/details. Full geometric CAD accuracy is not claimed.

Launch: double-click Launch_Visual_Demo.cmd on this computer, or python main.py --demo in a compatible environment. Backup original renderer before deployment. Standard physical-hardware tests remain outstanding.

Deployment: applied 11 files to the user-selected chatgui project after verifying the original renderer SHA-256. Each installed file was byte-compared with its tested staged counterpart. Original renderer backup: backups/visual_20261107_225744/gui/network_3d.py. Local launcher bootstrap imports passed.
