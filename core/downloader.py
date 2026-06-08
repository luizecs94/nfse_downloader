"""
Downloader de DANFSE (PDF) via API oficial do Portal Nacional NFS-e.

O XML já vem embutido no payload do ADN (GZip+Base64) — não há endpoint separado.
O PDF (DANFSE) é solicitado via GET /danfse/{chaveAcesso}.

Comportamento do 403:
    HTTP 403 indica que a nota pertence a um município que ainda opera no padrão
    próprio (não integrado ao Portal Nacional). Nesses casos o download deve ser
    feito diretamente no portal municipal — futuramente via automação Playwright.
    A nota é registrada como StatusDownload.MUNICIPAL_NECESSARIO.

Endpoint (requer mTLS):
    Produção:    https://adn.nfse.gov.br/danfse/{chaveAcesso}
    Homologação: https://adn.producaorestrita.nfse.gov.br/danfse/{chaveAcesso}
"""

import logging
from typing import Optional, Tuple

from core.api_client import ApiClientNfse
from core.models import NotaFiscal, StatusDownload

logger = logging.getLogger(__name__)

_ENDPOINT_DANFSE = "/danfse/{chave}"


class Downloader:
    """Baixa o DANFSE (PDF) de uma nota e trata o 403 como pendente municipal."""

    def __init__(self, client: ApiClientNfse) -> None:
        self.client = client

    def obter_pdf(self, nota: NotaFiscal) -> Tuple[Optional[bytes], StatusDownload]:
        """
        Tenta baixar o PDF (DANFSE) da nota via API.

        Returns:
            (bytes_do_pdf, StatusDownload.SUCESSO)            — download OK
            (None, StatusDownload.MUNICIPAL_NECESSARIO)        — 403, baixar no município
            (None, StatusDownload.ERRO)                        — outro erro

        Args:
            nota: NotaFiscal com chave_acesso preenchida.
        """
        if not nota.chave_acesso:
            logger.warning("Nota NSU=%s sem chave de acesso — PDF ignorado.", nota.nsu)
            return None, StatusDownload.ERRO

        endpoint = _ENDPOINT_DANFSE.format(chave=nota.chave_acesso)
        response = self.client.get(endpoint)

        if response.status_code == 200:
            logger.debug("PDF obtido para chave %s (%d bytes).",
                         nota.chave_acesso, len(response.content))
            return response.content, StatusDownload.SUCESSO

        if response.status_code == 403:
            logger.warning(
                "403 ao buscar PDF da nota %s (NSU=%s) — "
                "nota pertence a portal municipal não integrado.",
                nota.chave_acesso, nota.nsu,
            )
            return None, StatusDownload.MUNICIPAL_NECESSARIO

        if response.status_code == 404:
            logger.warning("PDF não encontrado (404) para chave %s.", nota.chave_acesso)
            return None, StatusDownload.ERRO

        logger.error(
            "Erro inesperado %d ao baixar PDF da nota %s: %s",
            response.status_code, nota.chave_acesso, response.text[:200],
        )
        return None, StatusDownload.ERRO
