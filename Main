#!/usr/bin/env python3
"""
CorbanX API - Wrapper multi-banco CLT + FGTS
Porta: 8004
Async + httpx para suportar múltiplas consultas simultâneas
"""

import asyncio
import logging
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional, List
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="CorbanX API", version="2.0.0")

BASE_URL = "https://cltx-backend-production.up.railway.app"

BANKS_CLT = [
    "V8_DIGITAL",
    "BANCO_PRATA_CELCOIN",
    "BANCO_HUB",
    "DREX",
    "PRESENCA",
    "CONSIGA",
    "MERCANTIL",
    "HAPPY_CONSIG"
]

BANKS_FGTS = [
    "FGTS_BANK1",
    "FGTS_BANK2",
    "FGTS_BANK3",
    "FGTS_BANK4"
]

POLLING_INTERVAL = 5    # segundos entre tentativas
POLLING_MAX = 24        # 24 x 5s = 120s (2 minutos)


# ─────────────────────────── MODELS ───────────────────────────

class ConsultaRequest(BaseModel):
    cpf: str
    email: str
    password: str
    banks: Optional[List[str]] = None


# ─────────────────────────── HELPERS ──────────────────────────

def limpar_cpf(cpf: str) -> str:
    return cpf.replace(".", "").replace("-", "").strip()


def montar_anotacao(results: list, tipo: str, parcial: bool = False, total_banks: int = 0) -> tuple[str, str]:
    responderam = {r.get("bank_name") for r in results}

    aprovados  = [r for r in results if r.get("status") == "COM_SALDO"]
    reprovados = [r for r in results if r.get("status") == "NAO_APROVADO"]
    falhas     = [r for r in results if r.get("status") not in ("COM_SALDO", "NAO_APROVADO")]

    # Ordena aprovados por margem decrescente
    def get_margem(r):
        try:
            val = r.get("margem", "0") or "0"
            return float(str(val).replace("R$", "").replace(".", "").replace(",", ".").strip())
        except Exception:
            return 0.0

    aprovados.sort(key=get_margem, reverse=True)

    linhas = []

    # Cabeçalho parcial
    if parcial:
        responderam_n = len(results)
        total_n = total_banks if total_banks > 0 else responderam_n
        linhas.append(f"⏱️ Consultado por 2 minutos — {responderam_n}/{total_n} bancos responderam")
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
        margem = r.get("margem", "N/A")
        parcela = r.get("valor_parcela") or r.get("saldo_24m")
        prazo  = r.get("prazo")
        nome   = r.get("name")
        linhas.append(f"✅ {banco}")
        if nome:
            linhas.append(f"Cliente: {nome}")
        linhas.append(f"Margem: {margem}")
        if parcela:
            label = "Saldo 24m" if not r.get("valor_parcela") else "Parcela"
            linhas.append(f"{label}: {parcela}" + (f" | Prazo: {prazo}x" if prazo else ""))
        linhas.append("")

    for r in reprovados:
        banco  = r.get("bank_name", "?")
        motivo = r.get("resultado") or "Sem informação"
        linhas.append(f"❌ {banco}")
        linhas.append(f"Motivo: {motivo}")
        linhas.append("")

    for r in falhas:
        banco  = r.get("bank_name", "?")
        status = r.get("status", "FALHA")
        motivo = r.get("resultado") or "Erro desconhecido"
        linhas.append(f"⚠️ {banco} ({status})")
        linhas.append(f"Motivo: {motivo}")
        linhas.append("")

    # Bancos que não responderam no tempo limite
    if parcial and total_banks > 0:
        all_banks = BANKS_CLT if tipo == "CLT" else BANKS_FGTS
        pendentes = [b for b in all_banks if b not in responderam]
        if pendentes:
            for b in pendentes:
                linhas.append(f"⏳ {b} — não respondeu em 2 minutos (ignorado)")

    return resultado, "\n".join(linhas).strip()


