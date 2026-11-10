@echo off
rem Start all five apps in zero-dependency mode (SQLite + in-process event bus).
rem Extra options are passed through, for example:  start.bat --mirror
title Xiaoer - E-commerce AI Customer Service
call "%~dp0xiaoer.bat" start %*
if errorlevel 1 pause
