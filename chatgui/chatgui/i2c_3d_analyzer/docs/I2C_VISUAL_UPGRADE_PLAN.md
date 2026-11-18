# Visual upgrade plan

Target: F:\Raspberry_PI_Project\softwareproject12\chatgui\chatgui\i2c_3d_analyzer (user-selected copy).

Entry: main.py; PyQt6 GUI, pyqtgraph OpenGL renderer with QPainter fallback in gui/network_3d.py. Both gui/dashboard.py and gui/network_page.py instantiate Network3D; the Device Activity page also shares it.

Scope: replace generated board shapes with transparent photo-derived artwork; preserve shared SDA/SCL topology, existing event-driven pulses, selection, camera, and measurement logic. Use cached QImage and numpy RGBA textures, GLImageItem planes for 2.5D, and aspect-preserving QPainter images for fallback. No new application rendering dependency.

Final references: Pico image 1, RTC image 4 (battery face as explicitly requested), purple board image 6, and newly attached OLED front-screen image. Built-in imagegen used for background extraction. These are AI-assisted photographic derivatives, not exact source-pixel masks or full CAD models. Small text/detail fidelity is not guaranteed.

Firmware confirms configured GP4/GP5 master, GP2/GP3 passive tap. Physical wiring and sensor identity remain unverified. PDF documents are scanned; page counts verified but electrical facts were not extracted from them. Electrical configuration is not changed.

Verify both views in DEMO, alpha assets, labels, hit testing, camera API, advancing/expiring pulses, and existing regression suite. Back up renderer before applying. Keep application measurements and protocol code unchanged.
