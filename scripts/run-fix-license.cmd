@echo off
chcp 65001 >nul
title 修复 SolidWorks 许可服务
echo 正在请求管理员权限...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File','\"%~dp0fix-license-service.ps1\"'"
