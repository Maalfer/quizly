FROM python:3.12-slim

# Evita prompts y archivos .pyc; logs sin buffer
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencias primero (mejor uso de la caché de capas)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código de la aplicación
COPY . .

# Las salas se sirven por WebSocket en este puerto
EXPOSE 9091

# La app crea las tablas y el admin por defecto al arrancar (lifespan -> init_db)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9091", "--workers", "1"]
