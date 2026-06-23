from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, logout_user, current_user
from werkzeug.security import generate_password_hash

from loja.modules.models import db, Product, User

admin_blueprint = Blueprint('admin', __name__, template_folder='templates/admin')


# ── Dashboard ─────────────────────────────────────────────────────────────────

@admin_blueprint.route('/admin')
@login_required
def admin_dashboard():
    products = Product.query.all()   # corrigido: Product
    users = User.query.all()         # corrigido: User
    return render_template('admin/dashboard.html', products=products, users=users)


# ── Produtos ──────────────────────────────────────────────────────────────────

@admin_blueprint.route('/admin/products/add', methods=['GET', 'POST'])
@login_required
def add_product():
    if request.method == 'POST':
        name        = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        price       = request.form.get('price', '').strip()
        image       = request.form.get('image', '').strip()

        errors = []
        if not name:
            errors.append('O campo "Nome" é obrigatório.')
        if not price:
            errors.append('O campo "Preço" é obrigatório.')
        elif not price.replace('.', '', 1).isdigit():
            errors.append('O campo "Preço" deve conter um número válido.')
        if not image:
            errors.append('O campo "Imagem" é obrigatório.')

        if errors:
            for error in errors:
                flash(error, 'danger')
            return redirect(url_for('admin.add_product'))

        try:
            new_product = Product(          # corrigido: Product
                name=name,
                description=description,
                price=float(price),
                image=image,
                user_id=current_user.id,
            )
            db.session.add(new_product)
            db.session.commit()

            flash('Produto adicionado com sucesso!', 'success')
            return redirect(url_for('admin.admin_dashboard'))
        except ValueError as ve:
            flash(f'Erro no campo "Preço": {str(ve)}', 'danger')
            return redirect(url_for('admin.add_product'))
        except Exception as e:
            db.session.rollback()
            flash(f'Ocorreu um erro ao adicionar o produto: {str(e)}', 'danger')
            return redirect(url_for('admin.add_product'))

    return render_template('admin/add_product.html')


@admin_blueprint.route('/admin/products/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_product(id):
    product = Product.query.get_or_404(id)   # corrigido: Product

    if request.method == 'POST':
        name        = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        price       = request.form.get('price', '').strip()
        image       = request.form.get('image', '').strip()

        if not name or not price or not image:
            flash('Preencha todos os campos obrigatórios!', 'danger')
            return redirect(url_for('admin.edit_product', id=product.id))

        try:
            product.name        = name
            product.description = description
            product.price       = float(price)
            product.image       = image
            product.user_id     = current_user.id

            db.session.commit()
            flash('Produto atualizado com sucesso!', 'success')
            return redirect(url_for('admin.admin_dashboard'))
        except Exception as e:
            db.session.rollback()
            flash(f'Ocorreu um erro ao atualizar o produto: {str(e)}', 'danger')
            return redirect(url_for('admin.edit_product', id=product.id))

    return render_template('admin/edit_product.html', product=product)


@admin_blueprint.route('/admin/products/delete/<int:id>', methods=['POST'])
@login_required
def delete_product(id):
    product = Product.query.get_or_404(id)   # corrigido: Product
    db.session.delete(product)
    db.session.commit()
    flash('Produto excluído com sucesso!', 'success')
    return redirect(url_for('admin.admin_dashboard'))


# ── Usuários ──────────────────────────────────────────────────────────────────

@admin_blueprint.route('/admin/users/add', methods=['GET', 'POST'])
@login_required
def add_user():
    if request.method == 'POST':
        name     = request.form.get('username', '').strip()   # corrigido: name
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()

        new_user = User(                                       # corrigido: User
            name=name,                                         # corrigido: name=
            email=email,
            password=generate_password_hash(password),        # corrigido: hash direto
        )
        db.session.add(new_user)
        db.session.commit()
        flash('Usuário adicionado com sucesso!', 'success')
        return redirect(url_for('admin.admin_dashboard'))

    return render_template('admin/add_user.html')


@admin_blueprint.route('/admin/users/delete/<int:id>', methods=['POST'])
@login_required
def delete_user(id):
    user = User.query.get_or_404(id)   # corrigido: User
    db.session.delete(user)
    db.session.commit()
    flash('Usuário excluído com sucesso!', 'success')
    return redirect(url_for('admin.admin_dashboard'))


# ── Logout ────────────────────────────────────────────────────────────────────

@admin_blueprint.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    flash("Logout bem-sucedido!", "success")
    return redirect(url_for('main.login'))