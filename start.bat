@echo off
title Goofish Automation - Start
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launcher.ps1" -action start
