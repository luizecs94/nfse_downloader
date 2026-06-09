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
    from core.portal_scraper import PortalScraper
    from core.relatorio_excel import gerar_relatorio

    logger.info("Modo Portal (scraping) | Período: %s a %s",
                inicio.strftime("%d/%m/%Y"), fim.strftime("%d/%m/%Y"))

    scraper = PortalScraper(cnpj=cfg["cnpj"], senha=cfg["senha"])
    scraper.autenticar()

    notas_raw = scraper.listar_notas(
        "recebidas",
        inicio.strftime("%d/%m/%Y"),
        fim.strftime("%d/%m/%Y"),
    )

    base = Path(cfg["download_path"]) / cfg["cnpj"] / inicio.strftime("%Y") / inicio.strftime("%m")
    base.mkdir(parents=True, exist_ok=True)

    xmls = pdfs = municipais = erros = 0
    registros_excel = []

    for nota in notas_raw:
        numero       = nota.get("numero") or ""
        data_emissao = nota.get("data_emissao", "")
        valor        = nota.get("valor", "")
        status_nota  = nota.get("status", "")
        chave        = nota.get("chave_acesso", "")
        link_xml     = nota.get("download_xml") or nota.get("baixar_xml") or ""
        link_pdf     = nota.get("download_danfs-e") or nota.get("baixar_danfs-e") or nota.get("download_pdf") or ""

        logger.info("  #%s | %s | %s | %s", numero or chave, data_emissao, valor, status_nota)

        nota_captcha = False

        if link_xml:
            conteudo, st = scraper.baixar_xml(link_xml)
            if st == scraper.RESULTADO_SUCESSO and conteudo:
                nome_arq = numero or chave or "nota"
                (base / "xmls").mkdir(exist_ok=True)
                (base / "xmls" / f"{nome_arq}.xml").write_bytes(conteudo)
                xmls += 1
                link_xml = ""
            elif st == scraper.RESULTADO_MUNICIPAL:
                nota_captcha = True
            else:
                erros += 1

        if link_pdf:
            conteudo, st = scraper.baixar_pdf(link_pdf)
            if st == scraper.RESULTADO_SUCESSO and conteudo:
                nome_arq = numero or chave or "nota"
                (base / "pdfs").mkdir(exist_ok=True)
                (base / "pdfs" / f"{nome_arq}.pdf").write_bytes(conteudo)
                pdfs += 1
                link_pdf = ""
            elif st == scraper.RESULTADO_MUNICIPAL:
                nota_captcha = True
            else:
                erros += 1

        if nota_captcha:
            municipais += 1

        registros_excel.append({
            "empresa":      cfg.get("empresa", ""),
            "cnpj":         cfg["cnpj"],
            "numero":       numero,
            "data_emissao": data_emissao,
            "valor":        valor,
            "status":       status_nota,
            "chave_acesso": chave,
            "link_xml":     link_xml,
            "link_pdf":     link_pdf,
        })

    if registros_excel:
        mes_ano = inicio.strftime("%Y-%m")
        caminho_excel = base / f"relatorio_recebidas_{cfg['cnpj']}_{mes_ano}.xlsx"
        gerar_relatorio(registros_excel, caminho_excel)
        logger.info("Relatório Excel salvo em: %s", caminho_excel)

    return {"notas": len(notas_raw), "xmls": xmls, "pdfs": pdfs,
            "municipais": municipais, "erros": erros}


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
