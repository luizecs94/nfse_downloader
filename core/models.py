"""Modelos de dados do sistema NFS-e Nacional."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class StatusDownload(Enum):
    """Estado do download de um arquivo para uma nota."""
    PENDENTE = "pendente"
    SUCESSO = "sucesso"
    MUNICIPAL_NECESSARIO = "municipal_necessario"  # API retornou 403
    ERRO = "erro"


@dataclass
class NotaFiscal:
    """Representa uma NFS-e recebida, com metadados e estado dos downloads."""
    nsu: str
    chave_acesso: str

    # Metadados extraídos do XML
    numero: Optional[str] = None
    data_emissao: Optional[datetime] = None
    cnpj_prestador: Optional[str] = None
    nome_prestador: Optional[str] = None
    cnpj_tomador: Optional[str] = None
    cpf_tomador: Optional[str] = None
    nome_tomador: Optional[str] = None
    valor: float = 0.0
    valor_servico: float = 0.0
    descricao_servico: Optional[str] = None
    municipio_codigo: Optional[str] = None
    status_code: Optional[str] = None

    # Conteúdo bruto (XML já decodificado do GZip+Base64)
    xml_content: Optional[str] = None

    # Estado dos arquivos salvos
    status_xml: StatusDownload = StatusDownload.PENDENTE
    status_pdf: StatusDownload = StatusDownload.PENDENTE
    caminho_xml: Optional[str] = None
    caminho_pdf: Optional[str] = None


@dataclass
class ResultadoSincronizacao:
    """Resumo de uma execução de sincronização."""
    notas_encontradas: int = 0
    xmls_salvos: int = 0
    pdfs_salvos: int = 0
    municipais_pendentes: int = 0
    erros: List[str] = field(default_factory=list)
    ultimo_nsu: str = "0"
