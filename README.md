# DevReaper

Ícono de bandeja + panel local (http://127.0.0.1:7777) para quien programa con agentes (Claude Code, Codex,
Gemini CLI, Cursor, VS Code, Windsurf, JetBrains…). Lista los procesos de desarrollo que esos agentes dejan
corriendo (`next dev`, Playwright MCP, uvicorn, Java, Go, .NET, Ruby…), cuánta RAM y CPU usan, en qué puerto
escuchan, qué agente los lanzó y si ese agente sigue en uso. Permite cerrarlos con dos clics y, cada 24 h,
cierra solo lo que lleve más de 2 días abierto **y nadie esté usando**.

## Instalación

Requiere Windows 10/11, Git y Python 3.11 o superior ([python.org](https://www.python.org)).
No hay nada que configurar: los proyectos se descubren solos (ver "Proyectos").

### Con tu agente (Claude Code, Codex, Cursor, Gemini CLI…)

Copia esto y pégalo en tu agente:

```text
Instala DevReaper en esta PC con Windows desde https://github.com/gfxjef/devreaper.
1. Clona el repo en %USERPROFILE%\DevReaper (si ya existe, actualízalo con git pull).
2. Verifica que haya Python 3.11 o superior con `py -3 --version` (o `python --version`). Si no hay, detente y dime que lo instale desde python.org.
3. En la carpeta del repo instala las dependencias: `py -m pip install -r requirements.txt` (usa `python` si `py` no existe).
4. Comprueba que funciona: `py -m pip install -r requirements-dev.txt` y luego `py -m pytest -q`. Si alguna prueba falla, muéstrame el error y no sigas.
5. Arráncalo ejecutando `DevReaper.cmd` desde la carpeta del repo y confirma que http://127.0.0.1:7777/api/ping responde {"app": "devreaper"}.
6. Al final dime dónde quedó instalado y cómo hacer que inicie con Windows (sección "Iniciar con Windows" del README). No modifiques la carpeta de Inicio tú mismo.
```

### A mano (PowerShell)

```powershell
git clone https://github.com/gfxjef/devreaper.git "$env:USERPROFILE\DevReaper"
cd "$env:USERPROFILE\DevReaper"
py -m pip install -r requirements.txt
.\DevReaper.cmd
```

Si `py` no se encuentra, usa `python` en su lugar. Para actualizar más adelante: `git pull` en esa carpeta,
cierra DevReaper desde el menú de la bandeja (Salir) y vuelve a abrir `DevReaper.cmd`.

## Uso

- Abrir: doble clic en `DevReaper.cmd` (o `pythonw -m devreaper`). El panel se abre en su propia ventana
  (Edge en modo app, sin pestañas ni barra de direcciones); sin Edge, en el navegador. Si ya está abierta,
  solo abre el panel.
- **Ícono de bandeja**: un anillo que se llena con la RAM usada; verde = todo bien, ámbar = hay huérfanos o
  procesos por vencer, rojo = RAM ≥ 90 % o más de 1 GB en huérfanos. Clic = abrir el panel.
- **Menú de la bandeja** (clic derecho), sin abrir el panel: RAM y CPU del momento; cada proceso con su RAM,
  puertos, agente, último uso y acciones (abrir en el navegador, cerrar, no cerrarlo en 24 h, verlo en el
  panel); cada agente con lo que lanzó (cerrar sus procesos, cerrar el agente); puertos (abrir, liberar);
  cerrar huérfanos, cerrar agentes sin uso en más de 24 h, ejecutar el ciclo y activar o pausar el cierre
  automático. Se reconstruye cada vez que lo abres.
- **Panel**: ocupa toda la ventana. Arriba, la RAM de los últimos 15 minutos en vivo (desarrollo, agentes,
  Docker/WSL, resto). Clic en una fila abre su detalle a la derecha, con RAM y CPU en vivo, por qué el ciclo lo
  cierra o no, carpeta, puertos y comando. "Cerrar" pide un segundo clic en 3 s.

### Atajos

`/` filtrar · `j`/`k` bajar y subir · `Espacio` seleccionar · `x` cerrar (dos veces) · `Esc` volver ·
`1`–`8` cambiar de vista.

## Vistas

- **Procesos**: cada grupo de procesos con estado, RAM de los últimos 15 minutos (mini gráfica), CPU, último
  uso, puertos y qué hará el cierre automático con él. Filtros por estado (activos, inactivos, huérfanos, sin
  agente), "Agrupar por agente", selección múltiple con "Cerrar seleccionados" y "No cerrar automáticamente
  en 24 h". La RAM de un grupo incluye a sus hijos que no son de desarrollo (esbuild, el binario de `go run`…).
- **Agentes**: cada agente abierto, su actividad, su RAM y la de lo que lanzó. "Cerrar sus procesos" deja
  el agente abierto; "Cerrar agente" (solo agentes de terminal) cierra el agente y sus procesos. Un editor
  (Cursor, VS Code) nunca se cierra desde aquí. "Cerrar agentes sin uso en más de 24 h" en bloque.
- **Puertos**: cada puerto en uso, quién lo tiene, si está abierto a la red, quién lo usó en los últimos
  7 días (y si lo usaron proyectos distintos). "Liberar" en un puerto de Docker detiene solo ese contenedor;
  nunca mata Docker Desktop ni WSL. "Ver todos" añade los puertos de otros programas.
- **Proyectos**: cada repo y sus worktrees, su rama ("Ya está en main", "Rama remota borrada"…), lo que corre
  en cada uno, "Abrir carpeta" y "Cerrar todo" (procesos y contenedores, no agentes). Candidato a cerrar =
  rama ya integrada que todavía consume RAM.
- **Docker y WSL**: RAM de la VM de WSL y de Docker Desktop, contenedores con puertos y RAM, "Detener" y
  "Apagar WSL" (bloqueado si algún contenedor usa un puerto protegido).
- **RAM 7 días**: una muestra por minuto con lo más pesado de cada momento; vista de tabla incluida. Aviso
  en la bandeja si la RAM pasa del umbral (90 % por defecto).
- **Historial**: qué se cerró, por qué, desde dónde (panel, bandeja o ciclo automático) y la línea de
  comando; total liberado en 7 días.
- La tarjeta **próximo ciclo** de la cabecera muestra, sin cerrar nada, lo que cerraría el próximo ciclo.

## Agentes y actividad

Un proceso pertenece al primer agente que aparece entre sus padres: eso no depende del formato de archivos
de ningún agente. Para saber si un agente está **en uso**:

1. Lo que el agente escribió: el archivo de sesión de Claude Code y la hora del último mensaje real de su
   transcripción (las líneas de mantenimiento no cuentan), o las sesiones de Codex.
2. Si no deja nada legible (editores), su uso de CPU, promediado por minuto.

Si un formato cambia, esa fuente deja de aportar y queda la siguiente. Hasta que DevReaper haya observado
un agente durante 2 h sin evidencia, lo considera activo: ante la duda, no cierra.

## Cierre automático

Cada 24 h cierra lo que lleve más de 48 h abierto, **salvo** si: tiene un puerto protegido, lo pospusiste,
su agente sigue activo, o usó CPU en las últimas 2 h. Cada fila dice cuál de esas razones aplica.

## Proyectos

Se descubren solos: las carpetas donde trabajan los agentes ahora, las que recuerdan (`~/.claude.json`,
`~/.gemini/projects.json`, sesiones de Codex) y las abiertas en editores de la familia VS Code en los
últimos 30 días. En Ajustes puedes agregar carpetas extra si falta alguno.

## Iniciar con Windows (manual)

DevReaper no escribe en la carpeta de Inicio: Bitdefender Endpoint Security lo trata como persistencia de
malware y bloquea PowerShell. Hazlo a mano: `Win + R` → `shell:startup`, y arrastra `DevReaper (inicio).cmd`
con el botón derecho → "Crear acceso directo aquí". El panel (Ajustes) detecta si ya está configurado.

## Seguridad

- Servidor solo en `127.0.0.1`; acciones con token por arranque, Host y Origin verificados.
- CSP estricta y `frame-ancestors 'none'`: el panel no se puede incrustar en otra página.
- Solo lista y cierra procesos de tu usuario; nunca de SYSTEM, servicios u otras cuentas.
- Los puertos protegidos sobreviven a todo: ciclo, cierres masivos, cerrar agente, cerrar proyecto, apagar WSL.
- Nota para TI sobre Bitdefender: `docs/bitdefender-excepcion.md`.

## Estados

| Estado | Significado |
|---|---|
| Activa | El agente que lo lanzó está abierto y se usó en las últimas 2 h |
| Inactiva | El agente está abierto pero lleva ≥ 2 h sin uso |
| Huérfano | Su agente ya se cerró |
| Sin agente | Nunca tuvo agente conocido (lanzado a mano o antes de instalar DevReaper) |

## Datos

`%LOCALAPPDATA%\DevReaper\devreaper.db` (SQLite: ajustes, puertos protegidos, linaje, historial de cierres,
cierres pospuestos, proyectos vistos, historial de puertos 30 días, muestras de RAM 7 días)
y `devreaper.log`. La primera vez, el ciclo automático espera 24 h para dar tiempo a proteger puertos.

## Pruebas

```
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Dependencias con versión fija en `requirements.txt` (psutil, pystray, Pillow).

## Licencia

MIT. Ver `LICENSE`.
