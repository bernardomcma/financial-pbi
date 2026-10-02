"""Ponto de entrada do bot financeiro do Discord.

Responsavel apenas pela inicializacao, seguranca global e roteamento. Toda a
logica de interface (Views, Modals, Selects) vive no pacote `ui`.
"""

import asyncio
import os
import subprocess
import sys
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from config import DISCORD_AUTHORIZED_USER_ID, DISCORD_TOKEN
from services.ai_parser import (
    construir_consulta_financeira,
    interpretar_consulta_financeira,
    interpretar_gasto_com_ia,
    parece_consulta_financeira,
    rotular_periodo_consulta,
)
from database import (
    atualizar_registro_db,
    executar_sql_livre,
    executar_sql_parametrizado,
    ler_tabela_db,
)
from services.google_sheets import atualizar_registro_sheets, conectar_google_sheets
from ui.constants import (
    CARTOES,
    CATEGORIAS,
    CATEGORIAS_RECEITA,
    CLASSES_INVESTIMENTO,
    CLASSE_INVESTIMENTO_PADRAO,
    CONTAS,
    COR_ASSINATURA,
    COR_CARTAO,
    COR_DEBITO,
    COR_PIX,
    COR_RECEITA,
    OPERACOES_INVESTIMENTO,
    PERIODICIDADES,
    PRIORIDADES,
)
from ui.modals import montar_assinatura
from ui.storage import gravar_e_confirmar
from ui.views import (
    AssinaturaToggleView,
    FaturaEditView,
    PainelView,
    PixEditView,
    painel_embed,
    registrar_acao_painel,
)
from utils.data_utils import converter_numero_flexivel
from utils.logging_config import get_logger

logger = get_logger(__name__)
spreadsheet = None

# Raiz do projeto (este arquivo agora vive em bots/, entao subimos um nivel).
PROJETO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_SCRIPT = os.path.join(PROJETO_ROOT, "main.py")

# ==========================================
#         SEGURANCA GLOBAL (TREE)
# ==========================================


