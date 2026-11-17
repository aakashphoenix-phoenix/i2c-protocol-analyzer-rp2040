# RP2040 Pico SDK project. Built with the official Pico extension or PICO_SDK_PATH.
cmake_minimum_required(VERSION 3.13)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
set(PICO_BOARD pico CACHE STRING "Original Raspberry Pi Pico (RP2040)")

# VS Code Pico projects already contain pico_sdk_import.cmake in their root.
# When compiling this supplied directory standalone, set PICO_SDK_PATH.
if(EXISTS "${CMAKE_CURRENT_LIST_DIR}/pico_sdk_import.cmake")
    include("${CMAKE_CURRENT_LIST_DIR}/pico_sdk_import.cmake")
else()
    if(NOT DEFINED PICO_SDK_PATH AND DEFINED ENV{PICO_SDK_PATH})
        set(PICO_SDK_PATH "$ENV{PICO_SDK_PATH}")
    endif()
    if(NOT DEFINED PICO_SDK_PATH)
        message(FATAL_ERROR "Open an existing Pico extension C project or set PICO_SDK_PATH to the Pico SDK location")
    endif()
    include("${PICO_SDK_PATH}/external/pico_sdk_import.cmake")
endif()

project(packet_analyzer C CXX ASM)
pico_sdk_init()
add_executable(packet_analyzer packet_analyzer.c)
pico_generate_pio_header(packet_analyzer ${CMAKE_CURRENT_LIST_DIR}/capture.pio)
target_link_libraries(packet_analyzer pico_stdlib hardware_pio hardware_dma hardware_clocks pico_multicore)
# USB CDC TX FIFO. The SDK default is ONE 64-byte packet, so the writer refilled after every packet and the link topped out
# near 78 KB/s, about what a busy two-master bench needs (the edge stream dropped batches about once a second).
# 256 bytes lifted that with zero lost bytes in 18000+ validated lines; 512 and 4096 were FASTER but occasionally
# lost ~64-byte chunks mid-line on this SDK/host combination, so do not raise it without re-running a line validation.
target_compile_definitions(packet_analyzer PRIVATE CFG_TUD_CDC_TX_BUFSIZE=256)
# Run the whole program from SRAM: Core 1 decodes up to ~13000 edges per 16.4 ms block at 400 kHz, and decoding
# from flash (XIP cache misses, shared with Core 0 printing) was too slow, so blocks were lost.
pico_set_binary_type(packet_analyzer copy_to_ram)
pico_enable_stdio_usb(packet_analyzer 1)
pico_enable_stdio_uart(packet_analyzer 0)
pico_add_extra_outputs(packet_analyzer)
