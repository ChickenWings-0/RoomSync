Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "cmd.exe /c C:\ROOMLIGHTS-APP\RoomSync\.venv\Scripts\python.exe main.py > NUL 2>&1", 0, False
