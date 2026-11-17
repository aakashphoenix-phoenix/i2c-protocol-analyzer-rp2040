
# == DO NOT EDIT THE FOLLOWING LINES for the Raspberry Pi Pico VS Code Extension to work ==
if(WIN32)
    set(USERHOME $ENV{USERPROFILE})
else()
    set(USERHOME $ENV{HOME})
endif()
set(sdkVersion 2.3.1)
set(toolchainVersion 15_2_Rel1)
set(picotoolVersion 2.3.1)
set(picoVscode ${USERHOME}/.pico-sdk/cmake/pico-vscode.cmake)
if (EXISTS ${picoVscode})
    include(${picoVscode})
endif()
# ====================================================================================

cmake_minimum_required(VERSION 3.13)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Original Raspberry Pi Pico RP2040
set(PICO_BOARD pico CACHE STRING "Board type")

# Import Pico SDK
include(pico_sdk_import.cmake)

project(packet_analyzer C CXX ASM)

pico_sdk_init()

# Main analyzer source code
add_executable(packet_analyzer
    packet_analyzer.c
)

# Program information
pico_set_program_name(packet_analyzer "packet_analyzer")
pico_set_program_version(packet_analyzer "1.0")

# Generate capture.pio.h automatically
pico_generate_pio_header(
    packet_analyzer
    ${CMAKE_CURRENT_LIST_DIR}/capture.pio
)

# Enable USB serial
pico_enable_stdio_usb(packet_analyzer 1)
pico_enable_stdio_uart(packet_analyzer 0)

# Required Pico SDK libraries
target_link_libraries(packet_analyzer
    pico_stdlib
    hardware_pio
    hardware_dma
    hardware_timer
    hardware_clocks
    pico_multicore
)

# Include current project directory
target_include_directories(packet_analyzer PRIVATE
    ${CMAKE_CURRENT_LIST_DIR}
)

# Generate .uf2 firmware
pico_add_extra_outputs(packet_analyzer)
