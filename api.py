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
