"""
Cliente web scraping para o Portal Nacional NFS-e (Emissor Nacional).

Usado como:
  1. Fallback de autenticação quando certificado digital não está disponível
  2. Consulta de notas EMITIDAS (ADN só distribui notas recebidas)

Portal: https://www.nfse.gov.br/EmissorNacional

Limitações vs API oficial:
  - Depende do layout HTML do portal (pode quebrar em mudanças visuais)
  - Mais lento (carrega página completa a cada requisição)
  - Não tem paginação por NSU — filtra por período de datas
"""

import logging
import re
from typing import Any, Dict, List, Optional

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

_BASE = "https://www.nfse.gov.br/EmissorNacional"
_URL_LOGIN = f"{_BASE}/Login"
_URL_DASHBOARD = f"{_BASE}/Dashboard"
_URL_EMITIDAS = f"{_BASE}/Notas/Emitidas"
_URL_RECEBIDAS = f"{_BASE}/Notas/Recebidas"


class FalhaAutenticacaoError(Exception):
    """Credenciais inválidas ou sessão expirada."""


class PortalScraper:
    """
    Acessa o portal web do Emissor Nacional via CNPJ + senha.

    Uso:
        scraper = PortalScraper(cnpj="00000000000000", senha="****")
        scraper.autenticar()
        notas = scraper.listar_notas("emitidas", "01/05/2026", "31/05/2026")
        xml = scraper.baixar_xml(notas[0]["download_xml"])
    """

    def __init__(self, cnpj: str, senha: str) -> None:
        self.cnpj = re.sub(r"\D", "", cnpj)
        self.senha = senha
        self._token_csrf: Optional[str] = None
        self._cnpj_autenticado: Optional[str] = None
        self._sessao = requests.Session()
        self._sessao.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        })
        self._configurar_retries()

    # ------------------------------------------------------------------
    # Configuração
    # ------------------------------------------------------------------

    def _configurar_retries(self) -> None:
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._sessao.mount("https://", adapter)
        self._sessao.mount("http://", adapter)

    # ------------------------------------------------------------------
    # Autenticação
    # ------------------------------------------------------------------

    def autenticar(self) -> bool:
        """
        Realiza login no portal via CNPJ + senha.

        Raises:
            FalhaAutenticacaoError: se o CSRF não for encontrado ou as credenciais forem inválidas.
        """
        self._obter_token_csrf()

        payload = {
            "Inscricao": self.cnpj,
            "Senha": self.senha,
            "__RequestVerificationToken": self._token_csrf,
        }
        headers = {
            "origin": "https://www.nfse.gov.br",
            "referer": _URL_LOGIN,
        }
        self._sessao.post(
            _URL_LOGIN, data=payload, headers=headers,
            verify=False, timeout=30,
        )

        if not self._verificar_sessao():
            raise FalhaAutenticacaoError(
                "Autenticação falhou. Verifique NFSE_USUARIO e NFSE_SENHA no .env"
            )

        logger.info("Portal: autenticado como CNPJ %s", self._cnpj_autenticado or self.cnpj)
        return True

    def _obter_token_csrf(self) -> None:
        resp = self._sessao.get(_URL_LOGIN, verify=False, timeout=30)
        soup = BeautifulSoup(resp.content, "html.parser")
        form = soup.find("form")
        if form:
            inp = form.find("input", {"name": "__RequestVerificationToken"})
            if inp and inp.get("value"):
                self._token_csrf = str(inp["value"])
                return
        raise FalhaAutenticacaoError("Token CSRF não encontrado na página de login.")

    def _verificar_sessao(self) -> bool:
        resp = self._sessao.get(_URL_DASHBOARD, timeout=30)
        soup = BeautifulSoup(resp.content, "html.parser")
        menu = soup.find("li", class_="dropdown perfil")
        if menu:
            li = menu.find("li")
            if li:
                match = re.search(r"\d{11,14}", li.text.strip())
                if match:
                    self._cnpj_autenticado = match.group()
                return True
        return False

    # ------------------------------------------------------------------
    # Listagem de notas
    # ------------------------------------------------------------------

    def listar_notas(
        self,
        tipo: str,
        data_inicio: str,
        data_fim: str,
    ) -> List[Dict[str, Any]]:
        """
        Lista notas fiscais do período informado.

        Args:
            tipo: "emitidas" ou "recebidas"
            data_inicio: formato DD/MM/AAAA
            data_fim: formato DD/MM/AAAA

        Returns:
            Lista de dicts com chaves: numero, data_emissao, valor, status,
            download_xml, download_pdf (ou download_danfs-e), tipo.
        """
        url = _URL_EMITIDAS if tipo == "emitidas" else _URL_RECEBIDAS
        params = {"busca": "", "datainicio": data_inicio, "datafim": data_fim}
        self._sessao.headers.update({"Referer": url})

        resp = self._sessao.get(url, params=params, timeout=30)
        notas = self._parsear_tabela(resp.content, tipo)
        logger.info("Portal %s: %d nota(s) encontrada(s) de %s a %s.",
                    tipo, len(notas), data_inicio, data_fim)
        return notas

    def _parsear_tabela(self, content: bytes, tipo: str) -> List[Dict[str, Any]]:  # noqa: C901
        soup = BeautifulSoup(content, "html.parser")
        tbody = soup.find("tbody")

        if not tbody:
            span = (
                soup.find("span", {"class": "field-validation-error"})
                or soup.find("span", {"class": "sem-registros"})
            )
            if span:
                logger.info("Portal: %s", span.get_text(strip=True))
            return []

        notas = []
        linhas = tbody.find_all("tr")
        _debug_feito = False
        for linha in linhas:
            div = linha.find("div", {"class": "list-group menu-content"})
            if not div:
                continue

            # Log de diagnóstico na primeira linha (apenas uma vez por listagem)
            if not _debug_feito and logger.isEnabledFor(logging.DEBUG):
                logger.debug("HTML primeira linha (attrs): %s", linha.attrs)
                for i, col in enumerate(linha.find_all("td")):
                    logger.debug("  td[%d]: %r", i, col.get_text(strip=True))
                _debug_feito = True

            # Status da nota (data-situacao ou data-status)
            raw = (
                linha.get("data-situacao")
                or linha.get("data-status")
                or "gerada"
            )
            status = (raw[0] if isinstance(raw, list) else raw).split("_")[-1].lower()

            # Metadados das colunas — tenta primeiro atributos data-* da linha,
            # depois varre o texto das células
            numero      = str(linha.get("data-numero", "")).strip()
            data_emissao = str(linha.get("data-emissao", "")).strip()
            valor       = str(linha.get("data-valor", "")).strip()

            # Se não achou nos atributos, varre as células de texto
            if not numero or not data_emissao:
                for col in linha.find_all("td"):
                    txt = col.get_text(strip=True)
                    if not data_emissao and re.match(r"\d{2}/\d{2}/\d{4}", txt):
                        data_emissao = txt
                    elif not valor and "R$" in txt:
                        valor = txt
                    elif not numero and re.match(r"^\d{1,15}$", txt):
                        numero = txt

            if not valor:
                valor = "0,00"

            # Chave de acesso — atributo data-chave ou href dos links
            chave_acesso = str(linha.get("data-chave", "")).strip()

            # Links de ação
            links: Dict[str, Any] = {}
            for a in div.find_all("a"):
                href = a.get("href", "")
                if not href:
                    continue
                # Extrai a chave de acesso do href se ainda não temos
                if not chave_acesso:
                    m = re.search(r"/(\d{44,50})", href)
                    if m:
                        chave_acesso = m.group(1)
                full_url = href if href.startswith("http") else f"https://www.nfse.gov.br{href}"
                key = a.get_text(strip=True).replace(" ", "_").lower()
                if key:
                    links[key] = full_url

            # Remove ações que não interessam
            for chave in ["cancelar_nfs-e", "substituir", "rejeitar", "confirmar"]:
                links.pop(chave, None)

            links.update({
                "numero":        numero,
                "data_emissao":  data_emissao,
                "valor":         valor,
                "status":        status,
                "tipo":          tipo,
                "chave_acesso":  chave_acesso,
            })
            notas.append(links)

        return notas

    # ------------------------------------------------------------------
    # Download de arquivos
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Constantes de resultado de download
    # ------------------------------------------------------------------

    RESULTADO_SUCESSO   = "sucesso"
    RESULTADO_MUNICIPAL = "municipal"   # 403 — nota pertence a município local
    RESULTADO_ERRO      = "erro"

    def baixar_xml(self, url: str) -> tuple[Optional[bytes], str]:
        """
        Baixa o XML da nota.

        Returns:
            (conteudo, status) onde status é um dos valores RESULTADO_*
        """
        try:
            resp = self._sessao.get(url, timeout=30)
            if resp.status_code == 403:
                logger.info("XML municipal (403) — nota requer portal municipal: %s", url)
                return None, self.RESULTADO_MUNICIPAL
            resp.raise_for_status()
            return resp.content, self.RESULTADO_SUCESSO
        except Exception as exc:
            logger.error("Erro ao baixar XML (%s): %s", url, exc)
            return None, self.RESULTADO_ERRO

    def baixar_pdf(self, url: str) -> tuple[Optional[bytes], str]:
        """
        Baixa o PDF (DANFSE) da nota.

        Returns:
            (conteudo, status) onde status é um dos valores RESULTADO_*
        """
        try:
            resp = self._sessao.get(url, stream=True, timeout=30)
            if resp.status_code == 403:
                logger.info("PDF municipal (403) — nota requer portal municipal: %s", url)
                return None, self.RESULTADO_MUNICIPAL
            resp.raise_for_status()
            return resp.content, self.RESULTADO_SUCESSO
        except Exception as exc:
            logger.error("Erro ao baixar PDF (%s): %s", url, exc)
            return None, self.RESULTADO_ERRO
