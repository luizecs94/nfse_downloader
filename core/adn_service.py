"""
Serviço de consulta ao ADN (Ambiente de Dados Nacional) do Portal Nacional NFS-e.

O ADN distribui as NFS-e recebidas pelo tomador usando paginação por NSU
(Número Sequencial Único). Cada chamada retorna até 50 documentos.

Fluxo:
    1. Chamar GET /DFe?ultNSU=<ultimo_nsu_conhecido>
    2. Receber lote de DFe (XML comprimido GZip + codificado Base64)
    3. Decodificar cada XML
    4. Repetir incrementando o NSU até não haver mais documentos

Endpoints (requerem mTLS com certificado ICP-Brasil):
    Produção:    https://adn.nfse.gov.br
    Homologação: https://adn.producaorestrita.nfse.gov.br

Swagger de referência (requer certificado para abrir):
    https://adn.nfse.gov.br/docs/index.html

IMPORTANTE: Os nomes de campos do JSON de resposta (lote, nsu, docZip, maxNSU)
foram definidos com base na documentação pública disponível. Valide contra o
Swagger oficial com seu certificado em caso de divergência.
"""

import base64
import gzip
import logging
from typing import List, Tuple

from core.api_client import ApiClientNfse
from core.models import NotaFiscal, StatusDownload
from core.xml_parser import extrair_dados_nfse_string

logger = logging.getLogger(__name__)

# URLs base — altere via .env se necessário
ADN_BASE_URL = "https://adn.nfse.gov.br"
ADN_HOMOLOG_URL = "https://adn.producaorestrita.nfse.gov.br"

# NSU é parâmetro de PATH: GET /contribuintes/DFe/{ultNSU}
# Retorna até 50 DFe a partir do NSU informado.
# Swagger de referência: https://adn.producaorestrita.nfse.gov.br/contribuintes/docs/index.html
_ENDPOINT_DFE = "/contribuintes/DFe/{nsu}"

# DANFSE (PDF): GET /danfse/{chaveAcesso} — pode retornar 403 para notas municipais
_ENDPOINT_DANFSE = "/danfse/{chave}"


