#!/usr/bin/env python3
"""
CorbanX API - v6.1.0
Multi-credencial, round-robin, dashboard, log por empresa
"""

import asyncio
import logging
import random
import sqlite3
import time
import threading
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="CorbanX API", version="6.1.0")

# ─────────────────────────── CONFIG ───────────────────────────

BASE_URL    = "https://neocashpromotora.log.br"
API_TOKEN   = "f4527e02f3a1cf14f9e9212d556a749f08926e456329e98db2e2d98723d435ac"
ADMIN_TOKEN = "corbanx-admin-2026"
DB_PATH     = "/opt/corbanx-api/corbanx.db"

BANKS_CLT = [
    "V8_DIGITAL", "BANCO_PRATA_CELCOIN", "NOVO_SAQUE_CLT",
    "PRESENCA", "CREFAZ", "VCTEX", "TITAN", "MERCANTIL"
]

BANKS_FGTS = [
    "BANCO_PRATA_BMP", "BANCO_PRATA_QITECH_FGTS",
    "V8_DIGITAL_FGTS", "NOVO_SAQUE_FGTS", "DSV", "LOTUS"
]

BANKS_ENERGIA = ["CREFAZ_LUZ"]

POLLING_INTERVAL  = 5
POLLING_MAX       = 36   # 3 minutos
FILA_CHEIA_CHECKS = 24   # 2 minutos

# ─────────────────────────── DATABASE ─────────────────────────

