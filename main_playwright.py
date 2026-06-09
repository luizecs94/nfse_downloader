"""
NFS-e Downloader — Todas as Filiais via Playwright (mês anterior).

Abre um navegador Chrome VISÍVEL para cada empresa que usa login/senha.
Quando o portal exibir CAPTCHA, resolva manualmente na janela aberta —
o script continua automaticamente após a resolução.

Empresas com CERTIFICADO são processadas via API ADN (sem navegador).
Empresas com LOGIN/SENHA são processadas via Playwright.

Uso:
    .\.venv\Scripts\python.exe main_playwright.py

Requisitos adicionais:
    pip install playwright
    playwright install chromium
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

ARQUIVO_EMPRESAS  = Path(__file__).parent / "empresas.json"
ADN_BASE_URL      = os.getenv("ADN_BASE_URL", "https://adn.nfse.gov.br")
DOWNLOAD_PATH     = os.getenv("DOWNLOAD_PATH", "downloads")
PATH_STRUCTURE    = os.getenv("PATH_STRUCTURE", "{CNPJ_TOMADOR}/{ANO}/{MES}")


# ------------------------------------------------------------------
# Período
# ------------------------------------------------------------------

def _periodo_mes_anterior() -> tuple[datetime, datetime]:
    hoje  = datetime.today()
    ultimo = hoje.replace(day=1) - timedelta(days=1)
    return ultimo.replace(day=1), ultimo


def _no_periodo(data: Optional[datetime], ini: datetime, fim: datetime) -> bool:
    if not data:
        return False
    return ini <= data <= fim.replace(hour=23, minute=59, second=59)


# ------------------------------------------------------------------
# Modo 1: API ADN com certificado (igual ao main_todas.py)
# ------------------------------------------------------------------

def _sincronizar_via_api(empresa: dict, inicio: datetime, fim: datetime) -> dict:
    from core.adn_service import AdnService
    from core.api_client import ApiClientNfse
    from core.certificado import GerenciadorCertificadoA1
    from core.downloader import Downloader
    from core.models import StatusDownload
    from core.storage import Storage

    storage = Storage(
        base_path=DOWNLOAD_PATH,
        path_structure=PATH_STRUCTURE,
        cnpj_tomador=empresa["cnpj"],
    )
    ult_nsu = storage.carregar_ultimo_nsu()

    xmls = pdfs = municipais = erros = 0
    pendentes = []

    with GerenciadorCertificadoA1(empresa["cert_path"], empresa["cert_senha"]) as cert_pem:
        with ApiClientNfse(cert=cert_pem, base_url=ADN_BASE_URL) as client:
            adn       = AdnService(client)
            downloader = Downloader(client)

            todas, ultimo_nsu = adn.sincronizar_todos(ult_nsu)
            notas = [n for n in todas if _no_periodo(n.data_emissao, inicio, fim)]

            for nota in notas:
                data = nota.data_emissao.strftime("%d/%m/%Y") if nota.data_emissao else "?"
                logger.info("  #%s | %s | R$ %.2f", nota.numero or nota.nsu, data, nota.valor)

                if storage.salvar_xml(nota):
                    xmls += 1
                else:
                    erros += 1

                conteudo, st = downloader.obter_pdf(nota)
                if st == StatusDownload.SUCESSO and conteudo:
                    storage.salvar_pdf(nota, conteudo)
                    pdfs += 1
                elif st == StatusDownload.MUNICIPAL_NECESSARIO:
                    pendentes.append(nota)
                    municipais += 1
                else:
                    erros += 1

            if pendentes:
                storage.registrar_pendentes_municipais(pendentes)
            storage.salvar_ultimo_nsu(ultimo_nsu)

    return {"notas": len(notas), "xmls": xmls, "pdfs": pdfs,
            "municipais": municipais, "erros": erros}


# ------------------------------------------------------------------
# Modo 2: Playwright (login/senha + CAPTCHA manual)
# ------------------------------------------------------------------

def _sincronizar_via_playwright(empresa: dict, inicio: datetime, fim: datetime) -> dict:
    from core.playwright_scraper import FalhaAutenticacaoError, PlaywrightScraper

    cnpj = empresa["cnpj"]
    nome = empresa["nome"]

    base = (
        Path(DOWNLOAD_PATH)
        / cnpj
        / inicio.strftime("%Y")
        / inicio.strftime("%m")
    )
    base.mkdir(parents=True, exist_ok=True)

    with PlaywrightScraper(cnpj=cnpj, senha=empresa["senha"]) as scraper:
        try:
            scraper.autenticar()
        except FalhaAutenticacaoError as exc:
            logger.error("[%s] %s", nome, exc)
            return {"notas": 0, "xmls": 0, "pdfs": 0, "municipais": 0, "erros": 1}

        res = scraper.listar_e_baixar_notas(
            tipo="recebidas",
            data_inicio=inicio.strftime("%d/%m/%Y"),
            data_fim=fim.strftime("%d/%m/%Y"),
            base_path=base,
            nome_empresa=nome,
            cnpj=cnpj,
        )

    return {
        "notas":      res["notas"],
        "xmls":       res["xmls"],
        "pdfs":       res["pdfs"],
        "municipais": res["captchas"],
        "erros":      res["erros"],
    }


# ------------------------------------------------------------------
# Carregamento de empresas
# ------------------------------------------------------------------

def _carregar_empresas() -> list[dict]:
    if not ARQUIVO_EMPRESAS.exists():
        raise SystemExit(
            f"\n[ERRO] '{ARQUIVO_EMPRESAS}' não encontrado.\n"
            "Copie empresas.example.json → empresas.json e preencha.\n"
        )
    with open(ARQUIVO_EMPRESAS, encoding="utf-8") as f:
        dados = json.load(f)

    validas = []
    for emp in dados:
        emp.pop("_comentario", None)
        cnpj = re.sub(r"\D", "", emp.get("cnpj", ""))
        if not cnpj:
            continue
        emp["cnpj"] = cnpj
        tem_cert  = bool(emp.get("cert_path") and emp.get("cert_senha"))
        tem_senha = bool(emp.get("senha"))
        if not tem_cert and not tem_senha:
            logger.warning("Empresa sem autenticação ignorada: %s", emp.get("nome", cnpj))
            continue
        validas.append(emp)
    return validas


# ------------------------------------------------------------------
# Resumo final
# ------------------------------------------------------------------

def _imprimir_resumo(resultados: list[dict]) -> None:
    sep = "=" * 62
    print(f"\n{sep}")
    print("  RESUMO GERAL — TODAS AS EMPRESAS")
    print(sep)
    tot_n = tot_x = tot_p = tot_c = tot_e = 0
    for r in resultados:
        ok = "✅" if r["erros"] == 0 else "⚠️ "
        linha = (
            f"  {ok} {r['nome'][:34]:<34} "
            f"notas:{r['notas']:>3}  xml:{r['xmls']:>3}  pdf:{r['pdfs']:>3}"
        )
        if r["municipais"]:
            linha += f"  captcha:{r['municipais']}"
        if r["erros"]:
            linha += f"  erros:{r['erros']}"
        print(linha)
        tot_n += r["notas"]; tot_x += r["xmls"]
        tot_p += r["pdfs"];  tot_c += r["municipais"]
        tot_e += r["erros"]
    print(sep)
    print(f"  TOTAL  notas:{tot_n:>3}  xml:{tot_x:>3}  pdf:{tot_p:>3}", end="")
    if tot_c: print(f"  captcha:{tot_c}", end="")
    if tot_e: print(f"  erros:{tot_e}", end="")
    print(f"\n{sep}\n")


# ------------------------------------------------------------------
# Ponto de entrada
# ------------------------------------------------------------------

def main() -> None:
    empresas = _carregar_empresas()
    if not empresas:
        raise SystemExit("\n[ERRO] Nenhuma empresa válida em empresas.json\n")

    inicio, fim = _periodo_mes_anterior()
    periodo = inicio.strftime("%m/%Y")

    print(f"\n{'=' * 62}")
    print(f"  NFS-e Playwright — Notas Recebidas — {periodo}")
    print(f"  Empresas: {len(empresas)}")
    print(f"{'=' * 62}")
    print("  ℹ️  Empresas COM certificado → API ADN (sem navegador)")
    print("  🌐 Empresas SEM certificado → Playwright (navegador visível)")
    print("  🔴 Se aparecer CAPTCHA → resolva na janela do Chrome")
    print(f"{'=' * 62}\n")

    resultados = []
    for emp in empresas:
        nome     = emp.get("nome", emp["cnpj"])
        tem_cert = bool(emp.get("cert_path") and emp.get("cert_senha"))
        metodo   = "API ADN" if tem_cert else "Playwright"

        print(f"\n{'─' * 62}")
        print(f"  {nome}  |  {emp['cnpj']}  |  {metodo}")
        print(f"{'─' * 62}")

        try:
            if tem_cert:
                res = _sincronizar_via_api(emp, inicio, fim)
            else:
                res = _sincronizar_via_playwright(emp, inicio, fim)
        except Exception as exc:
            logger.error("[%s] Erro inesperado: %s", nome, exc, exc_info=True)
            res = {"notas": 0, "xmls": 0, "pdfs": 0, "municipais": 0, "erros": 1}

        res["nome"] = nome
        resultados.append(res)

        print(
            f"  Notas: {res['notas']}  |  XMLs: {res['xmls']}  |  PDFs: {res['pdfs']}",
            end="",
        )
        if res["municipais"]:
            print(f"  |  Com CAPTCHA: {res['municipais']}", end="")
        if res["erros"]:
            print(f"  |  ⚠️  Erros: {res['erros']}", end="")
        print()

    _imprimir_resumo(resultados)


if __name__ == "__main__":
    main()
