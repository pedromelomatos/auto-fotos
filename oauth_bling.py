"""Assistente OAuth local para a API do Bling.

Este e o unico modulo do projeto autorizado a fazer POST. As chamadas sao
restritas ao endpoint OAuth de emissao/renovacao de tokens e nunca alteram
produtos ou outros registros do ERP.
"""

from __future__ import annotations

import argparse
import html
import os
import secrets
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import dotenv_values, load_dotenv, set_key


URL_AUTORIZACAO = "https://www.bling.com.br/Api/v3/oauth/authorize"
URL_TOKEN = "https://api.bling.com.br/Api/v3/oauth/token"
REDIRECT_PADRAO = "http://127.0.0.1:8765/callback"


class ErroOAuthBling(RuntimeError):
    """Erro seguro no fluxo OAuth, sem exposicao de credenciais ou tokens."""


@dataclass(frozen=True, slots=True)
class ConfiguracaoOAuth:
    client_id: str
    client_secret: str
    redirect_uri: str
    arquivo_env: Path


@dataclass(slots=True)
class ResultadoCallback:
    codigo: str | None = None
    state: str | None = None
    erro: str | None = None
    concluido: bool = False


@dataclass(frozen=True, slots=True)
class ResultadoToken:
    expires_in: int | None
    escopos: str | None


def tokens_configurados(configuracao: ConfiguracaoOAuth) -> bool:
    """Retorna True somente quando access e refresh tokens estao presentes."""
    valores = dotenv_values(configuracao.arquivo_env)
    access_token = str(valores.get("BLING_ACCESS_TOKEN") or "").strip()
    refresh_token = str(valores.get("BLING_REFRESH_TOKEN") or "").strip()
    return bool(access_token and refresh_token)


def token_precisa_renovacao(
    configuracao: ConfiguracaoOAuth,
    *,
    margem_segundos: int = 300,
    agora: datetime | None = None,
) -> bool:
    """Indica se ha um refresh token e o access token precisa ser renovado.

    Uma expiracao ausente ou malformada e tratada como desconhecida e provoca
    renovacao. Assim, instalacoes antigas passam a registrar uma validade
    confiavel na primeira abertura do GUI.
    """
    valores = dotenv_values(configuracao.arquivo_env)
    refresh_token = str(valores.get("BLING_REFRESH_TOKEN") or "").strip()
    if not refresh_token:
        return False

    access_token = str(valores.get("BLING_ACCESS_TOKEN") or "").strip()
    expira_em_texto = str(valores.get("BLING_TOKEN_EXPIRES_AT") or "").strip()
    if not access_token or not expira_em_texto:
        return True

    try:
        expira_em = datetime.fromisoformat(expira_em_texto.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expira_em.tzinfo is None:
        expira_em = expira_em.replace(tzinfo=timezone.utc)

    instante_atual = agora or datetime.now(timezone.utc)
    if instante_atual.tzinfo is None:
        instante_atual = instante_atual.replace(tzinfo=timezone.utc)
    limite = instante_atual.astimezone(timezone.utc) + timedelta(
        seconds=max(0, margem_segundos)
    )
    return expira_em.astimezone(timezone.utc) <= limite


def carregar_configuracao(arquivo_env: str | Path = ".env") -> ConfiguracaoOAuth:
    caminho = Path(arquivo_env).resolve()
    if not caminho.is_file():
        raise ErroOAuthBling(
            "Arquivo .env nao encontrado. Copie .env.example para .env primeiro."
        )

    valores = dotenv_values(caminho)
    client_id = str(valores.get("BLING_CLIENT_ID") or "").strip()
    client_secret = str(valores.get("BLING_CLIENT_SECRET") or "").strip()
    redirect_uri = str(
        valores.get("BLING_REDIRECT_URI") or REDIRECT_PADRAO
    ).strip()

    if not client_id or not client_secret:
        raise ErroOAuthBling(
            "Preencha BLING_CLIENT_ID e BLING_CLIENT_SECRET no arquivo .env."
        )

    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ErroOAuthBling(
            "BLING_REDIRECT_URI deve ser um callback HTTP local em 127.0.0.1 ou localhost."
        )
    if not parsed.port:
        raise ErroOAuthBling("BLING_REDIRECT_URI deve informar uma porta local.")
    if not parsed.path or parsed.path == "/":
        raise ErroOAuthBling("BLING_REDIRECT_URI deve informar o caminho /callback.")

    return ConfiguracaoOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        arquivo_env=caminho,
    )


