"""API de Gem Rush con autenticación JWT para Telegram Mini Apps.

Instalar:  pip install fastapi uvicorn pyjwt
Variables de entorno:
    BOT_TOKEN   token del bot (el NUEVO, tras hacer /revoke en BotFather)
    JWT_SECRET  clave larga y aleatoria. Genérala con:
                python -c "import secrets; print(secrets.token_urlsafe(48))"
    GAME_ORIGIN URL de tu juego, ej. https://tu-usuario.github.io
Ejecutar:  uvicorn api:app --host 0.0.0.0 --port 8000
"""
import hashlib
import hmac
import json
import os
import sqlite3
import time
from urllib.parse import parse_qsl

import jwt
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

BOT_TOKEN = os.environ["BOT_TOKEN"]
JWT_SECRET = os.environ["JWT_SECRET"]
GAME_ORIGIN = os.environ.get("GAME_ORIGIN", "*")

TOKEN_TTL = 60 * 60          # el JWT dura 1 hora
INITDATA_MAX_AGE = 60 * 60   # initData de Telegram válido por 1 hora
MAX_COINS_PER_GAME = 60      # tope de monedas por partida (anti-trampa básico)
MIN_SECONDS_BETWEEN_GAMES = 10

db = sqlite3.connect("game.db", check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, coins INTEGER DEFAULT 0)")
db.execute("CREATE TABLE IF NOT EXISTS last_game (user_id INTEGER PRIMARY KEY, ts REAL)")
db.commit()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[GAME_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
bearer = HTTPBearer()


def verify_init_data(init_data: str) -> dict:
    """Valida la firma que Telegram pone en initData y devuelve el usuario."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "initData sin firma")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(401, "Firma inválida")

    if time.time() - int(pairs.get("auth_date", 0)) > INITDATA_MAX_AGE:
        raise HTTPException(401, "initData caducado")

    return json.loads(pairs["user"])


def create_token(user_id: int) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + TOKEN_TTL},
        JWT_SECRET,
        algorithm="HS256",
    )


def current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> int:
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=["HS256"])
        return int(payload["sub"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token caducado, vuelve a iniciar sesión")
    except jwt.PyJWTError:
        raise HTTPException(401, "Token inválido")


class AuthIn(BaseModel):
    init_data: str


class GameIn(BaseModel):
    coins: int


@app.post("/auth")
def auth(body: AuthIn):
    user = verify_init_data(body.init_data)
    db.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user["id"],))
    db.commit()
    return {"token": create_token(user["id"]), "expires_in": TOKEN_TTL, "name": user.get("first_name")}


@app.get("/me")
def me(uid: int = Depends(current_user)):
    row = db.execute("SELECT coins FROM users WHERE id = ?", (uid,)).fetchone()
    friends = db.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id = ?", (uid,)).fetchone()[0] \
        if db.execute("SELECT name FROM sqlite_master WHERE name='referrals'").fetchone() else 0
    return {"coins": row[0] if row else 0, "friends": friends}


@app.post("/game-over")
def game_over(body: GameIn, uid: int = Depends(current_user)):
    """El juego envía las monedas de una partida; el servidor las limita y las suma."""
    now = time.time()
    last = db.execute("SELECT ts FROM last_game WHERE user_id = ?", (uid,)).fetchone()
    if last and now - last[0] < MIN_SECONDS_BETWEEN_GAMES:
        raise HTTPException(429, "Demasiado rápido")

    earned = max(0, min(body.coins, MAX_COINS_PER_GAME))
    db.execute("UPDATE users SET coins = coins + ? WHERE id = ?", (earned, uid))
    db.execute("INSERT OR REPLACE INTO last_game (user_id, ts) VALUES (?, ?)", (uid, now))
    db.commit()
    total = db.execute("SELECT coins FROM users WHERE id = ?", (uid,)).fetchone()[0]
    return {"earned": earned, "coins": total}


# ---------- Tareas: seguir el canal y el grupo de Telegram ----------
# Variables opcionales: TG_CHANNEL=@micanal  TG_GROUP=@migrupo
# IMPORTANTE: el bot debe ser administrador del canal y del grupo para poder verificar.
import urllib.parse
import urllib.request

TG_TASKS = {  # tarea -> (chat, recompensa)
    "tg_channel": (os.environ.get("TG_CHANNEL", "@TU_CANAL"), 100),
    "tg_group": (os.environ.get("TG_GROUP", "@TU_GRUPO"), 100),
}
db.execute("CREATE TABLE IF NOT EXISTS task_claims (user_id INTEGER, task TEXT, PRIMARY KEY (user_id, task))")
db.commit()


class TaskIn(BaseModel):
    task: str


def is_member(chat: str, user_id: int) -> bool:
    query = urllib.parse.urlencode({"chat_id": chat, "user_id": user_id})
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember?{query}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.load(r)
    except Exception:
        return False
    return bool(data.get("ok")) and data["result"]["status"] in ("member", "administrator", "creator")


@app.post("/tasks/claim")
def claim_task(body: TaskIn, uid: int = Depends(current_user)):
    if body.task not in TG_TASKS:
        raise HTTPException(400, "Tarea desconocida")
    chat, reward = TG_TASKS[body.task]
    if db.execute("SELECT 1 FROM task_claims WHERE user_id = ? AND task = ?", (uid, body.task)).fetchone():
        raise HTTPException(409, "Ya reclamaste esta tarea")
    if not is_member(chat, uid):
        raise HTTPException(403, "Aún no te has unido")
    db.execute("INSERT INTO task_claims (user_id, task) VALUES (?, ?)", (uid, body.task))
    db.execute("UPDATE users SET coins = coins + ? WHERE id = ?", (reward, uid))
    db.commit()
    total = db.execute("SELECT coins FROM users WHERE id = ?", (uid,)).fetchone()[0]
    return {"reward": reward, "coins": total}


# ---------- Retiros en TON (con aprobación manual) ----------
# El servidor NO guarda llaves privadas ni envía TON solo: registra la solicitud,
# descuenta las monedas y tú envías el pago desde tu billetera y lo marcas como pagado.
# Variables: COINS_PER_TON (def. 100000), MIN_TON (def. 1), ADMIN_KEY (clave larga para el panel admin)
import re
from decimal import Decimal, ROUND_DOWN

from fastapi import Header

COINS_PER_TON = int(os.environ.get("COINS_PER_TON", "100000"))
MIN_TON = Decimal(os.environ.get("MIN_TON", "1"))
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
TON_ADDR = re.compile(r"^((EQ|UQ)[A-Za-z0-9_-]{46}|-?[01]:[0-9a-fA-F]{64})$")

db.execute(
    "CREATE TABLE IF NOT EXISTS withdrawals ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, ton_address TEXT, coins INTEGER, "
    "ton_amount TEXT, status TEXT DEFAULT 'pending', tx_hash TEXT, created_at REAL)"
)
db.commit()


class WithdrawIn(BaseModel):
    ton_address: str
    ton_amount: float


class PaidIn(BaseModel):
    tx_hash: str


@app.post("/withdraw")
def withdraw(body: WithdrawIn, uid: int = Depends(current_user)):
    if not TON_ADDR.match(body.ton_address.strip()):
        raise HTTPException(400, "Dirección TON no válida")
    amount = Decimal(str(body.ton_amount)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if amount < MIN_TON:
        raise HTTPException(400, f"El mínimo es {MIN_TON} TON")
    if db.execute("SELECT 1 FROM withdrawals WHERE user_id = ? AND status = 'pending'", (uid,)).fetchone():
        raise HTTPException(409, "Ya tienes un retiro pendiente")

    coins = int(amount * COINS_PER_TON)
    # Descuento atómico: solo se resta si el saldo alcanza
    cur = db.execute("UPDATE users SET coins = coins - ? WHERE id = ? AND coins >= ?", (coins, uid, coins))
    if cur.rowcount == 0:
        raise HTTPException(400, "Saldo insuficiente")
    db.execute(
        "INSERT INTO withdrawals (user_id, ton_address, coins, ton_amount, created_at) VALUES (?, ?, ?, ?, ?)",
        (uid, body.ton_address.strip(), coins, str(amount), time.time()),
    )
    db.commit()
    left = db.execute("SELECT coins FROM users WHERE id = ?", (uid,)).fetchone()[0]
    return {"status": "pending", "coins": left}


@app.get("/withdrawals")
def my_withdrawals(uid: int = Depends(current_user)):
    rows = db.execute(
        "SELECT id, ton_amount, status, created_at FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT 20",
        (uid,),
    ).fetchall()
    return [{"id": r[0], "ton": r[1], "status": r[2], "date": r[3]} for r in rows]


def admin_only(x_admin_key: str = Header(default="")):
    if not ADMIN_KEY or not hmac.compare_digest(x_admin_key, ADMIN_KEY):
        raise HTTPException(403, "No autorizado")


@app.get("/admin/withdrawals", dependencies=[Depends(admin_only)])
def admin_list():
    rows = db.execute(
        "SELECT id, user_id, ton_address, ton_amount, created_at FROM withdrawals WHERE status = 'pending' ORDER BY id"
    ).fetchall()
    return [{"id": r[0], "user_id": r[1], "address": r[2], "ton": r[3], "date": r[4]} for r in rows]


@app.post("/admin/withdrawals/{wid}/paid", dependencies=[Depends(admin_only)])
def admin_paid(wid: int, body: PaidIn):
    """Llámalo DESPUÉS de enviar el TON desde tu billetera; guarda el hash de la transacción."""
    cur = db.execute(
        "UPDATE withdrawals SET status = 'paid', tx_hash = ? WHERE id = ? AND status = 'pending'", (body.tx_hash, wid)
    )
    db.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "Retiro no encontrado o ya procesado")
    return {"ok": True}


@app.post("/admin/withdrawals/{wid}/reject", dependencies=[Depends(admin_only)])
def admin_reject(wid: int):
    """Rechaza el retiro y devuelve las monedas al jugador."""
    row = db.execute("SELECT user_id, coins FROM withdrawals WHERE id = ? AND status = 'pending'", (wid,)).fetchone()
    if not row:
        raise HTTPException(404, "Retiro no encontrado o ya procesado")
    db.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = ?", (wid,))
    db.execute("UPDATE users SET coins = coins + ? WHERE id = ?", (row[1], row[0]))
    db.commit()
    return {"ok": True}