class AdnService:
    """
    Consulta e sincroniza NFS-e recebidas via API ADN.

    O CNPJ do tomador é identificado automaticamente pelo servidor
    através do certificado digital — não é necessário informá-lo.
    """

    def __init__(self, client: ApiClientNfse) -> None:
        self.client = client

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def buscar_proximo_lote(self, ult_nsu: str = "0") -> Tuple[List[NotaFiscal], str]:
        """
        Consulta um lote de até 50 DFe a partir do NSU informado.

        Args:
            ult_nsu: Último NSU já processado (string com zeros à esquerda ou "0").

        Returns:
            Tupla (lista_de_notas, max_nsu_do_lote).
            Se não houver novos documentos, retorna ([], ult_nsu).
        """
        # NSU é parâmetro de path: GET /contribuintes/DFe/{ultNSU}
        # NSU deve ser zero-padded com 15 dígitos (padrão ADN)
        nsu_formatado = str(ult_nsu).zfill(15)
        endpoint = _ENDPOINT_DFE.format(nsu=nsu_formatado)
        response = self.client.get(endpoint)

        if response.status_code in (204, 404):
            logger.info("Nenhum DFe disponível a partir do NSU %s.", ult_nsu)
            return [], ult_nsu

        if not response.ok:
            logger.error("Erro %d ao consultar DFe (NSU=%s): %s",
                         response.status_code, ult_nsu, response.text[:200])
            response.raise_for_status()

        payload = response.json()
        return self._processar_payload(payload, ult_nsu)

    def sincronizar_todos(self, ult_nsu: str = "0") -> Tuple[List[NotaFiscal], str]:
        """
        Pagina por todos os DFe disponíveis até não restar nenhum.

        Args:
            ult_nsu: NSU inicial (geralmente lido do arquivo de estado).

        Returns:
            Tupla (todas_as_notas, ultimo_nsu_processado).
        """
        todas: List[NotaFiscal] = []
        nsu_atual = ult_nsu

        while True:
            lote, max_nsu = self.buscar_proximo_lote(nsu_atual)

            # Para quando não há novos documentos ou o NSU não avançou
            if not lote or int(max_nsu) <= int(nsu_atual):
                logger.info("Sincronização completa. Último NSU: %s. Total: %d notas.",
                            nsu_atual, len(todas))
                break

            todas.extend(lote)
            nsu_atual = max_nsu
            logger.info("Lote recebido: %d notas | NSU atual: %s | Total acumulado: %d",
                        len(lote), nsu_atual, len(todas))

        return todas, nsu_atual

    # ------------------------------------------------------------------
    # Processamento interno
    # ------------------------------------------------------------------

    def _processar_payload(
        self, payload: dict, ult_nsu: str
    ) -> Tuple[List[NotaFiscal], str]:
        """
        Converte o payload JSON da API ADN em lista de NotaFiscal.

        Campos reais confirmados na API (junho/2026):
            StatusProcessamento : "DOCUMENTOS_LOCALIZADOS" | "SEM_DADOS"
            LoteDFe             : lista de documentos
              NSU               : int (ex: 1)
              ChaveAcesso       : str (44 dígitos)
              TipoDocumento     : "NFSE"
              ArquivoXml        : str (GZip + Base64)
        """
        notas: List[NotaFiscal] = []
        max_nsu_int = int(ult_nsu) if str(ult_nsu).isdigit() else 0

        for item in payload.get("LoteDFe", []):
            nsu_int = int(item.get("NSU", 0))
            nsu = str(nsu_int)
            chave_acesso = str(item.get("ChaveAcesso", ""))
            arquivo_xml_b64 = item.get("ArquivoXml", "")

            if not arquivo_xml_b64:
                logger.warning("Item sem ArquivoXml para NSU %s — ignorado.", nsu)
                continue

            try:
                xml_content = self._decodificar_xml(arquivo_xml_b64)
                nota = self._montar_nota(nsu, chave_acesso, xml_content)
                notas.append(nota)
                if nsu_int > max_nsu_int:
                    max_nsu_int = nsu_int
            except Exception as exc:
                logger.error("Falha ao processar DFe NSU=%s: %s", nsu, exc)

        return notas, str(max_nsu_int)

    @staticmethod
    def _decodificar_xml(arquivo_b64: str) -> str:
        """Decodifica Base64 → descomprime GZip → retorna string UTF-8."""
        dados_gz = base64.b64decode(arquivo_b64)
        xml_bytes = gzip.decompress(dados_gz)
        return xml_bytes.decode("utf-8")

    @staticmethod
    def _montar_nota(nsu: str, chave_acesso: str, xml_content: str) -> NotaFiscal:
        """Monta NotaFiscal usando a chave já fornecida pelo payload + metadados do XML."""
        dados = extrair_dados_nfse_string(xml_content)

        return NotaFiscal(
            nsu=nsu,
            # ChaveAcesso vem direto no payload — mais confiável que extrair do XML
            chave_acesso=chave_acesso or dados.get("chave") or "",
            numero=dados.get("numero"),
            data_emissao=dados.get("data_emissao"),
            cnpj_prestador=dados.get("cnpj_prestador"),
            cnpj_tomador=dados.get("cnpj_tomador"),
            cpf_tomador=dados.get("cpf_tomador"),
            nome_tomador=dados.get("nome_tomador"),
            valor=dados.get("valor", 0.0),
            valor_servico=dados.get("valor_servico", 0.0),
            descricao_servico=dados.get("descricao_servico"),
            municipio_codigo=dados.get("municipio_codigo"),
            status_code=dados.get("status_code"),
            xml_content=xml_content,
            status_xml=StatusDownload.PENDENTE,
            status_pdf=StatusDownload.PENDENTE,
        )
