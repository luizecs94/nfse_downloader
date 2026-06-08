"""
NFS-e Downloader — Sincronização automática do mês anterior.

Baixa todas as NFS-e recebidas pela empresa no mês anterior ao atual,
usando a API oficial do Portal Nacional (ADN) com autenticação mTLS.

Uso:
    python main.py
    (ou: .\.venv\Scripts\python.exe main.py no Windows)
"""

import logging
import os
import re
from datetime import datetime, timedelta

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


def _periodo_mes_anterior() -> tuple[datetime, datetime]:
    hoje = datetime.today()
    ultimo = hoje.replace(day=1) - timedelta(days=1)
    return ultimo.replace(day=1), ultimo


def _no_periodo(nota, inicio: datetime, fim: datetime) -> bool:
    if not nota.data_emissao:
        return False
    return inicio <= nota.data_emissao <= fim.replace(hour=23, minute=59, second=59)


def sincronizar(cfg: dict) -> ResultadoSincronizacao:
    resultado = ResultadoSincronizacao()
    inicio, fim = _periodo_mes_anterior()

    storage = Storage(
        base_path=cfg["download_path"],
        path_structure=cfg["path_structure"],
        cnpj_tomador=cfg["cnpj"],
    )

    ult_nsu = storage.carregar_ultimo_nsu()
    logger.info(
        "Empresa: %s | Período: %s a %s | NSU inicial: %s",
        cfg["empresa"], inicio.strftime("%d/%m/%Y"), fim.strftime("%d/%m/%Y"), ult_nsu,
    )

    with GerenciadorCertificadoA1(cfg["cert_path"], cfg["cert_senha"]) as cert_pem:
        with ApiClientNfse(cert=cert_pem, base_url=cfg["adn_base_url"]) as client:
            adn = AdnService(client)
            downloader = Downloader(client)

            todas, ultimo_nsu = adn.sincronizar_todos(ult_nsu)
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
            storage.salvar_ultimo_nsu(ultimo_nsu)

    return resultado


def main():
    cfg = _carregar_config()
    resultado = sincronizar(cfg)

    print("\n" + "=" * 50)
    print(f"  {cfg['empresa']}")
    print("  SINCRONIZAÇÃO CONCLUÍDA")
    print("=" * 50)
    print(f"  Notas encontradas:      {resultado.notas_encontradas}")
    print(f"  XMLs salvos:            {resultado.xmls_salvos}")
    print(f"  PDFs salvos:            {resultado.pdfs_salvos}")
    print(f"  Pendentes municipais:   {resultado.municipais_pendentes}")
    print(f"  Erros:                  {len(resultado.erros)}")
    print(f"  Último NSU:             {resultado.ultimo_nsu}")
    if resultado.erros:
        for e in resultado.erros:
            print(f"    - {e}")
    print("=" * 50)


if __name__ == "__main__":
    main()
