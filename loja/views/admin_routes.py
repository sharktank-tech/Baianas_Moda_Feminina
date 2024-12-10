from flask import Blueprint, render_template, request, redirect, url_for, flash
from loja.modules.models import db, Products, Users
from flask_login import login_required, logout_user, current_user

admin_blueprint = Blueprint('admin', __name__, template_folder='templates/admin')


# Rota principal do painel de administração
@admin_blueprint.route('/admin')
@login_required
def admin_dashboard():
    products = Products.query.all()
    users = Users.query.all()
    return render_template('admin/dashboard.html', products=products, users=users)

# Gerenciamento de Produtos
@admin_blueprint.route('/admin/products/add', methods=['GET', 'POST'])
@login_required
def add_product():
    if request.method == 'POST':
        # Captura os dados do formulário
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        price = request.form.get('price', '').strip()
        image = request.form.get('image', '').strip()

        # Validação básica dos dados
        errors = []
        if not name:
            errors.append('O campo "Nome" é obrigatório.')
        if not price:
            errors.append('O campo "Preço" é obrigatório.')
        elif not price.replace('.', '', 1).isdigit():  # Verifica se o preço é numérico
            errors.append('O campo "Preço" deve conter um número válido.')
        if not image:
            errors.append('O campo "Imagem" é obrigatório.')

        # Se houver erros, exibir mensagens e redirecionar
        if errors:
            for error in errors:
                flash(error, 'danger')
            return redirect(url_for('admin.add_product'))

        try:
            # Converte os valores necessários
            price = float(price)
            user_id = current_user.id

            # Criação do novo produto
            new_product = Products(
                name=name,
                description=description,
                price=price,
                image=image,
                user_id=user_id
            )
            db.session.add(new_product)
            db.session.commit()

            flash('Produto adicionado com sucesso!', 'success')
            return redirect(url_for('admin.admin_dashboard'))
        except ValueError as ve:
            # Tratamento de erro para conversão inválida
            flash(f'Erro no campo "Preço": {str(ve)}', 'danger')
            return redirect(url_for('admin.add_product'))
        except Exception as e:
            # Tratamento de outros erros
            db.session.rollback()
            flash(f'Ocorreu um erro ao adicionar o produto: {str(e)}', 'danger')
            return redirect(url_for('admin.add_product'))

    # Renderiza o formulário para adicionar um produto
    return render_template('admin/add_product.html')


@admin_blueprint.route('/admin/products/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_product(id):
    product = Products.query.get_or_404(id)

    if request.method == 'POST':
        # Captura os dados do formulário
        name = request.form.get('name')
        description = request.form.get('description')
        price = request.form.get('price')
        image = request.form.get('image')
        user_id = current_user.id  # Usando o user_id do admin logado, não do formulário

        # Validação dos campos obrigatórios
        if not name or not price or not image:
            flash('Preencha todos os campos obrigatórios!', 'danger')
            return redirect(url_for('admin.edit_product', id=product.id))

        try:
            # Atualiza os dados do produto
            product.name = name
            product.description = description
            product.price = float(price)
            product.image = image
            product.user_id = user_id  # Certificando-se de usar o user_id do admin

            db.session.commit()
            flash('Produto atualizado com sucesso!', 'success')
            return redirect(url_for('admin.admin_dashboard'))

        except Exception as e:
            # Tratamento de erro caso a atualização falhe
            db.session.rollback()
            flash(f'Ocorreu um erro ao atualizar o produto: {str(e)}', 'danger')
            return redirect(url_for('admin.edit_product', id=product.id))

    # Renderiza o formulário de edição com os dados do produto
    return render_template('admin/edit_product.html', product=product)


@admin_blueprint.route('/admin/products/delete/<int:id>', methods=['POST'])
@login_required
def delete_product(id):
    product = Products.query.get_or_404(id)
    db.session.delete(product)
    db.session.commit()
    flash('Produto excluído com sucesso!', 'success')
    return redirect(url_for('admin.admin_dashboard'))

# Gerenciamento de Usuários
@admin_blueprint.route('/admin/users/add', methods=['GET', 'POST'])
@login_required
def add_user():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']

        new_user = Users(username=username, email=email)
        new_user.set_password(password)  # Ajuste para hash da senha
        db.session.add(new_user)
        db.session.commit()
        flash('Usuário adicionado com sucesso!', 'success')
        return redirect(url_for('admin.admin_dashboard'))

    return render_template('admin/add_user.html')

@admin_blueprint.route('/admin/users/delete/<int:id>', methods=['POST'])
@login_required
def delete_user(id):
    user = Users.query.get_or_404(id)
    db.session.delete(user)
    db.session.commit()
    flash('Usuário excluído com sucesso!', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_blueprint.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    flash("Logout bem-sucedido!", "success")
    return redirect(url_for('main.login'))