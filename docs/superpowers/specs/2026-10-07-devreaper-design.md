# DevReaper — diseño

Fecha: 2026-10-07 · Autor: gfxjef (decisiones delegadas a Claude: "realiza todo bajo tu recomendación")

## Problema

Los servidores de desarrollo (`next dev`, Playwright MCP, Python) lanzados desde sesiones de Claude Code
quedan vivos durante días. El 2026-10-07 la RAM llegó al 98 % (13.4 GB en 65 procesos node).

## Objetivo / criterio de éxito

- Ver en < 1 s qué procesos de desarrollo pesan, cuánto llevan abiertos, en qué puerto escuchan
  y si su sesión de Claude existe y está activa.
- Cerrar cualquiera (árbol completo) con un clic.
- Ciclo automático cada 24 h que cierra todo lo que lleve más de 2 días, salvo puertos excluidos.
- La app en sí debe consumir muy poca RAM (< 60 MB).

## Decisiones (del usuario)

| Tema | Decisión |
|---|---|
| Formato | App de bandeja + panel web local, UX extremadamente rápida |
| Auto-cierre | Todo lo que pase 2 días, con o sin sesión viva. Solo se salvan los puertos excluidos |
| Alcance | Solo procesos de desarrollo (lista editable) |
| Ubicación | `Desktop\works\Python Proyects\devreaper` |
| Arranque | Con Windows (acceso en carpeta Inicio), desactivable desde el panel |

## Arquitectura

Python 3.11 + `psutil` + `pystray` + `Pillow`, servidor `http.server` de la stdlib, SQLite de la stdlib,
panel en HTML/CSS/JS sin framework ni build.

| Módulo | Responsabilidad |
|---|---|
| `db.py` | SQLite (`%LOCALAPPDATA%\DevReaper\devreaper.db`): settings, puertos excluidos, linaje, log de cierres |
| `sessions.py` | Lee `~/.claude/sessions/<pid>.json`; valida PID vivo + `procStart`; estado activa/inactiva |
| `scanner.py` | Lee procesos, filtra los de desarrollo, agrupa en árboles, puertos LISTEN, etiqueta proyecto, asocia sesión |
| `reaper.py` | Mata un árbol de procesos; política de auto-cierre (función pura) |
| `server.py` | HTTP en `127.0.0.1:7777`: panel estático + `/api/state` + acciones POST |
| `app.py` | Orquesta: hilo de escaneo cada 3 s, hilo del ciclo de cierre, ícono de bandeja |
| `startup.py` | Crea/borra el acceso directo en la carpeta Inicio |

### Procesos de desarrollo

Se vigilan si el ejecutable es `node`, `python`, `pythonw`, `uvicorn`, `bun`, `deno` (lista editable) o
si es `chrome.exe` lanzado desde `ms-playwright`. Se excluyen siempre: la propia app y sus hijos,
`claude.exe`, y ejecutables bajo `Program Files` (p. ej. el node de Adobe Creative Cloud).

### Agrupación

Cada proceso de desarrollo cuyo padre no es un proceso de desarrollo es la **raíz** de un grupo; sus
descendientes de desarrollo pertenecen al grupo. El panel muestra grupos (p. ej. `npx → next → next-server`
= una fila). RAM del grupo = suma; edad = edad de la raíz; puertos = unión.

### Sesión de Claude

1. Subir por los ancestros de la raíz; si aparece un `claude.exe` con archivo de sesión → vinculado.
2. Cada vínculo se guarda en `lineage` (pid + create_time → session_id, nombre, cwd).
3. Si el padre ya murió, se consulta `lineage`: si había sesión y ya no está viva → **huérfano**.

Estados: 🟢 activa (sesión viva, `busy` o actividad < 2 h) · 🟡 inactiva (viva, sin actividad ≥ 2 h)
· 🔴 huérfano (su sesión murió) · ⚪ sin sesión (nunca tuvo, o se desconoce).

El formato de `~/.claude/sessions` es interno de Claude Code; si cambia, todo cae a "sin sesión"
sin romper la app.

### Visibilidad

Por defecto se muestran grupos con RAM ≥ 300 MB, edad ≥ 24 h, o huérfanos. Interruptor "ver todos".

### Auto-cierre

- Intervalo 24 h, umbral 48 h (configurables). Habilitado por defecto.
- Al arrancar: si la última ejecución fue hace ≥ 24 h, corre a los 60 s.
- Cierra cada grupo con edad ≥ umbral cuyos puertos no estén excluidos. Registra en `kill_log`.

### Seguridad del servidor local

Solo `127.0.0.1`. Las acciones POST exigen el header `X-DevReaper-Token` (token aleatorio embebido en la
página) y Host `127.0.0.1:7777`/`localhost:7777`: un sitio web ajeno no puede disparar cierres.
Instancia única: si el puerto 7777 está ocupado por otra DevReaper, se abre el panel y se sale.

### Panel

Barra superior: RAM del sistema, RAM en procesos de desarrollo, nº de huérfanos, cuenta regresiva del
próximo ciclo. Tabla: estado, proyecto, puertos (clic en 🔒 para excluir), RAM, edad, sesión, botón Cerrar.
Acciones masivas: "Cerrar huérfanos", "Cerrar > 2 días", "Liberar puerto…". Ajustes: puertos excluidos,
umbrales, auto-cierre on/off, arranque con Windows. Historial de los últimos cierres.

## Pruebas

pytest: conversión FILETIME, estado de sesión, etiquetado de proyecto, agrupación, política de
auto-cierre, base de datos, y smoke test del servidor (GET estado, POST sin token = 403).

## Ampliación 2026-10-07 (tarde): foco en cerrar puertos y liberar RAM

Pedido: "haz todo esto, mi idea principal es el cierre de puertos y liberación de RAM".

| Pieza | Decisión |
|---|---|
| Puertos | Vista por puerto: dueño actual (grupo dev > contenedor > proceso), exposición a la red, historial 7 días (tabla `port_history`, 30 días), conflicto si lo usaron proyectos distintos |
| Liberar puerto | Grupo dev → matar árbol; puerto publicado por Docker → `docker stop` del contenedor; proceso de Docker/WSL (`com.docker.backend`, `wslrelay`…) → rechazar; nunca procesos ajenos |
| Docker y WSL | `infra.py`: `docker ps` + `docker stats`, VM `vmmemWSL`; acciones Detener / Apagar WSL con dos clics; Apagar WSL bloqueado si un contenedor tiene puerto protegido |
| Worktrees | `worktrees.py`: repos con `.git/worktrees` bajo `project_roots` (prof. 4) + los vistos en procesos/sesiones/compose; estado merged/gone/missing/open; candidato = rama integrada con algo cerrable corriendo |
| RAM | Muestra por minuto (`ram_samples`, 7 días), área apilada validada con dataviz, tabla; aviso en bandeja ≥ `ram_alert_percent` (90) cada 30 min como máximo |
| Origen | `kill_log.origin` (panel/bandeja/automático) y `cmd`; total liberado 7 días |
| Front | JS y CSS fuera del HTML; CSP `script-src 'self'` |
| Puerto protegido inicial | 3990 (waha), a pedido del usuario: "no lo detengas porque lo uso actualmente" |
