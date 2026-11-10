@echo off
rem Start all five apps on the standard stack: PostgreSQL + Redis (run by Docker Desktop).
title Xiaoer - E-commerce AI Customer Service (PostgreSQL + Redis)
call "%~dp0xiaoer.bat" start --docker %*
if errorlevel 1 pause
