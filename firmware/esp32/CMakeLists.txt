# DeskPet ESP32 设备固件（ESP-IDF 工程）。
#
# 用法见 README.md，最短路径：
#   . D:\Espressif\frameworks\esp-idf-v5.4.4\export.ps1
#   idf.py set-target esp32
#   idf.py build
#
# 本工程不负责烧写。刷机请自己确认端口和板子型号后再做。
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(deskpet_esp32)
