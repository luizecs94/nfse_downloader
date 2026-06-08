"""
NFS-e Downloader — Download com período personalizado.

Solicita data de início e fim antes de executar.

Uso:
    python main_manual.py
    (ou: .\.venv\Scripts\python.exe main_manual.py no Windows)
"""

import logging
import os
import re
from datetime import datetime

from dotenv import load_dotenv

from core.adn_service import ADN_BASE_URL, AdnService
from core.api_client import ApiClientNfse
from core.certificado import GerenciadorCertificadoA1
from core.downloader import Downloader
from core.models import ResultadoSincronizacao, StatusDownload
from core.storage import Storage

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _carregar_config() -> dict:
    cert_path = os.getenv("CERTIFICADO_PATH")
    cert_senha = os.getenv("CERTIFICADO_SENHA")
    if not cert_path or not cert_senha:
        raise SystemExit(
            "\n[ERRO] Preencha CERTIFICADO_PATH e CERTIFICADO_SENHA no arquivo .env\n"
            "Copie .env.example → .env e complete os dados da empresa.\n"
        )
    return {
        "empresa": os.getenv("EMPRESA_NOME", "Empresa"),
        "cnpj": re.sub(r"\D", "", os.getenv("NFSE_USUARIO", "")),
        "cert_path": cert_path,
        "cert_senha": cert_senha,
        "download_path": os.getenv("DOWNLOAD_PATH", "downloads"),
        "path_structure": os.getenv("PATH_STRUCTURE", "{CNPJ_TOMADOR}/{ANO}/{MES}"),
        "adn_base_url": os.getenv("ADN_BASE_URL", ADN_BASE_URL),
    }


def _pedir_data(label: str) -> datetime:
    while True:
        v = input(f"  {label} (DD/MM/AAAA): ").strip()
        try:
            return datetime.strptime(v, "%d/%m/%Y")
        except ValueError:
            print("  ⚠  Formato inválido. Ex: 01/05/2026")


def _pedir_periodo() -> tuple[datetime, datetime]:
    print("\n📅  Informe o período:")
    while True:
        inicio = _pedir_data("Data início")
        fim = _pedir_data("Data fim  ")
        if fim >= inicio:
            return inicio, fim
        print("  ⚠  A data fim deve ser igual ou posterior à data início.\n")


def _no_periodo(nota, inicio: datetime, fim: datetime) -> bool:
    if not nota.data_emissao:
        return False
    return inicio <= nota.data_emissao <= fim.replace(hour=23, minute=59, second=59)


def sincronizar_periodo(cfg: dict, inicio: datetime, fim: datetime) -> ResultadoSincronizacao:
    resultado = ResultadoSincronizacao()

    storage = Storage(
        base_path=cfg["download_path"],
        path_structure=cfg["path_structure"],
        cnpj_tomador=cfg["cnpj"],
    )

    logger.info(
        "Empresa: %s | Período: %s a %s | NSU inicial: 0",
        cfg["empresa"], inicio.strftime("%d/%m/%Y"), fim.strftime("%d/%m/%Y"),
    )

    with GerenciadorCertificadoA1(cfg["cert_path"], cfg["cert_senha"]) as cert_pem:
        with ApiClientNfse(cert=cert_pem, base_url=cfg["adn_base_url"]) as client:
            adn = AdnService(client)
            downloader = Downloader(client)

            # Sempre começa do zero para garantir notas de períodos passados
            # Não salva o NSU para não interferir com o main.py automático
            todas, ultimo_nsu = adn.sincronizar_todos("0")
            resultado.ultimo_nsu = ultimo_nsu

            notas = [n for n in todas if _no_periodo(n, inicio, fim)]
            resultado.notas_encontradas = len(notas)
            ignoradas = len(todas) - len(notas)
            if ignoradas:
                logger.info("%d nota(s) fora do período ignoradas.", ignoradas)

            pendentes = []
            for nota in notas:
                data = nota.data_emissao.strftime("%d/%m/%Y") if nota.data_emissao else "?"
                logger.info("  #%s | %s | R$ %.2f | %s",
                            nota.numero or nota.nsu, data, nota.valor, nota.cnpj_prestador or "?")

                caminho = storage.salvar_xml(nota)
                if caminho:
                    nota.caminho_xml = caminho
                    nota.status_xml = StatusDownload.SUCESSO
                    resultado.xmls_salvos += 1
                else:
                    resultado.erros.append(f"NSU {nota.nsu}: falha ao salvar XML.")

                conteudo, status = downloader.obter_pdf(nota)
                nota.status_pdf = status
                if status == StatusDownload.SUCESSO and conteudo:
                    storage.salvar_pdf(nota, conteudo)
                    resultado.pdfs_salvos += 1
                elif status == StatusDownload.MUNICIPAL_NECESSARIO:
                    pendentes.append(nota)
                    resultado.municipais_pendentes += 1
                else:
                    resultado.erros.append(f"NSU {nota.nsu}: falha ao baixar PDF.")

            if pendentes:
                storage.registrar_pendentes_municipais(pendentes)

    return resultado


def main():
    cfg = _carregar_config()

    print("=" * 50)
    print(f"  {cfg['empresa']}")
    print("  DOWNLOAD MANUAL DE NFS-e RECEBIDAS")
    print("=" * 50)

    inicio, fim = _pedir_periodo()
    print(f"\n🔄  Buscando de {inicio.strftime('%d/%m/%Y')} a {fim.strftime('%d/%m/%Y')}...\n")

    resultado = sincronizar_periodo(cfg, inicio, fim)

    print("\n" + "=" * 50)
    print("  CONCLUÍDO")
    print("=" * 50)
    print(f"  Notas encontradas:      {resultado.notas_encontradas}")
    print(f"  XMLs salvos:            {resultado.xmls_salvos}")
    print(f"  PDFs salvos:            {resultado.pdfs_salvos}")
    print(f"  Pendentes municipais:   {resultado.municipais_pendentes}")
    print(f"  Erros:                  {len(resultado.erros)}")
    if resultado.erros:
        for e in resultado.erros:
            print(f"    - {e}")
    print("=" * 50)


if __name__ == "__main__":
    main()
