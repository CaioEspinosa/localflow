@echo off
rem Remove o inicio automatico do LocalFlow.
powershell -NoProfile -Command "Remove-Item ([Environment]::GetFolderPath('Startup')+'\LocalFlow.lnk') -ErrorAction SilentlyContinue"
echo O LocalFlow nao inicia mais com o Windows.
pause
