#!/usr/bin/env python3
"""
CorbanX API - Wrapper multi-banco CLT + FGTS
Porta: 8004 | v4.3.0
"""

import asyncio
import logging
import requests
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional, List
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="CorbanX API", version="4.3.0")

BASE_URL = "https://corbanx-api-prod.up.railway.app"

BANKS_CLT = [
    "V8_DIGITAL",
    "BANCO_PRATA_CELCOIN",
    "BANCO_HUB",
    "HAPPY_CONSIG",
    "DREX",
    "NOVO_SAQUE_CLT",
    "PRESENCA",
    "FINTECH_DIGITAL",
    "MERCANTIL"
]

BANKS_FGTS = [
    "BANCO_PRATA_BMP",
    "BANCO_PRATA_QITECH_FGTS",
    "V8_DIGITAL_FGTS",
    "NOVO_SAQUE_FGTS",
    "DSV",
    "LOTUS"
]

POLLING_INTERVAL = 5
POLLING_MAX = 36        # 36 x 5s = 180s (3 minutos)
FILA_CHEIA_CHECKS = 24  # 24 x 5s = 120s (2 minutos) antes de considerar fila cheia


# ─────────────────────────── MODELS ───────────────────────────

class ConsultaRequest(BaseModel):
    cpf: str
    email: str
    password: str
    banks: Optional[List[str]] = None


# ─────────────────────────── HELPERS ──────────────────────────

def limpar_cpf(cpf: str) -> str:
    return cpf.replace(".", "").replace("-", "").strip()


def formatar_cpf(cpf: str) -> str:
    c = limpar_cpf(cpf)
    return f"{c[:3]}.{c[3:6]}.{c[6:9]}-{c[9:]}"


def fix_status(status: str) -> str:
    """Corrige status cortado que vem da API"""
    if not status:
        return "FALHA_CONSULTA"
    s = status.upper()
    if s in ("COM_SALDO", "NAO_APROVADO", "NAO_AUTORIZADO", "FALHA_CONSULTA"):
        return s
    if "SALDO" in s:
        return "COM_SALDO"
    if "APROVADO" in s:
        return "NAO_APROVADO"
    if "AUTORIZADO" in s:
        return "NAO_AUTORIZADO"
    return "FALHA_CONSULTA"


def extrair_margem_presenca(r: dict) -> tuple:
    """Extrai margem/parcela/prazo do PRESENCA via presenca_tabelas"""
    margem = r.get("margem", "N/A")
    parcela = None
    prazo = None
    tabelas = r.get("presenca_tabelas", [])
    if tabelas:
        melhor = sorted(tabelas, key=lambda t: t.get("valorLiberado", 0), reverse=True)[0]
        parcela = f"R$ {melhor['valorParcela']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        prazo = str(melhor.get("prazo", ""))
        liberado = melhor.get("valorLiberado", 0)
        valor_liberado = f"R$ {liberado:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return margem, parcela, prazo, valor_liberado
    return margem, None, None, None


def limpar_fila(session: requests.Session, cpf_clean: str):
    try:
        r = session.delete(f"{BASE_URL}/api/multi-bank/queue", timeout=10)
        data = r.json()
        logger.info(f"[{cpf_clean}] Fila limpa: {data.get('message', '')}")
    except Exception as e:
        logger.warning(f"[{cpf_clean}] Erro ao limpar fila: {e}")


