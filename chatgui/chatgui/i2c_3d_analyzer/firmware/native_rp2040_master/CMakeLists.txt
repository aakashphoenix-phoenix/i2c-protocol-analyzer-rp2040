# Generated Cmake Pico project file

cmake_minimum_required(VERSION 3.13)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Initialise pico_sdk from installed location
# (note this can come from environment, CMake cache etc)

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
# Multi-master bench (2026-11-14):
#   Master 1 = Raspberry Pi Pico H  -> configure with -DPICO_BOARD=pico   -> flash I2C.uf2
#   Master 2 = Raspberry Pi Pico W  -> configure with -DPICO_BOARD=pico_w -> flash I2C_master2.uf2
# Both targets are built in every build tree; PICO_BOARD only selects board defaults
# (neither firmware uses the LED or the Pico W radio, so either tree runs on either board).
set(PICO_BOARD pico CACHE STRING "Board type")

# Pull in Raspberry Pi Pico SDK (must be before project)
include(pico_sdk_import.cmake)

project(I2C C CXX ASM)

# Initialise the Raspberry Pi Pico SDK
pico_sdk_init()

function(add_i2c_master target master_id)
    add_executable(${target} I2C.c)
    target_compile_definitions(${target} PRIVATE MASTER_ID=${master_id})
    if(MM_STRESS)    # bench-only arbitration stress build (see I2C.c); never flash for normal use
        target_compile_definitions(${target} PRIVATE MM_STRESS=1)
    endif()
    pico_set_program_name(${target} "${target}")
    pico_set_program_description(${target} "I2C multi-master controller, MASTER_ID=${master_id}")
    pico_set_program_version(${target} "2.0")

    # USB CDC: each master has its own COM port for telemetry and commands
    pico_enable_stdio_uart(${target} 0)
    pico_enable_stdio_usb(${target} 1)

    target_link_libraries(${target}
            pico_stdlib
            hardware_i2c
            pico_rand)
    target_include_directories(${target} PRIVATE
            ${CMAKE_CURRENT_LIST_DIR}
    )
    pico_add_extra_outputs(${target})
endfunction()

# Master 1 keeps the original target name so the VS Code extension's Run/Flash still works.
add_i2c_master(I2C 1)
add_i2c_master(I2C_master2 2)
