"""
NFS-e Downloader — Todas as Empresas (mês anterior automático).

Lê a lista de empresas de `empresas.json` e baixa as notas recebidas
de cada uma, usando:

  ✅  certificado A1 disponível  → API oficial ADN
  ⚠️  apenas senha               → Portal web (scraping HTML)

Todos os downloads vão para a mesma pasta `downloads/` configurada em
DOWNLOAD_PATH (.env), organizados por CNPJ / ANO / MÊS.

Uso:
    .\.venv\Scripts\python.exe main_todas.py

Requisitos:
    - Arquivo `empresas.json` na raiz do projeto (ver empresas.example.json)
    - .env com pelo menos DOWNLOAD_PATH definido
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

ARQUIVO_EMPRESAS = Path(__file__).parent / "empresas.json"
ADN_BASE_URL_PADRAO = os.getenv("ADN_BASE_URL", "https://adn.nfse.gov.br")
DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "downloads")
PATH_STRUCTURE = os.getenv("PATH_STRUCTURE", "{CNPJ_TOMADOR}/{ANO}/{MES}")


# ------------------------------------------------------------------
# Período
# ------------------------------------------------------------------

def _periodo_mes_anterior() -> tuple[datetime, datetime]:
    hoje = datetime.today()
    ultimo = hoje.replace(day=1) - timedelta(days=1)
    return ultimo.replace(day=1), ultimo


def _no_periodo(data_emissao: Optional[datetime], inicio: datetime, fim: datetime) -> bool:
    if not data_emissao:
        return False
    return inicio <= data_emissao <= fim.replace(hour=23, minute=59, second=59)


# ------------------------------------------------------------------
# Modo 1: API ADN (certificado)
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
    logger.info("[%s] Modo API ADN | NSU inicial: %s", empresa["nome"], ult_nsu)

    xmls = pdfs = municipais = erros = 0
    pendentes = []

    with GerenciadorCertificadoA1(empresa["cert_path"], empresa["cert_senha"]) as cert_pem:
        with ApiClientNfse(cert=cert_pem, base_url=ADN_BASE_URL_PADRAO) as client:
            adn = AdnService(client)
            downloader = Downloader(client)

            todas, ultimo_nsu = adn.sincronizar_todos(ult_nsu)
            notas = [n for n in todas if _no_periodo(n.data_emissao, inicio, fim)]
            ignoradas = len(todas) - len(notas)
            if ignoradas:
                logger.info("[%s] %d nota(s) fora do período ignoradas.", empresa["nome"], ignoradas)

            for nota in notas:
                data = nota.data_emissao.strftime("%d/%m/%Y") if nota.data_emissao else "?"
                logger.info("  #%s | %s | R$ %.2f | %s",
                            nota.numero or nota.nsu, data, nota.valor, nota.cnpj_prestador or "?")

                if storage.salvar_xml(nota):
                    xmls += 1
                else:
                    erros += 1

                conteudo, status = downloader.obter_pdf(nota)
                if status == StatusDownload.SUCESSO and conteudo:
                    storage.salvar_pdf(nota, conteudo)
                    pdfs += 1
                elif status == StatusDownload.MUNICIPAL_NECESSARIO:
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
# Modo 2: Portal web (login/senha)
# ------------------------------------------------------------------

def _sincronizar_via_portal(empresa: dict, inicio: datetime, fim: datetime) -> dict:
    from core.portal_scraper import FalhaAutenticacaoError, PortalScraper

    cnpj = empresa["cnpj"]
    logger.info("[%s] Modo Portal (scraping) | Período: %s a %s",
                empresa["nome"], inicio.strftime("%d/%m/%Y"), fim.strftime("%d/%m/%Y"))

    scraper = PortalScraper(cnpj=cnpj, senha=empresa["senha"])
    scraper.autenticar()

    notas_raw = scraper.listar_notas(
        "recebidas",
        inicio.strftime("%d/%m/%Y"),
        fim.strftime("%d/%m/%Y"),
    )

    base = (
        Path(DOWNLOAD_PATH)
        / cnpj
        / inicio.strftime("%Y")
        / inicio.strftime("%m")
    )
    (base / "xmls").mkdir(parents=True, exist_ok=True)
    (base / "pdfs").mkdir(parents=True, exist_ok=True)

    xmls = pdfs = municipais = erros = 0
    for nota in notas_raw:
        numero = nota.get("numero") or nota.get("chave_acesso", "nota")
        logger.info("  #%s | %s | %s | %s",
                    numero, nota.get("data_emissao", "?"),
                    nota.get("valor", "?"), nota.get("status", "?"))

        nota_municipal = False

        url_xml = nota.get("download_xml") or nota.get("baixar_xml")
        if url_xml:
            conteudo, status = scraper.baixar_xml(url_xml)
            if status == scraper.RESULTADO_SUCESSO and conteudo:
                (base / "xmls" / f"{numero}.xml").write_bytes(conteudo)
                xmls += 1
            elif status == scraper.RESULTADO_MUNICIPAL:
                nota_municipal = True
            else:
                erros += 1

        url_pdf = nota.get("download_danfs-e") or nota.get("download_pdf") or nota.get("baixar_danfs-e")
        if url_pdf:
            conteudo, status = scraper.baixar_pdf(url_pdf)
            if status == scraper.RESULTADO_SUCESSO and conteudo:
                (base / "pdfs" / f"{numero}.pdf").write_bytes(conteudo)
                pdfs += 1
            elif status == scraper.RESULTADO_MUNICIPAL:
                nota_municipal = True
            else:
                erros += 1

        if nota_municipal:
            municipais += 1

    return {"notas": len(notas_raw), "xmls": xmls, "pdfs": pdfs,
            "municipais": municipais, "erros": erros}


# ------------------------------------------------------------------
# Ponto de entrada
# ------------------------------------------------------------------

def _carregar_empresas() -> list[dict]:
    if not ARQUIVO_EMPRESAS.exists():
        raise SystemExit(
            f"\n[ERRO] Arquivo '{ARQUIVO_EMPRESAS}' não encontrado.\n"
            "Copie 'empresas.example.json' para 'empresas.json' e preencha os dados.\n"
        )
    with open(ARQUIVO_EMPRESAS, encoding="utf-8") as f:
        empresas = json.load(f)

    # Normaliza CNPJs (remove formatação) e valida campos mínimos
    validas = []
    for emp in empresas:
        if emp.get("_comentario"):
            # Ignora linhas de comentário
            emp.pop("_comentario", None)
        cnpj = re.sub(r"\D", "", emp.get("cnpj", ""))
        if not cnpj:
            logger.warning("Empresa sem CNPJ ignorada: %s", emp.get("nome", "?"))
            continue
        emp["cnpj"] = cnpj
        tem_cert = bool(emp.get("cert_path") and emp.get("cert_senha"))
        tem_senha = bool(emp.get("senha"))
        if not tem_cert and not tem_senha:
            logger.warning("[%s] Sem autenticação configurada — ignorada.", emp.get("nome", cnpj))
            continue
        validas.append(emp)

    return validas


def _imprimir_resumo(resultados: list[dict]) -> None:
    sep = "=" * 60
    print(f"\n{sep}")
    print("  RESUMO GERAL — TODAS AS EMPRESAS")
    print(sep)
    total_notas = total_xmls = total_pdfs = total_mun = total_err = 0
    for r in resultados:
        status = "✅" if r["erros"] == 0 else "⚠️ "
        print(
            f"  {status} {r['nome'][:35]:<35} "
            f"notas: {r['notas']:>3}  xml: {r['xmls']:>3}  pdf: {r['pdfs']:>3}"
            + (f"  mun: {r['municipais']}" if r["municipais"] else "")
            + (f"  err: {r['erros']}" if r["erros"] else "")
        )
        total_notas += r["notas"]
        total_xmls += r["xmls"]
        total_pdfs += r["pdfs"]
        total_mun += r["municipais"]
        total_err += r["erros"]
    print(sep)
    print(f"  TOTAL  notas: {total_notas:>3}  xml: {total_xmls:>3}  pdf: {total_pdfs:>3}", end="")
    if total_mun:
        print(f"  municipais: {total_mun}", end="")
    if total_err:
        print(f"  erros: {total_err}", end="")
    print(f"\n{sep}\n")


def main():
    empresas = _carregar_empresas()
    if not empresas:
        raise SystemExit("\n[ERRO] Nenhuma empresa válida encontrada em empresas.json\n")

    inicio, fim = _periodo_mes_anterior()
    periodo = inicio.strftime("%m/%Y")

    print(f"\n{'=' * 60}")
    print(f"  NFS-e Downloader — Notas Recebidas — {periodo}")
    print(f"  Empresas carregadas: {len(empresas)}")
    print(f"{'=' * 60}\n")

    resultados = []

    for emp in empresas:
        nome = emp.get("nome", emp["cnpj"])
        tem_cert = bool(emp.get("cert_path") and emp.get("cert_senha"))
        metodo = "API ADN" if tem_cert else "Portal web"

        print(f"\n{'─' * 60}")
        print(f"  {nome}  |  CNPJ: {emp['cnpj']}  |  {metodo}")
        print(f"{'─' * 60}")

        try:
            if tem_cert:
                res = _sincronizar_via_api(emp, inicio, fim)
            else:
                res = _sincronizar_via_portal(emp, inicio, fim)
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] Falha: %s", nome, exc)
            res = {"notas": 0, "xmls": 0, "pdfs": 0, "municipais": 0, "erros": 1}

        res["nome"] = nome
        resultados.append(res)

        print(f"  Notas: {res['notas']}  |  XMLs: {res['xmls']}  |  PDFs: {res['pdfs']}", end="")
        if res["municipais"]:
            print(f"  |  Municipais: {res['municipais']}", end="")
        if res["erros"]:
            print(f"  |  ⚠️  Erros: {res['erros']}", end="")
        print()

    _imprimir_resumo(resultados)


if __name__ == "__main__":
    main()
