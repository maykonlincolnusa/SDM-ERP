"""Grant or remove the ERP administrator custom claim for one Firebase user."""

from __future__ import annotations

import argparse
import json

from app.config import get_settings
from app.firebase import get_firebase_app


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the erp_admin Firebase custom claim.")
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--uid")
    identity.add_argument("--email")
    parser.add_argument("--revoke", action="store_true")
    args = parser.parse_args()

    from firebase_admin import auth

    app = get_firebase_app(get_settings())
    user = auth.get_user(args.uid, app=app) if args.uid else auth.get_user_by_email(args.email, app=app)
    claims = dict(user.custom_claims or {})
    if args.revoke:
        claims.pop("erp_admin", None)
    else:
        claims["erp_admin"] = True
    auth.set_custom_user_claims(user.uid, claims or None, app=app)
    print(json.dumps({"uid": user.uid, "erp_admin": not args.revoke}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