class RestrictedTree(app_commands.CommandTree):
    """Trava global: apenas o usuario autorizado interage com qualquer comando."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == DISCORD_AUTHORIZED_USER_ID:
            return True

        logger.warning("Acesso via UI bloqueado. ID: %s", getattr(interaction.user, "id", "?"))
        # Autocomplete nao aceita send_message; so respondemos a comandos de fato.
        if interaction.type is discord.InteractionType.application_command:
            try:
                await interaction.response.send_message(
                    "⛔ Acesso restrito. Este bot é privado.", ephemeral=True
                )
            except discord.InteractionResponded:
                pass
        return False


# ==========================================
#         CONFIGURACAO DO BOT
# ==========================================

intents = discord.Intents.default()
intents.message_content = True


class FinancialBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="/",
            intents=intents,
            help_command=None,
            tree_cls=RestrictedTree,
        )

    async def setup_hook(self):
        # Registra a View persistente para que os botoes do painel sobrevivam a reinicios.
        self.add_view(PainelView())
        registrar_acao_painel("fatura", _abrir_fatura_menu)
        registrar_acao_painel("pix_editar", _abrir_pix_menu)
        registrar_acao_painel("assinatura_toggle", _abrir_assinatura_toggle_menu)
        registrar_acao_painel("status", _enviar_status)
        registrar_acao_painel("run_script", _executar_pipeline)
        registrar_acao_painel("clear", _limpar_chat)
        await self.tree.sync()
        logger.info("UI sincronizada e painel persistente registrado.")


bot = FinancialBot()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.CheckFailure):
        return  # A trava global ja respondeu ao usuario.
    logger.error("Erro no comando: %s", error)
    if not interaction.response.is_done():
        await interaction.response.send_message(
            "⚠️ Ocorreu um erro ao processar o comando.", ephemeral=True
        )


# ==========================================
#         EVENTOS DO SISTEMA
# ==========================================


@bot.event
async def on_ready():
    global spreadsheet
    logger.info("A ligar ao ecossistema Google...")
    # to_thread para nao bloquear o loop (compartilhado com o Telegram no modo combinado).
    spreadsheet = await asyncio.to_thread(conectar_google_sheets)
    logger.info("Bot logado como %s!", bot.user)


async def _abrir_fatura_menu(interaction: discord.Interaction):
    await interaction.response.send_message(
        "💳 Selecione o cartão para atualizar a fatura:",
        view=FaturaEditView(_confirmar_fatura),
        ephemeral=True,
    )


async def _abrir_pix_menu(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)

    df = ler_tabela_db("PixParcelado")
    if df.empty:
        await interaction.followup.send("📭 Nenhuma compra PIX encontrada.", ephemeral=True)
        return

    compras = [
        {
            "id": row.get("id", idx),
            "descricao": str(row.get("descricao", "—")),
            "valor": row.get("valor_total", 0),
            "pagas": row.get("qtd_pagas", 0),
        }
        for idx, row in df.tail(25).iterrows()
    ]

    await interaction.followup.send(
        "🔁 Selecione a compra PIX para atualizar as parcelas pagas:",
        view=PixEditView(compras, _confirmar_pix),
        ephemeral=True,
    )


def _valor_verdadeiro(valor) -> bool:
    """Interpreta o campo 'ativa' (booleano ou texto 'TRUE'/'FALSE') como bool."""
    if isinstance(valor, bool):
        return valor
    return str(valor).strip().upper() in ("TRUE", "1", "SIM", "T", "VERDADEIRO")


def _listar_assinaturas() -> list[dict]:
    """Lê as assinaturas do banco no formato consumido por AssinaturaToggleView."""
    df = ler_tabela_db("Assinaturas")
    if df.empty:
        return []
    return [
        {
            "id": row.get("id", idx),
            "nome": str(row.get("nome", "—")),
            "valor": row.get("valor", 0),
            "ativa": _valor_verdadeiro(row.get("ativa", False)),
        }
        for idx, row in df.tail(25).iterrows()
    ]


async def _abrir_assinatura_toggle_menu(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)

    assinaturas = _listar_assinaturas()
    if not assinaturas:
        await interaction.followup.send("📭 Nenhuma assinatura encontrada.", ephemeral=True)
        return

    await interaction.followup.send(
        "🔄 Selecione a assinatura para ativar/desativar:",
        view=AssinaturaToggleView(assinaturas, _confirmar_assinatura_toggle),
        ephemeral=True,
    )


def _telemetria() -> str:
    ram = subprocess.check_output(
        "free -m | awk 'NR==2{printf \"%.2f%%\", $3*100/$2 }'", shell=True
    ).decode().strip()
    disco = subprocess.check_output(
        "df -h / | awk '$NF==\"/\"{printf \"%s\", $5}'", shell=True
    ).decode().strip()
    return f"🖥️ **Saúde do Servidor:**\n• RAM: {ram}\n• Disco: {disco}\n• DB: Conectada"


async def _rodar_etl_e_reportar(enviar) -> None:
    """Executa main.py de forma assincrona e reporta via callback `enviar(texto)`."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, MAIN_SCRIPT, cwd=PROJETO_ROOT,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        await enviar("✅ Pipeline de Fluxo de Caixa concluída com sucesso!")
    else:
        out = (stderr or stdout or b"").decode(errors="replace").strip()
        logger.error("run_script falhou: %s", out)
        await enviar(f"❌ Falha na execução do script principal.\n```\n{(out or 'Erro desconhecido')[:1500]}\n```")


async def _enviar_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        msg = await asyncio.to_thread(_telemetria)
        await interaction.followup.send(msg, ephemeral=True)
    except Exception:
        await interaction.followup.send("⚠️ Erro ao obter telemetria.", ephemeral=True)


