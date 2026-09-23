' LocalFlow — inicia em segundo plano (sem janela).
' Um ícone aparece perto do relógio; clique com o botão direito > Sair para fechar.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

' garante que o Ollama esteja rodando (janela oculta)
sh.Run "cmd /c tasklist /FI ""IMAGENAME eq ollama.exe"" | find /I ""ollama.exe"" >nul || start """" /B ""%LOCALAPPDATA%\Programs\Ollama\ollama.exe"" serve", 0, False

' inicia o LocalFlow sem console (pythonw)
sh.Run """" & dir & "\.venv\Scripts\pythonw.exe"" """ & dir & "\app.py""", 0, False
