"""Bot financeiro do Telegram — paridade total com o bot do Discord.

Toda a UI interativa vive num Telegram Mini App (pagina estatica hospedada no
GitHub Pages, em `docs/`). O painel e um teclado de respostas persistente:

  - Botoes de formulario (Receita, Debito, Cartao, PIX, Wishlist, Investir,
    Assinatura, Fatura, SQL) abrem o Mini App e devolvem os dados via
    `WebApp.sendData(JSON)` (tratado em `on_webapp_data`).
  - Botoes que dependem de dados ao vivo (PIX Editar, Ass On/Off) sao botoes de
    texto: o bot le o banco e reabre o Mini App ja com a lista embutida na URL.
  - Botoes de acao (Status, ETL, Limpar) executam direto no bot.

Alem do Mini App, mantem os comandos manuais (/debito, /receita, ...) e o
registro por texto livre com IA. Apple Pay permanece exclusivo do Discord.
"""

import base64
import asyncio
import html
import json
import os
import subprocess
import sys
from collections import deque
from datetime import datetime
from functools import wraps

import pandas as pd
from telegram import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from config import AUTHORIZED_USER_ID, SCHEMA_ABAS, TELEGRAM_TOKEN
from services.ai_parser import interpretar_gasto_com_ia
from services.ai_parser import (
    construir_consulta_financeira,
    interpretar_consulta_financeira,
    parece_consulta_financeira,
    rotular_periodo_consulta,
)
from database import (
    adicionar_linha_db,
    atualizar_registro_db,
    buscar_registro_db,
    executar_sql_parametrizado,
    executar_sql_livre,
    ler_tabela_db,
)
from services.google_sheets import atualizar_registro_sheets, conectar_google_sheets
from ui.constants import (
    CARTOES,
    CLASSES_INVESTIMENTO,
    CLASSE_INVESTIMENTO_PADRAO,
    PERIODICIDADES,
    PERIODICIDADE_PADRAO,
)
from ui.table_image import montar_resposta_sql
from utils.data_utils import converter_numero_flexivel, resolver_cobranca_assinatura
from utils.logging_config import get_logger

logger = get_logger(__name__)

spreadsheet = None

# URL do Mini App (GitHub Pages). Pode ser sobrescrita em config.py se um dia
# houver dominio proprio; caso contrario, usa o Pages do repositorio publico.
MINIAPP_URL = getattr(config, "MINIAPP_URL", "https://bernardomrtns.github.io/financial-pbi/")

# Raiz do projeto (este arquivo agora vive em bots/, entao subimos um nivel).
PROJETO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_SCRIPT = os.path.join(PROJETO_ROOT, "main.py")


# ==========================================
#         ROTULOS DO PAINEL (TECLADO)
# ==========================================

BTN_PIX_EDITAR = "🔁 PIX Editar"
BTN_ASS_TOGGLE = "🔄 Ass On/Off"
BTN_STATUS = "🖥️ Status"
BTN_ETL = "⚙️ ETL"
BTN_LIMPAR = "🧹 Limpar"


def _webapp(rotulo: str, screen: str) -> KeyboardButton:
    """Botao de teclado que abre uma tela do Mini App."""
    return KeyboardButton(rotulo, web_app=WebAppInfo(url=f"{MINIAPP_URL}#{screen}"))


