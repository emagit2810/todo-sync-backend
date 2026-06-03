# Deploy Render: Todo Sync Backend

## Arquitectura

Usar una sola ruta de datos:

```text
Web y PWA -> FastAPI en Render -> Neon
```

Nunca agregar `VITE_DATABASE_URL` a una interfaz. Las variables `VITE_*` se
incluyen en el bundle publico del navegador.

## 1. Rotar Neon e importar el respaldo

1. En Neon, abre el proyecto `todo_db_fuente`.
2. Abre `Connect`, restablece la contrasena del rol y copia la URL nueva.
3. Guarda la URL solamente en:

```text
C:\Users\Lenovo\OneDrive\Desktop\chatbot\todo_app1\manual-recovery\.env.neon.local
```

4. Ejecuta:

```powershell
cd C:\Users\Lenovo\OneDrive\Desktop\chatbot\todo_app1\manual-recovery
.\import_recovered_backup.ps1
```

El script carga el JSON recuperado con propietario `emmabarca123@gmail.com` y
verifica los conteos almacenados en Neon.

## 2. Crear el backend en GitHub

Crear un repositorio nuevo llamado `todo-sync-backend` y subir los archivos de
esta carpeta. No subir `.venv`, `.env` ni una URL real de Neon.

## 3. Crear el Web Service en Render

Opcion recomendada: usar el `render.yaml` incluido como Blueprint.

El Blueprint solicita `DATABASE_URL` en el panel de Render y genera
`API_BEARER_TOKEN` sin guardarlos en Git.

Configuracion equivalente para crear el Web Service manualmente:

```text
Runtime: Python
Build Command: pip install -r requirements.txt
Start Command: uvicorn main:app --host 0.0.0.0 --port $PORT
Health Check Path: /healthz
```

## 4. Configurar temporalmente web y PWA

Copiar el valor generado `API_BEARER_TOKEN` del backend y definir estas
variables en los dos frontends:

```env
VITE_SYNC_API_URL=https://fast-api-v.onrender.com
VITE_SYNC_AUTH_TOKEN=PEGA_EL_TOKEN_TEMPORAL_GENERADO
VITE_SYNC_HTTP_TIMEOUT_MS=12000
```

Volver a desplegar ambos frontends. Este token temporal se reemplazara por
Google login cuando exista `GOOGLE_CLIENT_ID`.

Para habilitar Gmail, crear un cliente OAuth web en Google Cloud, registrar los
origenes HTTPS de web y PWA, y usar el mismo `GOOGLE_CLIENT_ID` como
`GOOGLE_CLIENT_ID` del backend y `VITE_GOOGLE_CLIENT_ID` de ambos frontends.

## 5. Verificar

```powershell
Invoke-RestMethod https://fast-api-v.onrender.com/healthz

$headers = @{ Authorization = "Bearer PEGA_EL_TOKEN_TEMPORAL_GENERADO" }
Invoke-RestMethod https://fast-api-v.onrender.com/v1/sync/schema -Headers $headers
Invoke-RestMethod https://fast-api-v.onrender.com/v1/sync/export -Headers $headers
```

La primera respuesta debe indicar `service=todo-sync-api`. La segunda debe
listar las tablas de sync. La tercera debe devolver los registros recuperados.
