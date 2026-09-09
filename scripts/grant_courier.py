"""Libera (ou revoga) o acesso de um entregador ao aplicativo de rua.

O entregador entra com a conta dele, no celular dele, e alcança apenas as rotas
de rua: check-in, fila do dia, entrega e check-out. Todo o resto do ERP exige
`erp_admin` e responde 403 para ele — o que mantém DRE, custo e precificação
fora de um aparelho que passa o dia na garupa de uma moto.

O vínculo entre a conta e o cadastro é o e-mail. Por isso o script confere que
existe um entregador cadastrado com aquele e-mail antes de conceder a
permissão: dar acesso a uma conta sem cadastro produziria alguém autenticado
que não consegue abrir turno nenhum, e a mensagem de erro apareceria só na
calçada.

    python -m scripts.grant_courier --email joao@loja.com
    python -m scripts.grant_courier --email joao@loja.com --revoke
"""

from __future__ import annotations

import argparse
import json
import unicodedata

from app.config import get_settings
from app.firebase import get_firebase_app
from app.repositories.store import build_store


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii").strip().lower()


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the courier Firebase custom claim.")
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--uid")
    identity.add_argument("--email")
    parser.add_argument("--revoke", action="store_true")
    # A conferência com o cadastro é o comportamento padrão. A saída existe
    # para o caso de a loja querer liberar a conta antes de cadastrar a pessoa.
    parser.add_argument("--skip-courier-check", action="store_true")
    args = parser.parse_args()

    from firebase_admin import auth

    settings = get_settings()
    app = get_firebase_app(settings)
    user = auth.get_user(args.uid, app=app) if args.uid else auth.get_user_by_email(args.email, app=app)

    cadastro = None
    if not args.revoke and not args.skip_courier_check:
        store = build_store(settings)
        alvo = _fold(user.email or "")
        cadastro = next(
            (linha for linha in store.list("couriers") if alvo and _fold(linha.get("email") or "") == alvo),
            None,
        )
        if not cadastro:
            print(json.dumps({
                "error": "sem-cadastro",
                "detail": (
                    f"Nenhum entregador cadastrado com o e-mail {user.email}. "
                    "Cadastre em Entrega › Entregadores com este e-mail, ou use --skip-courier-check."
                ),
            }, ensure_ascii=False))
            return 1
        # O uid é carimbado aqui para o primeiro acesso já encontrar o vínculo,
        # e para trocar o e-mail depois não desligar ninguém do próprio turno.
        if cadastro.get("uid") != user.uid:
            cadastro["uid"] = user.uid
            store.put("couriers", cadastro["id"], cadastro)

    claims = dict(user.custom_claims or {})
    if args.revoke:
        claims.pop("courier", None)
    else:
        claims["courier"] = True
    auth.set_custom_user_claims(user.uid, claims or None, app=app)

    print(json.dumps({
        "uid": user.uid,
        "email": user.email,
        "courier": not args.revoke,
        "courier_id": (cadastro or {}).get("id"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
