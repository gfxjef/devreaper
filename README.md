# DevReaper

Ícono de bandeja + panel local (http://127.0.0.1:7777) que lista los procesos de desarrollo
(`next dev`, Playwright MCP, uvicorn, python, node…), cuánto llevan abiertos, en qué puerto escuchan,
a qué sesión de Claude Code pertenecen y si esa sesión sigue activa. Permite cerrarlos con dos clics
y, cada 24 h, cierra solo todo lo que lleve más de 2 días abierto, salvo los puertos protegidos.

## Instalación

Requiere Windows 10/11 y Python 3.11 o superior ([python.org](https://www.python.org)).

```
git clone https://github.com/gfxjef/devreaper.git
cd devreaper
python -m pip install -r requirements.txt
```

Si `python` no se encuentra, usa `py` en su lugar (`py -m pip install -r requirements.txt`).

La carpeta de proyectos que se revisa para la vista Worktrees es `%USERPROFILE%\Desktop\works` por defecto;
cámbiala en Ajustes.

## Uso

- Abrir: doble clic en `DevReaper.cmd` (o `pythonw -m devreaper`). Si ya está abierta, solo abre el panel.
- Ícono de bandeja: verde = todo bien, ámbar = hay huérfanos o procesos por vencer, rojo = RAM ≥ 90 %
  o más de 1 GB en huérfanos. Clic = abrir panel. Menú: cerrar huérfanos, ejecutar ciclo, salir.
- Panel: botón de tema (automático, claro, oscuro) en la cabecera; `/` para filtrar, clic en un bloque de la barra para ir a su fila, candado en un puerto para
  protegerlo. "Cerrar" pide un segundo clic en 3 s.

## Vistas

- **Procesos**: cada grupo de procesos de desarrollo con RAM, edad, puertos y sesión.
- **Sesiones de Claude**: cada sesión con los procesos que lanzó y su RAM total (Claude + procesos).
  "Cerrar sesión" cierra claude.exe y lo que lanzó; los procesos con puertos protegidos siguen vivos.
- **Puertos**: cada puerto en uso, quién lo tiene, si está abierto a la red, quién lo usó en los últimos
  7 días (y si lo usaron proyectos distintos). "Liberar" en un puerto de Docker detiene solo ese contenedor;
  nunca mata Docker Desktop ni WSL. "Ver todos" añade los puertos de otros programas.
- **Worktrees**: los worktrees de los repos bajo las carpetas configuradas, su rama ("Ya está en main",
  "Rama remota borrada"…), lo que corre en cada uno y "Cerrar todo" (procesos y contenedores, no sesiones).
  Candidato a cerrar = rama ya integrada y todavía consume RAM.
- **Docker y WSL**: RAM de la VM de WSL y de Docker Desktop, contenedores con puertos y RAM, "Detener" y
  "Apagar WSL" (bloqueado si algún contenedor usa un puerto protegido).
- **RAM 7 días**: una muestra por minuto (desarrollo, Claude, Docker/WSL, resto) con lo más pesado de cada
  momento; vista de tabla incluida. Aviso en la bandeja si la RAM pasa del umbral (90 % por defecto).
- **Ver qué cerraría**: vista previa del próximo ciclo automático, sin cerrar nada.
- **Historial de cierres**: qué se cerró, por qué, desde dónde (panel, bandeja o ciclo automático) y la
  línea de comando; total liberado en 7 días.

## Iniciar con Windows (manual)

DevReaper no escribe en la carpeta de Inicio: Bitdefender Endpoint Security lo trata como persistencia de
malware y bloquea PowerShell. Hazlo a mano: `Win + R` → `shell:startup`, y arrastra `DevReaper (inicio).cmd`
con el botón derecho → "Crear acceso directo aquí". El panel (Ajustes) detecta si ya está configurado.

## Seguridad

- Servidor solo en `127.0.0.1`; acciones con token por arranque, Host y Origin verificados.
- CSP estricta y `frame-ancestors 'none'`: el panel no se puede incrustar en otra página.
- Solo lista y cierra procesos de tu usuario; nunca de SYSTEM, servicios u otras cuentas.
- Los puertos protegidos sobreviven a todo: ciclo, cierres masivos, cerrar sesión, cerrar worktree, apagar WSL.
- Nota para TI sobre Bitdefender: `docs/bitdefender-excepcion.md`.

## Estados

| Estado | Significado |
|---|---|
| Activa | La sesión de Claude que lo lanzó existe y tuvo actividad en las últimas 2 h |
| Inactiva | La sesión existe pero lleva ≥ 2 h sin actividad |
| Huérfano | Su sesión de Claude ya se cerró |
| Sin sesión | Nunca tuvo sesión conocida (lanzado a mano o antes de instalar DevReaper) |

## Datos

`%LOCALAPPDATA%\DevReaper\devreaper.db` (SQLite: ajustes, puertos protegidos, linaje, historial de cierres,
historial de puertos 30 días, muestras de RAM 7 días)
y `devreaper.log`. La primera vez, el ciclo automático espera 24 h para dar tiempo a proteger puertos.

## Pruebas

```
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Dependencias con versión fija en `requirements.txt` (psutil, pystray, Pillow).

## Licencia

MIT. Ver `LICENSE`.