def montar_anotacao(results: list, tipo: str, parcial: bool = False, total_banks: int = 0) -> tuple:
    responderam = {r.get("bank_name") for r in results}
    aprovados   = [r for r in results if fix_status(r.get("status", "")) == "COM_SALDO"]
    reprovados  = [r for r in results if fix_status(r.get("status", "")) in ("NAO_APROVADO", "NAO_AUTORIZADO")]
    falhas      = [r for r in results if fix_status(r.get("status", "")) not in ("COM_SALDO", "NAO_APROVADO", "NAO_AUTORIZADO")]

    def get_margem(r):
        try:
            val = r.get("margem", "0") or "0"
            return float(str(val).replace("R$", "").replace(".", "").replace(",", ".").strip())
        except Exception:
            return 0.0

    aprovados.sort(key=get_margem, reverse=True)

    linhas = []

    if parcial:
        responderam_n = len(results)
        total_n = total_banks if total_banks > 0 else responderam_n
        linhas.append(f"⏱️ Consultado por 3 minutos — {responderam_n}/{total_n} bancos responderam")
        linhas.append("")

    if aprovados:
        melhor = aprovados[0]
        banco  = melhor.get("bank_name", "DESCONHECIDO")
        margem = melhor.get("margem", "N/A")
        linhas.append("🔥 OPORTUNIDADE ENCONTRADA")
        linhas.append(f"🏦 Banco Principal: {banco}")
        linhas.append(f"💰 Margem: {margem}")
        resultado = "parcial" if parcial else "pre_aprovado"
    else:
        linhas.append("❌ SEM OPORTUNIDADE DISPONÍVEL")
        resultado = "sem_margem"

    linhas.append(f"\n📊 Detalhamento CorbanX {tipo.upper()}\n")

    for r in aprovados:
        banco  = r.get("bank_name", "?")
        nome   = r.get("name")
        linhas.append(f"✅ {banco}")
        if nome:
            linhas.append(f"Cliente: {nome}")

        if banco == "PRESENCA":
            margem, parcela, prazo, valor_liberado = extrair_margem_presenca(r)
            linhas.append(f"Margem: {margem}")
            if parcela:
                linhas.append(f"Parcela: {parcela}" + (f" | Prazo: {prazo}x" if prazo else ""))
            if valor_liberado:
                linhas.append(f"Valor Liberado: {valor_liberado}")
        else:
            margem  = r.get("margem", "N/A")
            parcela = r.get("valor_parcela") or r.get("saldo_24m")
            prazo   = r.get("prazo")
            valor_liberado = r.get("valor_liberado")
            linhas.append(f"Margem: {margem}")
            if parcela:
                label = "Saldo 24m" if not r.get("valor_parcela") else "Parcela"
                linhas.append(f"{label}: {parcela}" + (f" | Prazo: {prazo}x" if prazo else ""))
            if valor_liberado:
                linhas.append(f"Valor Liberado: {valor_liberado}")

        linhas.append("")

    for r in reprovados:
        banco  = r.get("bank_name", "?")
        motivo = r.get("resultado") or "Sem informação"
        linhas.append(f"❌ {banco}")
        linhas.append(f"Motivo: {motivo}")
        linhas.append("")

    for r in falhas:
        banco  = r.get("bank_name", "?")
        motivo = r.get("resultado") or "Erro desconhecido"
        linhas.append(f"⚠️ {banco} (FALHA_CONSULTA)")
        linhas.append(f"Motivo: {motivo}")
        linhas.append("")

    if parcial and total_banks > 0:
        all_banks = BANKS_CLT if tipo == "CLT" else BANKS_FGTS
        pendentes = [b for b in all_banks if b not in responderam]
        if pendentes:
            for b in pendentes:
                linhas.append(f"⏳ {b} — não respondeu em 3 minutos (ignorado)")

    return resultado, "\n".join(linhas).strip()


# ─────────────────────────── CORE SYNC ────────────────────────

def _polling(session, job_id, banks, cpf_clean, tipo, max_checks):
    """Roda o polling e retorna (status, last_results, fila_cheia)"""
    last_results = []
    for attempt in range(1, max_checks + 1):
        time.sleep(POLLING_INTERVAL)
        try:
            status_resp = session.get(
                f"{BASE_URL}/api/multi-bank/status/{job_id}",
                timeout=30
            )
            if status_resp.status_code in (401, 403):
                logger.warning(f"[{cpf_clean}] Sessão expirada no polling")
                return "erro_sessao", last_results, False

            status_data  = status_resp.json()
            status       = status_data.get("status", "processing")
            last_results = status_data.get("results") or last_results

            logger.info(f"[{cpf_clean}] Polling {attempt}/{max_checks}: {status} | {len(last_results)}/{len(banks)} bancos")

            if status == "completed":
                return "completed", last_results, False

            # Detecta fila cheia: 2 minutos sem nenhum banco responder
            if attempt == FILA_CHEIA_CHECKS and len(last_results) == 0:
                logger.warning(f"[{cpf_clean}] Fila cheia detectada após {FILA_CHEIA_CHECKS * POLLING_INTERVAL}s")
                return "fila_cheia", last_results, True

        except Exception as e:
            logger.warning(f"[{cpf_clean}] Erro polling {attempt}: {e}")

    return "timeout", last_results, False


