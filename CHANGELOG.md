# Version history

This repository is being prepared to retain the project’s **actual development history**, not only its latest state.

## Unreleased — Repository preparation

- Added public-facing overview, hardware wiring notes, architecture, setup guidance, and measurement/validation principles.
- Recorded the distinction between the original project and the later dual-master, 3D GUI implementation.
- Historical source archive still **awaiting inspection**: the supplied \`software project.zip\` has not been safely unpacked in this session.
- **No version tags or source release claims yet.**

## Planned: v1.0 — Original project

Will contain the original project files *as they actually existed*, before later enhancements.

To complete this release:
1. Review archive paths and remove generated binaries, virtual environments, private files, and unneeded artifacts.
2. Commit the original source with a file inventory and instructions that match that source.
3. Validate build/run instructions when practical.
4. Tag that specific source commit \`v1.0\`.

We will not invent missing original files or claim later features existed in Version 1.

## Planned: v2.0 — Extended system

Based on later project development:
- Pico 1 I²C master with DS3231, BMP280 and OLED.
- Pico W as a second master for RTC and BMP280 read experiments.
- Separate RP2040 PIO/DMA passive analyzer with USB transport.
- Python desktop GUI showing decoded I²C activity and performance metrics.
- Existing known limitations: reported record loss, address-NACK validation, lack of raw waveform export, and multi-master arbitration attribution.

The \`v2.0\` tag will be added after the matching firmware and GUI sources are committed and checked.

## Release discipline

A version tag must reference a commit containing the source files for that version. Documentation, screenshots, or planned designs alone should not be tagged as a complete firmware release.