async def _executar_pipeline(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    await _rodar_etl_e_reportar(lambda t: interaction.followup.send(t, ephemeral=True))


async def _limpar_chat(interaction: discord.Interaction):
    """Apaga as mensagens do próprio bot no canal atual (DM ou servidor).

    Deletar as próprias mensagens não exige `manage_messages`, então funciona em
    qualquer canal privado. A mensagem do painel (de onde veio o clique) é
    preservada para os botões não sumirem.
    """
    await interaction.response.defer(ephemeral=True, thinking=True)

    canal = interaction.channel
    painel_id = interaction.message.id if interaction.message else None

    apagadas = 0
    async for mensagem in canal.history(limit=200):
        if mensagem.author != bot.user or mensagem.id == painel_id:
            continue
        try:
            await mensagem.delete()
            apagadas += 1
        except discord.HTTPException:
            logger.warning("Nao foi possivel apagar uma mensagem do bot.")

    if apagadas == 0:
        mensagem = "🧹 Não encontrei mensagens para apagar neste canal."
    elif apagadas == 1:
        mensagem = "🧹 Limpei 1 mensagem minha neste canal."
    else:
        mensagem = f"🧹 Limpei {apagadas} mensagens minhas neste canal."

    await interaction.followup.send(mensagem, ephemeral=True)
    


# ==========================================
#         CHOICES (DROPDOWNS FIXOS)
# ==========================================


def _choices(opcoes):
    """Converte uma lista de valores canonicos em choices nativos do Discord."""
    return [app_commands.Choice(name=o, value=o) for o in opcoes]


CHOICES_CATEGORIA = _choices(CATEGORIAS)
CHOICES_CATEGORIA_RECEITA = _choices(CATEGORIAS_RECEITA)
CHOICES_CONTA = _choices(CONTAS)
CHOICES_CARTAO = _choices(CARTOES)
CHOICES_PRIORIDADE = _choices(PRIORIDADES)
CHOICES_PERIODICIDADE = _choices(PERIODICIDADES)
CHOICES_CLASSE_INVEST = _choices(CLASSES_INVESTIMENTO)
CHOICES_OPERACAO_INVEST = _choices(OPERACOES_INVESTIMENTO)


# ==========================================
#         PAINEL PRINCIPAL
# ==========================================


@bot.tree.command(name="painel", description="Abre o painel financeiro interativo")
async def painel_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(embed=painel_embed(), view=PainelView())


# ==========================================
#         SQL (CONSOLE DIRETO NO BANCO)
# ==========================================


def _sanitizar_valor(valor, largura_max: int | None = None) -> str:
    """Converte um valor de célula em texto de uma linha, opcionalmente truncado."""
    texto = "NULL" if valor is None else str(valor)
    texto = texto.replace("\n", " ").replace("\r", " ")
    if largura_max is not None and len(texto) > largura_max:
        texto = texto[: largura_max - 1] + "…"
    return texto


def _reordenar_colunas(colunas: list[str]) -> list[str]:
    """Move a coluna 'id' (se existir) para o início — chave mais útil primeiro."""
    if "id" in colunas:
        return ["id"] + [c for c in colunas if c != "id"]
    return colunas


def _formatar_tabela_sql(colunas: list[str], linhas: list[dict], largura_max: int = 24) -> str:
    """Tabela ASCII — usada só como fallback caso a renderização de imagem falhe."""
    if not linhas:
        return "(0 linhas)"

    larguras = {col: len(str(col)) for col in colunas}
    for linha in linhas:
        for col in colunas:
            larguras[col] = max(larguras[col], len(_sanitizar_valor(linha.get(col), largura_max)))

    cabecalho = " | ".join(str(col).ljust(larguras[col]) for col in colunas)
    separador = "-+-".join("-" * larguras[col] for col in colunas)
    corpo = "\n".join(
        " | ".join(_sanitizar_valor(linha.get(col), largura_max).ljust(larguras[col]) for col in colunas)
        for linha in linhas
    )
    return f"{cabecalho}\n{separador}\n{corpo}"


def _renderizar_tabela_imagem(colunas: list[str], linhas: list[dict]) -> discord.File:
    """Renderiza as linhas de um SELECT como imagem PNG (tema escuro, estilo Discord)."""
    import io

    from PIL import Image, ImageDraw, ImageFont

    def _fonte(tam: int, bold: bool = False):
        candidatos = (
            ["C:/Windows/Fonts/consolab.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"]
            if bold
            else ["C:/Windows/Fonts/consola.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]
        )
        for caminho in candidatos:
            try:
                return ImageFont.truetype(caminho, tam)
            except OSError:
                continue
        return ImageFont.load_default(tam)

    fonte = _fonte(20)
    fonte_bold = _fonte(20, bold=True)

    BG, HEADER_BG, ROW_ALT = (30, 31, 34), (43, 45, 49), (35, 36, 40)
    TXT, TXT_HEAD, ACCENT, GRID = (219, 222, 225), (255, 255, 255), (88, 101, 242), (55, 57, 62)
    PAD_X, PAD_Y, MARGEM = 14, 10, 16

    medidor = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    def _largura(txt: str, ft) -> int:
        return medidor.textbbox((0, 0), txt, font=ft)[2]

    larguras = {}
    for col in colunas:
        w = _largura(str(col), fonte_bold)
        for linha in linhas:
            w = max(w, _largura(_sanitizar_valor(linha.get(col)), fonte))
        larguras[col] = w + PAD_X * 2

    alt_linha = fonte.getbbox("Ag")[3] + PAD_Y * 2
    larg_total = sum(larguras.values())
    alt_total = alt_linha * (len(linhas) + 1)

    img = Image.new("RGB", (larg_total + MARGEM * 2, alt_total + MARGEM * 2), BG)
    d = ImageDraw.Draw(img)
    x0, y0 = MARGEM, MARGEM

    # Cabeçalho + linha de destaque
    d.rectangle([x0, y0, x0 + larg_total, y0 + alt_linha], fill=HEADER_BG)
    cx = x0
    for col in colunas:
        d.text((cx + PAD_X, y0 + PAD_Y), str(col), font=fonte_bold, fill=TXT_HEAD)
        cx += larguras[col]
    d.rectangle([x0, y0 + alt_linha - 2, x0 + larg_total, y0 + alt_linha], fill=ACCENT)

    # Linhas (com zebra e 'id' destacado)
    for i, linha in enumerate(linhas):
        ry = y0 + alt_linha * (i + 1)
        if i % 2 == 1:
            d.rectangle([x0, ry, x0 + larg_total, ry + alt_linha], fill=ROW_ALT)
        cx = x0
        for col in colunas:
            eh_id = col == "id"
            d.text(
                (cx + PAD_X, ry + PAD_Y),
                _sanitizar_valor(linha.get(col)),
                font=fonte_bold if eh_id else fonte,
                fill=ACCENT if eh_id else TXT,
            )
            cx += larguras[col]

    # Grade vertical sutil entre colunas
    cx = x0
    for col in colunas[:-1]:
        cx += larguras[col]
        d.line([cx, y0, cx, y0 + alt_total], fill=GRID, width=1)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return discord.File(buffer, filename="resultado.png")


def _montar_resposta_sql(resultado: dict, limite: int = 20):
    """Transforma o retorno de `executar_sql_livre` em (conteudo, arquivo) para o Discord."""
    if not resultado["ok"]:
        return f"❌ Erro ao executar a query:\n```\n{resultado['erro'][:1800]}\n```", None

    if resultado["tipo"] == "exec":
        return f"✅ Executado com sucesso. Linhas afetadas: **{resultado['rowcount']}**", None

    colunas = _reordenar_colunas(resultado["colunas"])
    linhas = resultado["linhas"]
    total = len(linhas)
    exibidas = linhas[: max(1, limite)]

    if not exibidas:
        return "📊 0 linha(s).", None

    rodape = f"📊 {total} linha(s)"
    if total > len(exibidas):
        rodape += f" — exibindo as primeiras {len(exibidas)}"

    # Resultado sempre como imagem; se a renderização falhar, cai para texto.
    try:
        return rodape, _renderizar_tabela_imagem(colunas, exibidas)
    except Exception as e:
        logger.warning("Falha ao renderizar imagem do /sql, usando texto: %s", e)
        tabela = _formatar_tabela_sql(colunas, exibidas)
        corpo = f"```\n{tabela}\n```\n\n{rodape}"
        if len(corpo) > 1990:
            import io

            arquivo = discord.File(io.BytesIO(tabela.encode("utf-8")), filename="resultado.txt")
            return rodape, arquivo
        return corpo, None


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


async def _processar_consulta_natural(interaction: discord.Interaction, texto_usuario: str) -> bool:
    if not parece_consulta_financeira(texto_usuario):
        return False

    await interaction.response.defer(thinking=True)
    plano = await asyncio.to_thread(interpretar_consulta_financeira, texto_usuario)

    if not plano or str(plano.get("tipo", "")).lower() != "consulta":
        return False

    try:
        query, params = construir_consulta_financeira(plano, texto_usuario)
    except Exception:
        return False

    resultado = await asyncio.to_thread(executar_sql_parametrizado, query, params)
    if not resultado.get("ok"):
        await interaction.followup.send(f"❌ Erro ao executar a consulta:\n```\n{resultado['erro'][:1800]}\n```")
        return True

    if resultado.get("tipo") == "select" and len(resultado.get("linhas", [])) == 1 and len(resultado.get("colunas", [])) == 1:
        valor = next(iter(resultado["linhas"][0].values()))
        await interaction.followup.send(_montar_texto_consulta(plano, valor))
        return True

    conteudo, arquivo = _montar_resposta_sql(resultado, int(plano.get("limite") or 20))
    await interaction.followup.send(conteudo, file=arquivo) if arquivo else await interaction.followup.send(conteudo)
    return True


@bot.tree.command(name="sql", description="Executa uma query SQL direto no banco (PostgreSQL)")
@app_commands.describe(
    query="Query SQL a executar. Ex: SELECT * FROM compras_cartao ORDER BY id DESC LIMIT 5",
    limite="Máximo de linhas exibidas (SELECT). Padrão: 20",
)
async def sql_cmd(interaction: discord.Interaction, query: str, limite: int = 20):
    # Não-ephemeral de propósito: mensagens ephemeral não carregam anexos de imagem
    # no mobile (limitação da API do Discord). Servidor é privado, então tudo bem.
    await interaction.response.defer(thinking=True)
    conteudo, arquivo = _montar_resposta_sql(executar_sql_livre(query), limite)
    await interaction.followup.send(conteudo, file=arquivo) if arquivo else \
        await interaction.followup.send(conteudo)


# ==========================================
#         SLASH COMMANDS DIRETOS
# ==========================================


@bot.tree.command(name="receita", description="Registra uma entrada de dinheiro")
@app_commands.describe(valor="Ex: 1500", conta="Conta de destino", categoria="Categoria", descricao="Origem da receita")
@app_commands.choices(conta=CHOICES_CONTA, categoria=CHOICES_CATEGORIA_RECEITA)
async def receita_cmd(interaction, valor: str, conta: str, categoria: str, descricao: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    dados = {
        "Data": datetime.now().strftime("%Y-%m-%d"),
        "Valor": converter_numero_flexivel(valor),
        "ContaDestino": conta,
        "Categoria": categoria,
        "Descricao": descricao,
    }
    await gravar_e_confirmar(interaction, "Receitas", dados, titulo="🟢 Receita registrada!", cor=COR_RECEITA)


@bot.tree.command(name="debito", description="Registra um débito avulso")
@app_commands.describe(valor="Ex: 15,50", conta="Conta de saída", categoria="Categoria", descricao="O que comprou")
@app_commands.choices(conta=CHOICES_CONTA, categoria=CHOICES_CATEGORIA)
async def debito_cmd(interaction, valor: str, conta: str, categoria: str, descricao: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    dados = {
        "Data": datetime.now().strftime("%Y-%m-%d"),
        "Valor": converter_numero_flexivel(valor),
        "ContaSaida": conta,
        "Categoria": categoria,
        "Descricao": descricao,
    }
    await gravar_e_confirmar(interaction, "DebitoAvulso", dados, titulo="🔴 Débito registrado!", cor=COR_DEBITO)


@bot.tree.command(name="cartao", description="Registra uma compra no cartão de crédito")
@app_commands.describe(valor_total="Valor final", cartao="Qual cartão", parcelas="Quantas vezes", categoria="Categoria", descricao="O que comprou")
@app_commands.choices(cartao=CHOICES_CARTAO, categoria=CHOICES_CATEGORIA)
async def cartao_cmd(interaction, valor_total: str, cartao: str, parcelas: int, categoria: str, descricao: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    dados = {
        "Data": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ValorTotal": converter_numero_flexivel(valor_total),
        "Cartao": cartao,
        "Parcelas": parcelas,
        "Categoria": categoria,
        "Descricao": descricao,
    }
    await gravar_e_confirmar(interaction, "ComprasCartao", dados, titulo="💳 Compra registrada!", cor=COR_CARTAO)


@bot.tree.command(name="pix", description="Registra uma compra via PIX Parcelado")
@app_commands.describe(valor_total="Valor final", entrada="O que já pagou agora", pagas="Nº parcelas já quitadas", categoria="Categoria", descricao="Detalhes")
@app_commands.choices(categoria=CHOICES_CATEGORIA)
async def pix_cmd(interaction, valor_total: str, entrada: str, pagas: int, categoria: str, descricao: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    dados = {
        "Data": datetime.now().strftime("%Y-%m-%d"),
        "ValorTotal": converter_numero_flexivel(valor_total),
        "ValorEntrada": converter_numero_flexivel(entrada),
        "QtdPagas": pagas,
        "Categoria": categoria,
        "Descricao": descricao,
    }
    await gravar_e_confirmar(interaction, "PixParcelado", dados, titulo="🔁 PIX registrado!", cor=COR_PIX)


@bot.tree.command(name="invest", description="Registra uma operação de investimento")
@app_commands.describe(
    classe="Classe do ativo (define a fonte da cotação)",
    tipo="Ticker do ativo. Ex: CDI, BTC, BOVA11, PETR4",
    operacao="Aporte/Saque",
    valor="Montante",
    quantidade="Cotas, ações ou cripto",
)
@app_commands.choices(classe=CHOICES_CLASSE_INVEST, operacao=CHOICES_OPERACAO_INVEST)
async def invest_cmd(
    interaction, classe: str, tipo: str, operacao: str, valor: str, quantidade: str = "0"
):
    await interaction.response.defer(ephemeral=True, thinking=True)
    dados = {
        "DataHora": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Classe": classe,
        "Tipo": tipo.upper(),
        "Operacao": operacao,
        "Valor": converter_numero_flexivel(valor),
        "Quantidade": converter_numero_flexivel(quantidade),
    }
    await gravar_e_confirmar(interaction, "Investimentos", dados, titulo="📈 Investimento registrado!", cor=COR_CARTAO)


@bot.tree.command(name="wish_add", description="Adiciona um item na Wishlist")
@app_commands.describe(preco="Valor do item", nome="O que você quer", cat="Categoria", prioridade="Baixa, Media, Alta")
@app_commands.choices(cat=CHOICES_CATEGORIA, prioridade=CHOICES_PRIORIDADE)
async def wishlist_add_cmd(interaction, preco: str, nome: str, cat: str, prioridade: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    dados = {
        "Nome": nome.replace("_", " "),
        "Preço": converter_numero_flexivel(preco),
        "Categoria": cat,
        "Prioridade": prioridade,
        "Link": "Adicionado via Bot",
    }
    await gravar_e_confirmar(interaction, "Wishlist", dados, titulo="⭐ Item na Wishlist!", cor=COR_PIX)


# ==========================================
#         EDICOES COM MENUS SUSPENSOS
# ==========================================


async def _confirmar_fatura(interaction: discord.Interaction, cartao: str, nova_data: str):
    payload = {"ultimo_ciclo_pago": nova_data}
    cartoes = CARTOES if cartao.strip().upper() == "ALL" else [cartao]
    for cartao_atual in cartoes:
        atualizar_registro_db("FaturasPagas", "cartao", cartao_atual, payload)
        if spreadsheet is not None:
            atualizar_registro_sheets(spreadsheet, "FaturasPagas", "cartao", cartao_atual, payload)
    destino = "todos os cartões" if cartao.strip().upper() == "ALL" else f"o cartão **{cartao}**"
    await interaction.followup.send(
        f"✅ Fatura de {destino} atualizada para: **{nova_data}**", ephemeral=True
    )


@bot.tree.command(name="fatura", description="Atualiza o último ciclo pago de um cartão")
async def fatura_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        "💳 Selecione o cartão para atualizar a fatura:",
        view=FaturaEditView(_confirmar_fatura),
        ephemeral=True,
    )


async def _confirmar_pix(interaction: discord.Interaction, id_compra: str, nova_qtd: int):
    payload = {"qtd_pagas": nova_qtd}
    atualizar_registro_db("PixParcelado", "id", id_compra, payload)
    if spreadsheet is not None:
        atualizar_registro_sheets(spreadsheet, "PixParcelado", "id", id_compra, payload)
    await interaction.followup.send(
        f"✅ Compra PIX #{id_compra} atualizada: **{nova_qtd} parcelas pagas**.", ephemeral=True
    )


@bot.tree.command(name="pix_editar", description="Atualiza quantas parcelas de um PIX foram pagas")
async def pix_editar_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)

    df = ler_tabela_db("PixParcelado")
    if df.empty:
        await interaction.followup.send("📭 Nenhuma compra PIX encontrada.", ephemeral=True)
        return

    compras = [
        {
            "id": row.get("id", idx),
            "descricao": str(row.get("descricao", "—")),
            "valor": row.get("valor_total", 0),
            "pagas": row.get("qtd_pagas", 0),
        }
        for idx, row in df.tail(25).iterrows()
    ]

    await interaction.followup.send(
        "🔁 Selecione a compra PIX para atualizar as parcelas pagas:",
        view=PixEditView(compras, _confirmar_pix),
        ephemeral=True,
    )


async def _confirmar_assinatura_toggle(
    interaction: discord.Interaction, id_assinatura: str, nome: str, nova_ativa: bool
):
    novo_valor = "TRUE" if nova_ativa else "FALSE"
    atualizar_registro_db("Assinaturas", "id", id_assinatura, {"ativa": novo_valor})
    if spreadsheet is not None:
        # No Sheets a chave é o nome e o cabeçalho segue o schema (capitalizado).
        atualizar_registro_sheets(spreadsheet, "Assinaturas", "Nome", nome, {"Ativa": novo_valor})
    estado = "🟢 ativada" if nova_ativa else "⚪ desativada"
    await interaction.followup.send(
        f"✅ Assinatura **{nome}** {estado}.", ephemeral=True
    )


@bot.tree.command(name="assinatura", description="Cria uma nova assinatura recorrente")
@app_commands.describe(
    nome="Ex: Netflix",
    valor="Valor por cobrança, ex: 39,90",
    proxima_cobranca="Data da próxima cobrança (DD/MM/AAAA)",
    periodicidade="Mensal ou Anual",
    categoria="Categoria",
    cartao="Cartão de cobrança",
)
@app_commands.choices(
    categoria=CHOICES_CATEGORIA, cartao=CHOICES_CARTAO, periodicidade=CHOICES_PERIODICIDADE
)
async def assinatura_cmd(
    interaction,
    nome: str,
    valor: str,
    proxima_cobranca: str,
    periodicidade: str,
    categoria: str,
    cartao: str,
):
    await interaction.response.defer(ephemeral=True, thinking=True)
    dados, aviso = montar_assinatura(
        nome=nome,
        valor=valor,
        proxima_cobranca=proxima_cobranca,
        categoria=categoria,
        cartao=cartao,
        periodicidade=periodicidade,
    )
    if aviso:
        await interaction.followup.send(aviso, ephemeral=True)
    await gravar_e_confirmar(
        interaction, "Assinaturas", dados, titulo="🔔 Assinatura criada!", cor=COR_ASSINATURA
    )


@bot.tree.command(name="ass_toggle", description="Ativa/desativa uma assinatura existente")
async def ass_toggle_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)

    assinaturas = _listar_assinaturas()
    if not assinaturas:
        await interaction.followup.send("📭 Nenhuma assinatura encontrada.", ephemeral=True)
        return

    await interaction.followup.send(
        "🔄 Selecione a assinatura para ativar/desativar:",
        view=AssinaturaToggleView(assinaturas, _confirmar_assinatura_toggle),
        ephemeral=True,
    )


@bot.tree.command(name="clear", description="Apaga as mensagens do bot neste canal (ou DM)")
async def clear_cmd(interaction: discord.Interaction):
    await _limpar_chat(interaction)


# ==========================================
#         TELEMETRIA / ETL
# ==========================================


@bot.tree.command(name="status", description="Telemetria da VM e banco de dados")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        ram = subprocess.check_output(
            "free -m | awk 'NR==2{printf \"%.2f%%\", $3*100/$2 }'", shell=True
        ).decode().strip()
        disco = subprocess.check_output(
            "df -h / | awk '$NF==\"/\"{printf \"%s\", $5}'", shell=True
        ).decode().strip()
        msg = f"🖥️ **Saúde do Servidor:**\n• RAM: {ram}\n• Disco: {disco}\n• DB: Conectada"
        await interaction.followup.send(msg, ephemeral=True)
    except Exception:
        await interaction.followup.send("⚠️ Erro ao obter telemetria.", ephemeral=True)


@bot.tree.command(name="run_script", description="Recalcular todo o Fluxo de Caixa via ETL")
async def run_script_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    await _rodar_etl_e_reportar(lambda t: interaction.followup.send(t, ephemeral=True))


# ==========================================
#         TEXTO LIVRE (IA PARSER)
# ==========================================


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    # Libera a execução se for o seu usuário autorizado OU o webhook do Apple Pay
    is_authorized_user = (message.author.id == DISCORD_AUTHORIZED_USER_ID)
    is_apple_pay_webhook = bool(message.webhook_id and message.content.startswith("[APPLEPAY]"))

    if not (is_authorized_user or is_apple_pay_webhook):
        return

    # Console SQL em texto livre: "/sql SELECT * FROM compras_cartao ORDER BY id DESC LIMIT 5"
    # (o slash command nativo abre um campo separado; aqui aceitamos a query na mesma linha).
    if is_authorized_user and message.content.lower().startswith("/sql "):
        query = message.content[len("/sql "):].strip()
        if not query:
            await message.reply("⚠️ Use: `/sql SELECT * FROM tabela LIMIT 5`")
            return
        async with message.channel.typing():
            conteudo, arquivo = _montar_resposta_sql(executar_sql_livre(query))
        await message.reply(conteudo, file=arquivo) if arquivo else await message.reply(conteudo)
        return

    if is_authorized_user and parece_consulta_financeira(message.content):
        async with message.channel.typing():
            plano = await asyncio.to_thread(interpretar_consulta_financeira, message.content)
            if plano and str(plano.get("tipo", "")).lower() == "consulta":
                try:
                    query, params = construir_consulta_financeira(plano, message.content)
                except Exception:
                    query = None
                    params = None
                if query is not None:
                    resultado = await asyncio.to_thread(executar_sql_parametrizado, query, params)
                    if resultado.get("ok"):
                        if resultado.get("tipo") == "select" and len(resultado.get("linhas", [])) == 1 and len(resultado.get("colunas", [])) == 1:
                            valor = next(iter(resultado["linhas"][0].values()))
                            await message.reply(_montar_texto_consulta(plano, valor))
                        else:
                            conteudo, arquivo = _montar_resposta_sql(resultado, int(plano.get("limite") or 20))
                            await message.reply(conteudo, file=arquivo) if arquivo else await message.reply(conteudo)
                        return
                    await message.reply(f"❌ Erro ao executar a consulta:\n```\n{resultado['erro'][:1800]}\n```")
                    return

        # Se parecia pergunta, mas não deu para transformar em consulta segura, não cai no parser de lançamento.
        await message.reply("❌ Não consegui transformar isso em uma consulta financeira. Tente algo como: `quanto eu gastei com comida esse mês?`")
        return

    # Ignora comandos de barra e mensagens vazias (anexos puros).
    if not message.content or message.content.startswith("/"):
        return

    # Se for o webhook, limpa a tag "[APPLEPAY] " para entregar o texto puro para a IA
    texto_processar = message.content.replace("[APPLEPAY] ", "") if is_apple_pay_webhook else message.content

    # Guarda anti-ruído: ignora mensagens triviais (ex: "a" digitado sem querer)
    # antes de gastar uma chamada de IA. O Apple Pay sempre traz comerciante + valor,
    # então nunca é bloqueado aqui.
    if len(texto_processar.strip()) < 3:
        if is_authorized_user:
            await message.reply("❌ Mensagem muito curta para registrar. Ex: `50 mercado`.")
        return

    status_msg = await message.reply("🧠 Interpretando registro...")

    # Chama a IA usando a string limpa
    dados_ia = await asyncio.to_thread(interpretar_gasto_com_ia, texto_processar)

    if not dados_ia:
        await status_msg.edit(content="❌ Não consegui entender os dados. Tente ser mais claro.")
        return

    tipo_transacao = dados_ia.get("tipo", "debito").lower()
    data_atual = datetime.now().strftime("%Y-%m-%d")

    if tipo_transacao == "invalido":
        await status_msg.edit(
            content="❌ Não identifiquei uma transação válida nessa mensagem. Nada foi gravado. 💸"
        )
        return

    # Backstop: sem valor positivo não gravamos (evita registros "fantasma" de R$ 0,00).
    # Wishlist é exceção — o preço pode ser desconhecido no momento.
    valor_ia = dados_ia.get("valor") or 0
    if tipo_transacao != "wishlist" and valor_ia <= 0:
        await status_msg.edit(
            content="❌ Não identifiquei um valor válido. Nada foi gravado. "
            "Ex: `50 mercado` ou `recebi 1200 salário`."
        )
        return

    if tipo_transacao == "credito":
        aba_destino = "ComprasCartao"
        dados_finais = {
            "Data": data_atual,
            "ValorTotal": dados_ia["valor"],
            "Cartao": dados_ia["conta_cartao"],
            "Parcelas": dados_ia.get("parcelas", 1),
            "Categoria": dados_ia["categoria"],
            "Descricao": dados_ia["descricao"],
        }
    elif tipo_transacao == "receita":
        aba_destino = "Receitas"
        dados_finais = {
            "Data": data_atual,
            "Valor": dados_ia["valor"],
            "ContaDestino": dados_ia["conta_cartao"],
            "Categoria": dados_ia["categoria"],
            "Descricao": dados_ia["descricao"],
        }
    elif tipo_transacao == "investimento":
        aba_destino = "Investimentos"
        dados_finais = {
            "DataHora": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Classe": dados_ia.get("classe_investimento") or CLASSE_INVESTIMENTO_PADRAO,
            "Tipo": dados_ia["tipo_investimento"].upper(),
            "Operacao": dados_ia["operacao"],
            "Valor": dados_ia["valor"],
            "Quantidade": dados_ia.get("quantidade", 0.0),
        }
    elif tipo_transacao == "pix":
        aba_destino = "PixParcelado"
        dados_finais = {
            "Data": data_atual,
            "ValorTotal": dados_ia["valor"],
            "ValorEntrada": dados_ia.get("valor_entrada", 0.0),
            "QtdPagas": dados_ia.get("qtd_pagas", 1),
            "Categoria": dados_ia["categoria"],
            "Descricao": dados_ia["descricao"],
        }
    elif tipo_transacao == "wishlist":
        aba_destino = "Wishlist"
        dados_finais = {
            "Nome": dados_ia["descricao"],
            "Preço": dados_ia["valor"],
            "Categoria": dados_ia["categoria"],
            "Prioridade": dados_ia.get("prioridade", "Media"),
            "Link": "Adicionado via Bot",
        }
    else:
        aba_destino = "DebitoAvulso"
        dados_finais = {
            "Data": data_atual,
            "Valor": dados_ia["valor"],
            "ContaSaida": dados_ia["conta_cartao"],
            "Categoria": dados_ia["categoria"],
            "Descricao": dados_ia["descricao"],
        }

    try:
        from ui.storage import salvar_transacao

        salvar_transacao(aba_destino, dados_finais)
        valor = dados_finais.get("Valor", dados_finais.get("ValorTotal", dados_finais.get("Preço", 0)))
        
        # Personaliza o emoji de sucesso se a entrada foi via Apple Pay
        sucesso_icone = "🍎 **Apple Pay processado!**\n" if is_apple_pay_webhook else "✅ "
        
        await status_msg.edit(content=f"{sucesso_icone}**{aba_destino}** atualizada! 💰 R$ {valor:.2f}")
        
    except Exception as e:
        logger.error("Falha ao processar transação IA em %s: %s", aba_destino, e)
        await status_msg.edit(content=f"❌ Erro ao comunicar com os serviços: {e}")

async def run_async() -> None:
    """Executa o bot dentro de um event loop ja existente (modo combinado)."""
    logger.info("[Discord] Iniciando bot...")
    async with bot:
        await bot.start(DISCORD_TOKEN)


def main() -> None:
    """Executa o bot sozinho (modo standalone, gerencia o proprio loop)."""
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
