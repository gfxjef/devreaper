# Nota para TI: DevReaper y Bitdefender Endpoint Security

Plantilla para enviar a tu área de TI si tu equipo usa Bitdefender (u otro EDR). Reemplaza `<ruta-del-proyecto>`
por la carpeta donde clonaste DevReaper.

## Qué es DevReaper

Herramienta de código abierto que libera RAM y puertos en un equipo de desarrollo. Lista los servidores de
desarrollo (Node, Python, Playwright, contenedores Docker) y los cierra cuando el usuario lo pide o cuando
llevan más de 48 horas abiertos.

- Se ejecuta con `pythonw.exe -m devreaper` desde `<ruta-del-proyecto>`.
- Abre un panel web **solo en `127.0.0.1:7777`** (no escucha en la red).
- Solo cierra procesos del usuario actual. Nunca toca procesos de SYSTEM, servicios ni otras cuentas.
- Llama a `git.exe`, `docker.exe` y `wsl.exe` (solo lectura, más `docker stop` y `wsl --shutdown` cuando el
  usuario lo pide).

## Arranque con Windows

Una versión inicial creaba un acceso directo en la carpeta Inicio de Windows con PowerShell
(`WScript.Shell.CreateShortcut`). Bitdefender lo detectó, con razón, como técnica de persistencia y bloqueó
`powershell.exe` durante varios minutos. **Esa función se eliminó.** El arranque con Windows ahora lo
configura el usuario a mano desde el Explorador.

## Qué pedir

Normalmente no hace falta ninguna excepción. **Solo si Bitdefender bloquea la herramienta**, pide una
exclusión acotada a la carpeta del proyecto (`<ruta-del-proyecto>`). No conviene excluir `pythonw.exe` en
general: cubriría cualquier script de Python del equipo.
