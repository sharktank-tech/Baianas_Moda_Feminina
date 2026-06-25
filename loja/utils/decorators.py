from functools import wraps
from flask import flash, redirect, url_for
from flask_login import current_user


def admin_required(func):
    @wraps(func)
    def decorated_view(*args, **kwargs):

        # Usuário não autenticado
        if not current_user.is_authenticated:
            flash("Faça login para acessar esta página.", "warning")
            return redirect(url_for("main.login"))

        # Usuário autenticado, mas não é administrador
        if not current_user.is_admin:
            flash("Você não possui permissão para acessar esta área.", "danger")
            return redirect(url_for("main.home"))

        return func(*args, **kwargs)

    return decorated_view