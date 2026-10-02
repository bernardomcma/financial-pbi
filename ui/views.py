"""Views interativas: painel persistente e menus suspensos de edicao.

- PainelView e persistente (timeout=None + custom_id em cada botao), por isso
  continua funcional mesmo apos o bot reiniciar (registrada em setup_hook).
- Views de edicao usam discord.ui.Select para escolher cartao/transacao sem
  digitar IDs manualmente.
"""

from __future__ import annotations

from typing import Awaitable, Callable

import discord

from config import DISCORD_AUTHORIZED_USER_ID
from utils.logging_config import get_logger

from ui.constants import CARTOES, COR_PAINEL, PERIODICIDADES
from ui.modals import (
    CartaoModal,
    DebitoModal,
    FaturaDataModal,
    PixModal,
    PixQtdModal,
    ReceitaModal,
    AssinaturaModal,
    InvestModal,
    WishlistModal,
)

logger = get_logger(__name__)

PanelAction = Callable[[discord.Interaction], Awaitable[None]]

PANEL_ACTIONS: dict[str, PanelAction | None] = {
    "fatura": None,
    "pix_editar": None,
    "assinatura_toggle": None,
    "status": None,
    "run_script": None,
    "clear": None,
}


def registrar_acao_painel(nome: str, acao: PanelAction | None) -> None:
    PANEL_ACTIONS[nome] = acao