def _consultar(session, cpf_fmt, tipo, banks):
    """Dispara a consulta e retorna job_id"""
    payload = {
        "cpf": cpf_fmt,
        "name": "", "birthDate": "", "motherName": "",
        "productType": tipo,
        "selectedBanks": banks,
        "userIP": "189.126.131.81",
        "phone": "", "cep": "", "clearCache": False,
        "maxWaitMs": 180000
    }
    resp = session.post(
        f"{BASE_URL}/api/multi-bank/consult-managed",
        json=payload,
        timeout=30
    )
    if resp.status_code not in (200, 202):
        return None, f"HTTP {resp.status_code}"
    job_id = resp.json().get("jobId")
    return job_id, None


def _executar_sync(cpf: str, email: str, password: str, tipo: str, banks: list) -> dict:
    cpf_clean = limpar_cpf(cpf)
    cpf_fmt   = formatar_cpf(cpf)
    session   = requests.Session()

    # ── LOGIN ──
    logger.info(f"[{cpf_clean}] Login ({email})")
    try:
        login_resp = session.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": email, "password": password},
            timeout=15
        )
        if login_resp.status_code not in (200, 201):
            return {"resultado": "erro", "anotacao": f"❌ Falha no login (HTTP {login_resp.status_code})"}
    except Exception as e:
        return {"resultado": "erro", "anotacao": f"❌ Erro de conexão no login: {e}"}

    logger.info(f"[{cpf_clean}] Login OK")

    # ── CONSULTA ──
    job_id, err = _consultar(session, cpf_fmt, tipo, banks)
    if not job_id:
        return {"resultado": "erro", "anotacao": f"❌ Falha na consulta: {err}"}

    logger.info(f"[{cpf_clean}] JobId: {job_id}")

    # ── POLLING (tentativa 1) ──
    status, last_results, fila_cheia = _polling(session, job_id, banks, cpf_clean, tipo, POLLING_MAX)

    # ── FILA CHEIA → LIMPA E REFAZ ──
    if fila_cheia:
        logger.info(f"[{cpf_clean}] Limpando fila e refazendo consulta...")
        limpar_fila(session, cpf_clean)
        time.sleep(2)

        job_id, err = _consultar(session, cpf_fmt, tipo, banks)
        if not job_id:
            return {"resultado": "erro", "anotacao": f"❌ Falha na segunda consulta após limpar fila: {err}"}

        logger.info(f"[{cpf_clean}] Segunda tentativa JobId: {job_id}")
        status, last_results, _ = _polling(session, job_id, banks, cpf_clean, tipo, POLLING_MAX)

    # ── RESULTADO ──
    if status == "completed":
        resultado, anotacao = montar_anotacao(last_results, tipo, parcial=False)
    elif status == "erro_sessao":
        return {"resultado": "erro", "anotacao": "❌ Sessão expirada durante consulta"}
    else:
        resultado, anotacao = montar_anotacao(last_results, tipo, parcial=True, total_banks=len(banks))

    return {
        "resultado": resultado,
        "anotacao": anotacao,
        "job_id": job_id,
        "bancos_consultados": len(last_results)
    }


# ─────────────────────────── ENDPOINTS ────────────────────────

@app.get("/")
async def health():
    return {"status": "online", "service": "corbanx-api", "version": "4.3.0"}


@app.post("/simular_corbanx_clt")
async def simular_clt(req: ConsultaRequest):
    banks = req.banks or BANKS_CLT
    return await asyncio.to_thread(_executar_sync, req.cpf, req.email, req.password, "CLT", banks)


@app.post("/simular_corbanx_fgts")
async def simular_fgts(req: ConsultaRequest):
    banks = req.banks or BANKS_FGTS
    return await asyncio.to_thread(_executar_sync, req.cpf, req.email, req.password, "FGTS", banks)
