"""
Gerenciamento de arquivos e estado de sincronização do sistema NFS-e.

Responsabilidades:
  - Salvar XMLs e PDFs em estrutura de pastas configurável via tags
  - Persistir o último NSU sincronizado (para retomada incremental)
  - Manter registro de notas com download pendente nos portais municipais

Tags disponíveis no path_structure:
  {ANO}              — Ano de emissão da nota (4 dígitos)
  {MES}              — Mês de emissão da nota (2 dígitos)
  {DIA}              — Dia de emissão (2 dígitos)
  {CNPJ_PRESTADOR}   — CNPJ do emissor da nota
  {CNPJ_TOMADOR}     — CNPJ do seu CNPJ (tomador)
  {MUNICIPIO}        — Código do município do prestador
  {TIPO}             — "xmls" ou "pdfs"

Exemplo de estrutura padrão:
  downloads/{CNPJ_PRESTADOR}/{ANO}/{MES}/xmls/nota.xml
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from core.models import NotaFiscal, StatusDownload

logger = logging.getLogger(__name__)

_DEFAULT_STRUCTURE = "{CNPJ_PRESTADOR}/{ANO}/{MES}"
_NSU_STATE_FILE = "nsu_state.json"
_PENDENTES_FILE = "pendentes_municipais.json"


class Storage:
    """Salva XMLs, PDFs e estado de sincronização em estrutura de pastas organizada."""

    def __init__(
        self,
        base_path: str = "downloads",
        path_structure: str = _DEFAULT_STRUCTURE,
        cnpj_tomador: Optional[str] = None,
    ) -> None:
        """
        Args:
            base_path: Diretório raiz onde os arquivos serão salvos.
            path_structure: Máscara de subpastas com tags (ver docstring do módulo).
            cnpj_tomador: CNPJ da empresa tomadora (usado na tag {CNPJ_TOMADOR}).
        """
        self.base_path = Path(base_path)
        self.path_structure = path_structure
        self.cnpj_tomador = cnpj_tomador
        self.base_path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Persistência de arquivos fiscais
    # ------------------------------------------------------------------

    def salvar_xml(self, nota: NotaFiscal) -> Optional[str]:
        """
        Salva o conteúdo XML da nota no disco.

        Returns:
            Caminho absoluto do arquivo salvo, ou None se a nota não tiver XML.
        """
        if not nota.xml_content:
            logger.warning("Nota NSU=%s sem xml_content — XML não salvo.", nota.nsu)
            return None

        diretorio = self._resolver_diretorio(nota, "xmls")
        nome = f"{nota.chave_acesso or nota.nsu}.xml"
        caminho = diretorio / nome

        xml_compacto = re.sub(r">\s+<", "><", nota.xml_content.strip())
        caminho.write_text(xml_compacto, encoding="utf-8")
        logger.debug("XML salvo: %s", caminho)
        return str(caminho)

    def salvar_pdf(self, nota: NotaFiscal, conteudo: bytes) -> Optional[str]:
        """
        Salva os bytes do PDF (DANFSE) no disco.

        Returns:
            Caminho absoluto do arquivo salvo.
        """
        diretorio = self._resolver_diretorio(nota, "pdfs")
        nome = f"{nota.chave_acesso or nota.nsu}.pdf"
        caminho = diretorio / nome

        caminho.write_bytes(conteudo)
        logger.debug("PDF salvo: %s", caminho)
        return str(caminho)

    # ------------------------------------------------------------------
    # Estado de sincronização (NSU)
    # ------------------------------------------------------------------

    def carregar_ultimo_nsu(self) -> str:
        """Lê o último NSU processado do arquivo de estado. Retorna "0" se inexistente."""
        caminho = self.base_path / _NSU_STATE_FILE
        if caminho.exists():
            try:
                data = json.loads(caminho.read_text(encoding="utf-8"))
                nsu = str(data.get("ultimo_nsu", "0"))
                logger.info("NSU retomado do estado salvo: %s", nsu)
                return nsu
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Erro ao ler arquivo de estado NSU: %s — iniciando do zero.", exc)
        return "0"

    def salvar_ultimo_nsu(self, nsu: str) -> None:
        """Persiste o último NSU processado para retomada incremental futura."""
        caminho = self.base_path / _NSU_STATE_FILE
        caminho.write_text(
            json.dumps(
                {"ultimo_nsu": nsu, "atualizado_em": datetime.now().isoformat()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("NSU salvo: %s → %s", nsu, caminho)

    # ------------------------------------------------------------------
    # Registro de pendentes municipais
    # ------------------------------------------------------------------

    def registrar_pendentes_municipais(self, notas: List[NotaFiscal]) -> None:
        """
        Acrescenta ao arquivo de pendentes as notas cujo PDF requer download municipal.
        Notas já registradas (mesma chave_acesso) são ignoradas para evitar duplicatas.
        """
        if not notas:
            return

        caminho = self.base_path / _PENDENTES_FILE
        existentes: list = []

        if caminho.exists():
            try:
                existentes = json.loads(caminho.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning("Arquivo de pendentes corrompido — será recriado.")

        chaves_existentes = {item.get("chave_acesso") for item in existentes}

        novos = [
            {
                "chave_acesso": n.chave_acesso,
                "nsu": n.nsu,
                "numero": n.numero,
                "cnpj_prestador": n.cnpj_prestador,
                "municipio_codigo": n.municipio_codigo,
                "valor": n.valor,
                "data_emissao": n.data_emissao.isoformat() if n.data_emissao else None,
                "status": StatusDownload.MUNICIPAL_NECESSARIO.value,
                "registrado_em": datetime.now().isoformat(),
            }
            for n in notas
            if n.chave_acesso not in chaves_existentes
        ]

        if novos:
            caminho.write_text(
                json.dumps(existentes + novos, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(
                "%d notas registradas em pendentes_municipais.json (%s).",
                len(novos), caminho,
            )

    # ------------------------------------------------------------------
    # Interno
    # ------------------------------------------------------------------

    def _resolver_diretorio(self, nota: NotaFiscal, tipo: str) -> Path:
        """Resolve e cria o diretório de destino aplicando as tags configuradas."""
        ano = mes = dia = "0000"
        if nota.data_emissao:
            ano = nota.data_emissao.strftime("%Y")
            mes = nota.data_emissao.strftime("%m")
            dia = nota.data_emissao.strftime("%d")

        tags = {
            "{ANO}": ano,
            "{MES}": mes,
            "{DIA}": dia,
            "{CNPJ_PRESTADOR}": nota.cnpj_prestador or "PRESTADOR_DESCONHECIDO",
            "{CNPJ_TOMADOR}": self.cnpj_tomador or nota.cnpj_tomador or "TOMADOR_DESCONHECIDO",
            "{MUNICIPIO}": nota.municipio_codigo or "nacional",
            "{TIPO}": tipo,
        }

        estrutura = self.path_structure
        for tag, valor in tags.items():
            estrutura = estrutura.replace(tag, valor)

        diretorio = self.base_path
        for parte in estrutura.replace("\\", "/").split("/"):
            if parte.strip():
                diretorio = diretorio / parte

        # Adiciona o subtipo (xmls/pdfs) se não estiver na estrutura
        if "{TIPO}" not in self.path_structure:
            diretorio = diretorio / tipo

        diretorio.mkdir(parents=True, exist_ok=True)
        return diretorio