# ─────────────────────────── CORE ASYNC ───────────────────────

async def executar_consulta(cpf: str, email: str, password: str, tipo: str, banks: list) -> dict:
    cpf_clean = limpar_cpf(cpf)

    async with httpx.AsyncClient(timeout=30) as client:

        # ── LOGIN ──
        logger.info(f"[{cpf_clean}] Login ({email})")
        try:
            login_resp = await client.post(
                f"{BASE_URL}/api/auth/login",
                json={"email": email, "password": password}
            )
            if login_resp.status_code not in (200, 201):
                return {"resultado": "erro", "anotacao": f"❌ Falha no login (HTTP {login_resp.status_code})"}

            # Preserva cookies da sessão
            cookies = dict(login_resp.cookies)

        except Exception as e:
            return {"resultado": "erro", "anotacao": f"❌ Erro de conexão no login: {e}"}

        logger.info(f"[{cpf_clean}] Login OK")

        # ── CONSULTA ──
        payload = {
            "cpf": cpf_clean,
            "name": "", "birthDate": "", "motherName": "",
            "productType": tipo,
            "selectedBanks": banks,
            "gender": "MASCULINO",
            "userIP": "189.126.131.81",
            "phone": "", "email": ""
        }

        try:
            consult_resp = await client.post(
                f"{BASE_URL}/api/multi-bank/consult",
                json=payload,
                cookies=cookies
            )
            if consult_resp.status_code not in (200, 202):
                return {"resultado": "erro", "anotacao": f"❌ Falha na consulta (HTTP {consult_resp.status_code})"}

            job_id = consult_resp.json().get("jobId")
            if not job_id:
                return {"resultado": "erro", "anotacao": "❌ jobId não retornado pela CorbanX"}

        except Exception as e:
            return {"resultado": "erro", "anotacao": f"❌ Erro ao consultar: {e}"}

        logger.info(f"[{cpf_clean}] JobId: {job_id}")

        # ── POLLING ASYNC ──
        last_results = []

        for attempt in range(1, POLLING_MAX + 1):
            await asyncio.sleep(POLLING_INTERVAL)
            try:
                status_resp = await client.get(
                    f"{BASE_URL}/api/multi-bank/status/{job_id}",
                    cookies=cookies
                )

                # Re-login automático se sessão expirou
                if status_resp.status_code in (401, 403):
                    logger.warning(f"[{cpf_clean}] Sessão expirada, re-login...")
                    relogin = await client.post(
                        f"{BASE_URL}/api/auth/login",
                        json={"email": email, "password": password}
                    )
                    if relogin.status_code in (200, 201):
                        cookies = dict(relogin.cookies)
                        logger.info(f"[{cpf_clean}] Re-login OK")
                    continue

                status_data  = status_resp.json()
                status       = status_data.get("status", "processing")
                last_results = status_data.get("results") or last_results

                logger.info(f"[{cpf_clean}] Polling {attempt}/{POLLING_MAX}: {status} | {len(last_results)}/{len(banks)} bancos")

                if status == "completed":
                    resultado, anotacao = montar_anotacao(last_results, tipo, parcial=False)
                    return {
                        "resultado": resultado,
                        "anotacao": anotacao,
                        "job_id": job_id,
                        "bancos_consultados": len(last_results)
                    }

            except Exception as e:
                logger.warning(f"[{cpf_clean}] Erro polling {attempt}: {e}")

        # Timeout — retorna parcial
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
    return {"status": "online", "service": "corbanx-api", "version": "2.0.0"}


@app.post("/simular_corbanx_clt")
async def simular_clt(req: ConsultaRequest):
    banks = req.banks or BANKS_CLT
    return await executar_consulta(req.cpf, req.email, req.password, "CLT", banks)


@app.post("/simular_corbanx_fgts")
async def simular_fgts(req: ConsultaRequest):
    banks = req.banks or BANKS_FGTS
    return await executar_consulta(req.cpf, req.email, req.password, "FGTS", banks)
