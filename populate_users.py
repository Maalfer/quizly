#!/usr/bin/env python3
"""
populate_users.py — Pobla la tabla `users` de Quizly con profesores de ejemplo.

Las contraseñas se almacenan con el mismo hash que usa la app:
    sha256(QUIZLY_SECRET + password)

Por eso este script DEBE usar el mismo QUIZLY_SECRET y la misma base de datos
que la aplicación (lee la configuración del entorno o de un fichero .env).

Uso:
    # Con Docker Compose (la BD ya levantada):
    docker compose run --rm web python populate_users.py

    # En local (con el venv activado y la BD accesible):
    python populate_users.py

    # Crear un profesor concreto:
    python populate_users.py --user laura --password Secreta123 --role admin
"""
import argparse
import os
import sys
from hashlib import sha256

import pymysql


# ── Carga simple de .env (sin dependencias externas) ────────────────────────
def load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


load_dotenv()

SECRET_KEY = os.environ.get("QUIZLY_SECRET", "change-me-quizly-secret-key")
DB_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MYSQL_PORT", "3306")),
    "user": os.environ.get("MYSQL_USER", "quizly"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
    "database": os.environ.get("MYSQL_DB", "quizly"),
    "charset": "utf8mb4",
}

# Profesores de ejemplo (usuario, contraseña, rol)
DEMO_USERS = [
    ("admin",   "admin",      "admin"),   # cuenta por defecto (cámbiala en producción)
    ("profe",   "profe1234",  "admin"),
    ("laura",   "laura1234",  "admin"),
    ("carlos",  "carlos1234", "admin"),
    ("invitado", "invitado",  "teacher"),
]


def hash_pw(pw: str) -> str:
    return sha256((SECRET_KEY + pw).encode()).hexdigest()


def ensure_users_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(190) UNIQUE NOT NULL,
        password VARCHAR(255) NOT NULL,
        role VARCHAR(32) NOT NULL DEFAULT 'admin'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")


def upsert_user(cur, username, password, role):
    cur.execute(
        "INSERT INTO users (username, password, role) VALUES (%s, %s, %s) "
        "ON DUPLICATE KEY UPDATE password=VALUES(password), role=VALUES(role)",
        (username, hash_pw(password), role),
    )


def main():
    ap = argparse.ArgumentParser(description="Pobla usuarios (profesores) en Quizly.")
    ap.add_argument("--user", help="Crear/actualizar un único usuario")
    ap.add_argument("--password", help="Contraseña para --user")
    ap.add_argument("--role", default="admin", help="Rol (admin|teacher), por defecto admin")
    args = ap.parse_args()

    try:
        conn = pymysql.connect(autocommit=False, **DB_CONFIG)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ No se pudo conectar a MariaDB en {DB_CONFIG['host']}:{DB_CONFIG['port']}: {exc}")
        sys.exit(1)

    try:
        with conn.cursor() as cur:
            ensure_users_table(cur)
            if args.user:
                if not args.password:
                    print("✗ Debes indicar --password junto con --user")
                    sys.exit(1)
                upsert_user(cur, args.user, args.password, args.role)
                users = [(args.user, args.password, args.role)]
            else:
                for u, p, r in DEMO_USERS:
                    upsert_user(cur, u, p, r)
                users = DEMO_USERS
        conn.commit()
    finally:
        conn.close()

    print("✓ Usuarios creados/actualizados:")
    print(f"  {'USUARIO':<12} {'CONTRASEÑA':<14} ROL")
    print(f"  {'-'*12} {'-'*14} ---")
    for u, p, r in users:
        print(f"  {u:<12} {p:<14} {r}")
    print("\n⚠  Cambia las contraseñas por defecto antes de usar Quizly en producción.")


if __name__ == "__main__":
    main()
