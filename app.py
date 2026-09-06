"""
Quizly — plataforma tipo Kahoot (FastAPI + WebSockets).

Funcionalidades:
- Tipos de pregunta: test (choice), verdadero/falso, respuesta múltiple, ordenar,
  numérico (slider), rellenar frase. Soporte de imagen por pregunta.
- Modo por equipos, power-up x2, aleatorizar preguntas/opciones.
- Sonidos/confeti (cliente). Foto de perfil real (subida) + avatares emoji.
- Generación de quizzes con IA (Claude), import/export JSON, duplicar, carpetas.
- Historial de partidas + informe + export CSV.
- Multi-profesor, cambio de contraseña, rate-limiting. PWA + modo proyector.

Estado del juego en memoria (salas efímeras). Persistencia en SQLite.
"""
import asyncio
import csv
import io
import json
import os
import random
import re
import secrets
import sqlite3
import string
import time
import urllib.request
from contextlib import asynccontextmanager
from hashlib import sha256
from typing import Dict, List, Optional

from fastapi import (Cookie, FastAPI, File, Form, Request, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

import pymysql
import pymysql.cursors
from pymysql.err import IntegrityError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MYSQL_PORT", "3306")),
    "user": os.environ.get("MYSQL_USER", "quizly"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
    "database": os.environ.get("MYSQL_DB", "quizly"),
    "charset": "utf8mb4",
}
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
SECRET_KEY = os.environ.get("QUIZLY_SECRET", "change-me-quizly-secret-key")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL = os.environ.get("QUIZLY_AI_MODEL", "claude-sonnet-4-6")

serializer = URLSafeSerializer(SECRET_KEY, salt="quizly-session")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

AVATARS = ["🦊", "🐼", "🐧", "🦁", "🐸", "🐙", "🦄", "🐳", "🦉", "🐝",
           "🦖", "🐯", "🦅", "🐢", "🦋", "🐬", "🦓", "🐨", "🦔", "🐺"]

# Filtro básico de nombres ofensivos (se censuran).
BADWORDS = ["puta", "puto", "mierda", "gilipollas", "cabron", "cabrón", "polla",
            "coño", "joder", "zorra", "maricon", "maricón", "nazi", "hitler",
            "fuck", "shit", "bitch", "nigger", "porn"]


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------
def _q(sql):
    # Traduce los placeholders estilo sqlite ('?') al estilo de PyMySQL ('%s').
    return sql.replace("?", "%s")


class _Cursor:
    """Envuelve un cursor de PyMySQL para imitar la API de sqlite3:
    placeholders '?', encadenar execute().fetchone()/fetchall() e iterar filas."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=()):
        self._cur.execute(_q(sql), params or None)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount

    def __iter__(self):
        return iter(self._cur.fetchall())


class _Conn:
    """Conexión MariaDB compatible con el código antiguo basado en sqlite3.
    Cada llamada a db() abre una conexión nueva (el código la cierra con close())."""

    def __init__(self):
        self._conn = pymysql.connect(
            cursorclass=pymysql.cursors.DictCursor, autocommit=False, **DB_CONFIG)

    def cursor(self):
        return _Cursor(self._conn.cursor())

    def execute(self, sql, params=()):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def db():
    return _Conn()


def hash_pw(pw: str) -> str:
    return sha256((SECRET_KEY + pw).encode()).hexdigest()


def _add_col(c, table, col, decl):
    exists = c.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema=DATABASE() AND table_name=? AND column_name=?",
        (table, col)).fetchone()
    if not exists:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(190) UNIQUE NOT NULL,
        password VARCHAR(255) NOT NULL,
        role VARCHAR(32) NOT NULL DEFAULT 'admin'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    c.execute("""CREATE TABLE IF NOT EXISTS quizzes (
        id INT AUTO_INCREMENT PRIMARY KEY,
        title VARCHAR(255) NOT NULL,
        theme VARCHAR(255) NOT NULL,
        kind VARCHAR(64) NOT NULL,
        questions LONGTEXT NOT NULL,
        owner VARCHAR(190),
        folder VARCHAR(255) DEFAULT 'General'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    c.execute("""CREATE TABLE IF NOT EXISTS results (
        id INT AUTO_INCREMENT PRIMARY KEY,
        code VARCHAR(64), quiz_title VARCHAR(255), theme VARCHAR(255), mode VARCHAR(64),
        started_at VARCHAR(32), ended_at VARCHAR(32), data LONGTEXT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    c.execute("""CREATE TABLE IF NOT EXISTS api_tokens (
        token VARCHAR(190) PRIMARY KEY, owner VARCHAR(190), label VARCHAR(255),
        created_at VARCHAR(32), last_used VARCHAR(32)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    # Migraciones suaves
    _add_col(c, "quizzes", "owner", "VARCHAR(190)")
    _add_col(c, "quizzes", "folder", "VARCHAR(255) DEFAULT 'General'")
    # Solo crea el admin por defecto si NO existe ningún administrador
    # (así, tras renombrar/cambiar el admin, un reinicio no recrea 'admin').
    if not c.execute("SELECT id FROM users WHERE role='admin'").fetchone():
        c.execute("INSERT INTO users (username, password, role) VALUES (?,?,?)",
                  ("admin", hash_pw("admin"), "admin"))
    if c.execute("SELECT COUNT(*) AS n FROM quizzes").fetchone()["n"] == 0:
        for q in SEED_QUIZZES:
            c.execute("INSERT INTO quizzes (title, theme, kind, questions, owner, folder) VALUES (?,?,?,?,?,?)",
                      (q["title"], q["theme"], q["kind"], json.dumps(q["questions"], ensure_ascii=False),
                       "admin", q.get("folder", "General")))
    conn.commit()
    conn.close()


SEED_QUIZZES = [
    {"title": "Ciberseguridad básica", "theme": "Ciberseguridad", "kind": "test", "folder": "Ciberseguridad", "questions": [
        {"type": "choice", "text": "¿Qué significa SQL en SQLi?", "options": ["Structured Query Language", "Simple Question Logic", "Secure Query Layer", "System Query Link"], "answer": 0, "time": 20},
        {"type": "truefalse", "text": "HTTPS usa el puerto 443 por defecto.", "options": ["Verdadero", "Falso"], "answer": 0, "time": 15},
        {"type": "choice", "text": "Un ataque que satura un servicio se llama...", "options": ["Phishing", "DoS", "XSS", "MITM"], "answer": 1, "time": 20},
        {"type": "multi", "text": "¿Cuáles son herramientas de pentesting? (varias)", "options": ["Nmap", "Excel", "Burp Suite", "Metasploit"], "answers": [0, 2, 3], "time": 25},
        {"type": "fill", "text": "El hash recomendable para contraseñas es ____.", "answer": "bcrypt", "accepted": ["bcrypt", "argon2", "scrypt"], "time": 20},
    ]},
    {"title": "Ataques web (OWASP)", "theme": "Ciberseguridad", "kind": "mixto", "folder": "Ciberseguridad", "questions": [
        {"type": "fill", "text": "Inyectar código en una web es cross-site ____.", "answer": "scripting", "time": 20},
        {"type": "choice", "text": "¿Qué cabecera mitiga clickjacking?", "options": ["X-Frame-Options", "Accept", "Cookie", "Referer"], "answer": 0, "time": 20},
        {"type": "order", "text": "Ordena las fases de un pentest", "items": ["Reconocimiento", "Escaneo", "Explotación", "Post-explotación"], "time": 30},
        {"type": "numeric", "text": "¿Cuántas capas tiene el modelo OSI?", "answer": 7, "tol": 0, "min": 1, "max": 12, "time": 20},
    ]},
    {"title": "Cultura general", "theme": "Cultura general", "kind": "test", "folder": "General", "questions": [
        {"type": "choice", "text": "¿Cuál es el río más largo del mundo?", "options": ["Nilo", "Amazonas", "Misisipi", "Yangtsé"], "answer": 1, "time": 15},
        {"type": "numeric", "text": "¿En qué año llegó el hombre a la Luna?", "answer": 1969, "tol": 2, "min": 1950, "max": 2000, "time": 20},
        {"type": "choice", "text": "¿Quién pintó La Gioconda?", "options": ["Van Gogh", "Picasso", "Da Vinci", "Goya"], "answer": 2, "time": 15},
        {"type": "truefalse", "text": "El Sol es una estrella.", "options": ["Verdadero", "Falso"], "answer": 0, "time": 12},
    ]},
    {"title": "Programación", "theme": "Programación", "kind": "mixto", "folder": "General", "questions": [
        {"type": "choice", "text": "¿Qué lenguaje usa indentación obligatoria?", "options": ["Java", "Python", "C", "PHP"], "answer": 1, "time": 15},
        {"type": "fill", "text": "El bucle que repite mientras se cumple una condición es el bucle ____.", "answer": "while", "time": 20},
        {"type": "order", "text": "Ordena de menor a mayor tamaño", "items": ["bit", "byte", "kilobyte", "megabyte"], "time": 25},
    ]},
]


# ---------------------------------------------------------------------------
# Rate limiting sencillo en memoria
# ---------------------------------------------------------------------------
_RATE: Dict[str, List[float]] = {}


def rate_ok(key: str, limit: int, window: float) -> bool:
    now = time.time()
    arr = [t for t in _RATE.get(key, []) if now - t < window]
    if len(arr) >= limit:
        _RATE[key] = arr
        return False
    arr.append(now)
    _RATE[key] = arr
    return True


def client_ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


def clean_name(name: str) -> str:
    name = (name or "Jugador").strip()[:20] or "Jugador"
    low = name.lower()
    for w in BADWORDS:
        if w in low:
            return "Jugador"
    return name


_AVATAR_UPLOAD_RE = re.compile(r"^/static/uploads/[0-9a-f]{20}\.(png|jpg|webp|gif)$")


def clean_avatar(avatar) -> str:
    """Solo se acepta un emoji de AVATARS o una ruta propia generada por
    /upload/avatar (mismo formato que secrets.token_hex(10) + extensión).
    Cualquier otro valor (URLs externas, data:, etc.) se descarta."""
    if isinstance(avatar, str):
        if avatar in AVATARS:
            return avatar
        if _AVATAR_UPLOAD_RE.match(avatar):
            return avatar
    return random.choice(AVATARS)


# ---------------------------------------------------------------------------
# Estado en memoria de las salas / juego
# ---------------------------------------------------------------------------
class Player:
    def __init__(self, pid: str, name: str, avatar: str, team: str = ""):
        self.pid = pid
        # Secreto de reconexión: solo se envía una vez, directo al propio jugador
        # en el "joined" -- nunca en lobby_payload()/scoreboard()/broadcasts, a
        # diferencia del pid (que sí es público entre jugadores de la sala).
        self.token = secrets.token_hex(16)
        self.name = name
        self.avatar = avatar
        self.team = team
        self.score = 0
        self.streak = 0
        self.last_points = 0
        self.answered = False
        self.boost = False        # power-up x2 armado para la pregunta actual
        self.boost_used = False
        self.ws: Optional[WebSocket] = None


TEAMS = [("Rojo", "🔴"), ("Azul", "🔵"), ("Verde", "🟢"), ("Amarillo", "🟡")]


class Room:
    def __init__(self, code: str):
        self.code = code
        self.owner: Optional[str] = None
        self.players: Dict[str, Player] = {}
        self.host_ws: Optional[WebSocket] = None
        self.state = "lobby"
        self.quiz: Optional[dict] = None
        self.q_index = -1
        self.q_started_at = 0.0
        self.lock = asyncio.Lock()
        self.timer_task: Optional[asyncio.Task] = None
        # opciones
        self.teams_on = False
        self.n_teams = 2
        self.shuffle_q = False
        self.shuffle_opts = False
        self.powerups = True
        # estadísticas
        self.stats: List[dict] = []
        self.started_at = ""

    async def send_host(self, payload: dict):
        if self.host_ws:
            try:
                await self.host_ws.send_json(payload)
            except Exception:
                pass

    async def broadcast_players(self, payload: dict):
        for p in list(self.players.values()):
            await _safe_send(p, payload)

    def team_assign(self) -> str:
        if not self.teams_on:
            return ""
        counts = {TEAMS[i][0]: 0 for i in range(self.n_teams)}
        for p in self.players.values():
            if p.team in counts:
                counts[p.team] += 1
        return min(counts, key=counts.get)

    def scoreboard(self, top: Optional[int] = None) -> List[dict]:
        ordered = sorted(self.players.values(), key=lambda p: p.score, reverse=True)
        if top:
            ordered = ordered[:top]
        return [{"name": p.name, "avatar": p.avatar, "score": p.score,
                 "last": p.last_points, "streak": p.streak, "team": p.team} for p in ordered]

    def team_board(self) -> List[dict]:
        if not self.teams_on:
            return []
        agg: Dict[str, dict] = {}
        for p in self.players.values():
            t = p.team or "—"
            emoji = next((e for n, e in TEAMS if n == t), "⚪")
            d = agg.setdefault(t, {"team": t, "emoji": emoji, "score": 0, "members": 0})
            d["score"] += p.score
            d["members"] += 1
        return sorted(agg.values(), key=lambda x: x["score"], reverse=True)

    def lobby_payload(self) -> dict:
        return {"type": "lobby",
                "players": [{"name": p.name, "avatar": p.avatar, "team": p.team, "pid": p.pid}
                            for p in self.players.values()],
                "count": len(self.players),
                "teams_on": self.teams_on, "n_teams": self.n_teams,
                "teams": [{"name": TEAMS[i][0], "emoji": TEAMS[i][1]} for i in range(self.n_teams)]}

    @property
    def current_question(self) -> Optional[dict]:
        if self.quiz and 0 <= self.q_index < len(self.quiz["questions"]):
            return self.quiz["questions"][self.q_index]
        return None


ROOMS: Dict[str, Room] = {}


def new_code() -> str:
    while True:
        code = "".join(random.choices(string.digits, k=6))
        if code not in ROOMS:
            return code


def load_quiz_into_room(room: "Room", quiz_id: int):
    """Carga un quiz de la BD en la sala, aplicando barajado si está activo.
    Devuelve (title, n) o None si no existe."""
    conn = db()
    q = conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
    conn.close()
    if not q:
        return None
    questions = json.loads(q["questions"])
    if room.shuffle_q:
        random.shuffle(questions)
    if room.shuffle_opts:
        for qq in questions:
            if qq["type"] in ("choice", "multi"):
                idx = list(range(len(qq["options"])))
                random.shuffle(idx)
                qq["options"] = [qq["options"][i] for i in idx]
                if qq["type"] == "choice":
                    qq["answer"] = idx.index(qq["answer"])
                else:
                    qq["answers"] = [idx.index(a) for a in qq.get("answers", [])]
    room.quiz = {"title": q["title"], "theme": q["theme"], "kind": q["kind"], "questions": questions}
    room.q_index = -1
    room.stats = []
    return q["title"], len(questions)


# ---------------------------------------------------------------------------
# Lógica del juego
# ---------------------------------------------------------------------------
SPEED_BONUS = 500
BASE_POINTS = 500


def normalize(s: str) -> str:
    s = str(s).strip().lower()
    repl = {"á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ü": "u", "ñ": "n"}
    s = "".join(repl.get(ch, ch) for ch in s)
    return "".join(ch for ch in s if ch.isalnum())


def grade(q: dict, answer) -> float:
    """Devuelve fracción de acierto en [0,1]."""
    t = q.get("type", "choice")
    try:
        if t in ("choice", "truefalse"):
            return 1.0 if int(answer) == int(q["answer"]) else 0.0
        if t == "fill":
            accepted = q.get("accepted") or [q.get("answer", "")]
            na = normalize(answer)
            return 1.0 if na and any(na == normalize(a) for a in accepted) else 0.0
        if t == "multi":
            correct = set(q.get("answers", []))
            sel = set(int(x) for x in (answer or []))
            if not correct:
                return 0.0
            frac = (len(sel & correct) - len(sel - correct)) / len(correct)
            return max(0.0, min(1.0, frac))
        if t == "numeric":
            val = float(answer)
            target = float(q["answer"])
            tol = float(q.get("tol", 0) or 0)
            d = abs(val - target)
            if tol <= 0:
                return 1.0 if d == 0 else 0.0
            if d > tol:
                return 0.0
            return 1.0 - (d / tol) * 0.5
        if t == "order":
            correct = q.get("items", [])
            arr = answer or []
            if not correct:
                return 0.0
            good = sum(1 for i, v in enumerate(arr) if i < len(correct) and str(v) == str(correct[i]))
            return good / len(correct)
    except (ValueError, TypeError):
        return 0.0
    return 0.0


def correct_repr(q: dict):
    t = q.get("type")
    if t == "multi":
        return q.get("answers", [])
    if t == "order":
        return q.get("items", [])
    if t in ("choice", "truefalse"):
        return q.get("answer")
    return q.get("answer")


def player_question_payload(room: Room, q: dict) -> dict:
    total = len(room.quiz["questions"])
    p = {"type": "question", "index": room.q_index + 1, "total": total,
         "qtype": q["type"], "text": q["text"], "time": int(q.get("time", 20)),
         "image": q.get("image", ""), "powerups": room.powerups}
    if q["type"] in ("choice", "multi"):
        p["options"] = q["options"]
    elif q["type"] == "truefalse":
        p["options"] = q.get("options", ["Verdadero", "Falso"])
    elif q["type"] == "numeric":
        p["min"] = q.get("min", 0)
        p["max"] = q.get("max", 100)
    elif q["type"] == "order":
        items = list(q["items"])
        random.shuffle(items)
        p["items"] = items
    return p


async def start_question(room: Room):
    room.q_index += 1
    q = room.current_question
    if q is None:
        await end_game(room)
        return
    room.state = "question"
    room.q_started_at = time.time()
    tlimit = int(q.get("time", 20))
    for p in room.players.values():
        p.answered = False
        p.last_points = 0
    if len(room.stats) <= room.q_index:
        room.stats.append({"text": q["text"], "type": q["type"], "answered": 0, "correct": 0,
                           "dist": {}})

    pq = player_question_payload(room, q)
    await room.broadcast_players(pq)
    hq = dict(pq)
    hq["type"] = "host_question"
    hq["answer"] = correct_repr(q)
    if q["type"] in ("choice", "multi", "truefalse"):
        hq["options"] = q.get("options", ["Verdadero", "Falso"])
    if q["type"] == "fill":
        hq["answer_text"] = q.get("answer", "")
    if q["type"] == "order":
        hq["correct_order"] = q.get("items", [])
    hq["players_total"] = len(room.players)
    await room.send_host(hq)

    if room.timer_task and not room.timer_task.done():
        room.timer_task.cancel()
    room.timer_task = asyncio.create_task(question_timer(room, room.q_index, tlimit))


async def question_timer(room: Room, q_index: int, tlimit: int):
    try:
        await asyncio.sleep(tlimit)
        async with room.lock:
            if room.state == "question" and room.q_index == q_index:
                await reveal_question(room)
    except asyncio.CancelledError:
        pass


async def reveal_question(room: Room):
    q = room.current_question
    if q is None:
        return
    room.state = "reveal"
    full = room.scoreboard()
    rank_by = {x["name"]: i + 1 for i, x in enumerate(full)}
    n = len(full)
    has_next = room.q_index + 1 < len(room.quiz["questions"])
    correct = correct_repr(q)
    for p in room.players.values():
        await _safe_send(p, {"type": "reveal", "correct": correct, "qtype": q["type"],
                             "you_got": p.last_points, "score": p.score, "streak": p.streak,
                             "rank": rank_by.get(p.name, n), "players": n,
                             "has_next": has_next, "q_index": room.q_index + 1,
                             "q_total": len(room.quiz["questions"])})
    st = room.stats[room.q_index] if room.q_index < len(room.stats) else {}
    await room.send_host({"type": "host_reveal", "correct": correct, "qtype": q["type"],
                          "answer_text": q.get("answer", "") if q["type"] in ("fill", "numeric") else "",
                          "correct_order": q.get("items", []) if q["type"] == "order" else [],
                          "options": q.get("options", []),
                          "scoreboard": room.scoreboard(top=5),
                          "team_board": room.team_board(),
                          "dist": st.get("dist", {}),
                          "answered": sum(1 for p in room.players.values() if p.answered),
                          "total": len(room.players), "has_next": has_next})


async def _safe_send(p: Player, payload: dict):
    if p.ws:
        try:
            await p.ws.send_json(payload)
        except Exception:
            p.ws = None


async def submit_answer(room: Room, player: Player, answer):
    if room.state != "question" or player.answered:
        return
    q = room.current_question
    if q is None:
        return
    player.answered = True
    frac = grade(q, answer)
    st = room.stats[room.q_index]
    st["answered"] += 1
    if q["type"] in ("choice", "truefalse") and answer is not None:
        key = str(answer)
        st["dist"][key] = st["dist"].get(key, 0) + 1
    if frac >= 0.999:
        st["correct"] += 1

    if frac > 0:
        elapsed = time.time() - room.q_started_at
        tlimit = max(1, int(q.get("time", 20)))
        speed = max(0.0, 1.0 - (elapsed / tlimit))
        points = int((BASE_POINTS + SPEED_BONUS * speed) * frac)
        if frac >= 0.999:
            player.streak += 1
            points += min(player.streak - 1, 5) * 50
        else:
            player.streak = 0
        if player.boost:
            points *= 2
        player.score += points
        player.last_points = points
    else:
        player.streak = 0
        player.last_points = 0
    boosted = player.boost
    player.boost = False

    await _safe_send(player, {"type": "answer_ack", "received": True, "boosted": boosted})
    answered = sum(1 for p in room.players.values() if p.answered)
    await room.send_host({"type": "progress", "answered": answered, "total": len(room.players)})
    if answered >= len(room.players) and len(room.players) > 0:
        if room.timer_task and not room.timer_task.done():
            room.timer_task.cancel()
        await reveal_question(room)


def save_result(room: Room):
    try:
        players = [{"name": p.name, "score": p.score, "team": p.team, "avatar": p.avatar}
                   for p in sorted(room.players.values(), key=lambda x: x.score, reverse=True)]
        data = {"players": players, "questions": room.stats, "teams": room.team_board(),
                "teams_on": room.teams_on}
        conn = db()
        conn.execute("INSERT INTO results (code, quiz_title, theme, mode, started_at, ended_at, data) VALUES (?,?,?,?,?,?,?)",
                     (room.code, room.quiz.get("title", "?") if room.quiz else "?",
                      room.quiz.get("theme", "") if room.quiz else "", "live",
                      room.started_at, time.strftime("%Y-%m-%d %H:%M"), json.dumps(data, ensure_ascii=False)))
        conn.commit()
        conn.close()
    except Exception:
        pass


async def end_game(room: Room):
    room.state = "ended"
    podium = room.scoreboard(top=3)
    full = room.scoreboard()
    save_result(room)
    await room.send_host({"type": "host_end", "podium": podium, "scoreboard": full,
                          "team_board": room.team_board(), "teams_on": room.teams_on})
    for p in room.players.values():
        rank = next((i for i, x in enumerate(full) if x["name"] == p.name), 0) + 1
        await _safe_send(p, {"type": "end", "rank": rank, "score": p.score,
                             "total": len(full), "podium": podium,
                             "team_board": room.team_board(), "teams_on": room.teams_on})


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def make_session(username: str) -> str:
    return serializer.dumps({"u": username})


def read_session(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        return serializer.loads(token).get("u")
    except BadSignature:
        return None


def get_user(username: str):
    conn = db()
    u = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    return u


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Quizly", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request, quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    return templates.TemplateResponse("index.html", {"request": request, "user": user})


@app.get("/manifest.webmanifest")
async def manifest():
    return JSONResponse({
        "name": "Quizly", "short_name": "Quizly", "start_url": "/", "display": "standalone",
        "background_color": "#020617", "theme_color": "#020617",
        "icons": [{"src": "/static/logo_120.png", "sizes": "120x120", "type": "image/png"},
                  {"src": "/static/logo_120.png", "sizes": "512x512", "type": "image/png"}]
    })


@app.get("/sw.js")
async def service_worker():
    # IMPORTANTE: el SW NO debe cachear HTML/dinámico (home, login, admin...),
    # porque serviría sesión y tema obsoletos. Solo assets estáticos versionados.
    js = """
const C='quizly-v2';
self.addEventListener('install',e=>{self.skipWaiting();});
self.addEventListener('activate',e=>{e.waitUntil(
  caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k)))).then(()=>self.clients.claim())
);});
self.addEventListener('fetch',e=>{
  const u=new URL(e.request.url);
  if(e.request.method==='GET' && u.pathname.startsWith('/static/') && u.pathname.indexOf('/uploads/')<0){
    e.respondWith(caches.open(C).then(c=>c.match(e.request).then(r=>r||fetch(e.request).then(res=>{
      c.put(e.request,res.clone()); return res;}))));
  }
});
"""
    return Response(js, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if not rate_ok("login:" + client_ip(request), 10, 60):
        return RedirectResponse("/login?error=1", status_code=303)
    row = get_user(username)
    if not row or row["password"] != hash_pw(password):
        return RedirectResponse("/login?error=1", status_code=303)
    resp = RedirectResponse("/admin", status_code=303)
    # Recordar sesión 30 días.
    resp.set_cookie("quizly_session", make_session(username), httponly=True,
                    samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("quizly_session")
    return resp


def visible_quizzes(user: str, role: str):
    conn = db()
    if role == "admin":
        rows = conn.execute("SELECT * FROM quizzes ORDER BY folder, id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM quizzes WHERE owner=? OR owner IS NULL ORDER BY folder, id",
                            (user,)).fetchall()
    conn.close()
    return rows


def owns_quiz(user: str, role: str, owner: Optional[str]) -> bool:
    """Autorización a nivel de objeto: mismo criterio que visible_quizzes()."""
    return role == "admin" or owner == user or owner is None


def owns_room(user: str, role: str, room: "Room") -> bool:
    """A diferencia de owns_quiz(), una sala sin owner NO se considera de nadie:
    las salas siempre se crean con owner asignado, así que None es fail-closed."""
    return role == "admin" or room.owner == user


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    if not user:
        return RedirectResponse("/login", status_code=303)
    u = get_user(user)
    role = u["role"] if u else "teacher"
    rows = visible_quizzes(user, role)
    quiz_list = [{"id": q["id"], "title": q["title"], "theme": q["theme"], "kind": q["kind"],
                  "folder": q["folder"] or "General", "n": len(json.loads(q["questions"]))} for q in rows]
    folders = sorted(set(q["folder"] for q in quiz_list))
    return templates.TemplateResponse("admin.html", {"request": request, "user": user, "role": role,
                                                     "quizzes": quiz_list, "folders": folders,
                                                     "ai_on": bool(ANTHROPIC_KEY)})


# ---- CRUD quizzes ----------------------------------------------------------
def _parse_quiz(data):
    return (data.get("title", "").strip(), data.get("theme", "General").strip() or "General",
            data.get("kind", "mixto"), data.get("folder", "General").strip() or "General",
            data.get("questions", []))


@app.post("/admin/quiz/new")
async def create_quiz(request: Request, quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    title, theme, kind, folder, questions = _parse_quiz(await request.json())
    if not title or not questions:
        return JSONResponse({"ok": False, "error": "Faltan datos"}, status_code=400)
    conn = db()
    cur = conn.execute("INSERT INTO quizzes (title, theme, kind, questions, owner, folder) VALUES (?,?,?,?,?,?)",
                       (title, theme, kind, json.dumps(questions, ensure_ascii=False), user, folder))
    conn.commit()
    qid = cur.lastrowid
    conn.close()
    return JSONResponse({"ok": True, "id": qid})


@app.post("/admin/quiz/{quiz_id}/update")
async def update_quiz(quiz_id: int, request: Request, quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    u = get_user(user)
    role = u["role"] if u else "teacher"
    title, theme, kind, folder, questions = _parse_quiz(await request.json())
    if not title or not questions:
        return JSONResponse({"ok": False, "error": "Faltan datos"}, status_code=400)
    conn = db()
    q = conn.execute("SELECT owner FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
    if not q:
        conn.close()
        return JSONResponse({"ok": False}, status_code=404)
    if not owns_quiz(user, role, q["owner"]):
        conn.close()
        return JSONResponse({"ok": False}, status_code=403)
    cur = conn.execute("UPDATE quizzes SET title=?, theme=?, kind=?, questions=?, folder=? WHERE id=?",
                       (title, theme, kind, json.dumps(questions, ensure_ascii=False), folder, quiz_id))
    conn.commit()
    found = cur.rowcount
    conn.close()
    return JSONResponse({"ok": bool(found)}, status_code=200 if found else 404)


@app.post("/admin/quiz/{quiz_id}/delete")
async def delete_quiz(quiz_id: int, quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    u = get_user(user)
    role = u["role"] if u else "teacher"
    conn = db()
    q = conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
    if not q:
        conn.close()
        return JSONResponse({"ok": False}, status_code=404)
    if not owns_quiz(user, role, q["owner"]):
        conn.close()
        return JSONResponse({"ok": False}, status_code=403)
    conn.execute("DELETE FROM quizzes WHERE id=?", (quiz_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/admin/quiz/{quiz_id}/duplicate")
async def duplicate_quiz(quiz_id: int, quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    u = get_user(user)
    role = u["role"] if u else "teacher"
    conn = db()
    q = conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
    if not q:
        conn.close()
        return JSONResponse({"ok": False}, status_code=404)
    if not owns_quiz(user, role, q["owner"]):
        conn.close()
        return JSONResponse({"ok": False}, status_code=403)
    conn.execute("INSERT INTO quizzes (title, theme, kind, questions, owner, folder) VALUES (?,?,?,?,?,?)",
                 (q["title"] + " (copia)", q["theme"], q["kind"], q["questions"], user, q["folder"]))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.get("/admin/quiz/{quiz_id}/export")
async def export_quiz(quiz_id: int, quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    u = get_user(user)
    role = u["role"] if u else "teacher"
    conn = db()
    q = conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
    conn.close()
    if not q:
        return JSONResponse({"ok": False}, status_code=404)
    if not owns_quiz(user, role, q["owner"]):
        return JSONResponse({"ok": False}, status_code=403)
    payload = {"title": q["title"], "theme": q["theme"], "kind": q["kind"],
               "folder": q["folder"], "questions": json.loads(q["questions"])}
    fn = re.sub(r"[^a-zA-Z0-9_-]+", "_", q["title"])[:40]
    return Response(json.dumps(payload, ensure_ascii=False, indent=2),
                    media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{fn}.json"'})


@app.post("/admin/quiz/import")
async def import_quiz(request: Request, quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    data = await request.json()
    payload = data.get("payload")
    if isinstance(payload, str):
        payload = json.loads(payload)
    title = payload.get("title", "Importado").strip() or "Importado"
    questions = payload.get("questions", [])
    if not questions:
        return JSONResponse({"ok": False, "error": "Sin preguntas"}, status_code=400)
    conn = db()
    conn.execute("INSERT INTO quizzes (title, theme, kind, questions, owner, folder) VALUES (?,?,?,?,?,?)",
                 (title, payload.get("theme", "General"), payload.get("kind", "mixto"),
                  json.dumps(questions, ensure_ascii=False), user, payload.get("folder", "General")))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


# ---- IA: generar quiz con Claude ------------------------------------------
def _claude_generate(topic: str, n: int, qtype: str) -> dict:
    if not ANTHROPIC_KEY:
        return {"ok": False, "error": "IA no configurada: falta ANTHROPIC_API_KEY en el servidor."}
    schema = {
        "choice": '{"type":"choice","text":"...","options":["a","b","c","d"],"answer":<indice 0-3>,"time":20}',
        "truefalse": '{"type":"truefalse","text":"...","options":["Verdadero","Falso"],"answer":<0 o 1>,"time":15}',
        "multi": '{"type":"multi","text":"...","options":["a","b","c","d"],"answers":[<indices correctos>],"time":25}',
        "fill": '{"type":"fill","text":"frase con ____","answer":"palabra","time":20}',
        "numeric": '{"type":"numeric","text":"...","answer":<numero>,"tol":1,"min":0,"max":100,"time":20}',
    }
    if qtype == "mixto":
        ej = "Mezcla los tipos. Cada objeto sigue uno de estos formatos:\n" + "\n".join(schema.values())
    else:
        ej = "Cada objeto con este formato exacto:\n" + schema.get(qtype, schema["choice"])
    prompt = (f"Eres un generador de cuestionarios educativos. Crea {n} preguntas en español sobre: "
              f"\"{topic}\".\n{ej}\nDevuelve EXCLUSIVAMENTE un array JSON válido, sin texto extra, "
              f"sin markdown. Las preguntas deben ser correctas y variadas.")
    body = {"model": AI_MODEL, "max_tokens": 3000,
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                 data=json.dumps(body).encode(),
                                 headers={"x-api-key": ANTHROPIC_KEY,
                                          "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.load(r)
        text = resp["content"][0]["text"]
        m = re.search(r"\[.*\]", text, re.S)
        questions = json.loads(m.group(0) if m else text)
        # saneado mínimo
        clean = []
        for q in questions:
            if isinstance(q, dict) and q.get("text"):
                q.setdefault("time", 20)
                clean.append(q)
        if not clean:
            return {"ok": False, "error": "La IA no devolvió preguntas válidas."}
        return {"ok": True, "questions": clean}
    except Exception as e:  # noqa
        return {"ok": False, "error": f"Error al llamar a la IA: {e}"}


@app.post("/admin/quiz/ai")
async def ai_quiz(request: Request, quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    if not rate_ok("ai:" + user, 8, 60):
        return JSONResponse({"ok": False, "error": "Demasiadas peticiones, espera un momento."}, status_code=429)
    data = await request.json()
    topic = (data.get("topic") or "").strip()
    n = max(1, min(15, int(data.get("n", 5))))
    qtype = data.get("qtype", "mixto")
    if not topic:
        return JSONResponse({"ok": False, "error": "Indica un tema."}, status_code=400)
    result = await asyncio.to_thread(_claude_generate, topic, n, qtype)
    return JSONResponse(result)


# ---- Subida de foto de perfil ---------------------------------------------
@app.post("/upload/avatar")
async def upload_avatar(request: Request, file: UploadFile = File(...)):
    if not rate_ok("up:" + client_ip(request), 15, 60):
        return JSONResponse({"ok": False, "error": "Demasiadas subidas."}, status_code=429)
    ct = file.content_type or ""
    if not ct.startswith("image/"):
        return JSONResponse({"ok": False, "error": "Debe ser una imagen."}, status_code=400)
    raw = await file.read()
    if len(raw) > 3 * 1024 * 1024:
        return JSONResponse({"ok": False, "error": "Máximo 3 MB."}, status_code=400)
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
           "image/gif": "gif"}.get(ct, "png")
    name = secrets.token_hex(10) + "." + ext
    with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
        f.write(raw)
    return JSONResponse({"ok": True, "url": "/static/uploads/" + name})


# ---- Cuenta / profesores ---------------------------------------------------
@app.get("/admin/account", response_class=HTMLResponse)
async def account_page(request: Request, quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    if not user:
        return RedirectResponse("/login", status_code=303)
    u = get_user(user)
    role = u["role"] if u else "teacher"
    conn = db()
    teachers = []
    if role == "admin":
        teachers = [dict(r) for r in conn.execute("SELECT username, role FROM users ORDER BY username").fetchall()]
    tokens = [dict(r) for r in conn.execute(
        "SELECT token, label, created_at, last_used FROM api_tokens WHERE owner=? ORDER BY created_at DESC",
        (user,)).fetchall()]
    conn.close()
    return templates.TemplateResponse("account.html", {"request": request, "user": user,
                                                       "role": role, "teachers": teachers, "tokens": tokens})


@app.post("/admin/account/password")
async def change_password(request: Request, quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    data = await request.json()
    cur, new = data.get("current", ""), data.get("new", "")
    u = get_user(user)
    if not u or u["password"] != hash_pw(cur):
        return JSONResponse({"ok": False, "error": "Contraseña actual incorrecta."}, status_code=400)
    if len(new) < 4:
        return JSONResponse({"ok": False, "error": "Mínimo 4 caracteres."}, status_code=400)
    conn = db()
    conn.execute("UPDATE users SET password=? WHERE username=?", (hash_pw(new), user))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/admin/account/teacher")
async def add_teacher(request: Request, quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    u = get_user(user) if user else None
    if not u or u["role"] != "admin":
        return JSONResponse({"ok": False}, status_code=403)
    data = await request.json()
    un = (data.get("username") or "").strip()
    pw = data.get("password") or ""
    if not un or len(pw) < 4:
        return JSONResponse({"ok": False, "error": "Usuario y contraseña (mín 4) requeridos."}, status_code=400)
    conn = db()
    try:
        conn.execute("INSERT INTO users (username, password, role) VALUES (?,?,?)",
                     (un, hash_pw(pw), "teacher"))
        conn.commit()
    except IntegrityError:
        conn.close()
        return JSONResponse({"ok": False, "error": "Ese usuario ya existe."}, status_code=400)
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/admin/account/teacher/delete")
async def del_teacher(request: Request, quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    u = get_user(user) if user else None
    if not u or u["role"] != "admin":
        return JSONResponse({"ok": False}, status_code=403)
    un = (await request.json()).get("username", "")
    if un == "admin":
        return JSONResponse({"ok": False, "error": "No se puede borrar admin."}, status_code=400)
    conn = db()
    conn.execute("DELETE FROM users WHERE username=? AND role!='admin'", (un,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


# ---- Resultados / historial ------------------------------------------------
@app.get("/admin/results", response_class=HTMLResponse)
async def results_page(request: Request, quizly_session: Optional[str] = Cookie(default=None)):
    if not read_session(quizly_session):
        return RedirectResponse("/login", status_code=303)
    conn = db()
    rows = conn.execute("SELECT id, quiz_title, theme, mode, ended_at, data FROM results ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    items = []
    for r in rows:
        d = json.loads(r["data"])
        items.append({"id": r["id"], "title": r["quiz_title"], "theme": r["theme"],
                      "mode": r["mode"], "ended_at": r["ended_at"],
                      "players": len(d.get("players", [])),
                      "winner": (d["players"][0]["name"] if d.get("players") else "—")})
    return templates.TemplateResponse("results.html", {"request": request, "items": items})


@app.get("/admin/results/{rid}", response_class=HTMLResponse)
async def result_detail(rid: int, request: Request, quizly_session: Optional[str] = Cookie(default=None)):
    if not read_session(quizly_session):
        return RedirectResponse("/login", status_code=303)
    conn = db()
    r = conn.execute("SELECT * FROM results WHERE id=?", (rid,)).fetchone()
    conn.close()
    if not r:
        return RedirectResponse("/admin/results", status_code=303)
    d = json.loads(r["data"])
    return templates.TemplateResponse("result_detail.html", {"request": request, "r": dict(r), "d": d})


@app.get("/admin/results/{rid}/csv")
async def result_csv(rid: int, quizly_session: Optional[str] = Cookie(default=None)):
    if not read_session(quizly_session):
        return JSONResponse({"ok": False}, status_code=401)
    conn = db()
    r = conn.execute("SELECT * FROM results WHERE id=?", (rid,)).fetchone()
    conn.close()
    if not r:
        return JSONResponse({"ok": False}, status_code=404)
    d = json.loads(r["data"])
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Puesto", "Jugador", "Equipo", "Puntos"])
    for i, p in enumerate(d.get("players", [])):
        w.writerow([i + 1, p["name"], p.get("team", ""), p["score"]])
    w.writerow([])
    w.writerow(["Pregunta", "Tipo", "Respondieron", "Aciertos"])
    for q in d.get("questions", []):
        w.writerow([q.get("text", ""), q.get("type", ""), q.get("answered", 0), q.get("correct", 0)])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="resultado_{rid}.csv"'})


# ---- Host / Sala -----------------------------------------------------------
@app.post("/admin/room/create")
async def create_room(quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    code = new_code()
    room = Room(code)
    room.owner = user
    ROOMS[code] = room
    return JSONResponse({"ok": True, "code": code})


@app.get("/host/{code}", response_class=HTMLResponse)
async def host_page(request: Request, code: str, quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if code not in ROOMS:
        return RedirectResponse("/admin", status_code=303)
    u = get_user(user)
    rows = visible_quizzes(user, u["role"] if u else "teacher")
    quiz_list = [{"id": q["id"], "title": q["title"], "theme": q["theme"], "kind": q["kind"],
                  "folder": q["folder"] or "General", "n": len(json.loads(q["questions"]))} for q in rows]
    return templates.TemplateResponse("host.html", {"request": request, "code": code, "quizzes": quiz_list})


@app.get("/join", response_class=HTMLResponse)
@app.get("/join/{code}", response_class=HTMLResponse)
async def join_page(request: Request, code: str = ""):
    room = ROOMS.get(code)
    valid = room is not None and room.state == "lobby"
    return templates.TemplateResponse("join.html", {"request": request, "code": code,
                                                    "valid": valid, "avatars": AVATARS})


@app.get("/play/{code}", response_class=HTMLResponse)
async def play_page(request: Request, code: str):
    return templates.TemplateResponse("play.html", {"request": request, "code": code})


@app.get("/api/room/{code}/exists")
async def room_exists(code: str, request: Request):
    if not rate_ok("roomex:" + client_ip(request), 20, 60):
        return JSONResponse({"exists": False, "joinable": False, "error": "Demasiadas peticiones."},
                            status_code=429)
    room = ROOMS.get(code)
    return {"exists": room is not None, "joinable": room is not None and room.state == "lobby"}


@app.get("/api/quiz/{quiz_id}")
async def get_quiz(quiz_id: int, quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    u = get_user(user)
    role = u["role"] if u else "teacher"
    conn = db()
    q = conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
    conn.close()
    if not q:
        return JSONResponse({"ok": False}, status_code=404)
    if not owns_quiz(user, role, q["owner"]):
        return JSONResponse({"ok": False}, status_code=403)
    return {"ok": True, "title": q["title"], "theme": q["theme"], "kind": q["kind"],
            "folder": q["folder"] or "General", "questions": json.loads(q["questions"])}


# ---------------------------------------------------------------------------
# API REST v1 con token Bearer (para gestionar desde Claude Code)
# ---------------------------------------------------------------------------
QUESTION_SCHEMA = {
    "choice": {"type": "choice", "text": "enunciado", "options": ["a", "b", "c", "d"],
               "answer": "indice 0-3 de la correcta", "time": 20, "image": "(opcional URL)"},
    "truefalse": {"type": "truefalse", "text": "afirmacion", "options": ["Verdadero", "Falso"],
                  "answer": "0=Verdadero, 1=Falso", "time": 15},
    "multi": {"type": "multi", "text": "enunciado", "options": ["a", "b", "c", "d"],
              "answers": "lista de indices correctos, p.ej [0,2]", "time": 25},
    "fill": {"type": "fill", "text": "frase con ____", "answer": "palabra",
             "accepted": "(opcional) lista de respuestas validas", "time": 20},
    "numeric": {"type": "numeric", "text": "enunciado", "answer": "numero",
                "tol": "tolerancia +/-", "min": 0, "max": 100, "time": 20},
    "order": {"type": "order", "text": "enunciado", "items": ["primero", "segundo", "tercero"],
              "time": 25, "note": "items en el ORDEN CORRECTO; se muestran barajados"},
}


def token_owner(request: Request) -> Optional[dict]:
    """Devuelve {'owner','role'} si el header Authorization: Bearer <token> es válido."""
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        return None
    tok = h[7:].strip()
    conn = db()
    r = conn.execute("SELECT owner FROM api_tokens WHERE token=?", (tok,)).fetchone()
    if r:
        conn.execute("UPDATE api_tokens SET last_used=? WHERE token=?",
                     (time.strftime("%Y-%m-%d %H:%M"), tok))
        conn.commit()
    conn.close()
    if not r:
        return None
    u = get_user(r["owner"])
    return {"owner": r["owner"], "role": u["role"] if u else "teacher"}


def _api_quiz_row(q):
    return {"id": q["id"], "title": q["title"], "theme": q["theme"], "kind": q["kind"],
            "folder": q["folder"] or "General", "n": len(json.loads(q["questions"]))}


@app.get("/api/v1/whoami")
async def api_whoami(request: Request):
    a = token_owner(request)
    if not a:
        return JSONResponse({"ok": False, "error": "Token inválido o ausente."}, status_code=401)
    return {"ok": True, "user": a["owner"], "role": a["role"], "app": "Quizly"}


@app.get("/api/v1/schema")
async def api_schema(request: Request):
    if not token_owner(request):
        return JSONResponse({"ok": False, "error": "Token inválido."}, status_code=401)
    return {"ok": True, "question_types": QUESTION_SCHEMA,
            "quiz_format": {"title": "str", "theme": "str", "folder": "str",
                            "kind": "test|mixto|...", "questions": "[ ...preguntas... ]"},
            "endpoints": {
                "quizzes": ["GET /api/v1/quizzes", "GET /api/v1/quizzes/{id}",
                            "POST /api/v1/quizzes", "PUT /api/v1/quizzes/{id}",
                            "DELETE /api/v1/quizzes/{id}", "POST /api/v1/quizzes/{id}/duplicate"],
                "rooms_live": ["POST /api/v1/rooms", "GET /api/v1/rooms/{code}",
                               "POST /api/v1/rooms/{code}/config", "POST /api/v1/rooms/{code}/load {quiz_id}",
                               "POST /api/v1/rooms/{code}/start", "POST /api/v1/rooms/{code}/next",
                               "POST /api/v1/rooms/{code}/reveal", "POST /api/v1/rooms/{code}/end",
                               "POST /api/v1/rooms/{code}/kick {pid}"],
                "analytics": ["GET /api/v1/results", "GET /api/v1/results/{id}"],
                "admin_only": ["GET /api/v1/teachers", "POST /api/v1/teachers",
                               "DELETE /api/v1/teachers/{username}"],
                "account": ["POST /api/v1/password", "GET /api/v1/whoami"]}}


@app.get("/api/v1/quizzes")
async def api_list(request: Request):
    a = token_owner(request)
    if not a:
        return JSONResponse({"ok": False, "error": "Token inválido."}, status_code=401)
    rows = visible_quizzes(a["owner"], a["role"])
    return {"ok": True, "quizzes": [_api_quiz_row(q) for q in rows]}


@app.get("/api/v1/quizzes/{quiz_id}")
async def api_get(quiz_id: int, request: Request):
    a = token_owner(request)
    if not a:
        return JSONResponse({"ok": False, "error": "Token inválido."}, status_code=401)
    conn = db()
    q = conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
    conn.close()
    if not q:
        return JSONResponse({"ok": False, "error": "No existe."}, status_code=404)
    if not owns_quiz(a["owner"], a["role"], q["owner"]):
        return JSONResponse({"ok": False, "error": "No autorizado."}, status_code=403)
    return {"ok": True, "quiz": {"id": q["id"], "title": q["title"], "theme": q["theme"],
            "kind": q["kind"], "folder": q["folder"] or "General",
            "questions": json.loads(q["questions"])}}


@app.post("/api/v1/quizzes")
async def api_create(request: Request):
    a = token_owner(request)
    if not a:
        return JSONResponse({"ok": False, "error": "Token inválido."}, status_code=401)
    title, theme, kind, folder, questions = _parse_quiz(await request.json())
    if not title or not questions:
        return JSONResponse({"ok": False, "error": "Faltan 'title' o 'questions'."}, status_code=400)
    conn = db()
    cur = conn.execute("INSERT INTO quizzes (title, theme, kind, questions, owner, folder) VALUES (?,?,?,?,?,?)",
                       (title, theme, kind, json.dumps(questions, ensure_ascii=False), a["owner"], folder))
    conn.commit()
    qid = cur.lastrowid
    conn.close()
    return {"ok": True, "id": qid}


@app.put("/api/v1/quizzes/{quiz_id}")
async def api_update(quiz_id: int, request: Request):
    a = token_owner(request)
    if not a:
        return JSONResponse({"ok": False, "error": "Token inválido."}, status_code=401)
    title, theme, kind, folder, questions = _parse_quiz(await request.json())
    if not title or not questions:
        return JSONResponse({"ok": False, "error": "Faltan 'title' o 'questions'."}, status_code=400)
    conn = db()
    q = conn.execute("SELECT owner FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
    if not q:
        conn.close()
        return JSONResponse({"ok": False, "error": "No existe."}, status_code=404)
    if not owns_quiz(a["owner"], a["role"], q["owner"]):
        conn.close()
        return JSONResponse({"ok": False, "error": "No autorizado."}, status_code=403)
    cur = conn.execute("UPDATE quizzes SET title=?, theme=?, kind=?, questions=?, folder=? WHERE id=?",
                       (title, theme, kind, json.dumps(questions, ensure_ascii=False), folder, quiz_id))
    conn.commit()
    found = cur.rowcount
    conn.close()
    return JSONResponse({"ok": bool(found)}, status_code=200 if found else 404)


@app.delete("/api/v1/quizzes/{quiz_id}")
async def api_delete(quiz_id: int, request: Request):
    a = token_owner(request)
    if not a:
        return JSONResponse({"ok": False, "error": "Token inválido."}, status_code=401)
    conn = db()
    q = conn.execute("SELECT owner FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
    if not q:
        conn.close()
        return JSONResponse({"ok": False, "error": "No existe."}, status_code=404)
    if not owns_quiz(a["owner"], a["role"], q["owner"]):
        conn.close()
        return JSONResponse({"ok": False, "error": "No autorizado."}, status_code=403)
    cur = conn.execute("DELETE FROM quizzes WHERE id=?", (quiz_id,))
    conn.commit()
    found = cur.rowcount
    conn.close()
    return {"ok": bool(found)}


@app.post("/api/v1/quizzes/{quiz_id}/duplicate")
async def api_duplicate(quiz_id: int, request: Request):
    a = token_owner(request)
    if not a:
        return JSONResponse({"ok": False, "error": "Token inválido."}, status_code=401)
    conn = db()
    q = conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
    if not q:
        conn.close()
        return JSONResponse({"ok": False}, status_code=404)
    if not owns_quiz(a["owner"], a["role"], q["owner"]):
        conn.close()
        return JSONResponse({"ok": False, "error": "No autorizado."}, status_code=403)
    cur = conn.execute("INSERT INTO quizzes (title, theme, kind, questions, owner, folder) VALUES (?,?,?,?,?,?)",
                       (q["title"] + " (copia)", q["theme"], q["kind"], q["questions"], a["owner"], q["folder"]))
    conn.commit()
    nid = cur.lastrowid
    conn.close()
    return {"ok": True, "id": nid}


# ---- API: salas en directo -------------------------------------------------
def _room_state(room: "Room") -> dict:
    q = room.current_question
    return {"ok": True, "code": room.code, "state": room.state,
            "count": len(room.players), "q_index": room.q_index + 1 if room.q_index >= 0 else 0,
            "q_total": len(room.quiz["questions"]) if room.quiz else 0,
            "quiz_title": room.quiz["title"] if room.quiz else None,
            "has_next": bool(room.quiz) and room.q_index + 1 < len(room.quiz["questions"]),
            "teams_on": room.teams_on,
            "players": [{"pid": p.pid, "name": p.name, "score": p.score, "team": p.team,
                         "answered": p.answered} for p in room.players.values()],
            "scoreboard": room.scoreboard(), "team_board": room.team_board(),
            "current_question": ({"text": q["text"], "type": q["type"]} if q else None)}


@app.post("/api/v1/rooms")
async def api_room_create(request: Request):
    a = token_owner(request)
    if not a:
        return JSONResponse({"ok": False, "error": "Token inválido."}, status_code=401)
    code = new_code()
    room = Room(code)
    room.owner = a["owner"]
    ROOMS[code] = room
    host = request.headers.get("host", "quizly.dockerlabs.es")
    base = f"https://{host}"
    return {"ok": True, "code": code, "join_url": f"{base}/join/{code}", "host_url": f"{base}/host/{code}"}


@app.get("/api/v1/rooms/{code}")
async def api_room_state(code: str, request: Request):
    a = token_owner(request)
    if not a:
        return JSONResponse({"ok": False, "error": "Token inválido."}, status_code=401)
    room = ROOMS.get(code)
    if not room:
        return JSONResponse({"ok": False, "error": "Sala no encontrada."}, status_code=404)
    if not owns_room(a["owner"], a["role"], room):
        return JSONResponse({"ok": False, "error": "No autorizado."}, status_code=403)
    return _room_state(room)


@app.post("/api/v1/rooms/{code}/config")
async def api_room_config(code: str, request: Request):
    a = token_owner(request)
    if not a:
        return JSONResponse({"ok": False, "error": "Token inválido."}, status_code=401)
    room = ROOMS.get(code)
    if not room:
        return JSONResponse({"ok": False}, status_code=404)
    if not owns_room(a["owner"], a["role"], room):
        return JSONResponse({"ok": False, "error": "No autorizado."}, status_code=403)
    data = await request.json()
    async with room.lock:
        if room.state == "lobby":
            room.teams_on = bool(data.get("teams_on", room.teams_on))
            room.n_teams = max(2, min(4, int(data.get("n_teams", room.n_teams))))
            room.shuffle_q = bool(data.get("shuffle_q", room.shuffle_q))
            room.shuffle_opts = bool(data.get("shuffle_opts", room.shuffle_opts))
            room.powerups = bool(data.get("powerups", room.powerups))
            if room.teams_on:
                for p in room.players.values():
                    if p.team not in [TEAMS[i][0] for i in range(room.n_teams)]:
                        p.team = room.team_assign()
            else:
                for p in room.players.values():
                    p.team = ""
            await room.send_host(room.lobby_payload())
            await room.broadcast_players(room.lobby_payload())
    return _room_state(room)


@app.post("/api/v1/rooms/{code}/load")
async def api_room_load(code: str, request: Request):
    a = token_owner(request)
    if not a:
        return JSONResponse({"ok": False, "error": "Token inválido."}, status_code=401)
    room = ROOMS.get(code)
    if not room:
        return JSONResponse({"ok": False}, status_code=404)
    if not owns_room(a["owner"], a["role"], room):
        return JSONResponse({"ok": False, "error": "No autorizado."}, status_code=403)
    quiz_id = int((await request.json()).get("quiz_id"))
    async with room.lock:
        res = load_quiz_into_room(room, quiz_id)
    if not res:
        return JSONResponse({"ok": False, "error": "Quiz no encontrado."}, status_code=404)
    await room.send_host({"type": "quiz_loaded", "title": res[0], "n": res[1]})
    return {"ok": True, "title": res[0], "n": res[1]}


async def _room_action(code: str, request: Request, action: str):
    a = token_owner(request)
    if not a:
        return JSONResponse({"ok": False, "error": "Token inválido."}, status_code=401)
    room = ROOMS.get(code)
    if not room:
        return JSONResponse({"ok": False}, status_code=404)
    if not owns_room(a["owner"], a["role"], room):
        return JSONResponse({"ok": False, "error": "No autorizado."}, status_code=403)
    async with room.lock:
        if action == "start" and room.quiz and room.state in ("lobby", "reveal"):
            room.started_at = time.strftime("%Y-%m-%d %H:%M")
            await room.broadcast_players({"type": "game_start"})
            await start_question(room)
        elif action == "next" and room.state == "reveal":
            await start_question(room)
        elif action == "reveal" and room.state == "question":
            if room.timer_task and not room.timer_task.done():
                room.timer_task.cancel()
            await reveal_question(room)
        elif action == "end":
            await end_game(room)
    return _room_state(room)


@app.post("/api/v1/rooms/{code}/start")
async def api_room_start(code: str, request: Request):
    return await _room_action(code, request, "start")


@app.post("/api/v1/rooms/{code}/next")
async def api_room_next(code: str, request: Request):
    return await _room_action(code, request, "next")


@app.post("/api/v1/rooms/{code}/reveal")
async def api_room_reveal(code: str, request: Request):
    return await _room_action(code, request, "reveal")


@app.post("/api/v1/rooms/{code}/end")
async def api_room_end(code: str, request: Request):
    return await _room_action(code, request, "end")


@app.post("/api/v1/rooms/{code}/kick")
async def api_room_kick(code: str, request: Request):
    a = token_owner(request)
    if not a:
        return JSONResponse({"ok": False, "error": "Token inválido."}, status_code=401)
    room = ROOMS.get(code)
    if not room:
        return JSONResponse({"ok": False}, status_code=404)
    if not owns_room(a["owner"], a["role"], room):
        return JSONResponse({"ok": False, "error": "No autorizado."}, status_code=403)
    pid = (await request.json()).get("pid")
    async with room.lock:
        pl = room.players.pop(pid, None)
        if pl and pl.ws:
            try:
                await pl.ws.close()
            except Exception:
                pass
        await room.send_host(room.lobby_payload())
        await room.broadcast_players(room.lobby_payload())
    return _room_state(room)


# ---- API: resultados / analítica ------------------------------------------
@app.get("/api/v1/results")
async def api_results(request: Request):
    if not token_owner(request):
        return JSONResponse({"ok": False, "error": "Token inválido."}, status_code=401)
    conn = db()
    rows = conn.execute("SELECT id, quiz_title, theme, mode, ended_at, data FROM results ORDER BY id DESC LIMIT 200").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = json.loads(r["data"])
        out.append({"id": r["id"], "title": r["quiz_title"], "theme": r["theme"], "mode": r["mode"],
                    "ended_at": r["ended_at"], "players": len(d.get("players", [])),
                    "winner": d["players"][0]["name"] if d.get("players") else None})
    return {"ok": True, "results": out}


@app.get("/api/v1/results/{rid}")
async def api_result_detail(rid: int, request: Request):
    if not token_owner(request):
        return JSONResponse({"ok": False, "error": "Token inválido."}, status_code=401)
    conn = db()
    r = conn.execute("SELECT * FROM results WHERE id=?", (rid,)).fetchone()
    conn.close()
    if not r:
        return JSONResponse({"ok": False}, status_code=404)
    return {"ok": True, "result": {"id": r["id"], "title": r["quiz_title"], "theme": r["theme"],
            "mode": r["mode"], "ended_at": r["ended_at"], "data": json.loads(r["data"])}}


# ---- API: profesores (solo token de admin) --------------------------------
@app.get("/api/v1/teachers")
async def api_teachers(request: Request):
    a = token_owner(request)
    if not a or a["role"] != "admin":
        return JSONResponse({"ok": False, "error": "Requiere token de admin."}, status_code=403)
    conn = db()
    rows = [dict(x) for x in conn.execute("SELECT username, role FROM users ORDER BY username").fetchall()]
    conn.close()
    return {"ok": True, "teachers": rows}


@app.post("/api/v1/teachers")
async def api_teacher_add(request: Request):
    a = token_owner(request)
    if not a or a["role"] != "admin":
        return JSONResponse({"ok": False, "error": "Requiere token de admin."}, status_code=403)
    data = await request.json()
    un = (data.get("username") or "").strip()
    pw = data.get("password") or ""
    if not un or len(pw) < 4:
        return JSONResponse({"ok": False, "error": "username y password (mín 4)."}, status_code=400)
    conn = db()
    try:
        conn.execute("INSERT INTO users (username, password, role) VALUES (?,?,?)", (un, hash_pw(pw), "teacher"))
        conn.commit()
    except IntegrityError:
        conn.close()
        return JSONResponse({"ok": False, "error": "Ya existe."}, status_code=400)
    conn.close()
    return {"ok": True}


@app.delete("/api/v1/teachers/{username}")
async def api_teacher_del(username: str, request: Request):
    a = token_owner(request)
    if not a or a["role"] != "admin":
        return JSONResponse({"ok": False, "error": "Requiere token de admin."}, status_code=403)
    if username == "admin":
        return JSONResponse({"ok": False, "error": "No se puede borrar admin."}, status_code=400)
    conn = db()
    conn.execute("DELETE FROM users WHERE username=? AND role!='admin'", (username,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/v1/password")
async def api_password(request: Request):
    a = token_owner(request)
    if not a:
        return JSONResponse({"ok": False, "error": "Token inválido."}, status_code=401)
    data = await request.json()
    cur, new = data.get("current", ""), data.get("new", "")
    u = get_user(a["owner"])
    if not u or u["password"] != hash_pw(cur):
        return JSONResponse({"ok": False, "error": "Contraseña actual incorrecta."}, status_code=400)
    if len(new) < 4:
        return JSONResponse({"ok": False, "error": "Mínimo 4 caracteres."}, status_code=400)
    conn = db()
    conn.execute("UPDATE users SET password=? WHERE username=?", (hash_pw(new), a["owner"]))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---- Gestión de tokens (sesión web) ---------------------------------------
@app.post("/admin/account/token")
async def create_token(request: Request, quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    label = ((await request.json()).get("label") or "Claude Code").strip()[:40]
    tok = "qz_" + secrets.token_urlsafe(24)
    conn = db()
    conn.execute("INSERT INTO api_tokens (token, owner, label, created_at) VALUES (?,?,?,?)",
                 (tok, user, label, time.strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    conn.close()
    return {"ok": True, "token": tok}


@app.post("/admin/account/token/delete")
async def del_token(request: Request, quizly_session: Optional[str] = Cookie(default=None)):
    user = read_session(quizly_session)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    tok = (await request.json()).get("token", "")
    conn = db()
    conn.execute("DELETE FROM api_tokens WHERE token=? AND owner=?", (tok, user))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# WebSockets
# ---------------------------------------------------------------------------
@app.websocket("/ws/host/{code}")
async def ws_host(ws: WebSocket, code: str):
    await ws.accept()
    user = read_session(ws.cookies.get("quizly_session"))
    if not user:
        await ws.send_json({"type": "error", "msg": "No autorizado"})
        await ws.close()
        return
    room = ROOMS.get(code)
    if not room:
        await ws.send_json({"type": "error", "msg": "Sala no encontrada"})
        await ws.close()
        return
    u = get_user(user)
    role = u["role"] if u else "teacher"
    if not owns_room(user, role, room):
        await ws.send_json({"type": "error", "msg": "No autorizado"})
        await ws.close()
        return
    room.host_ws = ws
    await ws.send_json({"type": "connected", "code": code})
    await ws.send_json(room.lobby_payload())
    try:
        while True:
            msg = await ws.receive_json()
            action = msg.get("action")
            async with room.lock:
                if action == "config":
                    if room.state == "lobby":
                        room.teams_on = bool(msg.get("teams_on", room.teams_on))
                        room.n_teams = max(2, min(4, int(msg.get("n_teams", room.n_teams))))
                        room.shuffle_q = bool(msg.get("shuffle_q", room.shuffle_q))
                        room.shuffle_opts = bool(msg.get("shuffle_opts", room.shuffle_opts))
                        room.powerups = bool(msg.get("powerups", room.powerups))
                        # reasignar equipos si hace falta
                        if room.teams_on:
                            for p in room.players.values():
                                if p.team not in [TEAMS[i][0] for i in range(room.n_teams)]:
                                    p.team = room.team_assign()
                        else:
                            for p in room.players.values():
                                p.team = ""
                        await ws.send_json(room.lobby_payload())
                        await room.broadcast_players(room.lobby_payload())
                elif action == "load_quiz":
                    res = load_quiz_into_room(room, int(msg.get("quiz_id")))
                    if res:
                        await ws.send_json({"type": "quiz_loaded", "title": res[0], "n": res[1]})
                elif action == "start":
                    if room.quiz and room.state in ("lobby", "reveal"):
                        room.started_at = time.strftime("%Y-%m-%d %H:%M")
                        await room.broadcast_players({"type": "game_start"})
                        await start_question(room)
                elif action == "next":
                    if room.state == "reveal":
                        await start_question(room)
                elif action == "reveal_now":
                    if room.state == "question":
                        if room.timer_task and not room.timer_task.done():
                            room.timer_task.cancel()
                        await reveal_question(room)
                elif action == "end":
                    await end_game(room)
                elif action == "kick":
                    pl = room.players.pop(msg.get("pid"), None)
                    if pl and pl.ws:
                        try:
                            await pl.ws.close()
                        except Exception:
                            pass
                    await ws.send_json(room.lobby_payload())
                    await room.broadcast_players(room.lobby_payload())
    except WebSocketDisconnect:
        room.host_ws = None
    except Exception:
        room.host_ws = None


@app.websocket("/ws/play/{code}")
async def ws_play(ws: WebSocket, code: str):
    await ws.accept()
    room = ROOMS.get(code)
    if not room:
        await ws.send_json({"type": "error", "msg": "Sala no encontrada"})
        await ws.close()
        return
    try:
        first = await ws.receive_json()
    except Exception:
        await ws.close()
        return
    if first.get("action") != "join":
        await ws.close()
        return

    req_pid = first.get("pid")
    req_token = first.get("token")
    name = clean_name(first.get("name"))
    avatar = clean_avatar(first.get("avatar"))
    want_team = first.get("team", "")

    player = room.players.get(req_pid) if req_pid else None
    if player is not None and player.token != req_token:
        # El pid es público (viaja en lobby_payload a toda la sala), pero el
        # token de reconexión no. Sin el token correcto no se concede la
        # identidad ajena: se trata como una incorporación nueva.
        player = None

    if player is None:
        if room.state != "lobby":
            await ws.send_json({"type": "error", "msg": "La partida ya ha empezado"})
            await ws.close()
            return
        existing = {p.name for p in room.players.values()}
        base, i = name, 2
        while name in existing:
            name = f"{base}{i}"
            i += 1
        team = ""
        if room.teams_on:
            valid_teams = [TEAMS[k][0] for k in range(room.n_teams)]
            team = want_team if want_team in valid_teams else room.team_assign()
        pid = secrets.token_hex(8)
        player = Player(pid, name, avatar, team)
        room.players[pid] = player
    player.ws = ws

    await ws.send_json({"type": "joined", "pid": player.pid, "token": player.token, "name": player.name,
                        "avatar": player.avatar, "team": player.team, "state": room.state})
    async with room.lock:
        await room.send_host(room.lobby_payload())
        await room.broadcast_players(room.lobby_payload())

    try:
        while True:
            msg = await ws.receive_json()
            action = msg.get("action")
            if action == "answer":
                if not rate_ok("ans:" + pid, 30, 30):
                    continue
                async with room.lock:
                    await submit_answer(room, player, msg.get("value"))
            elif action == "boost":
                if room.powerups and not player.boost_used and room.state == "question" and not player.answered:
                    player.boost = True
                    player.boost_used = True
                    await _safe_send(player, {"type": "boost_armed"})
            elif action == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        player.ws = None
        if room.state == "lobby":
            async with room.lock:
                room.players.pop(pid, None)
                await room.send_host(room.lobby_payload())
                await room.broadcast_players(room.lobby_payload())
    except Exception:
        player.ws = None