def construir_url_autorizacao(client_id: str, state: str) -> str:
    parametros = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "state": state,
        }
    )
    return f"{URL_AUTORIZACAO}?{parametros}"


def _pagina_callback(titulo: str, mensagem: str, sucesso: bool) -> bytes:
    cor = "#16794b" if sucesso else "#a12622"
    conteudo = f"""<!doctype html>
<html lang="pt-BR">
<head><meta charset="utf-8"><title>{html.escape(titulo)}</title></head>
<body style="font-family:Segoe UI,Arial,sans-serif;max-width:680px;margin:64px auto;padding:24px">
  <h1 style="color:{cor}">{html.escape(titulo)}</h1>
  <p>{html.escape(mensagem)}</p>
  <p>Esta aba pode ser fechada.</p>
</body>
</html>"""
    return conteudo.encode("utf-8")


def _criar_handler(caminho_callback: str, resultado: ResultadoCallback):
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - nome exigido por BaseHTTPRequestHandler
            parsed = urlparse(self.path)
            if parsed.path != caminho_callback:
                self.send_response(404)
                self.end_headers()
                return

            parametros = parse_qs(parsed.query)
            resultado.codigo = (parametros.get("code") or [None])[0]
            resultado.state = (parametros.get("state") or [None])[0]
            resultado.erro = (parametros.get("error_description") or parametros.get("error") or [None])[0]
            resultado.concluido = True

            sucesso = bool(resultado.codigo) and not resultado.erro
            corpo = _pagina_callback(
                "Autorizacao recebida" if sucesso else "Autorizacao nao concluida",
                "O codigo foi recebido e sera trocado por tokens localmente."
                if sucesso
                else (resultado.erro or "O Bling nao retornou um codigo de autorizacao."),
                sucesso,
            )
            self.send_response(200 if sucesso else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corpo)

        def log_message(self, format: str, *args: object) -> None:
            return

    return CallbackHandler


def aguardar_callback(
    redirect_uri: str,
    *,
    tempo_limite: int = 300,
    abrir_navegador: bool = True,
    client_id: str,
    state_esperado: str,
) -> str:
    parsed = urlparse(redirect_uri)
    resultado = ResultadoCallback()
    handler = _criar_handler(parsed.path, resultado)

    try:
        servidor = HTTPServer((parsed.hostname or "127.0.0.1", parsed.port or 8765), handler)
    except OSError as erro:
        raise ErroOAuthBling(
            f"Nao foi possivel abrir o callback local em {redirect_uri}. "
            "Verifique se a porta ja esta em uso."
        ) from erro

    url = construir_url_autorizacao(client_id, state_esperado)
    print("Aguardando autorizacao do Bling no navegador...")
    print("Se o navegador nao abrir, copie esta URL:")
    print(url)

    if abrir_navegador:
        webbrowser.open(url, new=2)

    inicio = time.monotonic()
    try:
        while not resultado.concluido:
            restante = tempo_limite - (time.monotonic() - inicio)
            if restante <= 0:
                raise ErroOAuthBling("Tempo esgotado aguardando a autorizacao do Bling.")
            servidor.timeout = min(1.0, restante)
            servidor.handle_request()
    finally:
        servidor.server_close()

    if resultado.erro:
        raise ErroOAuthBling(f"O Bling recusou a autorizacao: {resultado.erro}")
    if not resultado.codigo:
        raise ErroOAuthBling("O callback nao retornou o codigo de autorizacao.")
    if not secrets.compare_digest(resultado.state or "", state_esperado):
        raise ErroOAuthBling("O parametro state retornado nao corresponde ao enviado.")
    return resultado.codigo


def _mensagem_erro_api(resposta: requests.Response) -> str:
    try:
        payload = resposta.json()
    except ValueError:
        return f"HTTP {resposta.status_code}"

    erro = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(erro, dict):
        descricao = erro.get("description") or erro.get("message") or erro.get("type")
        if descricao:
            return f"HTTP {resposta.status_code}: {descricao}"
    if isinstance(erro, str):
        return f"HTTP {resposta.status_code}: {erro}"
    return f"HTTP {resposta.status_code}"


