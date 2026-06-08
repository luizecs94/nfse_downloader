"""
Cliente HTTP com suporte a mTLS para as APIs oficiais do Portal Nacional NFS-e.

A API exige autenticação mútua (mTLS) via certificado digital ICP-Brasil (A1/A3).
O CNPJ do contribuinte é extraído automaticamente pelo servidor a partir do
Subject Alternative Name do certificado (OID 2.16.76.1.3.3) — não há token ou login.

Referência Swagger (requer certificado para acesso):
  Produção:    https://adn.nfse.gov.br/docs/index.html
  Homologação: https://adn.producaorestrita.nfse.gov.br/docs/index.html
"""

import logging
from typing import Any, Dict, Optional, Tuple

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class ApiClientNfse:
    """
    Cliente HTTP resiliente com mTLS para as APIs do Portal Nacional NFS-e.

    Uso típico via context manager:
        with GerenciadorCertificadoA1(pfx, senha) as cert:
            with ApiClientNfse(cert=cert, base_url=ADN_BASE_URL) as client:
                resp = client.get("/DFe", params={"ultNSU": "0"})
    """

    def __init__(
        self,
        cert: Tuple[str, str],
        base_url: str,
        timeout: int = 30,
    ) -> None:
        """
        Args:
            cert: Tupla (caminho_cert_pem, caminho_key_pem) gerada pelo GerenciadorCertificadoA1.
            base_url: URL base da API (ADN ou SEFIN).
            timeout: Timeout em segundos para cada requisição.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.cert = cert
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._configurar_retries()

    # ------------------------------------------------------------------
    # Configuração interna
    # ------------------------------------------------------------------

    def _configurar_retries(self) -> None:
        """Retry automático para falhas transitórias de rede (não para 403/404)."""
        retry = Retry(
            total=3,
            backoff_factor=1,
            # 403 NÃO entra aqui — é tratado como lógica de negócio (portal municipal)
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("https://", adapter)

    # ------------------------------------------------------------------
    # Métodos públicos
    # ------------------------------------------------------------------

    def get(self, path: str, params: Optional[Dict[str, Any]] = None, **kwargs: Any) -> requests.Response:
        """
        Envia GET para {base_url}/{path}.

        Retorna o objeto Response sem lançar exceção para status 4xx/5xx,
        permitindo que cada serviço trate os códigos de status conforme a lógica
        de negócio (ex.: 403 = download municipal necessário).
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        logger.debug("GET %s params=%s", url, params)
        try:
            response = self._session.get(url, params=params, timeout=self.timeout, **kwargs)
            return response
        except requests.RequestException as exc:
            logger.error("Erro de rede em GET %s: %s", url, exc)
            raise

    def close(self) -> None:
        """Fecha a sessão HTTP liberando recursos."""
        self._session.close()

    def __enter__(self) -> "ApiClientNfse":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
