@echo off
title Goofish Automation - Stop
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launcher.ps1" -action stop
