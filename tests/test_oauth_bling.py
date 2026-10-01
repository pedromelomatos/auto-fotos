import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

from dotenv import dotenv_values

from oauth_bling import (
    ConfiguracaoOAuth,
    construir_url_autorizacao,
    salvar_tokens,
    solicitar_tokens,
    token_precisa_renovacao,
    tokens_configurados,
)


class OAuthBlingTests(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory()
        self.env_path = Path(self.pasta.name) / ".env"
        self.env_path.write_text(
            "BLING_CLIENT_ID=id-teste\nBLING_CLIENT_SECRET=segredo-teste\n",
            encoding="utf-8",
        )
        self.config = ConfiguracaoOAuth(
            client_id="id-teste",
            client_secret="segredo-teste",
            redirect_uri="http://127.0.0.1:8765/callback",
            arquivo_env=self.env_path,
        )

    def tearDown(self):
        self.pasta.cleanup()

    def test_url_de_autorizacao_contem_state_e_client_id(self):
        url = construir_url_autorizacao("cliente 123", "state-seguro")
        parametros = parse_qs(urlparse(url).query)
        self.assertEqual(parametros["response_type"], ["code"])
        self.assertEqual(parametros["client_id"], ["cliente 123"])
        self.assertEqual(parametros["state"], ["state-seguro"])

    def test_troca_codigo_usando_post_apenas_no_endpoint_oauth(self):
        resposta = Mock()
        resposta.ok = True
        resposta.json.return_value = {
            "access_token": "jwt-teste",
            "refresh_token": "refresh-teste",
            "expires_in": 21600,
        }
        sessao = Mock()
        sessao.post.return_value = resposta

        payload = solicitar_tokens(
            self.config,
            grant_type="authorization_code",
            valor="codigo-temporario",
            sessao=sessao,
        )

        self.assertEqual(payload["access_token"], "jwt-teste")
        sessao.post.assert_called_once()
        args, kwargs = sessao.post.call_args
        self.assertTrue(args[0].endswith("/oauth/token"))
        self.assertEqual(kwargs["headers"]["enable-jwt"], "1")
        self.assertEqual(kwargs["data"]["grant_type"], "authorization_code")

    def test_salva_tokens_sem_remover_credenciais(self):
        resultado = salvar_tokens(
            self.config,
            {
                "access_token": "jwt-teste",
                "refresh_token": "refresh-teste",
                "expires_in": 21600,
                "scope": "produto",
            },
        )
        valores = dotenv_values(self.env_path)
        self.assertEqual(valores["BLING_CLIENT_ID"], "id-teste")
        self.assertEqual(valores["BLING_CLIENT_SECRET"], "segredo-teste")
        self.assertEqual(valores["BLING_ACCESS_TOKEN"], "jwt-teste")
        self.assertEqual(valores["BLING_REFRESH_TOKEN"], "refresh-teste")
        self.assertEqual(resultado.expires_in, 21600)

    def test_token_valido_nao_precisa_ser_renovado(self):
        agora = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
        self.env_path.write_text(
            "BLING_CLIENT_ID=id-teste\n"
            "BLING_CLIENT_SECRET=segredo-teste\n"
            "BLING_ACCESS_TOKEN=access-teste\n"
            "BLING_REFRESH_TOKEN=refresh-teste\n"
            f"BLING_TOKEN_EXPIRES_AT={(agora + timedelta(hours=1)).isoformat()}\n",
            encoding="utf-8",
        )

        self.assertFalse(token_precisa_renovacao(self.config, agora=agora))

    def test_token_expirado_precisa_ser_renovado(self):
        agora = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
        self.env_path.write_text(
            "BLING_ACCESS_TOKEN=access-teste\n"
            "BLING_REFRESH_TOKEN=refresh-teste\n"
            f"BLING_TOKEN_EXPIRES_AT={(agora - timedelta(seconds=1)).isoformat()}\n",
            encoding="utf-8",
        )

        self.assertTrue(token_precisa_renovacao(self.config, agora=agora))

    def test_token_perto_de_expirar_usa_margem_de_seguranca(self):
        agora = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
        self.env_path.write_text(
            "BLING_ACCESS_TOKEN=access-teste\n"
            "BLING_REFRESH_TOKEN=refresh-teste\n"
            f"BLING_TOKEN_EXPIRES_AT={(agora + timedelta(minutes=4)).isoformat()}\n",
            encoding="utf-8",
        )

        self.assertTrue(token_precisa_renovacao(self.config, agora=agora))

    def test_validade_ausente_renova_se_houver_refresh_token(self):
        self.env_path.write_text(
            "BLING_ACCESS_TOKEN=access-teste\n"
            "BLING_REFRESH_TOKEN=refresh-teste\n",
            encoding="utf-8",
        )

        self.assertTrue(token_precisa_renovacao(self.config))

    def test_sem_refresh_token_nao_tenta_renovacao_automatica(self):
        self.env_path.write_text(
            "BLING_ACCESS_TOKEN=access-teste\n"
            "BLING_TOKEN_EXPIRES_AT=2020-01-01T00:00:00+00:00\n",
            encoding="utf-8",
        )

        self.assertFalse(token_precisa_renovacao(self.config))

    def test_tokens_configurados_exige_access_e_refresh(self):
        self.env_path.write_text(
            "BLING_ACCESS_TOKEN=access-teste\n"
            "BLING_REFRESH_TOKEN=refresh-teste\n",
            encoding="utf-8",
        )
        self.assertTrue(tokens_configurados(self.config))

        self.env_path.write_text(
            "BLING_ACCESS_TOKEN=access-teste\n",
            encoding="utf-8",
        )
        self.assertFalse(tokens_configurados(self.config))


if __name__ == "__main__":
    unittest.main()
