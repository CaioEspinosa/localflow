@echo off
rem Faz o LocalFlow iniciar automaticamente com o Windows (em segundo plano).
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\LocalFlow.lnk'); $s.TargetPath='%~dp0LocalFlow.vbs'; $s.WorkingDirectory='%~dp0'; $s.Save()"
echo.
echo Pronto! O LocalFlow vai iniciar junto com o Windows.
echo (Para desfazer, execute remover-inicio-automatico.bat)
echo.
pause