def get_db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS credenciais (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                ativo INTEGER DEFAULT 1,
                ultimo_uso REAL DEFAULT 0,
                total_consultas INTEGER DEFAULT 0,
                falhas_consecutivas INTEGER DEFAULT 0,
                criado_em TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS consultas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                empresa TEXT NOT NULL,
                cpf TEXT NOT NULL,
                tipo TEXT NOT NULL,
                resultado TEXT,
                credencial_email TEXT,
                tempo_segundos REAL,
                bancos_consultados INTEGER DEFAULT 0,
                criado_em TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        db.commit()
    logger.info("Banco de dados inicializado")

init_db()

# ─────────────────────────── ROUND-ROBIN ──────────────────────

_rr_lock = threading.Lock()
_rr_index = 0

def get_next_credencial():
    global _rr_index
    with get_db() as db:
        rows = db.execute(
            "SELECT id, email, password FROM credenciais WHERE ativo=1 ORDER BY ultimo_uso ASC"
        ).fetchall()
    if not rows:
        raise HTTPException(status_code=503, detail="Nenhuma credencial ativa disponível")
    with _rr_lock:
        idx = _rr_index % len(rows)
        _rr_index += 1
    return rows[idx]  # (id, email, password)

def registrar_uso(cred_id: int, sucesso: bool):
    with get_db() as db:
        if sucesso:
            db.execute("""
                UPDATE credenciais
                SET ultimo_uso=?, total_consultas=total_consultas+1, falhas_consecutivas=0
                WHERE id=?
            """, (time.time(), cred_id))
        else:
            db.execute("""
                UPDATE credenciais
                SET falhas_consecutivas=falhas_consecutivas+1
                WHERE id=?
            """, (cred_id,))
            # Desativa se falhou 3x seguidas
            db.execute("""
                UPDATE credenciais SET ativo=0
                WHERE id=? AND falhas_consecutivas >= 3
            """, (cred_id,))
        db.commit()

def log_consulta(empresa, cpf, tipo, resultado, credencial_email, tempo, bancos):
    with get_db() as db:
        db.execute("""
            INSERT INTO consultas (empresa, cpf, tipo, resultado, credencial_email, tempo_segundos, bancos_consultados)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (empresa, cpf, tipo, resultado, credencial_email, tempo, bancos))
        db.commit()

# ─────────────────────────── MODELS ───────────────────────────

class ConsultaRequest(BaseModel):
    cpf: str
    token: str
    empresa: str = "desconhecida"
    banks: Optional[List[str]] = None
    base_url: Optional[str] = None
    name: Optional[str] = None
    phone: Optional[str] = None

class ConsultaEnergiaRequest(BaseModel):
    cpf: str
    token: str
    empresa: str = "desconhecida"
    nome: str
    cep: str
    phone: Optional[str] = None
    base_url: Optional[str] = None

class LimparFilaRequest(BaseModel):
    email: str
    password: str
    token: str
    base_url: Optional[str] = None

class CredencialCreate(BaseModel):
    email: str
    password: str
    admin_token: str

class CredencialUpdate(BaseModel):
    admin_token: str
    ativo: Optional[int] = None
    password: Optional[str] = None

# ─────────────────────────── HELPERS ──────────────────────────

def limpar_cpf(cpf: str) -> str:
    return cpf.replace(".", "").replace("-", "").strip()

def formatar_cpf(cpf: str) -> str:
    c = limpar_cpf(cpf)
    return f"{c[:3]}.{c[3:6]}.{c[6:9]}-{c[9:]}"

def formatar_phone(phone: str) -> str:
    if not phone:
        return ""
    digits = ''.join(filter(str.isdigit, phone))
    if digits.startswith("55") and len(digits) > 11:
        digits = digits[2:]
    if len(digits) == 11:
        return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"
    elif len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
    return phone

def gerar_phone_aleatorio() -> str:
    ddds = [11,12,13,14,15,16,17,18,19,21,22,24,27,28,31,32,33,34,35,37,38,
            41,42,43,44,45,46,47,48,49,51,53,54,55,61,62,63,64,65,66,67,68,
            69,71,73,74,75,77,79,81,82,83,84,85,86,87,88,89,91,92,93,94,95,96,98,99]
    ddd = random.choice(ddds)
    numero = random.randint(90000000, 99999999)
    return f"({ddd:02d}) 9{numero}"

def fix_status(status: str) -> str:
    if not status:
        return "FALHA_CONSULTA"
    s = status.upper()
    if s in ("COM_SALDO", "NAO_APROVADO", "NAO_AUTORIZADO", "SEM_SALDO", "SEM_AUTORIZACAO", "FALHA_CONSULTA"):
        return s
    if "COM_SALDO" in s:
        return "COM_SALDO"
    if "SEM_SALDO" in s:
        return "SEM_SALDO"
    if "APROVADO" in s:
        return "NAO_APROVADO"
    if "AUTORIZADO" in s:
        return "NAO_AUTORIZADO"
    return "FALHA_CONSULTA"

def extrair_margem_presenca(r: dict) -> tuple:
    margem = r.get("margem", "N/A")
    tabelas = r.get("presenca_tabelas", [])
    if tabelas:
        melhor = sorted(tabelas, key=lambda t: t.get("valorLiberado", 0), reverse=True)[0]
        parcela = f"R$ {melhor['valorParcela']:,.2f}".replace(",","X").replace(".",",").replace("X",".")
        prazo = str(melhor.get("prazo", ""))
        liberado = melhor.get("valorLiberado", 0)
        valor_liberado = f"R$ {liberado:,.2f}".replace(",","X").replace(".",",").replace("X",".")
        return margem, parcela, prazo, valor_liberado
    return margem, None, None, None

def definir_resultado_fgts(results: list) -> str:
    STATUS_SEM_AUTH = ("NAO_AUTORIZADO", "SEM_AUTORIZACAO")
    statuses = [fix_status(r.get("status", "")) for r in results]
    if "COM_SALDO" in statuses:
        return "pre_aprovado"
    if "SEM_SALDO" in statuses:
        return "sem_saldo"
    auth = [s for s in statuses if s in STATUS_SEM_AUTH]
    if auth and all(s in STATUS_SEM_AUTH or s in ("FALHA_CONSULTA", "NAO_APROVADO") for s in statuses):
        return "aguardando_autorizacao"
    return "sem_saldo"

def montar_anotacao(results: list, tipo: str, parcial: bool = False, total_banks: int = 0, all_banks: list = None) -> tuple:
    responderam  = {r.get("bank_name") for r in results}
    aprovados    = [r for r in results if fix_status(r.get("status","")) == "COM_SALDO"]
    reprovados   = [r for r in results if fix_status(r.get("status","")) in ("NAO_APROVADO","NAO_AUTORIZADO","SEM_SALDO","SEM_AUTORIZACAO")]
    falhas       = [r for r in results if fix_status(r.get("status","")) not in ("COM_SALDO","NAO_APROVADO","NAO_AUTORIZADO","SEM_SALDO","SEM_AUTORIZACAO")]

    def get_margem(r):
        try:
            val = r.get("margem","0") or "0"
            return float(str(val).replace("R$","").replace(".","").replace(",",".").strip())
        except:
            return 0.0

    aprovados.sort(key=get_margem, reverse=True)
    linhas = []

    if parcial:
        linhas.append(f"⏱️ Consultado por 3 minutos — {len(results)}/{total_banks} bancos responderam")
        linhas.append("")

    if aprovados:
        melhor = aprovados[0]
        banco  = melhor.get("bank_name","DESCONHECIDO")
        margem = melhor.get("margem","N/A")
        linhas.append("🔥 OPORTUNIDADE ENCONTRADA")
        linhas.append(f"🏦 Banco Principal: {banco}")
        linhas.append(f"💰 Margem: {margem}")
        resultado = "parcial" if parcial else "pre_aprovado"
    else:
        linhas.append("❌ SEM OPORTUNIDADE DISPONÍVEL")
        resultado = "sem_margem"

    linhas.append(f"\n📊 Detalhamento CorbanX {tipo.upper()}\n")

    for r in aprovados:
        banco = r.get("bank_name","?")
        nome  = r.get("name")
        linhas.append(f"✅ {banco}")
        if nome:
            linhas.append(f"Cliente: {nome}")
        if tipo == "FGTS":
            linhas.append(f"Saldo: {r.get('margem','N/A')}")
        elif banco == "PRESENCA":
            m, p, pr, vl = extrair_margem_presenca(r)
            linhas.append(f"Margem: {m}")
            if p:
                linhas.append(f"Parcela: {p}" + (f" | Prazo: {pr}x" if pr else ""))
            if vl:
                linhas.append(f"Valor Liberado: {vl}")
        else:
            margem = r.get("margem","N/A")
            parcela = r.get("valor_parcela") or r.get("saldo_24m")
            prazo   = r.get("prazo")
            vl      = r.get("valor_liberado")
            linhas.append(f"Margem: {margem}")
            if parcela:
                label = "Saldo 24m" if not r.get("valor_parcela") else "Parcela"
                linhas.append(f"{label}: {parcela}" + (f" | Prazo: {prazo}x" if prazo else ""))
            if vl:
                linhas.append(f"Valor Liberado: {vl}")
        linhas.append("")

    for r in reprovados:
        linhas.append(f"❌ {r.get('bank_name','?')}")
        linhas.append(f"Motivo: {r.get('resultado') or 'Sem informação'}")
        linhas.append("")

    for r in falhas:
        linhas.append(f"⚠️ {r.get('bank_name','?')} (FALHA_CONSULTA)")
        linhas.append(f"Motivo: {r.get('resultado') or 'Erro desconhecido'}")
        linhas.append("")

    if parcial and total_banks > 0 and all_banks:
        pendentes = [b for b in all_banks if b not in responderam]
        for b in pendentes:
            linhas.append(f"⏳ {b} — não respondeu em 3 minutos (ignorado)")

    return resultado, "\n".join(linhas).strip()

# ─────────────────────────── CORE ─────────────────────────────

def _make_session(api_url: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": api_url,
        "referer": f"{api_url}/login",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    })
    return s

def _login(session, email, password, api_url, cpf_clean):
    try:
        r = session.post(f"{api_url}/api/auth/login",
            json={"email": email, "password": password}, timeout=15)
        if r.status_code not in (200, 201):
            logger.warning(f"[{cpf_clean}] Login falhou ({email}): HTTP {r.status_code}")
            return False
        return True
    except Exception as e:
        logger.error(f"[{cpf_clean}] Erro login: {e}")
        return False

def _limpar_fila(session, api_url, cpf_clean):
    try:
        r = session.delete(f"{api_url}/api/multi-bank/queue", timeout=10)
        data = r.json()
        logger.info(f"[{cpf_clean}] Fila limpa: {data.get('message','')}")
    except Exception as e:
        logger.warning(f"[{cpf_clean}] Erro ao limpar fila: {e}")

def _consultar(session, payload, api_url):
    payload = {k: v for k, v in payload.items() if k != "maxWaitMs"}
    try:
        r = session.post(f"{api_url}/api/multi-bank/consult", json=payload, timeout=30)
        if r.status_code not in (200, 202):
            return None, f"HTTP {r.status_code}"
        return r.json().get("jobId"), None
    except Exception as e:
        return None, str(e)

def _polling(session, job_id, banks, cpf_clean, api_url, max_checks):
    last_results = []
    for attempt in range(1, max_checks + 1):
        time.sleep(POLLING_INTERVAL)
        try:
            sr = session.get(f"{api_url}/api/multi-bank/status/{job_id}", timeout=30)
            if sr.status_code in (401, 403):
                return "erro_sessao", last_results, False
            data = sr.json()
            status = data.get("status", "processing")
            last_results = data.get("results") or last_results
            logger.info(f"[{cpf_clean}] Polling {attempt}/{max_checks}: {status} | {len(last_results)}/{len(banks)} bancos")
            if status == "completed":
                return "completed", last_results, False
            if attempt == FILA_CHEIA_CHECKS and len(last_results) == 0:
                logger.warning(f"[{cpf_clean}] Fila cheia detectada — retornando fila_cheia")
                return "fila_cheia", last_results, True
        except Exception as e:
            logger.warning(f"[{cpf_clean}] Erro polling {attempt}: {e}")
    return "timeout", last_results, False

def _executar_sync(cpf: str, tipo: str, banks: list, extra_payload: dict, empresa: str, base_url: str = None) -> dict:
    cpf_clean  = limpar_cpf(cpf)
    cpf_fmt    = formatar_cpf(cpf)
    api_url    = base_url.rstrip("/") if base_url else BASE_URL
    inicio     = time.time()

    # Pega próxima credencial (round-robin)
    max_tentativas = 3
    cred = None
    for _ in range(max_tentativas):
        try:
            cred = get_next_credencial()
        except HTTPException:
            return {"resultado": "erro", "anotacao": "❌ Nenhuma credencial ativa disponível"}

        cred_id, email, password = cred
        session = _make_session(api_url)

        logger.info(f"[{cpf_clean}] Login com {email}")
        if not _login(session, email, password, api_url, cpf_clean):
            registrar_uso(cred_id, False)
            continue

        logger.info(f"[{cpf_clean}] Login OK")

        payload = {
            "cpf": cpf_fmt,
            "name": extra_payload.get("name", "CLIENTE CORBAN"),
            "birthDate": "", "motherName": "",
            "productType": tipo,
            "selectedBanks": banks,
            "userIP": "189.126.131.81",
            "phone": extra_payload.get("phone", gerar_phone_aleatorio()),
            "cep": extra_payload.get("cep", ""),
            "clearCache": False,
            "titanOperationalSystem": "Windows",
            "titanDeviceModel": "Desktop Windows"
        }

        job_id, err = _consultar(session, payload, api_url)
        if not job_id:
            registrar_uso(cred_id, False)
            continue

        logger.info(f"[{cpf_clean}] JobId: {job_id} | Credencial: {email}")

        status, last_results, fila_cheia = _polling(session, job_id, banks, cpf_clean, api_url, POLLING_MAX)

        if fila_cheia:
            # Tenta limpar e usar próxima credencial
            _limpar_fila(session, api_url, cpf_clean)
            registrar_uso(cred_id, False)
            continue

        registrar_uso(cred_id, True)
        tempo = time.time() - inicio

        if status == "erro_sessao":
            continue

        parcial = status != "completed"
        resultado, anotacao = montar_anotacao(last_results, tipo, parcial=parcial, total_banks=len(banks), all_banks=banks)

        if tipo == "FGTS" and last_results:
            resultado = definir_resultado_fgts(last_results)

        log_consulta(empresa, cpf_clean, tipo, resultado, email, round(tempo, 1), len(last_results))

        return {
            "resultado": resultado,
            "anotacao": anotacao,
            "job_id": job_id,
            "bancos_consultados": len(last_results),
            "tempo_segundos": round(tempo, 1),
            "credencial": email
        }

    tempo = time.time() - inicio
    log_consulta(empresa, cpf_clean, tipo, "fila_cheia", None, round(tempo, 1), 0)
    return {
        "resultado": "fila_cheia",
        "anotacao": "⏳ Fila de consultas cheia em todas as credenciais. Tente novamente em alguns minutos.",
        "bancos_consultados": 0
    }

# ─────────────────────────── ENDPOINTS API ────────────────────

@app.get("/")
async def health():
    with get_db() as db:
        total_creds = db.execute("SELECT COUNT(*) FROM credenciais WHERE ativo=1").fetchone()[0]
        total_hoje  = db.execute("SELECT COUNT(*) FROM consultas WHERE date(criado_em)=date('now','localtime')").fetchone()[0]
    return {"status": "online", "service": "corbanx-api", "version": "6.1.0",
            "credenciais_ativas": total_creds, "consultas_hoje": total_hoje}

@app.post("/simular_corbanx_clt")
async def simular_clt(req: ConsultaRequest):
    if req.token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")
    banks = req.banks or BANKS_CLT
    extra = {"name": req.name or "CLIENTE CORBAN", "phone": formatar_phone(req.phone) if req.phone else gerar_phone_aleatorio()}
    return await asyncio.to_thread(_executar_sync, req.cpf, "CLT", banks, extra, req.empresa, req.base_url)

@app.post("/simular_corbanx_fgts")
async def simular_fgts(req: ConsultaRequest):
    if req.token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")
    banks = req.banks or BANKS_FGTS
    extra = {"name": req.name or "CLIENTE CORBAN", "phone": formatar_phone(req.phone) if req.phone else gerar_phone_aleatorio()}
    return await asyncio.to_thread(_executar_sync, req.cpf, "FGTS", banks, extra, req.empresa, req.base_url)

@app.post("/simular_corbanx_energia")
async def simular_energia(req: ConsultaEnergiaRequest):
    if req.token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")
    extra = {"name": req.nome, "cep": req.cep, "phone": formatar_phone(req.phone) if req.phone else gerar_phone_aleatorio()}
    return await asyncio.to_thread(_executar_sync, req.cpf, "CLT", BANKS_ENERGIA, extra, req.empresa, req.base_url)

@app.post("/limpar_fila")
async def endpoint_limpar_fila(req: LimparFilaRequest):
    if req.token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")
    def _limpar():
        api_url = req.base_url.rstrip("/") if req.base_url else BASE_URL
        session = _make_session(api_url)
        try:
            r = session.post(f"{api_url}/api/auth/login",
                json={"email": req.email, "password": req.password}, timeout=15)
            if r.status_code not in (200, 201):
                return {"status": "erro", "message": f"Falha no login (HTTP {r.status_code})"}
        except Exception as e:
            return {"status": "erro", "message": str(e)}
        try:
            r = session.delete(f"{api_url}/api/multi-bank/queue", timeout=10)
            data = r.json()
            return {"status": "ok", "removed": data.get("removed", 0), "message": data.get("message", "")}
        except Exception as e:
            return {"status": "erro", "message": str(e)}
    return await asyncio.to_thread(_limpar)

# ─────────────────────────── ADMIN: CREDENCIAIS ───────────────

@app.get("/admin/credenciais")
async def listar_credenciais(admin_token: str):
    if admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Token admin inválido")
    with get_db() as db:
        rows = db.execute("""
            SELECT id, email, ativo, total_consultas, falhas_consecutivas, ultimo_uso, criado_em
            FROM credenciais ORDER BY id
        """).fetchall()
    return [{"id": r[0], "email": r[1], "ativo": bool(r[2]),
             "total_consultas": r[3], "falhas_consecutivas": r[4],
             "ultimo_uso": datetime.fromtimestamp(r[5]).strftime("%d/%m/%Y %H:%M") if r[5] else None,
             "criado_em": r[6]} for r in rows]

@app.post("/admin/credenciais")
async def criar_credencial(req: CredencialCreate):
    if req.admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Token admin inválido")
    try:
        with get_db() as db:
            db.execute("INSERT INTO credenciais (email, password) VALUES (?, ?)", (req.email, req.password))
            db.commit()
        return {"status": "ok", "message": f"Credencial {req.email} criada"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Email já cadastrado")

@app.delete("/admin/credenciais/{cred_id}")
async def deletar_credencial(cred_id: int, admin_token: str):
    if admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Token admin inválido")
    with get_db() as db:
        db.execute("DELETE FROM credenciais WHERE id=?", (cred_id,))
        db.commit()
    return {"status": "ok", "message": "Credencial removida"}

@app.patch("/admin/credenciais/{cred_id}")
async def atualizar_credencial(cred_id: int, req: CredencialUpdate):
    if req.admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Token admin inválido")
    with get_db() as db:
        if req.ativo is not None:
            db.execute("UPDATE credenciais SET ativo=?, falhas_consecutivas=0 WHERE id=?", (req.ativo, cred_id))
        if req.password:
            db.execute("UPDATE credenciais SET password=? WHERE id=?", (req.password, cred_id))
        db.commit()
    return {"status": "ok"}

# ─────────────────────────── ADMIN: DASHBOARD ─────────────────

@app.get("/admin/stats")
async def stats(admin_token: str, dias: int = 7):
    if admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Token admin inválido")
    with get_db() as db:
        total_hoje = db.execute(
            "SELECT COUNT(*) FROM consultas WHERE date(criado_em)=date('now','localtime')"
        ).fetchone()[0]
        aprovados_hoje = db.execute(
            "SELECT COUNT(*) FROM consultas WHERE date(criado_em)=date('now','localtime') AND resultado='pre_aprovado'"
        ).fetchone()[0]
        tempo_medio = db.execute(
            "SELECT AVG(tempo_segundos) FROM consultas WHERE date(criado_em)=date('now','localtime')"
        ).fetchone()[0]
        por_empresa = db.execute("""
            SELECT empresa, COUNT(*) as total,
                   SUM(CASE WHEN resultado='pre_aprovado' THEN 1 ELSE 0 END) as aprovados
            FROM consultas WHERE date(criado_em)=date('now','localtime')
            GROUP BY empresa ORDER BY total DESC
        """).fetchall()
        por_dia = db.execute(f"""
            SELECT date(criado_em) as dia, COUNT(*) as total,
                   SUM(CASE WHEN resultado='pre_aprovado' THEN 1 ELSE 0 END) as aprovados
            FROM consultas
            WHERE criado_em >= date('now','localtime','-{dias} days')
            GROUP BY dia ORDER BY dia DESC
        """).fetchall()
        credenciais = db.execute("""
            SELECT email, ativo, total_consultas, falhas_consecutivas
            FROM credenciais ORDER BY total_consultas DESC
        """).fetchall()

    return {
        "hoje": {
            "total": total_hoje,
            "aprovados": aprovados_hoje,
            "taxa": round(aprovados_hoje/total_hoje*100, 1) if total_hoje else 0,
            "tempo_medio": round(tempo_medio or 0, 1)
        },
        "por_empresa": [{"empresa": r[0], "total": r[1], "aprovados": r[2],
                         "taxa": round(r[2]/r[1]*100,1) if r[1] else 0} for r in por_empresa],
        "por_dia": [{"dia": r[0], "total": r[1], "aprovados": r[2]} for r in por_dia],
        "credenciais": [{"email": r[0], "ativo": bool(r[1]),
                         "total": r[2], "falhas": r[3]} for r in credenciais]
    }

# ─────────────────────────── FRONTEND ─────────────────────────

@app.get("/painel", response_class=HTMLResponse)
async def painel():
    html = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CorbanX Admin</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  body{background:#0f172a;font-family:'Inter',sans-serif}
  .card{background:#1e293b;border:1px solid #334155;border-radius:12px}
  .badge-on{background:#064e3b;color:#34d399;border:1px solid #065f46}
  .badge-off{background:#450a0a;color:#f87171;border:1px solid #7f1d1d}
  .badge-ok{background:#1e3a5f;color:#60a5fa}
  .badge-err{background:#450a0a;color:#f87171}
  .tab{padding:8px 16px;border-radius:8px;cursor:pointer;font-size:13px;transition:all .2s;color:#94a3b8}
  .tab.active{background:#6366f1;color:#fff}
  .tab:hover:not(.active){background:#1e293b;color:#e2e8f0}
  input,select{background:#0f172a;border:1px solid #334155;color:#e2e8f0;border-radius:8px;padding:8px 12px}
  input:focus,select:focus{outline:none;border-color:#6366f1}
  .log-line{font-family:monospace;font-size:11px;padding:2px 4px;border-bottom:1px solid #1e293b;white-space:pre-wrap;word-break:break-all}
  .log-err{color:#f87171}.log-warn{color:#fbbf24}.log-ok{color:#34d399}.log-def{color:#94a3b8}
  ::-webkit-scrollbar{width:6px;height:6px}
  ::-webkit-scrollbar-track{background:#0f172a}
  ::-webkit-scrollbar-thumb{background:#334155;border-radius:3px}
</style>
</head>
<body class="text-slate-200 min-h-screen">

<!-- LOGIN -->
<div id="loginScreen" class="min-h-screen flex items-center justify-center p-4">
  <div class="card p-8 w-full max-w-sm">
    <div class="flex flex-col items-center mb-6">
      <div class="w-14 h-14 rounded-2xl bg-indigo-600 flex items-center justify-center text-3xl mb-3">🏦</div>
      <h1 class="text-xl font-bold text-white">CorbanX Admin</h1>
      <p class="text-slate-400 text-sm">Painel de controle</p>
    </div>
    <div class="space-y-3">
      <input id="loginToken" type="password" placeholder="Token de acesso" style="width:100%" onkeydown="if(event.key==='Enter')doLogin()" />
      <button onclick="doLogin()" class="w-full bg-indigo-600 hover:bg-indigo-700 text-white py-2.5 rounded-lg font-medium transition" style="width:100%">Entrar</button>
      <p id="loginError" class="text-red-400 text-xs text-center hidden">Token inválido</p>
    </div>
  </div>
</div>

<!-- MAIN -->
<div id="mainPanel" class="hidden p-6">

<!-- Header -->
<div class="flex items-center justify-between mb-6">
  <div class="flex items-center gap-3">
    <div class="w-10 h-10 rounded-xl bg-indigo-600 flex items-center justify-center text-xl">🏦</div>
    <div><h1 class="text-xl font-bold text-white">CorbanX Admin</h1><p class="text-xs text-slate-400">v6.0</p></div>
  </div>
  <button onclick="doLogout()" class="text-slate-400 hover:text-white text-sm transition">Sair →</button>
</div>

<!-- Tabs -->
<div class="flex gap-2 mb-6">
  <button class="tab active" onclick="showTab('dashboard',this)">📊 Dashboard</button>
  <button class="tab" onclick="showTab('consultas',this)">📋 Consultas</button>
  <button class="tab" onclick="showTab('credenciais',this)">🔑 Credenciais</button>
  <button class="tab" onclick="showTab('logs',this)">🖥️ Logs</button>
</div>

<!-- TAB: DASHBOARD -->
<div id="tab-dashboard">
  <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
    <div class="card p-4 text-center"><p class="text-slate-400 text-xs mb-1">Consultas Hoje</p><p class="text-3xl font-bold text-white" id="statTotal">—</p></div>
    <div class="card p-4 text-center"><p class="text-slate-400 text-xs mb-1">Pré-aprovados</p><p class="text-3xl font-bold text-emerald-400" id="statAprovados">—</p></div>
    <div class="card p-4 text-center"><p class="text-slate-400 text-xs mb-1">Taxa Aprovação</p><p class="text-3xl font-bold text-indigo-400" id="statTaxa">—</p></div>
    <div class="card p-4 text-center"><p class="text-slate-400 text-xs mb-1">Tempo Médio</p><p class="text-3xl font-bold text-amber-400" id="statTempo">—</p></div>
  </div>
  <div class="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-6">
    <div class="card p-4 lg:col-span-2">
      <h2 class="text-sm font-semibold text-slate-300 mb-4">📈 Consultas por dia (7 dias)</h2>
      <canvas id="chartDia" height="120"></canvas>
    </div>
    <div class="card p-4">
      <h2 class="text-sm font-semibold text-slate-300 mb-4">🏢 Por empresa (hoje)</h2>
      <div id="empresaList" class="space-y-2 text-sm"></div>
    </div>
  </div>
  <div class="card p-4">
    <h2 class="text-sm font-semibold text-slate-300 mb-3">🔑 Status das credenciais</h2>
    <div id="credStatus" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2"></div>
  </div>
</div>

<!-- TAB: CONSULTAS -->
<div id="tab-consultas" class="hidden">
  <div class="card p-4 mb-4">
    <div class="flex flex-wrap gap-3 items-end">
      <div>
        <label class="text-xs text-slate-400 block mb-1">Período</label>
        <select id="filtPeriodo" style="width:130px" onchange="loadConsultas()">
          <option value="hoje">Hoje</option>
          <option value="7dias">7 dias</option>
          <option value="30dias">30 dias</option>
          <option value="tudo">Tudo</option>
        </select>
      </div>
      <div>
        <label class="text-xs text-slate-400 block mb-1">Empresa</label>
        <select id="filtEmpresa" style="width:160px" onchange="loadConsultas()">
          <option value="">Todas</option>
        </select>
      </div>
      <div>
        <label class="text-xs text-slate-400 block mb-1">Resultado</label>
        <select id="filtResultado" style="width:150px" onchange="loadConsultas()">
          <option value="">Todos</option>
          <option value="pre_aprovado">Pré-aprovado</option>
          <option value="sem_margem">Sem margem</option>
          <option value="fila_cheia">Fila cheia</option>
          <option value="erro">Erro</option>
        </select>
      </div>
      <button onclick="loadConsultas()" class="bg-indigo-600 hover:bg-indigo-700 text-white text-sm px-4 py-2 rounded-lg transition">Filtrar</button>
    </div>
  </div>
  <div class="card overflow-hidden">
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead><tr class="border-b border-slate-700 text-slate-400 text-xs">
          <th class="p-3 text-left">Data/Hora</th>
          <th class="p-3 text-left">Empresa</th>
          <th class="p-3 text-left">CPF</th>
          <th class="p-3 text-left">Tipo</th>
          <th class="p-3 text-left">Resultado</th>
          <th class="p-3 text-left">Bancos</th>
          <th class="p-3 text-left">Tempo</th>
          <th class="p-3 text-left">Credencial</th>
        </tr></thead>
        <tbody id="consultasBody"></tbody>
      </table>
    </div>
    <div id="consultasEmpty" class="hidden p-8 text-center text-slate-500 text-sm">Nenhuma consulta encontrada</div>
  </div>
</div>

<!-- TAB: CREDENCIAIS -->
<div id="tab-credenciais" class="hidden">
  <div class="flex justify-end mb-4">
    <button onclick="document.getElementById('modalAdd').classList.remove('hidden')"
      class="bg-indigo-600 hover:bg-indigo-700 text-white text-sm px-4 py-2 rounded-lg transition">+ Nova credencial</button>
  </div>
  <div class="card overflow-hidden">
    <table class="w-full text-sm">
      <thead><tr class="border-b border-slate-700 text-slate-400 text-xs">
        <th class="p-3 text-left">Email</th>
        <th class="p-3 text-left">Status</th>
        <th class="p-3 text-left">Consultas</th>
        <th class="p-3 text-left">Falhas</th>
        <th class="p-3 text-left">Último uso</th>
        <th class="p-3 text-left">Criado em</th>
        <th class="p-3 text-left">Ações</th>
      </tr></thead>
      <tbody id="credTabela"></tbody>
    </table>
  </div>
</div>

<!-- TAB: LOGS -->
<div id="tab-logs" class="hidden">
  <div class="flex items-center gap-3 mb-4">
    <select id="logLinhas" style="width:120px">
      <option value="50">50 linhas</option>
      <option value="100" selected>100 linhas</option>
      <option value="200">200 linhas</option>
      <option value="500">500 linhas</option>
    </select>
    <button onclick="loadLogs()" class="bg-indigo-600 hover:bg-indigo-700 text-white text-sm px-4 py-2 rounded-lg transition">🔄 Atualizar</button>
    <label class="flex items-center gap-2 text-sm text-slate-400 cursor-pointer">
      <input type="checkbox" id="autoRefreshLogs" class="w-4 h-4" onchange="toggleAutoRefresh()"> Auto-refresh 5s
    </label>
  </div>
  <div class="card p-0 overflow-hidden">
    <div id="logContainer" class="overflow-y-auto" style="max-height:600px;min-height:300px;padding:12px"></div>
  </div>
</div>

</div><!-- fim mainPanel -->

<!-- Modal Add -->
<div id="modalAdd" class="hidden fixed inset-0 bg-black/60 flex items-center justify-center z-50">
  <div class="card p-6 w-full max-w-md">
    <h3 class="font-semibold text-white mb-4">Nova Credencial CorbanX</h3>
    <div class="space-y-3">
      <input id="newEmail" type="email" placeholder="Email" style="width:100%" />
      <input id="newPassword" type="password" placeholder="Senha" style="width:100%" />
    </div>
    <div class="flex gap-2 mt-4">
      <button onclick="addCredencial()" class="flex-1 bg-indigo-600 hover:bg-indigo-700 text-white py-2 rounded-lg text-sm transition">Salvar</button>
      <button onclick="document.getElementById('modalAdd').classList.add('hidden')"
        class="flex-1 bg-slate-700 hover:bg-slate-600 text-white py-2 rounded-lg text-sm transition">Cancelar</button>
    </div>
  </div>
</div>

<script>
let chartInstance = null;
let autoRefreshInterval = null;
let currentTab = 'dashboard';

function savedToken(){ return localStorage.getItem('corbanx_token')||''; }
function token(){ return savedToken(); }

async function doLogin(){
  const t = document.getElementById('loginToken').value.trim();
  if(!t) return;
  const r = await fetch(`/admin/credenciais?admin_token=${t}`);
  if(r.ok){
    localStorage.setItem('corbanx_token',t);
    document.getElementById('loginScreen').classList.add('hidden');
    document.getElementById('mainPanel').classList.remove('hidden');
    document.getElementById('loginError').classList.add('hidden');
    loadAll();
  } else {
    document.getElementById('loginError').classList.remove('hidden');
  }
}

function doLogout(){
  localStorage.removeItem('corbanx_token');
  document.getElementById('mainPanel').classList.add('hidden');
  document.getElementById('loginScreen').classList.remove('hidden');
  document.getElementById('loginToken').value='';
}

function showTab(name, btn){
  currentTab = name;
  document.querySelectorAll('[id^=tab-]').forEach(el=>el.classList.add('hidden'));
  document.getElementById('tab-'+name).classList.remove('hidden');
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  if(name==='consultas') { loadEmpresas(); loadConsultas(); }
  if(name==='credenciais') loadCredenciais();
  if(name==='logs') loadLogs();
  if(name==='dashboard') loadStats();
}

async function loadAll(){
  await Promise.all([loadStats(), loadCredenciais()]);
}

async function loadStats(){
  const r = await fetch(`/admin/stats?admin_token=${token()}&dias=7`);
  if(!r.ok) return;
  const d = await r.json();
  document.getElementById('statTotal').textContent = d.hoje.total;
  document.getElementById('statAprovados').textContent = d.hoje.aprovados;
  document.getElementById('statTaxa').textContent = d.hoje.taxa+'%';
  document.getElementById('statTempo').textContent = d.hoje.tempo_medio+'s';

  const dias   = d.por_dia.map(x=>x.dia).reverse();
  const totais = d.por_dia.map(x=>x.total).reverse();
  const aprov  = d.por_dia.map(x=>x.aprovados).reverse();
  if(chartInstance) chartInstance.destroy();
  chartInstance = new Chart(document.getElementById('chartDia'),{
    type:'bar',
    data:{ labels:dias, datasets:[
      {label:'Total',data:totais,backgroundColor:'#6366f1aa',borderRadius:4},
      {label:'Aprovados',data:aprov,backgroundColor:'#34d399aa',borderRadius:4}
    ]},
    options:{responsive:true,
      plugins:{legend:{labels:{color:'#94a3b8',font:{size:11}}}},
      scales:{x:{ticks:{color:'#94a3b8'},grid:{color:'#1e293b'}},y:{ticks:{color:'#94a3b8'},grid:{color:'#334155'}}}
    }
  });

  const el = document.getElementById('empresaList');
  el.innerHTML = d.por_empresa.length ? d.por_empresa.map(e=>`
    <div class="flex items-center justify-between py-1.5 border-b border-slate-700">
      <span class="text-slate-300 truncate max-w-[120px] text-xs">${e.empresa}</span>
      <div class="flex gap-2 text-xs"><span class="text-white font-medium">${e.total}</span><span class="text-emerald-400">${e.taxa}%</span></div>
    </div>`).join('') : '<p class="text-slate-500 text-xs">Sem consultas hoje</p>';

  const cs = document.getElementById('credStatus');
  cs.innerHTML = d.credenciais.map(c=>`
    <div class="flex items-center justify-between p-3 bg-slate-900 rounded-lg">
      <div class="min-w-0 flex-1">
        <p class="text-xs text-white truncate">${c.email}</p>
        <p class="text-xs text-slate-500">${c.total} consultas</p>
      </div>
      <span class="ml-2 text-xs px-2 py-0.5 rounded-full ${c.ativo?'badge-on':'badge-off'}">${c.ativo?'ON':'OFF'}</span>
    </div>`).join('');
}

async function loadEmpresas(){
  const r = await fetch(`/admin/empresas?admin_token=${token()}`);
  if(!r.ok) return;
  const empresas = await r.json();
  const sel = document.getElementById('filtEmpresa');
  sel.innerHTML = '<option value="">Todas</option>' + empresas.map(e=>`<option value="${e}">${e}</option>`).join('');
}

async function loadConsultas(){
  const periodo   = document.getElementById('filtPeriodo').value;
  const empresa   = document.getElementById('filtEmpresa').value;
  const resultado = document.getElementById('filtResultado').value;
  let url = `/admin/consultas?admin_token=${token()}&periodo=${periodo}&limit=200`;
  if(empresa)   url += `&empresa=${encodeURIComponent(empresa)}`;
  if(resultado) url += `&resultado=${resultado}`;
  const r = await fetch(url);
  if(!r.ok) return;
  const rows = await r.json();
  const tbody = document.getElementById('consultasBody');
  const empty = document.getElementById('consultasEmpty');
  if(!rows.length){ tbody.innerHTML=''; empty.classList.remove('hidden'); return; }
  empty.classList.add('hidden');

  const cores = {
    pre_aprovado: 'badge-on',
    parcial: 'badge-on',
    sem_margem: 'badge-off',
    fila_cheia: 'text-amber-400 bg-amber-900/30',
    aguardando_autorizacao: 'text-blue-400 bg-blue-900/30',
    erro: 'badge-err'
  };

  tbody.innerHTML = rows.map(r=>`
    <tr class="border-b border-slate-800 hover:bg-slate-800/50 text-xs">
      <td class="p-3 text-slate-400 whitespace-nowrap">${r.criado_em?.slice(0,16)||'—'}</td>
      <td class="p-3 text-slate-300">${r.empresa||'—'}</td>
      <td class="p-3 font-mono text-slate-300">${r.cpf||'—'}</td>
      <td class="p-3 text-slate-400">${r.tipo||'—'}</td>
      <td class="p-3"><span class="px-2 py-0.5 rounded-full text-xs ${cores[r.resultado]||'text-slate-400'}">${r.resultado||'—'}</span></td>
      <td class="p-3 text-slate-400">${r.bancos||0}/8</td>
      <td class="p-3 text-slate-400">${r.tempo||0}s</td>
      <td class="p-3 text-slate-500 truncate max-w-[120px]">${(r.credencial||'—').split('@')[0]}</td>
    </tr>`).join('');
}

async function loadCredenciais(){
  const r = await fetch(`/admin/credenciais?admin_token=${token()}`);
  if(!r.ok) return;
  const creds = await r.json();

  // Tabela completa
  const tbody = document.getElementById('credTabela');
  if(tbody) tbody.innerHTML = creds.map(c=>`
    <tr class="border-b border-slate-800 hover:bg-slate-800/50 text-sm">
      <td class="p-3 text-white">${c.email}</td>
      <td class="p-3"><span class="text-xs px-2 py-0.5 rounded-full ${c.ativo?'badge-on':'badge-off'}">${c.ativo?'Ativo':'Pausado'}</span></td>
      <td class="p-3 text-slate-300">${c.total_consultas}</td>
      <td class="p-3 text-slate-400">${c.falhas_consecutivas}</td>
      <td class="p-3 text-slate-400">${c.ultimo_uso||'—'}</td>
      <td class="p-3 text-slate-400 text-xs">${c.criado_em?.slice(0,16)||'—'}</td>
      <td class="p-3">
        <div class="flex gap-1">
          <button onclick="toggleCredencial(${c.id},${c.ativo})"
            class="text-xs px-2 py-1 rounded ${c.ativo?'bg-slate-700 hover:bg-red-900':'bg-slate-700 hover:bg-emerald-900'} transition">
            ${c.ativo?'Pausar':'Ativar'}</button>
          <button onclick="deleteCredencial(${c.id})" class="text-xs px-2 py-1 rounded bg-slate-700 hover:bg-red-900 transition">🗑</button>
        </div>
      </td>
    </tr>`).join('') || '<tr><td colspan="7" class="p-6 text-center text-slate-500 text-sm">Nenhuma credencial</td></tr>';
}

async function loadLogs(){
  const linhas = document.getElementById('logLinhas').value;
  const r = await fetch(`/admin/logs?admin_token=${token()}&linhas=${linhas}`);
  if(!r.ok) return;
  const d = await r.json();
  const container = document.getElementById('logContainer');
  const lines = (d.logs||'').split('\n').filter(Boolean);
  container.innerHTML = lines.map(l=>{
    let cls = 'log-def';
    if(l.includes('ERROR')||l.includes('error')||l.includes('Falha')) cls='log-err';
    else if(l.includes('WARNING')||l.includes('WARNING')||l.includes('Fila cheia')) cls='log-warn';
    else if(l.includes('Login OK')||l.includes('completed')) cls='log-ok';
    return `<div class="log-line ${cls}">${l.replace(/</g,'&lt;')}</div>`;
  }).join('');
  container.scrollTop = container.scrollHeight;
}

function toggleAutoRefresh(){
  if(document.getElementById('autoRefreshLogs').checked){
    autoRefreshInterval = setInterval(loadLogs, 5000);
  } else {
    clearInterval(autoRefreshInterval);
  }
}

async function addCredencial(){
  const email = document.getElementById('newEmail').value;
  const password = document.getElementById('newPassword').value;
  if(!email||!password){alert('Preencha email e senha');return;}
  const r = await fetch('/admin/credenciais',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({email,password,admin_token:token()})
  });
  const d = await r.json();
  if(r.ok){
    document.getElementById('modalAdd').classList.add('hidden');
    document.getElementById('newEmail').value='';
    document.getElementById('newPassword').value='';
    loadCredenciais();
  } else { alert(d.detail||'Erro ao adicionar'); }
}

async function toggleCredencial(id,ativo){
  await fetch(`/admin/credenciais/${id}`,{
    method:'PATCH',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({admin_token:token(),ativo:ativo?0:1})
  });
  loadCredenciais();
  loadStats();
}

async function deleteCredencial(id){
  if(!confirm('Remover credencial?')) return;
  await fetch(`/admin/credenciais/${id}?admin_token=${token()}`,{method:'DELETE'});
  loadCredenciais();
}

// Auto-refresh dashboard a cada 30s
setInterval(()=>{
  if(savedToken()&&currentTab==='dashboard') loadStats();
},30000);

// Verifica token salvo
window.onload = async()=>{
  const t = savedToken();
  if(t){
    const r = await fetch(`/admin/credenciais?admin_token=${t}`);
    if(r.ok){
      document.getElementById('loginScreen').classList.add('hidden');
      document.getElementById('mainPanel').classList.remove('hidden');
      loadAll();
    } else { localStorage.removeItem('corbanx_token'); }
  }
};
</script>
</body>
</html>"""
    return HTMLResponse(html)
---
