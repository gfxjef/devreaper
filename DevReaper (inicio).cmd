@echo off
rem For the Windows Startup folder: starts DevReaper in the tray without opening the panel.
rem Uses pythonw from PATH, or the py launcher (pyw) when pythonw is not on PATH.
cd /d "%~dp0"
where pythonw >nul 2>&1 && (start "" pythonw -m devreaper --startup & exit /b)
where pyw >nul 2>&1 && (start "" pyw -m devreaper --startup & exit /b)
echo No se encontro Python. Instala Python 3.11 o superior desde https://www.python.org y vuelve a intentarlo.
pause
