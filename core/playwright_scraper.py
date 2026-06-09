"""
Automação do Portal Nacional NFS-e via Playwright (navegador real visível).

Usado quando o portal exige CAPTCHA para download de XML/PDF.
O navegador abre na tela — quando aparecer CAPTCHA, resolva manualmente
e o script continua automaticamente.

Requer:
    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional
import re

logger = logging.getLogger(__name__)

_BASE        = "https://www.nfse.gov.br/EmissorNacional"
_URL_LOGIN   = f"{_BASE}/Login"
_URL_DASH    = f"{_BASE}/Dashboard"
_URL_RECEB   = f"{_BASE}/Notas/Recebidas"
_URL_EMIT    = f"{_BASE}/Notas/Emitidas"

# Seletores de CAPTCHA conhecidos no portal
_CAPTCHA_SELETORES = [
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "div.g-recaptcha",
    "div#captcha",
    "input[name='captcha']",
    "#dvCaptcha",
    ".captcha",
]

# Tempo máximo aguardando o usuário resolver o CAPTCHA (segundos)
_TIMEOUT_CAPTCHA_S = 300   # 5 minutos
_TIMEOUT_NAV_MS    = 30_000
_TIMEOUT_DOWN_MS   = 60_000


class FalhaAutenticacaoError(Exception):
    """Credenciais inválidas ou sessão expirada."""


class PlaywrightScraper:
    """
    Automação do portal NFS-e usando Playwright com Chromium visível.

    O navegador permanece aberto entre chamadas — use como context manager:

        with PlaywrightScraper(cnpj="...", senha="...") as scraper:
            scraper.autenticar()
            notas = scraper.listar_notas("recebidas", "01/05/2026", "31/05/2026")
            scraper.baixar_arquivo(notas[0]["link_xml"], Path("saida.xml"))
    """

    RESULTADO_SUCESSO   = "sucesso"
    RESULTADO_CAPTCHA   = "captcha_resolvido"
    RESULTADO_ERRO      = "erro"

    def __init__(self, cnpj: str, senha: str, pasta_downloads: Optional[Path] = None) -> None:
        self.cnpj   = re.sub(r"\D", "", cnpj)
        self.senha  = senha
        self.pasta_downloads = pasta_downloads or Path("downloads_temp")
        self._pw        = None
        self._browser   = None
        self._context   = None
        self._page      = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "PlaywrightScraper":
        self._iniciar_navegador()
        return self

    def __exit__(self, *_) -> None:
        self.fechar()

    def fechar(self) -> None:
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._context = self._page = None

    # ------------------------------------------------------------------
    # Inicialização do navegador
    # ------------------------------------------------------------------

    def _iniciar_navegador(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw      = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=False,                   # navegador VISÍVEL
            slow_mo=120,                      # suaviza ações para parecer humano
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
        )
        self._context = self._browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        # Remove o sinal "controlado por automação"
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        self._page = self._context.new_page()
        self._page.set_default_timeout(_TIMEOUT_NAV_MS)
        logger.info("Navegador Chromium iniciado (modo visível).")

    # ------------------------------------------------------------------
    # Autenticação
    # ------------------------------------------------------------------

    def autenticar(self) -> bool:
        """
        Faz login no portal via CNPJ + senha.
        Se aparecer CAPTCHA na tela de login, aguarda resolução manual.
        """
        page = self._page
        logger.info("Acessando portal: %s", _URL_LOGIN)
        page.goto(_URL_LOGIN, wait_until="domcontentloaded")

        # Preenche CNPJ
        page.fill("input[name='Inscricao']", self.cnpj)
        time.sleep(0.3)

        # Preenche senha
        page.fill("input[name='Senha']", self.senha)
        time.sleep(0.3)

        # Verifica CAPTCHA na tela de login antes de submeter
        self._aguardar_captcha_se_necessario("login")

        # Clica em Entrar
        page.click("button[type='submit'], input[type='submit']")

        # Aguarda redirecionamento para o dashboard
        try:
            page.wait_for_url(f"**{_URL_DASH}**", timeout=15_000)
        except Exception:
            # Verifica se voltou para o login (credenciais erradas)
            if "Login" in page.url or "login" in page.url:
                raise FalhaAutenticacaoError(
                    f"Login falhou para CNPJ {self.cnpj}. Verifique a senha."
                )

        logger.info("Autenticado com sucesso: CNPJ %s", self.cnpj)
        return True

    # ------------------------------------------------------------------
    # Listagem de notas
    # ------------------------------------------------------------------

    def listar_e_baixar_notas(
        self,
        tipo: str,
        data_inicio: str,
        data_fim: str,
        base_path: Path,
        nome_empresa: str = "",
        cnpj: str = "",
    ) -> dict:
        """
        Lista notas do período E faz o download de cada uma clicando
        diretamente nos links da tabela (sem navegar para URL direta).

        Retorna dict com: notas, xmls, pdfs, captchas, erros, registros_excel
        """
        from core.relatorio_excel import gerar_relatorio

        url_lista = _URL_EMIT if tipo == "emitidas" else _URL_RECEB
        page = self._page
        page.goto(url_lista, wait_until="domcontentloaded")
        self._preencher_filtro_datas(data_inicio, data_fim)

        try:
            page.wait_for_selector("tbody tr", timeout=10_000)
        except Exception:
            logger.info("Nenhuma nota encontrada no período.")
            return {"notas": 0, "xmls": 0, "pdfs": 0, "captchas": 0,
                    "erros": 0, "registros_excel": []}

        # Coleta os dados de todas as linhas ANTES de baixar
        notas_info = self._extrair_notas_da_pagina(tipo)
        total = len(notas_info)
        logger.info("Portal %s: %d nota(s) encontrada(s) de %s a %s.",
                    tipo, total, data_inicio, data_fim)

        xmls = pdfs = captchas = erros = 0
        registros_excel = []

        for idx, nota in enumerate(notas_info):
            numero       = nota.get("numero") or ""
            data_emissao = nota.get("data_emissao", "")
            valor        = nota.get("valor", "")
            status_nota  = nota.get("status", "")
            chave        = nota.get("chave_acesso", "")
            link_xml     = nota.get("link_xml", "")
            link_pdf     = nota.get("link_pdf", "")

            nome_arq = numero or chave or f"nota_{idx+1}"
            logger.info("  [%d/%d] #%s | %s | %s", idx+1, total, nome_arq, data_emissao, valor)

            xml_baixado = pdf_baixado = False

            # Volta para a página de listagem antes de cada download
            # (evita perder o contexto de sessão)
            if page.url.split("?")[0] != url_lista:
                page.goto(url_lista, wait_until="domcontentloaded")
                self._preencher_filtro_datas(data_inicio, data_fim)
                page.wait_for_selector("tbody tr", timeout=10_000)

            # --- Clica no link XML pela href exata na tabela ---
            if link_xml:
                dest = base_path / "xmls" / f"{nome_arq}.xml"
                ok, st = self._clicar_link_download(link_xml, dest, "XML")
                if ok:
                    xmls += 1
                    xml_baixado = True
                    if st == self.RESULTADO_CAPTCHA:
                        captchas += 1
                    link_xml = ""
                else:
                    erros += 1

            # --- Clica no link PDF pela href exata na tabela ---
            if link_pdf:
                dest = base_path / "pdfs" / f"{nome_arq}.pdf"
                ok, st = self._clicar_link_download(link_pdf, dest, "PDF")
                if ok:
                    pdfs += 1
                    pdf_baixado = True
                    if st == self.RESULTADO_CAPTCHA:
                        captchas += 1
                    link_pdf = ""
                else:
                    erros += 1

            registros_excel.append({
                "empresa":      nome_empresa,
                "cnpj":         cnpj,
                "numero":       numero,
                "data_emissao": data_emissao,
                "valor":        valor,
                "status":       status_nota,
                "chave_acesso": chave,
                "link_xml":     link_xml,
                "link_pdf":     link_pdf,
            })

        # Relatório Excel
        if registros_excel:
            mes_ano = data_inicio.replace("/", "-")[3:]  # "05/2026" → "05-2026"
            caminho_excel = base_path / f"relatorio_recebidas_{cnpj}_{mes_ano}.xlsx"
            gerar_relatorio(registros_excel, caminho_excel)
            logger.info("Relatório Excel: %s", caminho_excel)

        return {"notas": total, "xmls": xmls, "pdfs": pdfs,
                "captchas": captchas, "erros": erros,
                "registros_excel": registros_excel}

    def _preencher_filtro_datas(self, data_inicio: str, data_fim: str) -> None:
        page = self._page
        # Tenta pelo name, depois pelo placeholder
        for seletor in ["input[name='datainicio']", "input[placeholder*='nício']", "#DataInicio"]:
            try:
                page.fill(seletor, data_inicio, timeout=3_000)
                break
            except Exception:
                continue

        for seletor in ["input[name='datafim']", "input[placeholder*='fim']", "#DataFim"]:
            try:
                page.fill(seletor, data_fim, timeout=3_000)
                break
            except Exception:
                continue

        # Clica em Pesquisar/Filtrar
        for seletor in ["button[type='submit']", "input[type='submit']", "#btnBuscar", ".btn-pesquisar"]:
            try:
                page.click(seletor, timeout=3_000)
                time.sleep(1.5)
                break
            except Exception:
                continue

    def _extrair_notas_da_pagina(self, tipo: str) -> list[dict]:
        """Extrai dados das linhas da tabela usando o DOM."""
        page = self._page
        notas = []

        linhas = page.query_selector_all("tbody tr")
        for linha in linhas:
            # Atributos data-* da linha
            numero       = linha.get_attribute("data-numero") or ""
            data_emissao = linha.get_attribute("data-emissao") or ""
            valor        = linha.get_attribute("data-valor") or ""
            chave        = linha.get_attribute("data-chave") or ""
            status_raw   = (linha.get_attribute("data-situacao")
                            or linha.get_attribute("data-status")
                            or "gerada")
            status = status_raw.split("_")[-1].lower()

            # Se não achou nos atributos, varre as células
            if not numero or not data_emissao:
                celulas = linha.query_selector_all("td")
                for cel in celulas:
                    txt = (cel.inner_text() or "").strip()
                    if not data_emissao and re.match(r"\d{2}/\d{2}/\d{4}", txt):
                        data_emissao = txt
                    elif not valor and "R$" in txt:
                        valor = txt
                    elif not numero and re.match(r"^\d{1,15}$", txt):
                        numero = txt

            # Links de ação
            link_xml = link_pdf = ""
            menu = linha.query_selector("div.list-group.menu-content, ul.dropdown-menu")
            if menu:
                for a in menu.query_selector_all("a"):
                    href  = a.get_attribute("href") or ""
                    texto = (a.inner_text() or "").strip().lower()
                    if not href:
                        continue
                    full = href if href.startswith("http") else f"https://www.nfse.gov.br{href}"

                    # Extrai chave do href se ainda não temos
                    if not chave:
                        m = re.search(r"/(\d{44,50})", href)
                        if m:
                            chave = m.group(1)

                    if "xml" in texto or "nfse" in href.lower():
                        link_xml = full
                    elif "danfse" in texto or "pdf" in texto or "danfse" in href.lower():
                        link_pdf = full

            if link_xml or link_pdf or chave:
                notas.append({
                    "numero":       numero,
                    "data_emissao": data_emissao,
                    "valor":        valor,
                    "status":       status,
                    "chave_acesso": chave,
                    "link_xml":     link_xml,
                    "link_pdf":     link_pdf,
                    "tipo":         tipo,
                })

        return notas

    # ------------------------------------------------------------------
    # Download — clica no link dentro da tabela (não navega para URL direta)
    # ------------------------------------------------------------------

    def _clicar_link_download(
        self,
        href_alvo: str,
        destino: Path,
        tipo_arquivo: str = "",
    ) -> tuple[bool, str]:
        """
        Localiza o link na tabela pelo href e clica nele.
        Captura o download resultante e salva em `destino`.

        Estratégia:
          1. Procura <a href="..."> na página cujo href bate com href_alvo
          2. Clica — o portal processa a requisição com o contexto de sessão correto
          3. Se aparecer CAPTCHA, aguarda resolução manual
          4. Captura o arquivo baixado via expect_download()
        """
        page = self._page
        destino.parent.mkdir(parents=True, exist_ok=True)

        # Normaliza o href para comparação (remove domínio se presente)
        href_relativo = href_alvo.replace("https://www.nfse.gov.br", "")

        logger.info("  ⬇ %s → %s", tipo_arquivo, destino.name)

        try:
            # Localiza o elemento <a> pelo href
            elemento = page.locator(f'a[href="{href_relativo}"]').first
            if not elemento.is_visible(timeout=3_000):
                # Tenta com URL completa
                elemento = page.locator(f'a[href="{href_alvo}"]').first

            captcha_apareceu = False

            # Abre o menu dropdown da nota se o link não estiver visível
            if not elemento.is_visible(timeout=2_000):
                self._abrir_menu_nota_por_href(href_relativo)
                time.sleep(0.5)

            # Clica e captura o download
            with page.expect_download(timeout=_TIMEOUT_DOWN_MS) as dl_info:
                elemento.click(timeout=5_000)
                time.sleep(0.8)

                # Verifica CAPTCHA após o clique
                captcha_apareceu = self._aguardar_captcha_se_necessario(tipo_arquivo)

                # Botão de confirmação pós-CAPTCHA (se existir)
                if captcha_apareceu:
                    for sel in ["button#btnDownload", "button.btn-confirmar",
                                "input[value='Confirmar']", "button[type='submit']"]:
                        try:
                            page.click(sel, timeout=3_000)
                            break
                        except Exception:
                            continue

            dl = dl_info.value
            dl.save_as(str(destino))
            logger.info("  ✅ %s salvo: %s", tipo_arquivo, destino.name)
            status = self.RESULTADO_CAPTCHA if captcha_apareceu else self.RESULTADO_SUCESSO
            return True, status

        except Exception as exc:
            # Fallback: tenta capturar conteúdo XML da página atual
            if "xml" in tipo_arquivo.lower():
                conteudo = self._salvar_xml_da_pagina(destino)
                if conteudo:
                    return True, self.RESULTADO_SUCESSO

            logger.warning("  ⚠️  Falha %s (%s): %s", tipo_arquivo, destino.name, exc)
            return False, self.RESULTADO_ERRO

    def _abrir_menu_nota_por_href(self, href_parcial: str) -> None:
        """Clica no botão de ações da linha que contém o href alvo."""
        page = self._page
        linhas = page.query_selector_all("tbody tr")
        for linha in linhas:
            links = linha.query_selector_all("a")
            for a in links:
                if href_parcial in (a.get_attribute("href") or ""):
                    # Abre o dropdown de ações dessa linha
                    btn = linha.query_selector(
                        "button.dropdown-toggle, a.dropdown-toggle, .btn-acoes"
                    )
                    if btn:
                        btn.click()
                        time.sleep(0.4)
                    return

    def _salvar_xml_da_pagina(self, destino: Path) -> bool:
        """Captura o conteúdo XML exibido diretamente na página."""
        try:
            content = self._page.content()
            if "<NFS" in content or "<?xml" in content.lower():
                destino.write_text(content, encoding="utf-8")
                logger.info("  ✅ XML capturado da página: %s", destino.name)
                return True
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # Detecção e espera de CAPTCHA
    # ------------------------------------------------------------------

    def _aguardar_captcha_se_necessario(self, contexto: str = "") -> bool:
        """
        Verifica se há CAPTCHA na página. Se sim, pausa e aguarda resolução manual.

        Returns:
            True se CAPTCHA foi detectado e resolvido, False se não havia.
        """
        page = self._page
        time.sleep(0.5)  # pequena pausa para o CAPTCHA renderizar

        captcha_presente = False
        for seletor in _CAPTCHA_SELETORES:
            try:
                el = page.query_selector(seletor)
                if el and el.is_visible():
                    captcha_presente = True
                    break
            except Exception:
                continue

        if not captcha_presente:
            return False

        # Alerta sonoro e mensagem no console
        print("\n" + "🔴 " * 30)
        print(f"  CAPTCHA DETECTADO [{contexto}] — CNPJ {self.cnpj}")
        print("  ➡️  Resolva o CAPTCHA na janela do navegador.")
        print("  ⏳  Aguardando até 5 minutos...")
        print("🔴 " * 30 + "\n")

        inicio = time.time()
        while time.time() - inicio < _TIMEOUT_CAPTCHA_S:
            time.sleep(1.5)
            # Verifica se CAPTCHA sumiu
            ainda_presente = False
            for seletor in _CAPTCHA_SELETORES:
                try:
                    el = page.query_selector(seletor)
                    if el and el.is_visible():
                        ainda_presente = True
                        break
                except Exception:
                    continue

            if not ainda_presente:
                print("  ✅ CAPTCHA resolvido! Continuando...\n")
                logger.info("CAPTCHA resolvido pelo usuário [%s].", contexto)
                time.sleep(0.8)
                return True

        logger.warning("Timeout aguardando CAPTCHA [%s].", contexto)
        return True
