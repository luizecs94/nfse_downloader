# 📄 NFS-e Downloader

Ferramenta Python para baixar automaticamente as **NFS-e recebidas** de uma empresa via **API oficial do Portal Nacional NFS-e (ADN)**.

- ✅ Autenticação segura via Certificado Digital A1 (mTLS)
- ✅ Paginação incremental por NSU — retoma de onde parou
- ✅ Salva XML e PDF organizados por CNPJ, ano e mês
- ✅ Detecta automaticamente notas de portais municipais (403)
- ✅ Dois modos: automático (mês anterior) e manual (período personalizado)

---

## ⚠️ Pré-requisito: Certificado Digital

A API oficial ADN usa autenticação **mTLS** — o servidor identifica a empresa pelo certificado digital. **Não é possível usar apenas login e senha para acesso via API.**

Você precisará do arquivo `.pfx` (A1) da empresa com sua senha.

---

## 🚀 Configuração para nova empresa

### Passo 1 — Clone ou copie o projeto

```bash
git clone https://github.com/SEU_USUARIO/nfse_downloader.git
cd nfse_downloader
```

Ou simplesmente copie a pasta para o computador da empresa.

---

### Passo 2 — Crie o ambiente virtual Python

**Windows:**
```powershell
python -m venv .venv
.\.venv\Scripts\activate
```

**Linux / Mac:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

---

### Passo 3 — Instale as dependências

```bash
pip install -r requirements.txt
```

---

### Passo 4 — Configure o arquivo `.env`

Copie o arquivo de exemplo e preencha com os dados da empresa:

```bash
# Windows
copy .env.example .env

# Linux/Mac
cp .env.example .env
```

Abra o `.env` em qualquer editor de texto e preencha:

```env
EMPRESA_NOME=Nome da Empresa Ltda
NFSE_USUARIO=00000000000000        # CNPJ somente números
CERTIFICADO_PATH=certificados/empresa.pfx
CERTIFICADO_SENHA=senha_do_certificado
```

> O `.env` nunca é enviado ao Git — seus dados ficam apenas no seu computador.

---

### Passo 5 — Coloque o certificado na pasta `certificados/`

```
nfse_downloader/
  certificados/
    empresa.pfx   ← coloque aqui
```

> A pasta `certificados/` também está no `.gitignore` — o arquivo `.pfx` nunca será enviado ao repositório.

---

### Passo 6 — Execute

**Download automático (mês anterior ao atual):**
```bash
python main.py
```

**Download com período personalizado:**
```bash
python main_manual.py
```
O programa vai perguntar a data de início e fim antes de executar.

---

## 📁 Estrutura dos arquivos gerados

```
downloads/
  00000000000000/          ← CNPJ da empresa (tomador)
    2026/
      05/                  ← mês das notas
        xmls/
          chave_acesso.xml
        pdfs/
          chave_acesso.pdf
  nsu_state.json           ← controle de sincronização (não apague)
  pendentes_municipais.json← notas com PDF pendente no município
```

### O que é `nsu_state.json`?

Guarda o último NSU (Número Sequencial Único) processado. Na próxima execução, o sistema retoma exatamente de onde parou — sem baixar duplicatas.

### O que é `pendentes_municipais.json`?

Quando a API retorna **HTTP 403** para o PDF, significa que o município do prestador ainda não aderiu ao padrão nacional e mantém portal próprio. Nesses casos:
- ✅ O **XML** é salvo normalmente
- ⚠️ O **PDF** precisa ser baixado manualmente no portal da prefeitura
- 📋 Todos os dados necessários ficam registrados neste arquivo

---

## 🗂️ Estrutura do projeto

```
nfse_downloader/
├── core/
│   ├── api_client.py     — cliente HTTP com autenticação mTLS
│   ├── adn_service.py    — consulta DFe por NSU com paginação
│   ├── certificado.py    — carrega .pfx e gera certificados temporários
│   ├── downloader.py     — download de PDF com tratamento de erro 403
│   ├── models.py         — modelos de dados (NotaFiscal, StatusDownload…)
│   ├── storage.py        — salvamento de arquivos e estado de sincronização
│   └── xml_parser.py     — extração de dados do XML da NFS-e
├── municipal/            — (futuro) automação para portais municipais
├── certificados/         — coloque o .pfx aqui (ignorado pelo Git)
├── .env.example          — modelo de configuração
├── .gitignore
├── main.py               — execução automática (mês anterior)
├── main_manual.py        — execução com período personalizado
└── requirements.txt
```

---

## 🔄 Fluxo de funcionamento

```
Certificado .pfx
      ↓
  Autenticação mTLS na API ADN
      ↓
  Consulta DFe por NSU (50 por vez)
      ↓
  Para cada nota:
    ├── Decodifica XML (GZip + Base64)
    ├── Salva XML em disco
    └── Solicita PDF (DANFSE)
          ├── HTTP 200 → salva PDF
          └── HTTP 403 → registra em pendentes_municipais.json
      ↓
  Salva último NSU para próxima execução
```

---

## ❓ Dúvidas frequentes

**P: Posso usar login e senha em vez de certificado?**
R: Não para esta API. A API ADN exige mTLS — o certificado é obrigatório para empresas. Apenas MEIs podem usar login GOV.BR no portal web.

**P: O que fazer se aparecer um aviso sobre parsing BER no certificado?**
R: É um aviso do Python sobre o formato interno do `.pfx`, não um erro. O certificado funciona normalmente.

**P: Como rodar para mais de uma empresa?**
R: Copie a pasta inteira do projeto para cada empresa, cada uma com seu próprio `.env` e certificado. Os downloads ficam isolados pela pasta `downloads/`.

**P: O que é o NSU?**
R: Número Sequencial Único — é a forma que o Portal Nacional usa para distribuir documentos. Cada nota recebida tem um NSU único e crescente. O sistema usa esse número para saber de onde continuar nas próximas execuções.