def painel_keyboard() -> ReplyKeyboardMarkup:
    """Teclado persistente que reproduz o painel do Discord."""
    return ReplyKeyboardMarkup(
        [
            [_webapp("🟢 Receita", "receita"), _webapp("🔴 Débito", "debito")],
            [_webapp("💳 Cartão", "cartao"), _webapp("🔁 PIX", "pix")],
            [_webapp("⭐ Wishlist", "wishlist"), _webapp("📈 Investir", "invest")],
            [_webapp("🔔 Assinatura", "assinatura"), _webapp("📅 Fatura", "fatura")],
            [KeyboardButton(BTN_PIX_EDITAR), KeyboardButton(BTN_ASS_TOGGLE)],
            [_webapp("🗄️ SQL", "sql"), KeyboardButton(BTN_STATUS)],
            [KeyboardButton(BTN_ETL), KeyboardButton(BTN_LIMPAR)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# ==========================================
#         SEGURANCA + RASTREIO DE MENSAGENS
# ==========================================


def restrito(func):
    """Garante que apenas o administrador autorizado interaja com o bot."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update.effective_user or update.effective_user.id != AUTHORIZED_USER_ID:
            uid = getattr(update.effective_user, "id", "?")
            logger.warning("Acesso nao autorizado bloqueado. ID: %s", uid)
            if update.effective_message:
                await update.effective_message.reply_text("⛔ Acesso restrito. Este bot é privado.")
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


def _rastrear(context: ContextTypes.DEFAULT_TYPE, message) -> None:
    """Guarda o id de uma mensagem do bot para o /limpar poder apaga-la depois."""
    if message is None:
        return
    fila: deque = context.chat_data.setdefault("bot_msgs", deque(maxlen=300))
    fila.append(message.message_id)

def _formatar_moeda_br(valor) -> str:
    try:
        numero = float(valor)
    except Exception:
        return str(valor)
    texto = f"{numero:,.2f}"
    return f"R$ {texto.replace(',', 'X').replace('.', ',').replace('X', '.')}"


def _rotulo_periodo(periodo: str) -> str:
    return rotular_periodo_consulta(periodo)


def _montar_texto_consulta(plano: dict, valor) -> str:
    tipo_consulta = str(plano.get("tipo_consulta", "")).strip().lower()
    categoria = str(plano.get("categoria", "")).strip()
    periodo = _rotulo_periodo(str(plano.get("periodo", "mes_atual")).strip().lower())
    valor_fmt = _formatar_moeda_br(valor)

    if tipo_consulta == "gasto_categoria":
        if categoria:
            return f"Você gastou {valor_fmt} com {categoria} {periodo}."
        return f"Você gastou {valor_fmt} {periodo}."
    if tipo_consulta == "gasto_total":
        return f"Você gastou {valor_fmt} {periodo}."
    if tipo_consulta == "receita_categoria":
        if categoria:
            return f"Você recebeu {valor_fmt} de {categoria} {periodo}."
        return f"Você recebeu {valor_fmt} {periodo}."
    if tipo_consulta == "receita_total":
        return f"Você recebeu {valor_fmt} {periodo}."
    if tipo_consulta == "saldo_periodo":
        return f"Saldo {periodo}: {valor_fmt}."
    return valor_fmt


async def _processar_consulta_natural(update, context, texto_usuario: str, forcar: bool = False) -> bool:
    if not forcar and not parece_consulta_financeira(texto_usuario):
        return False

    status_msg = await update.effective_message.reply_text("🔎 Interpretando consulta...")
    plano = await asyncio.to_thread(interpretar_consulta_financeira, texto_usuario)

    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg.message_id)
    except Exception:  # noqa: BLE001
        pass

    if not plano or str(plano.get("tipo", "")).lower() != "consulta":
        if forcar:
            await responder(
                update,
                context,
                "❌ Não consegui transformar isso em uma consulta financeira. Tente algo como: `quanto eu gastei com comida esse mês?`",
            )
            return True
        return False

    try:
        query, params = construir_consulta_financeira(plano, texto_usuario)
    except Exception:
        return False

    resultado = await asyncio.to_thread(executar_sql_parametrizado, query, params)
    if not resultado.get("ok"):
        await responder(update, context, f"❌ Erro ao executar a consulta:\n<pre>{html.escape(resultado['erro'][:1500])}</pre>")
        return True

    if resultado.get("tipo") == "select" and len(resultado.get("linhas", [])) == 1 and len(resultado.get("colunas", [])) == 1:
        valor = next(iter(resultado["linhas"][0].values()))
        await responder(update, context, _montar_texto_consulta(plano, valor))
        return True

    texto, png = montar_resposta_sql(resultado, int(plano.get("limite") or 20))
    if png is not None:
        msg = await update.effective_message.reply_photo(photo=png, caption=texto)
        _rastrear(context, msg)
        return True
    await responder(update, context, texto)
    return True

async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str, **kwargs):
    """Envia texto (HTML) e rastreia o id para a limpeza."""
    kwargs.setdefault("parse_mode", ParseMode.HTML)
    msg = await update.effective_message.reply_text(texto, **kwargs)
    _rastrear(context, msg)
    return msg


# ==========================================
#         PERSISTENCIA (DB + SHEETS)
# ==========================================


def salvar_registro(aba: str, dados: dict) -> float:
    """Monta o DataFrame na ordem do schema, persiste no PostgreSQL e devolve o valor."""
    df = pd.DataFrame([dados])
    if aba in SCHEMA_ABAS:
        df = df[SCHEMA_ABAS[aba]]
    adicionar_linha_db(aba, df)
    logger.info("Transacao registrada na aba %s", aba)
    for chave in ("Valor", "ValorTotal", "Preço"):
        if chave in dados:
            try:
                return float(dados[chave])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


async def gravar_e_confirmar(update, context, aba: str, dados: dict, titulo: str) -> None:
    """Grava a transacao e envia um recibo formatado (equivalente ao embed do Discord)."""
    try:
        valor = salvar_registro(aba, dados)
    except Exception as e:  # noqa: BLE001
        logger.error("Falha ao salvar em %s: %s", aba, e)
        await responder(update, context, f"❌ Erro ao comunicar com os serviços: {html.escape(str(e))}")
        return

    linhas = [titulo, f"💰 <b>Valor:</b> R$ {valor:,.2f}"]
    rotulos = {
        "Categoria": "🏷️ Categoria", "Descricao": "📝 Descrição", "Nome": "📝 Item",
        "ContaSaida": "🏦 Conta", "ContaDestino": "🏦 Conta", "Cartao": "💳 Cartão",
        "Parcelas": "🔢 Parcelas", "ValorEntrada": "💵 Entrada", "QtdPagas": "✅ Parcelas pagas",
        "Prioridade": "⭐ Prioridade", "Tipo": "📈 Tipo", "Operacao": "🔁 Operação",
        "DiaCobranca": "📅 Dia da cobrança", "Inicio": "▶️ Início",
    }
    for chave, rotulo in rotulos.items():
        if chave in dados and str(dados[chave]).strip() not in ("", "0", "0.0"):
            linhas.append(f"{rotulo}: {html.escape(str(dados[chave]))}")
    await responder(update, context, "\n".join(linhas))


# ==========================================
#         ROTEADOR DO MINI APP (web_app_data)
# ==========================================


def _classe_investimento(valor) -> str:
    """Casa o texto recebido com a lista canonica de classes, sem depender de caixa."""
    alvo = str(valor or "").strip().upper()
    for classe in CLASSES_INVESTIMENTO:
        if classe.upper() == alvo:
            return classe
    return CLASSE_INVESTIMENTO_PADRAO


def _periodicidade(valor) -> str:
    alvo = str(valor or "").strip().upper()
    for periodicidade in PERIODICIDADES:
        if periodicidade.upper() == alvo:
            return periodicidade
    return PERIODICIDADE_PADRAO


def _montar_assinatura(
    *, nome, valor, proxima_cobranca, categoria, cartao, periodicidade
) -> tuple[dict, str | None]:
    """Mesma linha de Assinaturas que o bot do Discord monta (ver ui/modals.py).

    Nao reaproveita ui.modals diretamente porque aquele modulo importa discord.
    """
    aviso = None
    cobranca = resolver_cobranca_assinatura(proxima_cobranca)

    if cobranca is None:
        agora = datetime.now()
        inicio, dia = agora.strftime("%Y-%m-%d"), agora.day
        aviso = (
            f"⚠️ Não consegui ler <code>{html.escape(str(proxima_cobranca))}</code> "
            f"como data (use DD/MM/AAAA). Usei hoje ({agora.strftime('%d/%m/%Y')}) "
            "como primeira cobrança."
        )
    else:
        inicio, dia = cobranca

    dados = {
        "Nome": str(nome).strip(),
        "Categoria": categoria,
        "Valor": converter_numero_flexivel(str(valor)),
        "Periodicidade": _periodicidade(periodicidade),
        "DiaCobranca": dia,
        "Cartao": cartao,
        "Ativa": "TRUE",
        "Inicio": inicio,
        "Fim": None,
    }
    return dados, aviso


def _hoje() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@restrito
async def on_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Recebe o JSON enviado por `WebApp.sendData` e roteia para a acao correta."""
    try:
        payload = json.loads(update.effective_message.web_app_data.data)
    except (json.JSONDecodeError, AttributeError):
        await responder(update, context, "❌ Dados do Mini App inválidos.")
        return

    acao = payload.get("action")
    try:
        if acao == "receita":
            dados = {
                "Data": _hoje(),
                "Valor": converter_numero_flexivel(payload["valor"]),
                "ContaDestino": payload["conta"],
                "Categoria": payload["categoria"],
                "Descricao": payload["descricao"],
            }
            await gravar_e_confirmar(update, context, "Receitas", dados, "🟢 <b>Receita registrada!</b>")

        elif acao == "debito":
            dados = {
                "Data": _hoje(),
                "Valor": converter_numero_flexivel(payload["valor"]),
                "ContaSaida": payload["conta"],
                "Categoria": payload["categoria"],
                "Descricao": payload["descricao"],
            }
            await gravar_e_confirmar(update, context, "DebitoAvulso", dados, "🔴 <b>Débito registrado!</b>")

        elif acao == "cartao":
            try:
                parcelas = max(1, int(str(payload.get("parcelas", "1")).strip() or "1"))
            except ValueError:
                parcelas = 1
            dados = {
                "Data": _agora(),
                "ValorTotal": converter_numero_flexivel(payload["valor"]),
                "Cartao": payload["cartao"],
                "Parcelas": parcelas,
                "Categoria": payload["categoria"],
                "Descricao": payload["descricao"],
            }
            await gravar_e_confirmar(update, context, "ComprasCartao", dados, "💳 <b>Compra registrada!</b>")

        elif acao == "pix":
            try:
                pagas = max(0, int(str(payload.get("pagas", "1")).strip() or "1"))
            except ValueError:
                pagas = 1
            dados = {
                "Data": _hoje(),
                "ValorTotal": converter_numero_flexivel(payload["valor"]),
                "ValorEntrada": converter_numero_flexivel(payload.get("entrada", "0")),
                "QtdPagas": pagas,
                "Categoria": payload["categoria"],
                "Descricao": payload["descricao"],
            }
            await gravar_e_confirmar(update, context, "PixParcelado", dados, "🔁 <b>PIX registrado!</b>")

        elif acao == "wishlist":
            dados = {
                "Nome": str(payload["nome"]).replace("_", " "),
                "Preço": converter_numero_flexivel(payload["preco"]),
                "Categoria": payload["categoria"],
                "Prioridade": payload.get("prioridade", "Media"),
                "Link": "Adicionado via Bot",
            }
            await gravar_e_confirmar(update, context, "Wishlist", dados, "⭐ <b>Item na Wishlist!</b>")

        elif acao == "invest":
            dados = {
                "DataHora": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "Classe": _classe_investimento(payload.get("classe")),
                "Tipo": str(payload["tipo"]).upper(),
                "Operacao": payload["operacao"],
                "Valor": converter_numero_flexivel(payload["valor"]),
                "Quantidade": converter_numero_flexivel(payload.get("qtd", "0")),
            }
            await gravar_e_confirmar(update, context, "Investimentos", dados, "📈 <b>Investimento registrado!</b>")

        elif acao == "assinatura":
            dados, aviso = _montar_assinatura(
                nome=payload["nome"],
                valor=payload["valor"],
                proxima_cobranca=payload.get("proxima_cobranca", ""),
                categoria=payload["categoria"],
                cartao=payload["cartao"],
                periodicidade=payload.get("periodicidade"),
            )
            if aviso:
                await responder(update, context, aviso)
            await gravar_e_confirmar(update, context, "Assinaturas", dados, "🔔 <b>Assinatura criada!</b>")

        elif acao == "fatura":
            await _aplicar_fatura(update, context, payload["cartao"], payload["nova_data"])

        elif acao == "pix_editar":
            await _aplicar_pix_editar(update, context, payload["id"], payload["qtd"])

        elif acao == "ass_toggle":
            await _aplicar_ass_toggle(update, context, payload["id"], payload["nome"], bool(payload["ativa"]))

        elif acao == "sql":
            try:
                limite = int(str(payload.get("limite", "20")).strip() or "20")
            except ValueError:
                limite = 20
            await _executar_sql(update, context, payload["query"], limite)

        else:
            await responder(update, context, "❓ Ação desconhecida do Mini App.")
    except KeyError as e:
        await responder(update, context, f"❌ Campo ausente no formulário: {html.escape(str(e))}")
    except Exception as e:  # noqa: BLE001
        logger.error("Erro ao processar acao '%s' do Mini App: %s", acao, e)
        await responder(update, context, f"❌ Erro ao processar: {html.escape(str(e))}")


# ==========================================
#         EDICOES (DB + SHEETS)
# ==========================================


async def _aplicar_fatura(update, context, cartao: str, nova_data: str) -> None:
    payload = {"ultimo_ciclo_pago": nova_data}
    cartoes = CARTOES if str(cartao).strip().upper() == "ALL" else [cartao]
    for cartao_atual in cartoes:
        atualizar_registro_db("FaturasPagas", "cartao", cartao_atual, payload)
        if spreadsheet is not None:
            atualizar_registro_sheets(spreadsheet, "FaturasPagas", "cartao", cartao_atual, payload)
    destino = "todos os cartões" if str(cartao).strip().upper() == "ALL" else html.escape(str(cartao))
    await responder(update, context, f"✅ Fatura de <b>{destino}</b> atualizada para: <b>{html.escape(nova_data)}</b>")


async def _aplicar_pix_editar(update, context, id_compra, qtd) -> None:
    nova_qtd = int(str(qtd).strip())
    atualizar_registro_db("PixParcelado", "id", id_compra, {"qtd_pagas": nova_qtd})
    if spreadsheet is not None:
        atualizar_registro_sheets(spreadsheet, "PixParcelado", "id", id_compra, {"qtd_pagas": nova_qtd})
    await responder(update, context, f"✅ Compra PIX #{html.escape(str(id_compra))} atualizada: <b>{nova_qtd} parcelas pagas</b>.")


async def _aplicar_ass_toggle(update, context, id_assinatura, nome: str, nova_ativa: bool) -> None:
    novo_valor = "TRUE" if nova_ativa else "FALSE"
    atualizar_registro_db("Assinaturas", "id", id_assinatura, {"ativa": novo_valor})
    if spreadsheet is not None:
        # No Sheets a chave e o nome e o cabecalho segue o schema (capitalizado).
        atualizar_registro_sheets(spreadsheet, "Assinaturas", "Nome", nome, {"Ativa": novo_valor})
    estado = "🟢 ativada" if nova_ativa else "⚪ desativada"
    await responder(update, context, f"✅ Assinatura <b>{html.escape(nome)}</b> {estado}.")


async def _executar_sql(update, context, query: str, limite: int = 20) -> None:
    texto, png = montar_resposta_sql(executar_sql_livre(query), limite)
    if png is not None:
        msg = await update.effective_message.reply_photo(photo=png, caption=texto, parse_mode=ParseMode.HTML)
    else:
        msg = await update.effective_message.reply_text(texto, parse_mode=ParseMode.HTML)
    _rastrear(context, msg)


# ==========================================
#         BOTOES DE TEXTO (DADOS AO VIVO / ACOES)
# ==========================================


def _b64_payload(dados: list[dict]) -> str:
    """Codifica a lista (JSON UTF-8) em base64 urlsafe para embutir na URL do Mini App."""
    bruto = json.dumps(dados, ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(bruto).decode("ascii")


def _valor_verdadeiro(valor) -> bool:
    if isinstance(valor, bool):
        return valor
    return str(valor).strip().upper() in ("TRUE", "1", "SIM", "T", "VERDADEIRO")


@restrito
async def abrir_pix_editar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Le as compras PIX e reabre o Mini App com a lista embutida."""
    df = ler_tabela_db("PixParcelado")
    if df.empty:
        await responder(update, context, "📭 Nenhuma compra PIX encontrada.")
        return
    compras = [
        {
            "id": int(row.get("id", idx)) if pd.notna(row.get("id", idx)) else idx,
            "descricao": str(row.get("descricao", "—"))[:60],
            "valor": row.get("valor_total", 0),
            "pagas": row.get("qtd_pagas", 0),
        }
        for idx, row in df.tail(20).iterrows()
    ]
    url = f"{MINIAPP_URL}#pixedit?d={_b64_payload(compras)}"
    teclado = ReplyKeyboardMarkup(
        [[KeyboardButton("🔁 Escolher compra PIX", web_app=WebAppInfo(url=url))]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    msg = await update.effective_message.reply_text(
        "🔁 Toque abaixo para escolher a compra PIX a editar:", reply_markup=teclado
    )
    _rastrear(context, msg)


@restrito
async def abrir_ass_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Le as assinaturas e reabre o Mini App com a lista embutida."""
    df = ler_tabela_db("Assinaturas")
    if df.empty:
        await responder(update, context, "📭 Nenhuma assinatura encontrada.")
        return
    assinaturas = [
        {
            "id": int(row.get("id", idx)) if pd.notna(row.get("id", idx)) else idx,
            "nome": str(row.get("nome", "—"))[:60],
            "valor": row.get("valor", 0),
            "ativa": _valor_verdadeiro(row.get("ativa", False)),
        }
        for idx, row in df.tail(20).iterrows()
    ]
    url = f"{MINIAPP_URL}#asstoggle?d={_b64_payload(assinaturas)}"
    teclado = ReplyKeyboardMarkup(
        [[KeyboardButton("🔄 Escolher assinatura", web_app=WebAppInfo(url=url))]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    msg = await update.effective_message.reply_text(
        "🔄 Toque abaixo para escolher a assinatura a alternar:", reply_markup=teclado
    )
    _rastrear(context, msg)


def _telemetria() -> str:
    ram = subprocess.check_output(
        "free -m | awk 'NR==2{printf \"%.2f%%\", $3*100/$2 }'", shell=True
    ).decode().strip()
    disco = subprocess.check_output(
        "df -h / | awk '$NF==\"/\"{printf \"%s\", $5}'", shell=True
    ).decode().strip()
    return f"🖥️ <b>Saúde do Servidor:</b>\n• RAM: {ram}\n• Disco: {disco}\n• DB: Conectada"


@restrito
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        # to_thread para nao bloquear o event loop (compartilhado com o Discord).
        msg = await asyncio.to_thread(_telemetria)
        await responder(update, context, msg)
    except Exception:  # noqa: BLE001
        await responder(update, context, "⚠️ Erro ao obter telemetria.")


@restrito
async def run_script_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await responder(update, context, "🔄 Executando motor ETL... Aguarde.")
    # Subprocesso assincrono: o ETL e longo e nao pode congelar os dois bots.
    proc = await asyncio.create_subprocess_exec(
        sys.executable, MAIN_SCRIPT, cwd=PROJETO_ROOT,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        await responder(update, context, "✅ Pipeline de Fluxo de Caixa concluída com sucesso!")
    else:
        out = (stderr or stdout or b"").decode(errors="replace").strip()
        logger.error("run_script falhou: %s", out)
        detalhe = html.escape(out or "Erro desconhecido")
        await responder(update, context, f"❌ Falha na execução do script principal.\n<pre>{detalhe[:1500]}</pre>")


@restrito
async def limpar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Apaga as mensagens que o bot enviou neste chat (rastreadas em memoria)."""
    fila: deque = context.chat_data.get("bot_msgs", deque())
    chat_id = update.effective_chat.id
    origem = update.effective_message.message_id
    ids = [mid for mid in list(fila) if mid != origem]

    apagadas = 0
    for mid in ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            apagadas += 1
        except Exception:  # noqa: BLE001
            pass  # >48h, ja apagada ou sem permissao
    fila.clear()
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=origem)
    except Exception:  # noqa: BLE001
        pass

    if apagadas == 0:
        texto = "🧹 Não encontrei mensagens minhas para apagar neste chat."
    elif apagadas == 1:
        texto = "🧹 Limpei 1 mensagem minha neste chat."
    else:
        texto = f"🧹 Limpei {apagadas} mensagens minhas neste chat."
    aviso = await context.bot.send_message(chat_id=chat_id, text=texto)
    _rastrear(context, aviso)


# ==========================================
#         COMANDOS BASICOS
# ==========================================


@restrito
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.effective_message.reply_text(
        "🚀 <b>Sistema Financeiro Online</b>\n\n"
        "Use o painel abaixo (Mini Apps) para registrar em um toque, "
        "mande um gasto por texto (ex: <i>gastei 30 no ifood</i>) ou pergunte algo como <i>quanto eu gastei com comida esse mês?</i>.",
        parse_mode=ParseMode.HTML,
        reply_markup=painel_keyboard(),
    )
    _rastrear(context, msg)


@restrito
async def painel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.effective_message.reply_text(
        "💠 <b>Painel Financeiro</b>\nToque em um botão para abrir o formulário.",
        parse_mode=ParseMode.HTML,
        reply_markup=painel_keyboard(),
    )
    _rastrear(context, msg)


@restrito
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    guia = (
        "🤖 <b>Centro de Comando Financeiro</b>\n"
        "<i>Toque em um comando para copiá-lo!</i>\n\n"
        "💠 <b>Painel (Mini App):</b> /painel — botões para tudo\n\n"
        "💰 <b>Registros Diários:</b>\n"
        "🔴 <code>/debito [valor] [conta] [categoria] [descrição]</code>\n"
        "🟢 <code>/receita [valor] [conta] [categoria] [descrição]</code>\n"
        "💳 <code>/cartao [total] [cartao] [parcelas] [categoria] [descrição]</code>\n"
        "🔁 <code>/pix [total] [entrada] [pagas] [categoria] [descrição]</code>\n"
        "📈 <code>/invest [classe] [ticker] [op] [valor] [qtd]</code>\n\n"
        "🔄 <b>Edições:</b>\n"
        "📅 <code>/fatura_update [cartao|ALL] [nova_data]</code>\n"
        "🔢 <code>/pix_update [id_compra] [qtd_pagas]</code>\n"
        "🔔 <code>/ass_toggle [nome_assinatura]</code>\n\n"
        "⭐ <b>Desejos:</b>\n"
        "🛒 <code>/wish_add [preco] [nome_com_underline] [cat] [prioridade]</code>\n\n"
        "🗄️ <b>Banco:</b>\n"
        "<code>/sql [query]</code> — resultado como imagem\n\n"
        "🛠️ <b>Sistema:</b>\n"
        "🖥️ /status — <i>Telemetria da VM</i>\n"
        "⚙️ /run_script — <i>Recalcular Fluxo de Caixa</i>\n"
        "🧹 /limpar — <i>Apaga as mensagens do bot</i>\n\n"
        "💡 <b>Dica:</b> Você pode apenas escrever <i>'15 no inter com lanche'</i> ou perguntar <i>'quanto eu gastei com comida esse mês?'</i>."
    )
    msg = await update.effective_message.reply_text(guia, parse_mode=ParseMode.HTML)
    _rastrear(context, msg)


# ==========================================
#         COMANDOS MANUAIS (BACKUP/GERAL)
# ==========================================


@restrito
async def debito_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 4:
        return await responder(update, context, "⚠️ Formato: /debito [valor] [conta] [cat] [desc]")
    dados = {
        "Data": _hoje(),
        "Valor": converter_numero_flexivel(context.args[0]),
        "ContaSaida": context.args[1],
        "Categoria": context.args[2],
        "Descricao": " ".join(context.args[3:]),
    }
    await gravar_e_confirmar(update, context, "DebitoAvulso", dados, "🔴 <b>Débito registrado!</b>")


@restrito
async def receita_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 4:
        return await responder(update, context, "⚠️ Formato: /receita [valor] [conta] [cat] [desc]")
    dados = {
        "Data": _hoje(),
        "Valor": converter_numero_flexivel(context.args[0]),
        "ContaDestino": context.args[1],
        "Categoria": context.args[2],
        "Descricao": " ".join(context.args[3:]),
    }
    await gravar_e_confirmar(update, context, "Receitas", dados, "🟢 <b>Receita registrada!</b>")


@restrito
async def cartao_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 5:
        return await responder(update, context, "⚠️ Formato: /cartao [valor] [cartao] [parc] [cat] [desc]")
    dados = {
        "Data": _agora(),
        "ValorTotal": converter_numero_flexivel(context.args[0]),
        "Cartao": context.args[1],
        "Parcelas": int(context.args[2]),
        "Categoria": context.args[3],
        "Descricao": " ".join(context.args[4:]),
    }
    await gravar_e_confirmar(update, context, "ComprasCartao", dados, "💳 <b>Compra registrada!</b>")


@restrito
async def pix_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 5:
        return await responder(update, context, "⚠️ Formato: /pix [total] [ent] [pagas] [cat] [desc]")
    dados = {
        "Data": _hoje(),
        "ValorTotal": converter_numero_flexivel(context.args[0]),
        "ValorEntrada": converter_numero_flexivel(context.args[1]),
        "QtdPagas": int(context.args[2]),
        "Categoria": context.args[3],
        "Descricao": " ".join(context.args[4:]),
    }
    await gravar_e_confirmar(update, context, "PixParcelado", dados, "🔁 <b>PIX registrado!</b>")


@restrito
async def invest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 4:
        return await responder(
            update,
            context,
            "⚠️ Formato: /invest [classe] [ticker] [op] [valor] [qtd]\n"
            f"Classes: {', '.join(CLASSES_INVESTIMENTO)}",
        )
    qtd = converter_numero_flexivel(context.args[4]) if len(context.args) > 4 else 0.0
    dados = {
        "DataHora": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Classe": _classe_investimento(context.args[0]),
        "Tipo": context.args[1].upper(),
        "Operacao": context.args[2],
        "Valor": converter_numero_flexivel(context.args[3]),
        "Quantidade": qtd,
    }
    await gravar_e_confirmar(update, context, "Investimentos", dados, "📈 <b>Investimento registrado!</b>")


@restrito
async def wishlist_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 4:
        return await responder(update, context, "⚠️ Formato: /wish_add [preco] [nome_underline] [cat] [prioridade]")
    dados = {
        "Nome": context.args[1].replace("_", " "),
        "Preço": converter_numero_flexivel(context.args[0]),
        "Categoria": context.args[2],
        "Prioridade": context.args[3],
        "Link": "Adicionado via Bot",
    }
    await gravar_e_confirmar(update, context, "Wishlist", dados, "⭐ <b>Item na Wishlist!</b>")


@restrito
async def fatura_update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        return await responder(update, context, "⚠️ Use: /fatura_update [Cartão] [Nova_Data]")
    await _aplicar_fatura(update, context, context.args[0], context.args[1])


@restrito
async def pix_update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        return await responder(update, context, "⚠️ Use: /pix_update [ID_da_Compra] [Qtd_Pagas]")
    try:
        await _aplicar_pix_editar(update, context, context.args[0], context.args[1])
    except ValueError:
        await responder(update, context, "❌ Quantidade inválida.")


@restrito
async def ass_toggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inverte o estado da assinatura pelo nome (le o estado atual e alterna)."""
    if len(context.args) < 1:
        return await responder(update, context, "⚠️ Use: /ass_toggle [Nome_Assinatura]")
    nome = " ".join(context.args)
    registro = buscar_registro_db("Assinaturas", "nome", nome)
    if not registro:
        return await responder(update, context, f"📭 Assinatura <b>{html.escape(nome)}</b> não encontrada.")
    nova_ativa = not _valor_verdadeiro(registro.get("ativa", False))
    await _aplicar_ass_toggle(update, context, registro.get("id"), nome, nova_ativa)


@restrito
async def sql_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        return await responder(update, context, "⚠️ Use: <code>/sql SELECT * FROM compras_cartao LIMIT 5</code>")
    await _executar_sql(update, context, query)


# ==========================================
#         TEXTO LIVRE (IA PARSER)
# ==========================================


@restrito
async def mensagem_livre_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Processa texto natural usando IA e roteia para a aba correta."""
    texto_usuario = update.effective_message.text or ""

    if await _processar_consulta_natural(update, context, texto_usuario, forcar=parece_consulta_financeira(texto_usuario)):
        return

    status_msg = await update.effective_message.reply_text("🧠 Interpretando registro...")
    # to_thread: a chamada ao Gemini e bloqueante e nao pode congelar o Discord.
    dados_ia = await asyncio.to_thread(interpretar_gasto_com_ia, texto_usuario)

    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg.message_id)
    except Exception:  # noqa: BLE001
        pass

    if not dados_ia:
        await responder(update, context, "❌ Não consegui entender os dados. Tente ser mais claro.")
        return

    tipo_transacao = dados_ia.get("tipo", "debito").lower()
    data_atual = _hoje()

    if tipo_transacao == "invalido":
        await responder(
            update, context,
            "❌ Injeção bloqueada! Eu sou um bot financeiro, não converso sobre outros assuntos. 💸",
        )
        return

    # Backstop: sem valor positivo nao gravamos (exceto wishlist, cujo preco pode faltar).
    valor_ia = dados_ia.get("valor") or 0
    if tipo_transacao != "wishlist" and valor_ia <= 0:
        await responder(
            update, context,
            "❌ Não identifiquei um valor válido. Nada foi gravado. Ex: <code>50 mercado</code>.",
        )
        return

    if tipo_transacao == "credito":
        aba_destino = "ComprasCartao"
        dados_finais = {
            "Data": data_atual, "ValorTotal": dados_ia["valor"], "Cartao": dados_ia["conta_cartao"],
            "Parcelas": dados_ia.get("parcelas", 1), "Categoria": dados_ia["categoria"],
            "Descricao": dados_ia["descricao"],
        }
        titulo = "💳 <b>Compra registrada!</b>"
    elif tipo_transacao == "receita":
        aba_destino = "Receitas"
        dados_finais = {
            "Data": data_atual, "Valor": dados_ia["valor"], "ContaDestino": dados_ia["conta_cartao"],
            "Categoria": dados_ia["categoria"], "Descricao": dados_ia["descricao"],
        }
        titulo = "🟢 <b>Receita registrada!</b>"
    elif tipo_transacao == "investimento":
        aba_destino = "Investimentos"
        dados_finais = {
            "DataHora": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Classe": _classe_investimento(dados_ia.get("classe_investimento")),
            "Tipo": dados_ia["tipo_investimento"].upper(), "Operacao": dados_ia["operacao"],
            "Valor": dados_ia["valor"], "Quantidade": dados_ia.get("quantidade", 0.0),
        }
        titulo = "📈 <b>Investimento registrado!</b>"
    elif tipo_transacao == "pix":
        aba_destino = "PixParcelado"
        dados_finais = {
            "Data": data_atual, "ValorTotal": dados_ia["valor"],
            "ValorEntrada": dados_ia.get("valor_entrada", 0.0), "QtdPagas": dados_ia.get("qtd_pagas", 1),
            "Categoria": dados_ia["categoria"], "Descricao": dados_ia["descricao"],
        }
        titulo = "🔁 <b>PIX registrado!</b>"
    elif tipo_transacao == "wishlist":
        aba_destino = "Wishlist"
        dados_finais = {
            "Nome": dados_ia["descricao"], "Preço": dados_ia["valor"], "Categoria": dados_ia["categoria"],
            "Prioridade": dados_ia.get("prioridade", "Media"), "Link": "Adicionado via Bot",
        }
        titulo = "⭐ <b>Item na Wishlist!</b>"
    else:
        aba_destino = "DebitoAvulso"
        dados_finais = {
            "Data": data_atual, "Valor": dados_ia["valor"], "ContaSaida": dados_ia["conta_cartao"],
            "Categoria": dados_ia["categoria"], "Descricao": dados_ia["descricao"],
        }
        titulo = "🔴 <b>Débito registrado!</b>"

    await gravar_e_confirmar(update, context, aba_destino, dados_finais, titulo)


# ==========================================
#         STARTUP
# ==========================================


def build_app():
    """Monta a Application do Telegram com todos os handlers registrados."""
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Basicos
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("painel", painel_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("run_script", run_script_cmd))
    app.add_handler(CommandHandler("limpar", limpar_cmd))

    # Lancamento manual
    app.add_handler(CommandHandler("debito", debito_cmd))
    app.add_handler(CommandHandler("receita", receita_cmd))
    app.add_handler(CommandHandler("cartao", cartao_cmd))
    app.add_handler(CommandHandler("pix", pix_cmd))
    app.add_handler(CommandHandler("invest", invest_cmd))
    app.add_handler(CommandHandler("wish_add", wishlist_add_cmd))

    # Edicao
    app.add_handler(CommandHandler("fatura_update", fatura_update_cmd))
    app.add_handler(CommandHandler("pix_update", pix_update_cmd))
    app.add_handler(CommandHandler("ass_toggle", ass_toggle_cmd))
    app.add_handler(CommandHandler("sql", sql_cmd))

    # Mini App: dados enviados por WebApp.sendData
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_webapp_data))

    # Botoes de texto do painel (dados ao vivo / acoes)
    app.add_handler(MessageHandler(filters.Text([BTN_PIX_EDITAR]), abrir_pix_editar))
    app.add_handler(MessageHandler(filters.Text([BTN_ASS_TOGGLE]), abrir_ass_toggle))
    app.add_handler(MessageHandler(filters.Text([BTN_STATUS]), status_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_ETL]), run_script_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_LIMPAR]), limpar_cmd))

    # Texto livre (IA) SEMPRE POR ULTIMO
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), mensagem_livre_cmd))
    return app


async def run_async() -> None:
    """Executa o bot dentro de um event loop ja existente (modo combinado).

    Usado pelo `run_bots.py` para rodar Telegram e Discord no mesmo processo.
    """
    global spreadsheet
    logger.info("[Telegram] A ligar ao ecossistema Google...")
    spreadsheet = await asyncio.to_thread(conectar_google_sheets)

    app = build_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("[Telegram] Bot com Mini App em escuta (modo combinado)!")
    try:
        await asyncio.Event().wait()  # roda ate ser cancelado
    finally:
        if app.updater.running:
            await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main() -> None:
    """Executa o bot sozinho (modo standalone, gerencia o proprio loop)."""
    global spreadsheet
    logger.info("A ligar ao ecossistema Google...")
    spreadsheet = conectar_google_sheets()
    logger.info("Bot financeiro com Mini App em escuta!")
    build_app().run_polling()


if __name__ == "__main__":
    main()