def solicitar_tokens(
    configuracao: ConfiguracaoOAuth,
    *,
    grant_type: str,
    valor: str,
    sessao: requests.Session | None = None,
) -> dict[str, Any]:
    if grant_type not in {"authorization_code", "refresh_token"}:
        raise ValueError("grant_type OAuth nao permitido.")
    campo = "code" if grant_type == "authorization_code" else "refresh_token"
    cliente = sessao or requests.Session()

    try:
        resposta = cliente.post(
            URL_TOKEN,
            auth=(configuracao.client_id, configuracao.client_secret),
            headers={
                "Accept": "1.0",
                "Content-Type": "application/x-www-form-urlencoded",
                "enable-jwt": "1",
                "User-Agent": "auto-fotos-bling/1.0",
            },
            data={"grant_type": grant_type, campo: valor},
            timeout=30,
        )
    except requests.RequestException as erro:
        raise ErroOAuthBling("Falha de rede ao solicitar tokens ao Bling.") from erro

    if not resposta.ok:
        raise ErroOAuthBling(
            f"O Bling nao emitiu os tokens ({_mensagem_erro_api(resposta)})."
        )

    try:
        payload = resposta.json()
    except ValueError as erro:
        raise ErroOAuthBling("A resposta de tokens do Bling nao e JSON valido.") from erro
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ErroOAuthBling("A resposta do Bling nao contem access_token.")
    return payload


def salvar_tokens(
    configuracao: ConfiguracaoOAuth,
    payload: dict[str, Any],
) -> ResultadoToken:
    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    if not access_token:
        raise ErroOAuthBling("Access token ausente; nada foi salvo.")

    set_key(str(configuracao.arquivo_env), "BLING_ACCESS_TOKEN", access_token)
    if refresh_token:
        set_key(str(configuracao.arquivo_env), "BLING_REFRESH_TOKEN", refresh_token)

    expires_in_raw = payload.get("expires_in")
    try:
        expires_in = int(expires_in_raw) if expires_in_raw is not None else None
    except (TypeError, ValueError):
        expires_in = None
    if expires_in is not None:
        expira_em = datetime.fromtimestamp(
            time.time() + expires_in,
            tz=timezone.utc,
        ).isoformat()
        set_key(str(configuracao.arquivo_env), "BLING_TOKEN_EXPIRES_AT", expira_em)

    escopos = payload.get("scope")
    return ResultadoToken(
        expires_in=expires_in,
        escopos=str(escopos) if escopos is not None else None,
    )


def autorizar(configuracao: ConfiguracaoOAuth, *, abrir_navegador: bool = True) -> ResultadoToken:
    state = secrets.token_urlsafe(32)
    codigo = aguardar_callback(
        configuracao.redirect_uri,
        client_id=configuracao.client_id,
        state_esperado=state,
        abrir_navegador=abrir_navegador,
    )
    payload = solicitar_tokens(
        configuracao,
        grant_type="authorization_code",
        valor=codigo,
    )
    return salvar_tokens(configuracao, payload)


def renovar(configuracao: ConfiguracaoOAuth) -> ResultadoToken:
    valores = dotenv_values(configuracao.arquivo_env)
    refresh_token = str(valores.get("BLING_REFRESH_TOKEN") or "").strip()
    if not refresh_token:
        raise ErroOAuthBling("BLING_REFRESH_TOKEN nao esta preenchido no .env.")
    payload = solicitar_tokens(
        configuracao,
        grant_type="refresh_token",
        valor=refresh_token,
    )
    return salvar_tokens(configuracao, payload)


def criar_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Autorizacao OAuth local do Bling.")
    parser.add_argument(
        "acao",
        choices=("autorizar", "renovar"),
        help="Inicia uma autorizacao nova ou renova os tokens existentes.",
    )
    parser.add_argument(
        "--sem-navegador",
        action="store_true",
        help="Mostra a URL sem tentar abrir o navegador automaticamente.",
    )
    return parser


def main() -> int:
    load_dotenv()
    argumentos = criar_parser().parse_args()
    try:
        configuracao = carregar_configuracao()
        if argumentos.acao == "autorizar":
            resultado = autorizar(
                configuracao,
                abrir_navegador=not argumentos.sem_navegador,
            )
        else:
            resultado = renovar(configuracao)
    except (ErroOAuthBling, ValueError) as erro:
        print(f"ERRO: {erro}", file=sys.stderr)
        return 1

    print("Tokens JWT salvos com seguranca no arquivo .env.")
    if resultado.expires_in is not None:
        print(f"Validade informada pelo Bling: {resultado.expires_in} segundos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