class AuthorizedView(discord.ui.View):
    """Base que barra qualquer usuario diferente do autorizado nos componentes."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != DISCORD_AUTHORIZED_USER_ID:
            logger.warning("Acesso a componente bloqueado. ID: %s", interaction.user.id)
            await interaction.response.send_message(
                "⛔ Acesso restrito. Este bot é privado.", ephemeral=True
            )
            return False
        return True


class PainelView(AuthorizedView):
    """Painel principal persistente com botoes de registro rapido."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Receita", emoji="🟢", style=discord.ButtonStyle.success, custom_id="painel:receita"
    )
    async def receita(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ReceitaModal())

    @discord.ui.button(
        label="Débito", emoji="🔴", style=discord.ButtonStyle.danger, custom_id="painel:debito"
    )
    async def debito(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(DebitoModal())

    @discord.ui.button(
        label="Cartão", emoji="💳", style=discord.ButtonStyle.primary, custom_id="painel:cartao"
    )
    async def cartao(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CartaoModal())

    @discord.ui.button(
        label="PIX", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="painel:pix"
    )
    async def pix(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(PixModal())

    @discord.ui.button(
        label="Wishlist", emoji="⭐", style=discord.ButtonStyle.success, custom_id="painel:wishlist"
    )
    async def wishlist(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(WishlistModal())

    @discord.ui.button(
        label="Investimento",
        emoji="📈",
        style=discord.ButtonStyle.primary,
        custom_id="painel:invest",
    )
    async def investimento(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(InvestModal())

    @discord.ui.button(
        label="Fatura", emoji="💳", style=discord.ButtonStyle.secondary, custom_id="painel:fatura"
    )
    async def fatura(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        acao = PANEL_ACTIONS.get("fatura")
        if acao is None:
            await interaction.response.send_message("⚠️ Ação de fatura ainda não configurada.", ephemeral=True)
            return
        await acao(interaction)

    @discord.ui.button(
        label="PIX Editar",
        emoji="🔁",
        style=discord.ButtonStyle.secondary,
        custom_id="painel:pix_editar",
    )
    async def pix_editar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        acao = PANEL_ACTIONS.get("pix_editar")
        if acao is None:
            await interaction.response.send_message(
                "⚠️ Ação de edição do PIX ainda não configurada.", ephemeral=True
            )
            return
        await acao(interaction)

    @discord.ui.button(
        label="Assinatura",
        emoji="🔔",
        style=discord.ButtonStyle.secondary,
        custom_id="painel:assinatura",
    )
    async def assinatura(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        # A periodicidade e escolhida antes do modal porque o Discord limita
        # modais a 5 componentes e o AssinaturaModal ja usa os cinco.
        await interaction.response.send_message(
            "🔔 Com que frequência essa assinatura é cobrada?",
            view=AssinaturaPeriodicidadeView(),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Ass. On/Off",
        emoji="🔄",
        style=discord.ButtonStyle.secondary,
        custom_id="painel:assinatura_toggle",
    )
    async def assinatura_toggle(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        acao = PANEL_ACTIONS.get("assinatura_toggle")
        if acao is None:
            await interaction.response.send_message(
                "⚠️ Ação de assinatura ainda não configurada.", ephemeral=True
            )
            return
        await acao(interaction)

    @discord.ui.button(
        label="Status", emoji="🖥️", style=discord.ButtonStyle.secondary, custom_id="painel:status"
    )
    async def status(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        acao = PANEL_ACTIONS.get("status")
        if acao is None:
            await interaction.response.send_message("⚠️ Ação de status ainda não configurada.", ephemeral=True)
            return
        await acao(interaction)

    @discord.ui.button(
        label="Executar ETL",
        emoji="⚙️",
        style=discord.ButtonStyle.primary,
        custom_id="painel:run_script",
    )
    async def run_script(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        acao = PANEL_ACTIONS.get("run_script")
        if acao is None:
            await interaction.response.send_message("⚠️ Ação de ETL ainda não configurada.", ephemeral=True)
            return
        await acao(interaction)

    @discord.ui.button(
        label="Limpar Chat",
        emoji="🧹",
        style=discord.ButtonStyle.danger,
        custom_id="painel:clear",
    )
    async def clear(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        acao = PANEL_ACTIONS.get("clear")
        if acao is None:
            await interaction.response.send_message("⚠️ Ação de limpeza ainda não configurada.", ephemeral=True)
            return
        await acao(interaction)

    @discord.ui.button(
        label="Fechar",
        emoji="✖️",
        style=discord.ButtonStyle.danger,
        custom_id="painel:fechar",
    )
    async def fechar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            await interaction.response.defer()
            if interaction.message is not None:
                await interaction.message.delete()
        except discord.HTTPException:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "⚠️ Não foi possível fechar o painel.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "⚠️ Não foi possível fechar o painel.", ephemeral=True
                )


def painel_embed() -> discord.Embed:
    """Embed limpo que acompanha o painel principal."""
    embed = discord.Embed(
        title="💠 Painel Financeiro",
        description=(
            "Registre movimentações em um clique. Use os botões abaixo para abrir "
            "o formulário correspondente.\n\n"
            "🟢 **Receita** · dinheiro entrando\n"
            "🔴 **Débito** · gasto avulso\n"
            "💳 **Cartão** · compra no crédito\n"
            "🔁 **PIX** · compra parcelada\n\n"
            "*Ou simplesmente digite naturalmente (ex: \"gastei 30 no ifood\") "
            "que a IA interpreta.*"
        ),
        color=COR_PAINEL,
    )
    embed.set_footer(text="Painel Financeiro • sempre ativo")
    return embed


# --- Callback assinaturas para as views de edicao ---
FaturaConfirm = Callable[[discord.Interaction, str, str], Awaitable[None]]
PixConfirm = Callable[[discord.Interaction, str, int], Awaitable[None]]
AssinaturaToggleConfirm = Callable[[discord.Interaction, str, str, bool], Awaitable[None]]


class _AssinaturaPeriodicidadeSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(
                label=p,
                value=p,
                emoji="📅" if p == "Mensal" else "🗓️",
            )
            for p in PERIODICIDADES
        ]
        super().__init__(
            placeholder="Mensal ou anual...", min_values=1, max_values=1, options=options
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AssinaturaModal(self.values[0]))


class AssinaturaPeriodicidadeView(AuthorizedView):
    """Passo previo ao AssinaturaModal, que nao tem espaco para mais um campo."""

    def __init__(self) -> None:
        super().__init__(timeout=180)
        self.add_item(_AssinaturaPeriodicidadeSelect())


class _FaturaSelect(discord.ui.Select):
    def __init__(self, on_confirm: FaturaConfirm) -> None:
        options = [
            discord.SelectOption(label="ALL - Todos os cartões", value="ALL", emoji="💳")
        ] + [discord.SelectOption(label=c, value=c, emoji="💳") for c in CARTOES]
        super().__init__(placeholder="Escolha o cartão...", min_values=1, max_values=1, options=options)
        self._on_confirm = on_confirm

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            FaturaDataModal(self.values[0], self._on_confirm)
        )


class FaturaEditView(AuthorizedView):
    """Menu suspenso para escolher o cartao cuja fatura sera atualizada."""

    def __init__(self, on_confirm: FaturaConfirm) -> None:
        super().__init__(timeout=180)
        self.add_item(_FaturaSelect(on_confirm))


class _PixSelect(discord.ui.Select):
    def __init__(self, compras: list[dict], on_confirm: PixConfirm) -> None:
        self._mapa = {str(c["id"]): c for c in compras}
        self._on_confirm = on_confirm
        options = [
            discord.SelectOption(
                label=f"#{c['id']} · {c['descricao']}"[:100],
                description=f"Total R$ {c['valor']} · {c['pagas']} pagas"[:100],
                value=str(c["id"]),
            )
            for c in compras[:25]
        ]
        super().__init__(placeholder="Escolha a compra PIX...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        compra = self._mapa[self.values[0]]
        await interaction.response.send_modal(
            PixQtdModal(self.values[0], compra["descricao"], self._on_confirm)
        )


class PixEditView(AuthorizedView):
    """Menu suspenso listando compras PIX reais para atualizar as parcelas pagas."""

    def __init__(self, compras: list[dict], on_confirm: PixConfirm) -> None:
        super().__init__(timeout=180)
        self.add_item(_PixSelect(compras, on_confirm))


class _AssinaturaToggleSelect(discord.ui.Select):
    def __init__(self, assinaturas: list[dict], on_confirm: AssinaturaToggleConfirm) -> None:
        self._mapa = {str(a["id"]): a for a in assinaturas}
        self._on_confirm = on_confirm
        options = [
            discord.SelectOption(
                label=f"{a['nome']}"[:100],
                description=(
                    f"R$ {a['valor']} · {'🟢 Ativa' if a['ativa'] else '⚪ Inativa'} "
                    f"→ {'desativar' if a['ativa'] else 'ativar'}"
                )[:100],
                value=str(a["id"]),
                emoji="🟢" if a["ativa"] else "⚪",
            )
            for a in assinaturas[:25]
        ]
        super().__init__(
            placeholder="Escolha a assinatura para alternar...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        assinatura = self._mapa[self.values[0]]
        nova_ativa = not assinatura["ativa"]
        await self._on_confirm(
            interaction, self.values[0], assinatura["nome"], nova_ativa
        )


class AssinaturaToggleView(AuthorizedView):
    """Menu suspenso listando assinaturas reais para ativar/desativar."""

    def __init__(self, assinaturas: list[dict], on_confirm: AssinaturaToggleConfirm) -> None:
        super().__init__(timeout=180)
        self.add_item(_AssinaturaToggleSelect(assinaturas, on_confirm))
