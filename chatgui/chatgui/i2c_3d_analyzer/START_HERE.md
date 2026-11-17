# START HERE — Raspberry Pi Pico I²C 3D Analyzer (Integrated Bundle)

**Your uploaded GUI, with a compatible analyzer protocol adapter and RP2040 analyzer C firmware sources.**

1. First read [CONNECT_PICOS_WINDOWS.md](CONNECT_PICOS_WINDOWS.md) including the safety check.
2. Flash **Pico 2** with the C files in `firmware/native_rp2040_analyzer/` (compile using your existing Pico SDK VS Code project).
3. Keep **Pico 1** running your working RTC+BMP280+OLED master firmware. Optional JSON-telemetry master source is in `firmware/native_rp2040_master/`.
4. Run `python main.py` after installing `requirements.txt` inside your Windows `.venv`.
5. Stop VS Code Serial Monitor on analyzer COM port. Click GUI **Connect → Start capture**.
6. Optional: connect Pico 1 as **Master telemetry** on its own COM port for live sensor values.

**Current limitation:** Real packet table and 3D activity are supported; raw SDA/SCL waveform streaming is not supplied by this analyzer firmware. No claims of lossless capture or fully validated hardware timing.
