"""
Módulo para extração de dados de XMLs da NFS-e Nacional (padrão SPED).
Utiliza apenas a stdlib (xml.etree.ElementTree) — sem dependências extras.

Namespace oficial: http://www.sped.fazenda.gov.br/nfse

Funções exportadas:
    extrair_dados_nfse(xml_path)   — lê de arquivo no disco
    extrair_dados_nfse_string(xml) — lê de string (usado pela API ADN)
"""
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional, cast

logger = logging.getLogger(__name__)

# Namespace padrão da NFS-e Nacional
NS = {"nfse": "http://www.sped.fazenda.gov.br/nfse"}


def _find_text(root: Optional[ET.Element], xpath: str) -> Optional[str]:
    """Busca um elemento pelo XPath com namespace e retorna seu texto, ou None."""
    if root is None:
        return None
    el = root.find(xpath, NS)
    return el.text.strip() if el is not None and el.text else None


def _parse_float(value: Optional[str]) -> float:
    """Converte string monetária para float com segurança."""
    if not value:
        return 0.0
    try:
        return float(value.replace(",", ".").strip())
    except (ValueError, AttributeError):
        return 0.0


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    """
    Converte string ISO 8601 (com ou sem offset) para datetime.
    Exemplos: '2025-09-01T16:24:00-03:00', '2025-09-01T19:24:00Z'
    """
    if not value:
        return None
    # Remove o offset de timezone para simplificar a normalização do timestamp.
    try:
        # Tenta formato com offset: 2025-09-01T16:24:00-03:00
        clean = value[:19]  # pega só 'YYYY-MM-DDTHH:MM:SS'
        return datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return None


def _extrair_de_root(root: ET.Element) -> dict:
    """Núcleo de extração compartilhado entre as duas funções públicas."""
    inf = cast(ET.Element, root.find("nfse:infNFSe", NS) or root)

    chave = inf.get("Id", "").replace("NFS", "") if inf is not None else None
    numero = _find_text(inf, "nfse:nNFSe")

    data_str = _find_text(inf, "nfse:dhProc")
    data_emissao = _parse_datetime(data_str)
    if data_emissao is None:
        dps = inf.find("nfse:DPS/nfse:infDPS", NS)
        if dps is not None:
            data_emissao = _parse_datetime(_find_text(dps, "nfse:dhEmi"))

    valor_liq = _parse_float(_find_text(inf, "nfse:valores/nfse:vLiq"))

    dps_node = inf.find("nfse:DPS/nfse:infDPS", NS)
    valor_serv = 0.0
    cnpj_tomador = cpf_tomador = nome_tomador = descricao = municipio_codigo = None

    if dps_node is not None:
        valor_serv = _parse_float(
            _find_text(dps_node, "nfse:valores/nfse:vServPrest/nfse:vServ")
        )
        toma = dps_node.find("nfse:toma", NS)
        if toma is not None:
            cnpj_tomador = _find_text(toma, "nfse:CNPJ")
            cpf_tomador = _find_text(toma, "nfse:CPF")
            nome_tomador = _find_text(toma, "nfse:xNome")

        descricao = _find_text(dps_node, "nfse:serv/nfse:cServ/nfse:xDescServ")

        # Código do município do prestador (cMun dentro de infDPS)
        municipio_codigo = _find_text(dps_node, "nfse:cLocEmi")

    emit = inf.find("nfse:emit", NS)
    cnpj_prestador = _find_text(emit, "nfse:CNPJ") if emit is not None else None
    status_code = _find_text(inf, "nfse:cStat")

    return {
        "numero": numero,
        "chave": chave or None,
        "valor": valor_liq if valor_liq > 0 else valor_serv,
        "valor_servico": valor_serv,
        "data_emissao": data_emissao,
        "cnpj_prestador": cnpj_prestador,
        "cnpj_tomador": cnpj_tomador,
        "cpf_tomador": cpf_tomador,
        "nome_tomador": nome_tomador,
        "descricao_servico": descricao,
        "municipio_codigo": municipio_codigo,
        "status_code": status_code,
    }


def extrair_dados_nfse_string(xml_content: str) -> dict:
    """
    Extrai dados de um XML da NFS-e recebido como string (payload da API ADN).

    Args:
        xml_content: Conteúdo XML já decodificado (string UTF-8).

    Returns:
        dict com os mesmos campos de extrair_dados_nfse().
        Retorna {} em caso de erro de parse.
    """
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        logger.error("Erro ao parsear XML string: %s", exc)
        return {}
    return _extrair_de_root(root)


def extrair_dados_nfse(xml_path: str) -> dict:
    """
    Lê um arquivo XML da NFS-e Nacional do disco e extrai os campos relevantes.

    Args:
        xml_path: Caminho absoluto ou relativo do arquivo XML.

    Returns:
        dict com as chaves: numero, chave, valor, valor_servico, data_emissao,
        cnpj_prestador, cnpj_tomador, cpf_tomador, nome_tomador,
        descricao_servico, municipio_codigo, status_code.
        Retorna {} em caso de erro.
    """
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except ET.ParseError as exc:
        logger.error("Erro ao parsear XML %s: %s", xml_path, exc)
        return {}
    except FileNotFoundError:
        logger.error("Arquivo XML não encontrado: %s", xml_path)
        return {}
    return _extrair_de_root(root)
