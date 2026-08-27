#!/usr/bin/env python3
"""
CorbanX API - Wrapper multi-banco CLT + FGTS + Energia
Porta: 8004 | v4.4.0
"""

import asyncio
import logging
import requests
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional, List
import time
import random

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="CorbanX API", version="5.0.2")

BASE_URL = "https://neocashpromotora.log.br"

BANKS_CLT = [
    "V8_DIGITAL",
    "BANCO_PRATA_CELCOIN",
    "NOVO_SAQUE_CLT",
    "PRESENCA",
    "CREFAZ",
    "VCTEX",
    "TITAN",
    "C6_BANK",
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

BANKS_ENERGIA = ["CREFAZ_LUZ"]

POLLING_INTERVAL = 5
POLLING_MAX = 36        # 36 x 5s = 180s (3 minutos)
FILA_CHEIA_CHECKS = 24  # 24 x 5s = 120s (2 minutos)

STATUS_NEGADOS = ("NAO_APROVADO", "NAO_AUTORIZADO", "SEM_SALDO", "SEM_AUTORIZACAO")
STATUS_SEM_AUTORIZACAO = ("NAO_AUTORIZADO", "SEM_AUTORIZACAO")


# ─────────────────────────── MODELS ───────────────────────────

class ConsultaRequest(BaseModel):
    cpf: str
    email: str
    password: str
    banks: Optional[List[str]] = None
    base_url: Optional[str] = None
    name: Optional[str] = "Cliente"
    phone: Optional[str] = "(11) 99999-9999"


class ConsultaEnergiaRequest(BaseModel):
    cpf: str
    email: str
    password: str
    nome: str
    cep: str
    phone: Optional[str] = None
    base_url: Optional[str] = None


# ─────────────────────────── HELPERS ──────────────────────────

def limpar_cpf(cpf: str) -> str:
    return cpf.replace(".", "").replace("-", "").strip()


def formatar_cpf(cpf: str) -> str:
    c = limpar_cpf(cpf)
    return f"{c[:3]}.{c[3:6]}.{c[6:9]}-{c[9:]}"


def fix_status(status: str) -> str:
    if not status:
        return "FALHA_CONSULTA"
    s = status.upper()
    if s in ("COM_SALDO", "NAO_APROVADO", "NAO_AUTORIZADO", "SEM_SALDO", "SEM_AUTORIZACAO", "FALHA_CONSULTA"):
        return s
    if "COM_SALDO" in s or "SALDO" in s and "SEM" not in s:
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
        parcela = f"R$ {melhor['valorParcela']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        prazo = str(melhor.get("prazo", ""))
        liberado = melhor.get("valorLiberado", 0)
        valor_liberado = f"R$ {liberado:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return margem, parcela, prazo, valor_liberado
    return margem, None, None, None


def extrair_oferta_mercantil(r: dict) -> tuple:
    """Mercantil não usa os campos genéricos (valor_parcela/prazo/
    valor_liberado no nível raiz) — vem estruturado em
    mercantil_simulacao.tabelaFlexivel[].prestacao[], cada tabela pode
    ter mais de uma opção de prestação. Achado em produção (14/08) —
    Rodrigo reportou que o texto da oferta pro Mercantil só mostrava
    margem, sem parcela/prazo/valor liberado. Mesmo padrão de tratamento
    especial já usado pro PRESENCA (extrair_margem_presenca) — cada
    banco pode estruturar a resposta diferente."""
    margem = r.get("margem", "N/A")
    simulacao = r.get("mercantil_simulacao") or {}
    tabelas = simulacao.get("tabelaFlexivel") or []
    todas_prestacoes = []
    for tabela in tabelas:
        todas_prestacoes.extend(tabela.get("prestacao") or [])
    if todas_prestacoes:
        melhor = sorted(todas_prestacoes, key=lambda p: p.get("valorLiberado", 0) or 0, reverse=True)[0]
        parcela = f"R$ {melhor['valorParcela']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        prazo = str(melhor.get("quantidadeParcelas", ""))
        liberado = melhor.get("valorLiberado", 0)
        valor_liberado = f"R$ {liberado:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return margem, parcela, prazo, valor_liberado
    return margem, None, None, None


def gerar_phone_aleatorio() -> str:
    """Gera um número de celular aleatório válido (DDD + 9 dígitos)"""
    ddd = random.choice([
        11, 12, 13, 14, 15, 16, 17, 18, 19,  # SP
        21, 22, 24,  # RJ
        27, 28,  # ES
        31, 32, 33, 34, 35, 37, 38,  # MG
        41, 42, 43, 44, 45, 46,  # PR
        47, 48, 49,  # SC
        51, 53, 54, 55,  # RS
        61,  # DF
        62, 64,  # GO
        63,  # TO
        65, 66,  # MT
        67,  # MS
        68,  # AC
        69,  # RO
        71, 73, 74, 75, 77,  # BA
        79,  # SE
        81, 87,  # PE
        82,  # AL
        83,  # PB
        84,  # RN
        85, 88,  # CE
        86, 89,  # PI
        91, 93, 94,  # PA
        92, 97,  # AM
        95,  # RR
        96,  # AP
        98, 99,  # MA
    ])
    numero = random.randint(90000000, 99999999)
    return f"({ddd:02d}) 9{numero}"


def formatar_phone(phone: str) -> str:
    """Normaliza telefone para (DD) 9XXXX-XXXX ou (DD) XXXX-XXXX"""
    if not phone:
        return ""
    digits = ''.join(filter(str.isdigit, phone))
    # Remove 55 do início se vier com DDI
    if digits.startswith("55") and len(digits) > 11:
        digits = digits[2:]
    if len(digits) == 11:
        return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"
    elif len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
    return phone  # retorna original se não conseguir formatar


def limpar_fila(session: requests.Session, cpf_clean: str, api_url: str = None):
    url = api_url or BASE_URL
    try:
        r = session.delete(f"{url}/api/multi-bank/queue", timeout=10)
        data = r.json()
        logger.info(f"[{cpf_clean}] Fila limpa: {data.get('message', '')}")
    except Exception as e:
        logger.warning(f"[{cpf_clean}] Erro ao limpar fila: {e}")


def montar_anotacao(results: list, tipo: str, parcial: bool = False, total_banks: int = 0, all_banks: list = None) -> tuple:
    responderam = {r.get("bank_name") for r in results}
    aprovados   = [r for r in results if fix_status(r.get("status", "")) == "COM_SALDO"]
    reprovados  = [r for r in results if fix_status(r.get("status", "")) in STATUS_NEGADOS]
    falhas      = [r for r in results if fix_status(r.get("status", "")) not in ("COM_SALDO",) + STATUS_NEGADOS]

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
        banco = r.get("bank_name", "?")
        nome  = r.get("name")
        linhas.append(f"✅ {banco}")
        if nome:
            linhas.append(f"Cliente: {nome}")

        if tipo == "FGTS":
            saldo = r.get("margem", "N/A")
            linhas.append(f"Saldo: {saldo}")
        elif banco == "PRESENCA":
            margem, parcela, prazo, valor_liberado = extrair_margem_presenca(r)
            linhas.append(f"Margem: {margem}")
            if parcela:
                linhas.append(f"Parcela: {parcela}" + (f" | Prazo: {prazo}x" if prazo else ""))
            if valor_liberado:
                linhas.append(f"Valor Liberado: {valor_liberado}")
        elif banco == "MERCANTIL":
            margem, parcela, prazo, valor_liberado = extrair_oferta_mercantil(r)
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

    if parcial and total_banks > 0 and all_banks:
        pendentes = [b for b in all_banks if b not in responderam]
        if pendentes:
            for b in pendentes:
                linhas.append(f"⏳ {b} — não respondeu em 3 minutos (ignorado)")

    return resultado, "\n".join(linhas).strip()


def definir_resultado_fgts(results: list) -> str:
    """
    Inteligência FGTS:
    - COM_SALDO em qualquer banco → pre_aprovado
    - SEM_SALDO em qualquer banco → sem_saldo (prioridade sobre autorização)
    - Todos NAO_AUTORIZADO/SEM_AUTORIZACAO → aguardando_autorizacao
    """
    statuses = [fix_status(r.get("status", "")) for r in results]

    if "COM_SALDO" in statuses:
        return "pre_aprovado"

    if "SEM_SALDO" in statuses:
        return "sem_saldo"

    if all(s in STATUS_SEM_AUTORIZACAO for s in statuses if s not in ("FALHA_CONSULTA", "NAO_APROVADO")):
        autorizacao = [s for s in statuses if s in STATUS_SEM_AUTORIZACAO]
        if autorizacao:
            return "aguardando_autorizacao"

    return "sem_saldo"


# ─────────────────────────── CORE ─────────────────────────────

def _polling(session, job_id, banks, cpf_clean, max_checks, api_url=None):
    last_results = []
    for attempt in range(1, max_checks + 1):
        time.sleep(POLLING_INTERVAL)
        try:
            _url = api_url or BASE_URL
            sr = session.get(f"{_url}/api/multi-bank/status/{job_id}", timeout=30)
            if sr.status_code in (401, 403):
                return "erro_sessao", last_results, False

            data = sr.json()
            status = data.get("status", "processing")
            last_results = data.get("results") or last_results

            logger.info(f"[{cpf_clean}] Polling {attempt}/{max_checks}: {status} | {len(last_results)}/{len(banks)} bancos")

            if status == "completed":
                return "completed", last_results, False

            if attempt == FILA_CHEIA_CHECKS and len(last_results) == 0:
                logger.warning(f"[{cpf_clean}] Fila cheia detectada")
                return "fila_cheia", last_results, True

        except Exception as e:
            logger.warning(f"[{cpf_clean}] Erro polling {attempt}: {e}")

    return "timeout", last_results, False


def _consultar(session, payload, api_url=None):
    _url = api_url or BASE_URL
    payload = {k: v for k, v in payload.items() if k != "maxWaitMs"}
    resp = session.post(
        f"{_url}/api/multi-bank/consult",
        json=payload,
        timeout=30
    )
    if resp.status_code not in (200, 202):
        return None, f"HTTP {resp.status_code}"
    return resp.json().get("jobId"), None


def _login(session, email, password, cpf_clean, api_url=None):
    url = api_url or BASE_URL
    try:
        r = session.post(
            f"{url}/api/auth/login",
            json={"email": email, "password": password},
            timeout=15
        )
        if r.status_code not in (200, 201):
            return False
        return True
    except Exception as e:
        logger.error(f"[{cpf_clean}] Erro login: {e}")
        return False


def _executar_sync(cpf: str, email: str, password: str, tipo: str, banks: list, extra_payload: dict = None, base_url: str = None) -> dict:
    cpf_clean = limpar_cpf(cpf)
    cpf_fmt   = formatar_cpf(cpf)
    session   = requests.Session()
    api_url   = base_url.rstrip("/") if base_url else BASE_URL
    session.headers.update({
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": api_url,
        "referer": f"{api_url}/login",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    })

    logger.info(f"[{cpf_clean}] Login ({email})")
    if not _login(session, email, password, cpf_clean, api_url):
        return {"resultado": "erro", "anotacao": "❌ Falha no login"}
    logger.info(f"[{cpf_clean}] Login OK")

    payload = {
        "cpf": cpf_fmt,
        "name": extra_payload.get("name", "Cliente") if extra_payload else "Cliente",
        "birthDate": "", "motherName": "",
        "productType": tipo,
        "selectedBanks": banks,
        "userIP": "189.126.131.81",
        "phone": extra_payload.get("phone", "(11) 99999-9999") if extra_payload else "(11) 99999-9999",
        "cep": "", "clearCache": False,
        "maxWaitMs": 180000,
        "titanOperationalSystem": "Windows",
        "titanDeviceModel": "Desktop Windows"
    }
    if extra_payload:
        payload.update(extra_payload)

    job_id, err = _consultar(session, payload, api_url)
    if not job_id:
        return {"resultado": "erro", "anotacao": f"❌ Falha na consulta: {err}"}
    logger.info(f"[{cpf_clean}] JobId: {job_id}")

    status, last_results, fila_cheia = _polling(session, job_id, banks, cpf_clean, POLLING_MAX, api_url)

    if fila_cheia:
        logger.warning(f"[{cpf_clean}] Fila cheia detectada — retornando fila_cheia para retry")
        return {
            "resultado": "fila_cheia",
            "anotacao": "⏳ Fila de consultas cheia, tente novamente em alguns segundos",
            "job_id": job_id,
            "bancos_consultados": 0
        }

    if status == "erro_sessao":
        return {"resultado": "erro", "anotacao": "❌ Sessão expirada durante consulta"}

    parcial = status != "completed"
    resultado, anotacao = montar_anotacao(
        last_results, tipo,
        parcial=parcial,
        total_banks=len(banks),
        all_banks=banks
    )

    # Sobrescreve resultado com inteligência FGTS
    if tipo == "FGTS" and last_results:
        resultado = definir_resultado_fgts(last_results)
    return {
        "resultado": resultado,
        "anotacao": anotacao,
        "job_id": job_id,
        "bancos_consultados": len(last_results)
    }


# ─────────────────────────── ENDPOINTS ────────────────────────

class LimparFilaRequest(BaseModel):
    email: str
    password: str
    base_url: Optional[str] = None


@app.post("/limpar_fila")
async def endpoint_limpar_fila(req: LimparFilaRequest):
    """Limpa a fila do usuário na CorbanX"""
    def _limpar():
        session = requests.Session()
        try:
            login_resp = session.post(
                f"{api_url}/api/auth/login",
                json={"email": req.email, "password": req.password},
                timeout=15
            )
            if login_resp.status_code not in (200, 201):
                return {"status": "erro", "message": f"Falha no login (HTTP {login_resp.status_code})"}
        except Exception as e:
            return {"status": "erro", "message": f"Erro de conexão: {e}"}

        try:
            r = session.delete(f"{api_url}/api/multi-bank/queue", timeout=10)
            data = r.json()
            return {"status": "ok", "removed": data.get("removed", 0), "message": data.get("message", "")}
        except Exception as e:
            return {"status": "erro", "message": f"Erro ao limpar fila: {e}"}

    return await asyncio.to_thread(_limpar)


@app.get("/")
async def health():
    return {"status": "online", "service": "corbanx-api", "version": "5.0.2"}


@app.post("/simular_corbanx_clt")
async def simular_clt(req: ConsultaRequest):
    banks = req.banks or BANKS_CLT
    extra = {"name": req.name or "Cliente", "phone": formatar_phone(req.phone) if req.phone else gerar_phone_aleatorio()}
    return await asyncio.to_thread(_executar_sync, req.cpf, req.email, req.password, "CLT", banks, extra, req.base_url)


@app.post("/simular_corbanx_fgts")
async def simular_fgts(req: ConsultaRequest):
    banks = req.banks or BANKS_FGTS
    extra = {"name": req.name or "Cliente", "phone": formatar_phone(req.phone) if req.phone else gerar_phone_aleatorio()}
    return await asyncio.to_thread(_executar_sync, req.cpf, req.email, req.password, "FGTS", banks, extra, req.base_url)


@app.post("/simular_corbanx_energia")
async def simular_energia(req: ConsultaEnergiaRequest):
    extra = {"name": req.nome, "cep": req.cep, "phone": formatar_phone(req.phone or "")}
    return await asyncio.to_thread(_executar_sync, req.cpf, req.email, req.password, "CLT", BANKS_ENERGIA, extra, req.base_url)
