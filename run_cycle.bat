@echo off
cd /d "C:\ELENA"
if not exist logs mkdir logs
"C:\Users\Administrator.WIN-6U07LVV6SKO\.local\bin\uv.exe" run elena-fuel run-cycle --log-dir logs >> logs\task_scheduler.out 2>&1
exit /b %ERRORLEVEL%
