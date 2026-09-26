# RPi Monitor 🍓📊

Dashboard web ligero (Flask + HTML puro, ~40 MB de RAM) para monitorear una Raspberry Pi
y los contenedores Docker que corren en ella.

## Qué muestra

- **Webs desplegadas**: contenedores activos que publican un puerto TCP (o con la etiqueta `monitor.web=true`).
- **Contenedores** activos / detenidos / con healthcheck fallando.
- **Por contenedor**: CPU %, RAM usada, **% de la RAM total de la Pi**, red, tiempo activo, reinicios y puertos (con enlace directo).
- **Host**: CPU total y por núcleo, carga, RAM, RAM consumida por contenedores, swap, disco, temperatura, bajo voltaje / throttling, red y uptime.
- **Gráficos** de los últimos 30 min: CPU y RAM, temperatura y tráfico de red.

## Instalación en la Raspberry

```bash
# 1. Docker (si aún no lo tienes)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # cierra sesión y vuelve a entrar

# 2. Copia la carpeta rpi-monitor a la Pi y entra en ella
cd rpi-monitor

# 3. Construir y levantar
docker compose up -d --build

# 4. Abrir en el navegador
http://<IP-de-tu-raspberry>:8088
```

La primera construcción tarda unos minutos en una Pi. Funciona en Raspberry Pi OS de 64 bits (arm64) y 32 bits (armv7).

### Si no ves la RAM de los contenedores (muestra 0 B)

En algunas versiones de Raspberry Pi OS el cgroup de memoria viene desactivado. Actívalo añadiendo al final
de la única línea de `/boot/firmware/cmdline.txt` (o `/boot/cmdline.txt` en versiones antiguas):

```
cgroup_enable=memory cgroup_memory=1
```

y reinicia con `sudo reboot`. Puedes comprobarlo con `docker stats`.

## Controlar qué cuenta como "web"

Por defecto, cualquier contenedor con un puerto TCP publicado cuenta como web. Puedes ajustarlo con etiquetas
en el `docker-compose.yml` de tus otros proyectos:

```yaml
services:
  mi-web:
    image: nginx
    ports: ["8080:80"]
    labels:
      - monitor.web=true
      - monitor.url=https://miweb.midominio.com   # enlace que aparece en el dashboard

  mi-base-de-datos:
    image: postgres
    ports: ["5432:5432"]
    labels:
      - monitor.web=false     # tiene puerto, pero no es una web
```

## Configuración

Variables en `docker-compose.yml`:

| Variable   | Por defecto | Qué hace                                      |
|------------|-------------|-----------------------------------------------|
| `INTERVAL` | `5`         | Segundos entre mediciones                     |
| `HISTORY`  | `360`       | Puntos guardados para los gráficos (30 min)   |
| `TZ`       | `America/Lima` | Zona horaria                               |

Cambia el puerto publicado (`"8088:8088"`) si ya lo estás usando.

## API

- `GET /api/metrics` → JSON con host, contenedores y resumen.
- `GET /api/history` → serie temporal de CPU, RAM, temperatura y red.
- `GET /healthz` → `ok` si el recolector está funcionando.

## Seguridad

El contenedor monta el socket de Docker en **solo lectura**, pero aun así el socket da mucho poder sobre la máquina.
No expongas el puerto 8088 directamente a internet: úsalo en tu red local, detrás de una VPN (Tailscale/WireGuard)
o detrás de un proxy inverso con autenticación.

## Comandos útiles

```bash
docker compose logs -f          # ver logs
docker compose up -d --build    # actualizar tras cambiar el código
docker compose down             # detener
```
