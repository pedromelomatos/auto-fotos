"""Interface gráfica sob demanda para a automação de imagens do Bling."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable
from urllib.parse import unquote, urlsplit, urlunsplit

from dotenv import load_dotenv

from bling import BlingImagens, BlingSomenteLeitura
from main import (
    URL_PADRAO,
    ResultadoAplicacao,
    aplicar_imagens_piloto,
    criar_plano_imagens,
    gerar_conferencia_bling,
    gerar_plano_imagens,
    gerar_relatorio_aplicacao,
    valor_booleano,
)
from oauth_bling import (
    autorizar,
    carregar_configuracao,
    renovar,
    token_precisa_renovacao,
    tokens_configurados,
)
from servidor import ResultadoColeta, coletar_imagens, gerar_csv, listar_diretorios


@dataclass(frozen=True, slots=True)
class EstadoSimulacao:
    resultado: ResultadoColeta
    encontrados: dict[str, list[Any]]
    linhas: list[dict[str, str | int]]
    pasta_saida: Path


@dataclass(slots=True)
class SeletorSubpasta:
    quadro: ttk.Frame
    seletor: ttk.Combobox
    url_pai: str
    urls: tuple[str, ...]


def totais_plano(linhas: list[dict[str, str | int]]) -> Counter[str]:
    return Counter(str(linha["status_planejado"]) for linha in linhas)


def linha_pode_ser_aplicada(linha: dict[str, str | int]) -> bool:
    return linha.get("status_planejado") == "ADICIONAR"


def resumo_plano(linhas: list[dict[str, str | int]]) -> str:
    totais = totais_plano(linhas)
    ignoradas = totais["IGNORADO_PRODUTO_NAO_ENCONTRADO"]
    bloqueadas = len(linhas) - totais["ADICIONAR"] - totais["JA_EXISTE"] - ignoradas
    return (
        f"Novas: {totais['ADICIONAR']}  |  "
        f"Já cadastradas: {totais['JA_EXISTE']}  |  "
        f"Não encontradas: {ignoradas}  |  "
        f"Com problemas: {bloqueadas}"
    )


def pasta_e_raiz_da_url(url: str) -> tuple[str, str]:
    """Separa a pasta selecionada e a URL do diretório que a contém."""
    partes = urlsplit(url.strip())
    caminho = partes.path.rstrip("/")
    if not partes.scheme or not partes.netloc or not caminho:
        raise ValueError(f"URL de pasta inválida: {url!r}")
    caminho_pai, nome = caminho.rsplit("/", 1)
    raiz = urlunsplit((partes.scheme, partes.netloc, f"{caminho_pai}/", "", ""))
    return unquote(nome), raiz


def mensagem_erro_amigavel(erro: Exception) -> str:
    texto = str(erro)
    if "Token do Bling" in texto:
        return (
            "A autorização do Bling expirou ou não está disponível.\n\n"
            "Abra Configurações avançadas e selecione Renovar token."
        )
    if "permissao para salvar imagens" in texto.casefold() or "permissão para salvar imagens" in texto.casefold():
        return (
            "O aplicativo ainda não tem permissão para salvar imagens.\n\n"
            "Habilite o escopo Salvar imagens dos Produtos no Bling e use "
            "Configurações avançadas > Autorizar Bling."
        )
    if isinstance(erro, PermissionError):
        return (
            "Não foi possível atualizar um dos relatórios. Feche o arquivo no "
            "Excel e tente novamente."
        )
    return texto


class AutoFotosGUI:
    COLUNAS = (
        "codigo_servidor",
        "codigo_bling",
        "nome_bling",
        "posicao",
        "status_planejado",
        "motivo",
    )
    ROTULOS_STATUS = {
        "ADICIONAR": "Nova imagem",
        "JA_EXISTE": "Já cadastrada",
        "IGNORADO_PRODUTO_NAO_ENCONTRADO": "Produto não encontrado",
        "BLOQUEADO_SEQUENCIA_INCONSISTENTE": "Sequência inconsistente",
        "BLOQUEADO_MULTIPLOS_PRODUTOS_ATIVOS": "Mais de um produto ativo",
        "BLOQUEADO_ERRO_AO_CONSULTAR_DETALHES": "Erro ao consultar produto",
        "BLOQUEADO_CODIGO_DIVERGENTE": "SKU divergente",
        "BLOQUEADO_PRODUTO_NAO_ATIVO": "Produto não ativo",
    }
    FILTROS = (
        "Todos",
        "Somente novas",
        "Já cadastradas",
        "Não encontradas",
        "Com problemas",
    )

    def __init__(self, raiz: tk.Tk) -> None:
        self.raiz = raiz
        self.raiz.title("Auto Fotos - Bling")
        self.raiz.geometry("1180x780")
        self.raiz.minsize(940, 650)

        load_dotenv(override=True)
        url_inicial = os.getenv("IMAGENS_BASE_URL", URL_PADRAO)
        pasta_inicial, self.servidor_raiz = pasta_e_raiz_da_url(url_inicial)
        self.url = tk.StringVar(value=url_inicial)
        self.pasta_remota = tk.StringVar(value=pasta_inicial)
        self.urls_por_pasta = {pasta_inicial: url_inicial}
        self.subpastas: list[SeletorSubpasta] = []
        self.pasta_saida = tk.StringVar(value=str(Path(__file__).resolve().parent))
        self.validar_certificado = tk.BooleanVar(
            value=valor_booleano(os.getenv("SERVIDOR_VERIFY_SSL"), padrao=False)
        )
        self.status = tk.StringVar(value="Pronto.")
        self.resumo = tk.StringVar(value="Escolha a pasta e verifique as imagens.")
        self.orientacao = tk.StringVar(
            value="O envio será liberado somente quando houver uma imagem nova selecionada."
        )
        self.filtro_status = tk.StringVar(value="Todos")
        self.total_novas = tk.StringVar(value="0")
        self.total_existentes = tk.StringVar(value="0")
        self.total_ignoradas = tk.StringVar(value="0")
        self.total_problemas = tk.StringVar(value="0")
        self.avancadas_visiveis = False

        self.estado: EstadoSimulacao | None = None
        self.linhas_por_item: dict[str, dict[str, str | int]] = {}
        self.fila: queue.Queue[tuple[str, str, Any]] = queue.Queue()
        self.em_execucao = False
        self.ao_concluir: Callable[[Any], None] | None = None

        self._montar_interface()
        self.raiz.protocol("WM_DELETE_WINDOW", self._fechar)
        self.raiz.after(100, self._processar_fila)
        self.raiz.after(250, self._inicializar)

    def _inicializar(self) -> None:
        """Valida a autorização sem impedir o restante da inicialização."""
        try:
            configuracao = carregar_configuracao()
        except Exception as erro:
            self._avisar_bling_nao_autorizado(
                "A configuração local do aplicativo está incompleta. "
                f"Detalhe: {mensagem_erro_amigavel(erro)}"
            )
            self.atualizar_pastas()
            return

        precisa_renovar = token_precisa_renovacao(configuracao)
        if not tokens_configurados(configuracao) and not precisa_renovar:
            self._avisar_bling_nao_autorizado()
            self.atualizar_pastas()
            return

        if precisa_renovar:
            self._renovar_automaticamente(configuracao)
            return

        self._validar_autorizacao_inicial(configuracao)

    def _avisar_bling_nao_autorizado(self, detalhe: str = "") -> None:
        if not self.avancadas_visiveis:
            self._alternar_configuracoes()
        mensagem = (
            "O Bling ainda precisa ser autorizado para consultar produtos e "
            "enviar imagens.\n\n"
            "1. Confirme no cadastro do aplicativo Bling que o escopo "
            "‘Salvar imagens dos Produtos’ está habilitado.\n"
            "2. Clique em ‘Autorizar Bling’, abaixo.\n"
            "3. Faça login no navegador e aprove o acesso.\n"
            "4. Volte ao programa e clique em ‘Verificar imagens’."
        )
        if detalhe:
            mensagem += f"\n\n{detalhe}"
        messagebox.showwarning("Autorize o Bling para continuar", mensagem)
        self.botao_autorizar.focus_set()

    @staticmethod
    def _erro_exige_nova_autorizacao(erro: Exception) -> bool:
        texto = str(erro).casefold()
        return "token do bling" in texto or "permissao para consultar" in texto

    def _validar_autorizacao_inicial(self, configuracao: Any) -> None:
        def tarefa() -> Exception | None:
            try:
                load_dotenv(override=True)
                BlingSomenteLeitura.do_ambiente().verificar_acesso()
            except Exception as erro:
                return erro
            return None

        def concluir(erro: Exception | None) -> None:
            if erro is not None and self._erro_exige_nova_autorizacao(erro):
                # Um token pode ser revogado antes da data gravada, por exemplo
                # depois de uma alteração de escopos. Tenta o refresh uma vez.
                self._renovar_automaticamente(configuracao)
                return
            if erro is not None:
                self.status.set(
                    "Não foi possível confirmar o acesso ao Bling agora; "
                    "a interface continuará disponível."
                )
            self.raiz.after(50, self.atualizar_pastas)

        self._executar_em_segundo_plano(
            "Verificando a autorização do Bling...",
            tarefa,
            concluir,
        )

    def _renovar_automaticamente(self, configuracao: Any) -> None:
        def tarefa() -> tuple[Any | None, Exception | None]:
            try:
                return renovar(configuracao), None
            except Exception as erro:
                return None, erro

        def concluir(resultado: tuple[Any | None, Exception | None]) -> None:
            token, erro = resultado
            if erro is None:
                validade = getattr(token, "expires_in", None)
                detalhe = (
                    f" por mais {validade // 3600} hora(s)"
                    if isinstance(validade, int) and validade >= 3600
                    else ""
                )
                self.status.set(
                    f"A autorização do Bling foi renovada automaticamente{detalhe}."
                )
            else:
                self._avisar_bling_nao_autorizado(
                    "A renovação automática não foi aceita. Isso acontece quando "
                    "os tokens foram revogados ou ficaram tempo demais sem uso."
                )
            self.raiz.after(50, self.atualizar_pastas)

        self._executar_em_segundo_plano(
            "Renovando automaticamente a autorização do Bling...",
            tarefa,
            concluir,
        )

    def _montar_interface(self) -> None:
        estilo = ttk.Style(self.raiz)
        estilo.configure("Titulo.TLabel", font=("Segoe UI", 20, "bold"))
        estilo.configure(
            "Subtitulo.TLabel", font=("Segoe UI", 10), foreground="#5b6470"
        )
        estilo.configure(
            "Etapa.TLabelframe.Label", font=("Segoe UI", 10, "bold")
        )
        estilo.configure(
            "Primario.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 8)
        )
        estilo.configure("Metrica.TLabel", font=("Segoe UI", 18, "bold"))
        estilo.configure("MetricaNome.TLabel", foreground="#5b6470")

        principal = ttk.Frame(self.raiz, padding=16)
        principal.grid(row=0, column=0, sticky="nsew")
        self.raiz.rowconfigure(0, weight=1)
        self.raiz.columnconfigure(0, weight=1)
        principal.columnconfigure(0, weight=1)
        principal.rowconfigure(3, weight=1)

        cabecalho = ttk.Frame(principal)
        cabecalho.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(cabecalho, text="Imagens de produtos", style="Titulo.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            cabecalho,
            text=(
                "Encontre imagens novas no servidor e envie somente o que "
                "estiver faltando no Bling."
            ),
            style="Subtitulo.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        origem = ttk.LabelFrame(
            principal,
            text="1. Escolha a origem e verifique",
            padding=12,
            style="Etapa.TLabelframe",
        )
        origem.grid(row=1, column=0, sticky="ew")
        origem.columnconfigure(1, weight=1)

        ttk.Label(origem, text="Fabricante / pasta:").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=4
        )
        self.seletor_pasta = ttk.Combobox(
            origem,
            textvariable=self.pasta_remota,
            values=tuple(self.urls_por_pasta),
            state="readonly",
        )
        self.seletor_pasta.grid(row=0, column=1, sticky="ew", pady=4)
        self.seletor_pasta.bind("<<ComboboxSelected>>", self._ao_trocar_pasta)
        self.botao_atualizar_pastas = ttk.Button(
            origem,
            text="Atualizar lista",
            command=self.atualizar_pastas,
        )
        self.botao_atualizar_pastas.grid(row=0, column=2, padx=(8, 0), pady=4)
        self.botao_configuracoes = ttk.Button(
            origem,
            text="Configurações avançadas ▸",
            command=self._alternar_configuracoes,
        )
        self.botao_configuracoes.grid(row=0, column=3, padx=(8, 0), pady=4)

        self.quadro_subpastas = ttk.Frame(origem)
        self.quadro_subpastas.grid(row=1, column=0, columnspan=4, sticky="ew")
        ttk.Label(
            self.quadro_subpastas, textvariable=self.url, style="Subtitulo.TLabel"
        ).pack(anchor="w", pady=(4, 0))

        self.botao_simular = ttk.Button(
            origem,
            text="Verificar imagens",
            command=self.simular,
            style="Primario.TButton",
        )
        self.botao_simular.grid(row=2, column=1, sticky="w", pady=(8, 2))
        ttk.Label(
            origem,
            text="Esta etapa apenas consulta e compara; nenhuma imagem é enviada.",
            style="Subtitulo.TLabel",
        ).grid(row=2, column=2, columnspan=2, sticky="w", padx=(10, 0), pady=(8, 2))

        self.quadro_avancado = ttk.LabelFrame(
            origem,
            text="Configurações avançadas",
            padding=10,
        )
        self.quadro_avancado.grid(
            row=3, column=0, columnspan=4, sticky="ew", pady=(10, 0)
        )
        self.quadro_avancado.columnconfigure(1, weight=1)
        ttk.Label(self.quadro_avancado, text="Pasta dos relatórios:").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=4
        )
        self.entrada_saida = ttk.Entry(
            self.quadro_avancado, textvariable=self.pasta_saida
        )
        self.entrada_saida.grid(row=0, column=1, sticky="ew", pady=4)
        self.botao_escolher_saida = ttk.Button(
            self.quadro_avancado,
            text="Escolher...",
            command=self._escolher_pasta,
        )
        self.botao_escolher_saida.grid(row=0, column=2, padx=(8, 0), pady=4)
        self.botao_pasta = ttk.Button(
            self.quadro_avancado,
            text="Abrir relatórios",
            command=self._abrir_pasta,
        )
        self.botao_pasta.grid(row=0, column=3, padx=(8, 0), pady=4)
        self.check_tls = ttk.Checkbutton(
            self.quadro_avancado,
            text="Validar certificado TLS do servidor de imagens",
            variable=self.validar_certificado,
        )
        self.check_tls.grid(row=1, column=1, sticky="w", pady=4)

        botoes_bling = ttk.Frame(self.quadro_avancado)
        botoes_bling.grid(row=2, column=1, columnspan=3, sticky="w", pady=(4, 0))
        self.botao_autorizar = ttk.Button(
            botoes_bling,
            text="Autorizar Bling",
            command=self.autorizar_bling,
        )
        self.botao_autorizar.pack(side="left")
        self.botao_renovar = ttk.Button(
            botoes_bling,
            text="Renovar token",
            command=self.renovar_token,
        )
        self.botao_renovar.pack(side="left", padx=(8, 0))
        ttk.Label(
            botoes_bling,
            text="A renovação é automática; use estes botões somente se houver erro.",
            style="Subtitulo.TLabel",
        ).pack(side="left", padx=(12, 0))
        self.quadro_avancado.grid_remove()

        metricas = ttk.Frame(principal, padding=(0, 12))
        metricas.grid(row=2, column=0, sticky="ew")
        for coluna in range(4):
            metricas.columnconfigure(coluna, weight=1, uniform="metricas")
        for coluna, (titulo, variavel) in enumerate(
            (
                ("Novas", self.total_novas),
                ("Já cadastradas", self.total_existentes),
                ("Não encontradas", self.total_ignoradas),
                ("Precisam de atenção", self.total_problemas),
            )
        ):
            caixa = ttk.LabelFrame(metricas, padding=(12, 7))
            caixa.grid(
                row=0,
                column=coluna,
                sticky="ew",
                padx=(0 if coluna == 0 else 4, 0 if coluna == 3 else 4),
            )
            ttk.Label(caixa, textvariable=variavel, style="Metrica.TLabel").pack()
            ttk.Label(caixa, text=titulo, style="MetricaNome.TLabel").pack()

        quadro_tabela = ttk.LabelFrame(
            principal,
            text="2. Revise o resultado",
            padding=8,
            style="Etapa.TLabelframe",
        )
        quadro_tabela.grid(row=3, column=0, sticky="nsew")
        quadro_tabela.rowconfigure(1, weight=1)
        quadro_tabela.columnconfigure(0, weight=1)

        barra_tabela = ttk.Frame(quadro_tabela)
        barra_tabela.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 7))
        barra_tabela.columnconfigure(0, weight=1)
        ttk.Label(barra_tabela, textvariable=self.resumo).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(barra_tabela, text="Mostrar:").grid(row=0, column=1, padx=(8, 6))
        self.seletor_filtro = ttk.Combobox(
            barra_tabela,
            textvariable=self.filtro_status,
            values=self.FILTROS,
            state="disabled",
            width=19,
        )
        self.seletor_filtro.grid(row=0, column=2)
        self.seletor_filtro.bind("<<ComboboxSelected>>", self._ao_filtrar)

        self.tabela = ttk.Treeview(
            quadro_tabela,
            columns=self.COLUNAS,
            show="headings",
            selectmode="browse",
        )
        titulos = {
            "codigo_servidor": "Código servidor",
            "codigo_bling": "SKU Bling",
            "nome_bling": "Produto",
            "posicao": "Posição",
            "status_planejado": "Status",
            "motivo": "Observação",
        }
        larguras = {
            "codigo_servidor": 105,
            "codigo_bling": 120,
            "nome_bling": 310,
            "posicao": 65,
            "status_planejado": 175,
            "motivo": 390,
        }
        for coluna in self.COLUNAS:
            self.tabela.heading(coluna, text=titulos[coluna])
            self.tabela.column(
                coluna,
                width=larguras[coluna],
                minwidth=60,
                stretch=coluna in {"nome_bling", "motivo"},
            )
        self.tabela.tag_configure(
            "ADICIONAR", background="#dff3e4", foreground="#14532d"
        )
        self.tabela.tag_configure("JA_EXISTE", foreground="#6b7280")
        self.tabela.tag_configure(
            "IGNORADO_PRODUTO_NAO_ENCONTRADO",
            background="#fff4cc",
            foreground="#7c4a03",
        )
        self.tabela.tag_configure(
            "BLOQUEADO", background="#f8d7da", foreground="#7f1d1d"
        )
        self.tabela.bind("<<TreeviewSelect>>", self._ao_selecionar)

        rolagem_vertical = ttk.Scrollbar(
            quadro_tabela, orient="vertical", command=self.tabela.yview
        )
        rolagem_horizontal = ttk.Scrollbar(
            quadro_tabela, orient="horizontal", command=self.tabela.xview
        )
        self.tabela.configure(
            yscrollcommand=rolagem_vertical.set,
            xscrollcommand=rolagem_horizontal.set,
        )
        self.tabela.grid(row=1, column=0, sticky="nsew")
        rolagem_vertical.grid(row=1, column=1, sticky="ns")
        rolagem_horizontal.grid(row=2, column=0, sticky="ew")

        rodape = ttk.Frame(principal, padding=(0, 10, 0, 0))
        rodape.grid(row=4, column=0, sticky="ew")
        rodape.columnconfigure(0, weight=1)
        ttk.Label(rodape, textvariable=self.orientacao).grid(
            row=0, column=0, sticky="w", padx=(0, 12)
        )
        self.botao_aplicar = ttk.Button(
            rodape,
            text="Enviar imagens do produto selecionado",
            command=self.aplicar_selecionado,
            state="disabled",
            style="Primario.TButton",
        )
        self.botao_aplicar.grid(row=0, column=1, sticky="e")

        barra_status = ttk.Frame(rodape)
        barra_status.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        barra_status.columnconfigure(0, weight=1)
        ttk.Label(
            barra_status, textvariable=self.status, style="Subtitulo.TLabel"
        ).grid(row=0, column=0, sticky="w")
        self.progresso = ttk.Progressbar(
            barra_status, mode="indeterminate", length=180
        )
        self.progresso.grid(row=0, column=1, padx=(10, 0))

    def _alternar_configuracoes(self) -> None:
        self.avancadas_visiveis = not self.avancadas_visiveis
        if self.avancadas_visiveis:
            self.quadro_avancado.grid()
            self.botao_configuracoes.configure(text="Configurações avançadas ▾")
        else:
            self.quadro_avancado.grid_remove()
            self.botao_configuracoes.configure(text="Configurações avançadas ▸")

    def _escolher_pasta(self) -> None:
        escolhida = filedialog.askdirectory(
            title="Escolha a pasta dos relatórios",
            initialdir=self.pasta_saida.get(),
        )
        if escolhida:
            self.pasta_saida.set(escolhida)

    def _fechar(self) -> None:
        if self.em_execucao:
            messagebox.showwarning(
                "Operação em andamento",
                "Aguarde a operação terminar antes de fechar a janela.",
            )
            return
        self.raiz.destroy()

    def _abrir_pasta(self) -> None:
        pasta = Path(self.pasta_saida.get()).expanduser().resolve()
        pasta.mkdir(parents=True, exist_ok=True)
        os.startfile(pasta)  # type: ignore[attr-defined]

    def atualizar_pastas(self) -> None:
        raiz = self.servidor_raiz
        validar_certificado = self.validar_certificado.get()

        def tarefa() -> Any:
            return listar_diretorios(
                raiz,
                verificar_certificado=validar_certificado,
            )

        self._executar_em_segundo_plano(
            "Carregando pastas disponíveis no servidor...",
            tarefa,
            self._mostrar_pastas,
        )

    def _mostrar_pastas(self, diretorios: Any) -> None:
        if not diretorios:
            messagebox.showwarning(
                "Nenhuma pasta encontrada",
                "O servidor não publicou pastas selecionáveis.",
            )
            return
        atual = self.pasta_remota.get()
        self.urls_por_pasta = {item.nome: item.url for item in diretorios}
        nomes = tuple(self.urls_por_pasta)
        self.seletor_pasta.configure(values=nomes)
        selecionada = atual if atual in self.urls_por_pasta else nomes[0]
        self.pasta_remota.set(selecionada)
        self._remover_subpastas(0)
        nova_url = self.urls_por_pasta[selecionada]
        if nova_url != self.url.get():
            self.url.set(nova_url)
            self._invalidar_plano_por_troca(selecionada)
        self.status.set(f"{len(nomes)} pasta(s) disponível(is) no servidor.")
        self._carregar_subpastas(nova_url)

    def _ao_trocar_pasta(self, _evento: object | None = None) -> None:
        selecionada = self.pasta_remota.get()
        nova_url = self.urls_por_pasta.get(selecionada)
        if not nova_url:
            return
        self._remover_subpastas(0)
        self._selecionar_url(nova_url)
        self._carregar_subpastas(nova_url)

    def _selecionar_url(self, url: str) -> None:
        if url != self.url.get():
            self.url.set(url)
            self._invalidar_plano_por_troca(unquote(urlsplit(url).path))

    def _remover_subpastas(self, a_partir: int) -> None:
        for nivel in self.subpastas[a_partir:]:
            nivel.quadro.destroy()
        del self.subpastas[a_partir:]

    def _carregar_subpastas(self, url_pai: str) -> None:
        validar_certificado = self.validar_certificado.get()
        self._executar_em_segundo_plano(
            "Procurando subpastas na pasta selecionada...",
            lambda: listar_diretorios(
                url_pai, verificar_certificado=validar_certificado
            ),
            lambda diretorios: self._mostrar_subpastas(url_pai, diretorios),
        )

    def _mostrar_subpastas(self, url_pai: str, diretorios: Any) -> None:
        if self.url.get() != url_pai:
            return
        if not diretorios:
            self.status.set("Pasta selecionada sem subpastas; pronta para verificar imagens.")
            return
        indice = len(self.subpastas)
        quadro = ttk.Frame(self.quadro_subpastas)
        quadro.pack(fill="x", pady=4)
        quadro.columnconfigure(1, weight=1)
        ttk.Label(quadro, text=f"Subpasta (nível {indice + 1}):").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        seletor = ttk.Combobox(
            quadro,
            values=("Buscar nesta pasta (raiz deste nível)",)
            + tuple(item.nome for item in diretorios),
            state="readonly",
        )
        seletor.grid(row=0, column=1, sticky="ew")
        seletor.current(0)
        self.subpastas.append(
            SeletorSubpasta(quadro, seletor, url_pai, tuple(item.url for item in diretorios))
        )
        seletor.bind(
            "<<ComboboxSelected>>",
            lambda _evento: self._ao_trocar_subpasta(indice),
        )
        self.status.set("Escolha uma subpasta ou verifique as imagens desta pasta.")

    def _ao_trocar_subpasta(self, indice: int) -> None:
        nivel = self.subpastas[indice]
        escolha = nivel.seletor.current()
        if escolha < 0:
            return
        self._remover_subpastas(indice + 1)
        nova_url = nivel.url_pai if escolha == 0 else nivel.urls[escolha - 1]
        self._selecionar_url(nova_url)
        if escolha != 0:
            self._carregar_subpastas(nova_url)

    def _invalidar_plano_por_troca(self, selecionada: str) -> None:
        self.estado = None
        self.linhas_por_item.clear()
        for item in self.tabela.get_children():
            self.tabela.delete(item)
        self.filtro_status.set("Todos")
        self.seletor_filtro.configure(state="disabled")
        self.total_novas.set("0")
        self.total_existentes.set("0")
        self.total_ignoradas.set("0")
        self.total_problemas.set("0")
        self.resumo.set("Pasta alterada; verifique as imagens novamente.")
        self.status.set(f"Pasta selecionada: {selecionada}")
        self._atualizar_botao_aplicar()

    def _definir_ocupado(self, ocupado: bool, descricao: str = "") -> None:
        self.em_execucao = ocupado
        estado = "disabled" if ocupado else "normal"
        for botao in (
            self.botao_simular,
            self.botao_autorizar,
            self.botao_renovar,
            self.botao_pasta,
            self.botao_escolher_saida,
            self.botao_atualizar_pastas,
            self.botao_configuracoes,
        ):
            botao.configure(state=estado)
        self.seletor_pasta.configure(state="disabled" if ocupado else "readonly")
        for nivel in self.subpastas:
            nivel.seletor.configure(state="disabled" if ocupado else "readonly")
        self.seletor_filtro.configure(
            state="disabled" if ocupado or self.estado is None else "readonly"
        )
        self.entrada_saida.configure(state="disabled" if ocupado else "normal")
        self.check_tls.configure(state="disabled" if ocupado else "normal")
        if ocupado:
            self.botao_aplicar.configure(state="disabled")
            self.progresso.start(12)
            self.status.set(descricao)
            self.orientacao.set("Aguarde a operação terminar.")
        else:
            self.progresso.stop()
            self._atualizar_botao_aplicar()

    def _executar_em_segundo_plano(
        self,
        descricao: str,
        tarefa: Callable[[], Any],
        ao_concluir: Callable[[Any], None],
    ) -> None:
        if self.em_execucao:
            return
        self.ao_concluir = ao_concluir
        self._definir_ocupado(True, descricao)

        def executar() -> None:
            try:
                resultado = tarefa()
            except Exception as erro:  # a UI precisa transformar falhas em diálogo
                self.fila.put(("erro", descricao, erro))
            else:
                self.fila.put(("sucesso", descricao, resultado))

        threading.Thread(target=executar, daemon=True).start()

    def _processar_fila(self) -> None:
        try:
            tipo, descricao, payload = self.fila.get_nowait()
        except queue.Empty:
            self.raiz.after(100, self._processar_fila)
            return

        self._definir_ocupado(False)
        if tipo == "erro":
            self.status.set(f"Falha em: {descricao}")
            messagebox.showerror(
                "Operação não concluída",
                mensagem_erro_amigavel(payload),
            )
        else:
            self.status.set(f"Concluído: {descricao}")
            if self.ao_concluir is not None:
                self.ao_concluir(payload)
        self.raiz.after(100, self._processar_fila)

    def simular(self) -> None:
        url = self.url.get().strip()
        pasta_remota = unquote(urlsplit(url).path)
        if not url:
            messagebox.showwarning(
                "Pasta obrigatória",
                "Selecione uma pasta de imagens publicada no servidor.",
            )
            return
        pasta = Path(self.pasta_saida.get()).expanduser().resolve()
        validar_certificado = self.validar_certificado.get()

        def tarefa() -> EstadoSimulacao:
            load_dotenv(override=True)
            pasta.mkdir(parents=True, exist_ok=True)
            resultado = coletar_imagens(
                url,
                verificar_certificado=validar_certificado,
            )
            gerar_csv(resultado, str(pasta / "imagens_produtos.csv"))
            cliente = BlingSomenteLeitura.do_ambiente()
            codigos = [produto.codigo_bling for produto in resultado.produtos]
            encontrados = cliente.buscar_produtos_por_codigos(codigos)
            gerar_conferencia_bling(
                resultado,
                encontrados,
                str(pasta / "conferencia_bling.csv"),
            )
            linhas = criar_plano_imagens(resultado, encontrados, cliente)
            gerar_plano_imagens(linhas, str(pasta / "plano_imagens.csv"))
            return EstadoSimulacao(resultado, encontrados, linhas, pasta)

        self._executar_em_segundo_plano(
            f"Verificando imagens da pasta {pasta_remota}...",
            tarefa,
            self._mostrar_simulacao,
        )

    def _atualizar_metricas(self, linhas: list[dict[str, str | int]]) -> None:
        totais = totais_plano(linhas)
        ignoradas = totais["IGNORADO_PRODUTO_NAO_ENCONTRADO"]
        problemas = len(linhas) - totais["ADICIONAR"] - totais["JA_EXISTE"] - ignoradas
        self.total_novas.set(str(totais["ADICIONAR"]))
        self.total_existentes.set(str(totais["JA_EXISTE"]))
        self.total_ignoradas.set(str(ignoradas))
        self.total_problemas.set(str(problemas))

    def _linha_passa_filtro(self, linha: dict[str, str | int]) -> bool:
        filtro = self.filtro_status.get()
        status = str(linha["status_planejado"])
        if filtro == "Somente novas":
            return status == "ADICIONAR"
        if filtro == "Já cadastradas":
            return status == "JA_EXISTE"
        if filtro == "Não encontradas":
            return status == "IGNORADO_PRODUTO_NAO_ENCONTRADO"
        if filtro == "Com problemas":
            return status not in {
                "ADICIONAR",
                "JA_EXISTE",
                "IGNORADO_PRODUTO_NAO_ENCONTRADO",
            }
        return True

    def _ao_filtrar(self, _evento: object | None = None) -> None:
        self._preencher_tabela()

    def _preencher_tabela(self) -> None:
        self.linhas_por_item.clear()
        for item in self.tabela.get_children():
            self.tabela.delete(item)
        if self.estado is None:
            self._atualizar_botao_aplicar()
            return

        for indice, linha in enumerate(self.estado.linhas):
            if not self._linha_passa_filtro(linha):
                continue
            item = f"linha-{indice}"
            status = str(linha["status_planejado"])
            tag = (
                status
                if status
                in {
                    "ADICIONAR",
                    "JA_EXISTE",
                    "IGNORADO_PRODUTO_NAO_ENCONTRADO",
                }
                else "BLOQUEADO"
            )
            valores = []
            for coluna in self.COLUNAS:
                valor = linha.get(coluna, "")
                if coluna == "status_planejado":
                    valor = self.ROTULOS_STATUS.get(status, "Revisar")
                valores.append(valor)
            self.tabela.insert("", "end", iid=item, values=valores, tags=(tag,))
            self.linhas_por_item[item] = linha
        self._atualizar_botao_aplicar()

    def _mostrar_simulacao(self, estado: EstadoSimulacao) -> None:
        self.estado = estado
        self.filtro_status.set("Todos")
        self.seletor_filtro.configure(state="readonly")
        self._atualizar_metricas(estado.linhas)
        self._preencher_tabela()
        self.resumo.set(resumo_plano(estado.linhas))
        self.status.set(f"Relatórios atualizados em {estado.pasta_saida}")
        self._atualizar_botao_aplicar()

    def _linha_selecionada(self) -> dict[str, str | int] | None:
        selecao = self.tabela.selection()
        if not selecao:
            return None
        return self.linhas_por_item.get(selecao[0])

    def _ao_selecionar(self, _evento: object | None = None) -> None:
        self._atualizar_botao_aplicar()

    def _atualizar_botao_aplicar(self) -> None:
        linha = self._linha_selecionada()
        habilitado = (
            not self.em_execucao
            and self.estado is not None
            and linha is not None
            and linha_pode_ser_aplicada(linha)
        )
        self.botao_aplicar.configure(state="normal" if habilitado else "disabled")

        if self.em_execucao:
            return
        if self.estado is None:
            self.orientacao.set(
                "O envio será liberado somente quando houver uma imagem nova selecionada."
            )
            return
        total_novas = totais_plano(self.estado.linhas)["ADICIONAR"]
        if not total_novas:
            self.orientacao.set("Nenhuma imagem nova precisa ser enviada.")
        elif linha is None:
            self.orientacao.set("Selecione uma linha verde com status “Nova imagem”.")
        elif not linha_pode_ser_aplicada(linha):
            self.orientacao.set("A linha selecionada não precisa ser enviada.")
        else:
            sku = str(linha["codigo_bling"])
            novas_sku = sum(
                1
                for item in self.estado.linhas
                if item["codigo_bling"] == sku and linha_pode_ser_aplicada(item)
            )
            self.orientacao.set(
                f"Pronto para enviar {novas_sku} imagem(ns) nova(s) do SKU {sku}."
            )

    def aplicar_selecionado(self) -> None:
        linha = self._linha_selecionada()
        if self.estado is None or linha is None or not linha_pode_ser_aplicada(linha):
            messagebox.showwarning(
                "Seleção inválida",
                "Selecione uma linha verde com status Nova imagem.",
            )
            return

        sku = str(linha["codigo_bling"])
        nome = str(linha.get("nome_bling") or "Produto sem nome")
        total_novas = sum(
            1
            for item in self.estado.linhas
            if item["codigo_bling"] == sku and linha_pode_ser_aplicada(item)
        )
        confirmacao = simpledialog.askstring(
            "Confirmar envio ao Bling",
            f"Produto: {nome}\nSKU: {sku}\nImagens novas: {total_novas}\n\n"
            "Digite APLICAR para enviar:",
            parent=self.raiz,
        )
        if confirmacao != "APLICAR":
            self.status.set("Envio cancelado; nenhuma alteração foi realizada.")
            return

        estado = self.estado

        def tarefa() -> ResultadoAplicacao:
            load_dotenv(override=True)
            cliente = BlingImagens.do_ambiente()
            encontrados_atualizados = cliente.buscar_produtos_por_codigos([sku])
            aplicacao = aplicar_imagens_piloto(
                estado.resultado,
                encontrados_atualizados,
                cliente,
                sku,
            )
            gerar_relatorio_aplicacao(
                aplicacao,
                str(estado.pasta_saida / "aplicacao_imagens.csv"),
            )
            return aplicacao

        self._executar_em_segundo_plano(
            f"Enviando imagens somente para o SKU {sku}...",
            tarefa,
            self._mostrar_aplicacao,
        )

    def _mostrar_aplicacao(self, aplicacao: ResultadoAplicacao) -> None:
        texto = (
            f"SKU: {aplicacao.codigo_bling}\n"
            f"Imagens anteriores: {aplicacao.quantidade_antes}\n"
            f"Imagens novas: {aplicacao.quantidade_novas}\n"
            f"Total conferido: {aplicacao.quantidade_depois}\n\n"
            f"{aplicacao.motivo}"
        )
        if aplicacao.status == "APLICADO_E_VERIFICADO":
            messagebox.showinfo("Imagens enviadas e conferidas", texto)
            self.raiz.after(200, self.simular)
        elif aplicacao.status == "ERRO_VERIFICACAO_POS_PATCH":
            messagebox.showerror("Não foi possível confirmar o envio", texto)
        else:
            messagebox.showwarning("Nenhuma imagem enviada", texto)

    def autorizar_bling(self) -> None:
        if not messagebox.askokcancel(
            "Autorizar Bling",
            "Antes de continuar, confirme no cadastro do aplicativo que o escopo "
            "Salvar imagens dos Produtos está habilitado. O navegador será aberto.",
        ):
            return

        def tarefa() -> Any:
            configuracao = carregar_configuracao()
            return autorizar(configuracao, abrir_navegador=True)

        self._executar_em_segundo_plano(
            "Aguardando autorização no navegador...",
            tarefa,
            lambda resultado: messagebox.showinfo(
                "Bling autorizado",
                "Novos tokens foram salvos com segurança."
                + (
                    f"\nValidade informada: {resultado.expires_in} segundos."
                    if resultado.expires_in is not None
                    else ""
                ),
            ),
        )

    def renovar_token(self) -> None:
        def tarefa() -> Any:
            configuracao = carregar_configuracao()
            return renovar(configuracao)

        self._executar_em_segundo_plano(
            "Renovando autorização do Bling...",
            tarefa,
            lambda resultado: messagebox.showinfo(
                "Autorização renovada",
                "Os tokens foram atualizados com segurança."
                + (
                    f"\nValidade informada: {resultado.expires_in} segundos."
                    if resultado.expires_in is not None
                    else ""
                ),
            ),
        )


def main() -> None:
    raiz = tk.Tk()
    AutoFotosGUI(raiz)
    raiz.mainloop()


if __name__ == "__main__":
    main()
