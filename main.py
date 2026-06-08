"""
NFS-e Downloader — Notas Recebidas (mês anterior automático).

Detecta automaticamente o método de autenticação disponível no .env:

  ✅ Certificado A1 disponível → API oficial ADN (recomendado)
       - Paginação incremental por NSU
       - Retoma de onde parou
       - Mais confiável e rápido

  ⚠️  Apenas login/senha → Portal web (scraping HTML)
       - Filtra por período de datas
       - Funciona sem certificado
       - Pode ser afetado por mudanças no portal

Uso:
    .\.venv\Scripts\python.exe main.py
"""

import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Utilitários de período
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
# Modo 1: API oficial ADN (com certificado)
# ------------------------------------------------------------------

def _sincronizar_via_api(cfg: dict, inicio: datetime, fim: datetime) -> dict:
    from core.adn_service import ADN_BASE_URL, AdnService
    from core.api_client import ApiClientNfse
    from core.certificado import GerenciadorCertificadoA1
    from core.downloader import Downloader
    from core.models import ResultadoSincronizacao, StatusDownload
    from core.storage import Storage

    storage = Storage(
        base_path=cfg["download_path"],
        path_structure=cfg["path_structure"],
        cnpj_tomador=cfg["cnpj"],
    )
    ult_nsu = storage.carregar_ultimo_nsu()
    logger.info("Modo API ADN | NSU inicial: %s", ult_nsu)

    xmls = pdfs = municipais = erros = 0
    pendentes = []

    with GerenciadorCertificadoA1(cfg["cert_path"], cfg["cert_senha"]) as cert_pem:
        with ApiClientNfse(cert=cert_pem, base_url=cfg.get("adn_base_url", ADN_BASE_URL)) as client:
            adn = AdnService(client)
            downloader = Downloader(client)

            todas, ultimo_nsu = adn.sincronizar_todos(ult_nsu)
            notas = [n for n in todas if _no_periodo(n.data_emissao, inicio, fim)]
            ignoradas = len(todas) - len(notas)
            if ignoradas:
                logger.info("%d nota(s) fora do período ignoradas.", ignoradas)

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
# Modo 2: Portal web scraping (com login/senha)
# ------------------------------------------------------------------

def _sincronizar_via_portal(cfg: dict, inicio: datetime, fim: datetime) -> dict:
    from core.portal_scraper import FalhaAutenticacaoError, PortalScraper
    from core.xml_parser import extrair_dados_nfse_string

    logger.info("Modo Portal (scraping) | Período: %s a %s",
                inicio.strftime("%d/%m/%Y"), fim.strftime("%d/%m/%Y"))

    scraper = PortalScraper(cnpj=cfg["cnpj"], senha=cfg["senha"])
    scraper.autenticar()

    data_ini_str = inicio.strftime("%d/%m/%Y")
    data_fim_str = fim.strftime("%d/%m/%Y")
    notas_raw = scraper.listar_notas("recebidas", data_ini_str, data_fim_str)

    base = Path(cfg["download_path"]) / cfg["cnpj"] / inicio.strftime("%Y") / inicio.strftime("%m")
    (base / "xmls").mkdir(parents=True, exist_ok=True)
    (base / "pdfs").mkdir(parents=True, exist_ok=True)

    xmls = pdfs = erros = 0
    for nota in notas_raw:
        numero = nota.get("numero", "?")
        logger.info("  #%s | %s | %s | %s",
                    numero, nota.get("data_emissao"), nota.get("valor"), nota.get("status"))

        # XML
        url_xml = nota.get("download_xml")
        if url_xml:
            conteudo = scraper.baixar_xml(url_xml)
            if conteudo:
                nome = f"{numero or 'nota'}.xml"
                (base / "xmls" / nome).write_bytes(conteudo)
                xmls += 1
            else:
                erros += 1

        # PDF — chave pode ser download_danfs-e ou download_pdf
        url_pdf = nota.get("download_danfs-e") or nota.get("download_pdf")
        if url_pdf:
            conteudo = scraper.baixar_pdf(url_pdf)
            if conteudo:
                nome = f"{numero or 'nota'}.pdf"
                (base / "pdfs" / nome).write_bytes(conteudo)
                pdfs += 1
            else:
                erros += 1

    return {"notas": len(notas_raw), "xmls": xmls, "pdfs": pdfs,
            "municipais": 0, "erros": erros}


# ------------------------------------------------------------------
# Ponto de entrada
# ------------------------------------------------------------------

def _carregar_config() -> dict:
    return {
        "empresa":      os.getenv("EMPRESA_NOME", "Empresa"),
        "cnpj":         re.sub(r"\D", "", os.getenv("NFSE_USUARIO", "")),
        "cert_path":    os.getenv("CERTIFICADO_PATH"),
        "cert_senha":   os.getenv("CERTIFICADO_SENHA"),
        "senha":        os.getenv("NFSE_SENHA"),
        "download_path": os.getenv("DOWNLOAD_PATH", "downloads"),
        "path_structure": os.getenv("PATH_STRUCTURE", "{CNPJ_TOMADOR}/{ANO}/{MES}"),
        "adn_base_url": os.getenv("ADN_BASE_URL", "https://adn.nfse.gov.br"),
    }


def main():
    from typing import Optional

    cfg = _carregar_config()
    inicio, fim = _periodo_mes_anterior()

    tem_certificado = bool(cfg["cert_path"] and cfg["cert_senha"])
    tem_senha = bool(cfg["cnpj"] and cfg["senha"])

    if not tem_certificado and not tem_senha:
        raise SystemExit(
            "\n[ERRO] Configure no .env pelo menos uma das opções:\n"
            "  Opção 1 (recomendado): CERTIFICADO_PATH + CERTIFICADO_SENHA\n"
            "  Opção 2 (fallback):    NFSE_USUARIO + NFSE_SENHA\n"
        )

    print(f"\n{'=' * 50}")
    print(f"  {cfg['empresa']}")
    print(f"  Notas Recebidas — {inicio.strftime('%m/%Y')}")
    metodo = "API ADN (certificado)" if tem_certificado else "Portal web (login/senha)"
    print(f"  Método: {metodo}")
    print(f"{'=' * 50}\n")

    if tem_certificado:
        res = _sincronizar_via_api(cfg, inicio, fim)
    else:
        logger.warning("Certificado não encontrado — usando fallback via portal web.")
        res = _sincronizar_via_portal(cfg, inicio, fim)

    print(f"\n{'=' * 50}")
    print("  CONCLUÍDO")
    print(f"{'=' * 50}")
    print(f"  Notas encontradas:      {res['notas']}")
    print(f"  XMLs salvos:            {res['xmls']}")
    print(f"  PDFs salvos:            {res['pdfs']}")
    if res.get("municipais"):
        print(f"  Pendentes municipais:   {res['municipais']}")
    if res.get("erros"):
        print(f"  Erros:                  {res['erros']}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    from typing import Optional
    main()
