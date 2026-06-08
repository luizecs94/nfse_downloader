"""
NFS-e Downloader — Notas Emitidas (mês anterior automático).

Baixa as NFS-e emitidas pela empresa via portal web (scraping HTML).
Requer NFSE_USUARIO e NFSE_SENHA no .env.

Nota: A API ADN distribui apenas notas recebidas (tomador).
Para notas emitidas, o portal web é a fonte disponível.

Uso:
    .\.venv\Scripts\python.exe main_emitidas.py
"""

import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from core.portal_scraper import FalhaAutenticacaoError, PortalScraper

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _periodo_mes_anterior() -> tuple[datetime, datetime]:
    hoje = datetime.today()
    ultimo = hoje.replace(day=1) - timedelta(days=1)
    return ultimo.replace(day=1), ultimo


def _carregar_config() -> dict:
    cnpj = re.sub(r"\D", "", os.getenv("NFSE_USUARIO", ""))
    senha = os.getenv("NFSE_SENHA")
    if not cnpj or not senha:
        raise SystemExit(
            "\n[ERRO] NFSE_USUARIO e NFSE_SENHA são obrigatórios no .env\n"
            "para download de notas emitidas via portal web.\n"
        )
    return {
        "empresa":       os.getenv("EMPRESA_NOME", "Empresa"),
        "cnpj":          cnpj,
        "senha":         senha,
        "download_path": os.getenv("DOWNLOAD_PATH", "downloads"),
    }


def sincronizar_emitidas(cfg: dict, inicio: datetime, fim: datetime) -> dict:
    scraper = PortalScraper(cnpj=cfg["cnpj"], senha=cfg["senha"])
    scraper.autenticar()

    data_ini = inicio.strftime("%d/%m/%Y")
    data_fim = fim.strftime("%d/%m/%Y")
    notas = scraper.listar_notas("emitidas", data_ini, data_fim)

    # Pasta organizada por CNPJ / ANO / MES / emitidas
    base = (
        Path(cfg["download_path"])
        / cfg["cnpj"]
        / inicio.strftime("%Y")
        / inicio.strftime("%m")
        / "emitidas"
    )
    (base / "xmls").mkdir(parents=True, exist_ok=True)
    (base / "pdfs").mkdir(parents=True, exist_ok=True)

    xmls = pdfs = erros = 0

    for nota in notas:
        numero = nota.get("numero", "?")
        data   = nota.get("data_emissao", "?")
        valor  = nota.get("valor", "?")
        status = nota.get("status", "?")

        logger.info("  #%s | %s | %s | %s", numero, data, valor, status)

        # --- XML ---
        url_xml = nota.get("download_xml")
        if url_xml:
            conteudo = scraper.baixar_xml(url_xml)
            if conteudo:
                (base / "xmls" / f"{numero}.xml").write_bytes(conteudo)
                xmls += 1
                logger.info("    XML salvo: %s", numero)
            else:
                erros += 1

        # --- PDF (chave pode variar entre portais) ---
        url_pdf = nota.get("download_danfs-e") or nota.get("download_pdf")
        if url_pdf:
            conteudo = scraper.baixar_pdf(url_pdf)
            if conteudo:
                (base / "pdfs" / f"{numero}.pdf").write_bytes(conteudo)
                pdfs += 1
                logger.info("    PDF salvo: %s", numero)
            else:
                erros += 1

    return {"notas": len(notas), "xmls": xmls, "pdfs": pdfs, "erros": erros}


def main():
    cfg = _carregar_config()
    inicio, fim = _periodo_mes_anterior()

    print(f"\n{'=' * 50}")
    print(f"  {cfg['empresa']}")
    print(f"  Notas Emitidas — {inicio.strftime('%m/%Y')}")
    print(f"  Método: Portal web (login/senha)")
    print(f"{'=' * 50}\n")

    try:
        res = sincronizar_emitidas(cfg, inicio, fim)
    except FalhaAutenticacaoError as exc:
        raise SystemExit(f"\n[ERRO] {exc}\n")

    print(f"\n{'=' * 50}")
    print("  CONCLUÍDO")
    print(f"{'=' * 50}")
    print(f"  Notas encontradas:  {res['notas']}")
    print(f"  XMLs salvos:        {res['xmls']}")
    print(f"  PDFs salvos:        {res['pdfs']}")
    if res["erros"]:
        print(f"  Erros:              {res['erros']}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
