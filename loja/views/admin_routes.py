from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, logout_user, current_user
from werkzeug.security import generate_password_hash
from loja.utils.decorators import admin_required
from loja.modules.services import get_home_data

from loja.modules.models import (
    db,
    Product, User,
    LegalLink, CategoryLink, NavLink,
    ContactInfo, FooterConfig, SocialLink,
    SiteConfig, HeroSlide, Testimonial,
    Supplier, Promotion,
)

admin_blueprint = Blueprint(
    "admin",
    __name__,
    template_folder="templates/admin"
)

# ── Variáveis de contexto globais ────────────────────────────────────────────────────────

@admin_blueprint.context_processor
def inject_global_data():
    return get_home_data()

# -------------------------------------------------------------------
# Dashboard
# -------------------------------------------------------------------

@admin_blueprint.route("/admin")
@admin_required
def admin_dashboard():

    if not current_user.is_admin:
        flash("Acesso negado.", "danger")
        return redirect(url_for("main.home"))

    return render_template(
        "admin/dashboard.html",
        products     = Product.query.all(),
        users        = User.query.all(),
        promotions   = Promotion.query.all(),
        suppliers    = Supplier.query.all(),
        testimonials = Testimonial.query.all(),
        hero_slides  = HeroSlide.query.all(),
        config       = SiteConfig.query.first(),
        social_links = SocialLink.query.all(),
        footer_config   = FooterConfig.query.first(),
        contact_info    = ContactInfo.query.first(),
        nav_links       = NavLink.query.all(),
        category_links  = CategoryLink.query.all(),
        legal_links     = LegalLink.query.all(),
    )


# -------------------------------------------------------------------
# Adicionar produto
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/products/add", methods=["GET", "POST"])
@admin_required
def add_product():

    if request.method == "POST":

        name        = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        price       = request.form.get("price", "").strip()
        image1      = request.form.get("image1", "").strip()
        image2      = request.form.get("image2", "").strip()
        image3      = request.form.get("image3", "").strip()
        image4      = request.form.get("image4", "").strip()
        category    = request.form.get("category", "").strip()
        stock       = request.form.get("stock", "0").strip()

        try:
            product = Product(
                name        = name,
                description = description,
                price       = float(price),
                image1      = image1 or None,
                image2      = image2 or None,
                image3      = image3 or None,
                image4      = image4 or None,
                category    = category or None,
                stock       = int(stock),
                is_active   = True,
            )
            db.session.add(product)
            db.session.commit()
            flash("Produto adicionado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="products"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/products/add_product.html")


# -------------------------------------------------------------------
# Editar produto
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/products/edit/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_product(id):

    product = Product.query.get_or_404(id)

    if request.method == "POST":

        product.name        = request.form.get("name")
        product.description = request.form.get("description")
        product.price       = float(request.form.get("price"))
        product.image1      = request.form.get("image1") or None
        product.image2      = request.form.get("image2") or None
        product.image3      = request.form.get("image3") or None
        product.image4      = request.form.get("image4") or None
        product.category    = request.form.get("category") or None
        product.stock       = int(request.form.get("stock"))
        product.is_active   = request.form.get("is_active") == "on"

        try:
            db.session.commit()
            flash("Produto atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="products"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/products/edit_product.html", product=product)


# -------------------------------------------------------------------
# Excluir produto
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/products/delete/<uuid:id>", methods=["POST"])
@admin_required
def delete_product(id):

    product = Product.query.get_or_404(id)
    db.session.delete(product)
    db.session.commit()
    flash("Produto excluído com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="products"))


# -------------------------------------------------------------------
# Adicionar promoção
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_promotion", methods=["GET", "POST"])
@admin_required
def add_promotion():

    if request.method == "POST":

        name           = request.form.get("name", "").strip()
        image          = request.form.get("image", "").strip()
        original_price = request.form.get("original_price", "").strip()
        sale_price     = request.form.get("sale_price", "").strip()

        try:
            promo = Promotion(
                name           = name or None,
                image          = image or None,
                original_price = float(original_price) if original_price else None,
                sale_price     = float(sale_price)     if sale_price     else None,
                is_active      = True,
            )
            db.session.add(promo)
            db.session.commit()
            flash("Promoção adicionada com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="promotions"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/promotions/add_promotions.html")


# -------------------------------------------------------------------
# Editar promoção
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/edit_promotion/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_promotion(id):

    promo = Promotion.query.get_or_404(id)

    if request.method == "POST":

        promo.name           = request.form.get("name") or None
        promo.image          = request.form.get("image") or None
        original_price       = request.form.get("original_price", "").strip()
        sale_price           = request.form.get("sale_price", "").strip()
        promo.original_price = float(original_price) if original_price else None
        promo.sale_price     = float(sale_price)     if sale_price     else None
        promo.is_active      = request.form.get("is_active") == "on"

        try:
            db.session.commit()
            flash("Promoção atualizada com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="promotions"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/promotions/edit_promotion.html", promotion=promo)


# -------------------------------------------------------------------
# Excluir promoção
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/delete_promotion/<uuid:id>", methods=["POST"])
@admin_required
def delete_promotion(id):

    promo = Promotion.query.get_or_404(id)
    db.session.delete(promo)
    db.session.commit()
    flash("Promoção excluída com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="promotions"))


# -------------------------------------------------------------------
# Adicionar fornecedor
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_supplier", methods=["GET", "POST"])
@admin_required
def add_supplier():

    if request.method == "POST":

        name         = request.form.get("name", "").strip()
        company_name = request.form.get("company_name", "").strip()
        phone        = request.form.get("phone", "").strip()
        email        = request.form.get("email", "").strip()
        website      = request.form.get("website", "").strip()
        address      = request.form.get("address", "").strip()

        try:
            supplier = Supplier(
                name         = name,
                company_name = company_name or None,
                phone        = phone        or None,
                email        = email        or None,
                website      = website      or None,
                address      = address      or None,
            )
            db.session.add(supplier)
            db.session.commit()
            flash("Fornecedor adicionado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="supplier"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/suppliers/add_supplier.html")


# -------------------------------------------------------------------
# Editar fornecedor
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/edit_supplier/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_supplier(id):

    supplier = Supplier.query.get_or_404(id)

    if request.method == "POST":

        supplier.name         = request.form.get("name", "").strip()
        supplier.company_name = request.form.get("company_name") or None
        supplier.phone        = request.form.get("phone")        or None
        supplier.email        = request.form.get("email")        or None
        supplier.website      = request.form.get("website")      or None
        supplier.address      = request.form.get("address")      or None

        try:
            db.session.commit()
            flash("Fornecedor atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="promotions"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/suppliers/edit_supplier.html", supplier=supplier)


# -------------------------------------------------------------------
# Excluir fornecedor
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/delete_supplier/<uuid:id>", methods=["POST"])
@admin_required
def delete_supplier(id):

    supplier = Supplier.query.get_or_404(id)
    db.session.delete(supplier)
    db.session.commit()
    flash("Fornecedor excluído com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="promotions"))


# -------------------------------------------------------------------
# Adicionar testemunho
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_testimonial", methods=["GET", "POST"])
@admin_required
def add_testimonial():

    if request.method == "POST":

        author = request.form.get("author", "").strip()
        text   = request.form.get("text", "").strip()
        rating = request.form.get("rating", "5").strip()

        try:
            testimonial = Testimonial(
                author     = author or None,
                text       = text   or None,
                rating     = int(rating),
                is_visible = True,
            )
            db.session.add(testimonial)
            db.session.commit()
            flash("Depoimento adicionado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="testimonials"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/testimonials/add_testimonial.html")


# -------------------------------------------------------------------
# Editar testemunho
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/edit_testimonial/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_testimonial(id):

    testimonial = Testimonial.query.get_or_404(id)

    if request.method == "POST":

        testimonial.author     = request.form.get("author") or None
        testimonial.text       = request.form.get("text")   or None
        testimonial.rating     = int(request.form.get("rating", 5))
        testimonial.is_visible = request.form.get("is_visible") == "on"

        try:
            db.session.commit()
            flash("Depoimento atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="testimonials"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template(
        "admin/testimonials/edit_testimonial.html",
        testimonial=testimonial
    )


# -------------------------------------------------------------------
# Excluir testemunho
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/delete_testimonial/<uuid:id>", methods=["POST"])
@admin_required
def delete_testimonial(id):

    testimonial = Testimonial.query.get_or_404(id)
    db.session.delete(testimonial)
    db.session.commit()
    flash("Depoimento excluído com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="testimonials"))


# -------------------------------------------------------------------
# Adicionar hero slide
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_hero_slide", methods=["GET", "POST"])
@admin_required
def add_hero_slide():

    if request.method == "POST":

        image   = request.form.get("image", "").strip()
        alt     = request.form.get("alt", "").strip()
        eyebrow = request.form.get("eyebrow", "").strip()
        title   = request.form.get("title", "").strip()

        try:
            slide = HeroSlide(
                image   = image,
                alt     = alt     or None,
                eyebrow = eyebrow or None,
                title   = title   or None,
            )
            db.session.add(slide)
            db.session.commit()
            flash("Slide adicionado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="hero"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/hero/add_hero_slide.html")


# -------------------------------------------------------------------
# Editar hero slide
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/edit_hero_slide/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_hero_slide(id):

    slide = HeroSlide.query.get_or_404(id)

    if request.method == "POST":

        slide.image   = request.form.get("image", "").strip()
        slide.alt     = request.form.get("alt")     or None
        slide.eyebrow = request.form.get("eyebrow") or None
        slide.title   = request.form.get("title")   or None

        try:
            db.session.commit()
            flash("Slide atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="hero"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/hero/edit_hero_slide.html", slide=slide)


# -------------------------------------------------------------------
# Excluir hero slide
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/delete_hero_slide/<uuid:id>", methods=["POST"])
@admin_required
def delete_hero_slide(id):

    slide = HeroSlide.query.get_or_404(id)
    db.session.delete(slide)
    db.session.commit()
    flash("Slide excluído com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="hero"))


# -------------------------------------------------------------------
# Adicionar / editar configurações do site  (singleton)
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_site_config", methods=["GET", "POST"])
@admin_required
def add_site_config():

    # se já existe um registro, redireciona para edição
    existing = SiteConfig.query.first()
    if existing:
        return redirect(url_for("admin.edit_site_config", _anchor="siteconfig"))

    if request.method == "POST":

        try:
            config = SiteConfig(
                name             = request.form.get("name", "").strip(),
                meta_description = request.form.get("meta_description") or None,
                banner_quote     = request.form.get("banner_quote")     or None,
                banner_cta_text  = request.form.get("banner_cta_text")  or None,
                promo_badge      = request.form.get("promo_badge")      or None,
                copyright_year   = int(request.form.get("copyright_year") or 0) or None,
                copyright_text   = request.form.get("copyright_text")   or None,
            )
            db.session.add(config)
            db.session.commit()
            flash("Configurações do site salvas com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="siteconfig"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/config/add_site_config.html")


@admin_blueprint.route("/admin/edit_site_config", methods=["GET", "POST"])
@admin_required
def edit_site_config():

    config = SiteConfig.query.first()

    if not config:
        return redirect(url_for("admin.add_site_config"))

    if request.method == "POST":

        config.name             = request.form.get("name", "").strip()
        config.meta_description = request.form.get("meta_description") or None
        config.banner_quote     = request.form.get("banner_quote")     or None
        config.banner_cta_text  = request.form.get("banner_cta_text")  or None
        config.promo_badge      = request.form.get("promo_badge")      or None
        config.copyright_text   = request.form.get("copyright_text")   or None

        year = request.form.get("copyright_year", "").strip()
        config.copyright_year = int(year) if year.isdigit() else None

        try:
            db.session.commit()
            flash("Configurações do site atualizadas com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="siteconfig"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/config/edit_site_config.html", config=config)


# -------------------------------------------------------------------
# Adicionar link de rede social
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_social_link", methods=["GET", "POST"])
@admin_required
def add_social_link():

    if request.method == "POST":

        label = request.form.get("label", "").strip()
        url   = request.form.get("url", "").strip()
        icon  = request.form.get("icon", "").strip()

        try:
            social = SocialLink(
                label = label,
                url   = url  or None,
                icon  = icon or None,
            )
            db.session.add(social)
            db.session.commit()
            flash("Rede social adicionada com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/config/add_social_link.html")


# -------------------------------------------------------------------
# Editar link de rede social
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/edit_social_link/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_social_link(id):

    social = SocialLink.query.get_or_404(id)

    if request.method == "POST":

        social.label = request.form.get("label", "").strip()
        social.url   = request.form.get("url")  or None
        social.icon  = request.form.get("icon") or None

        try:
            db.session.commit()
            flash("Rede social atualizada com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/config/edit_social_link.html", social=social)


# -------------------------------------------------------------------
# Excluir link de rede social
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/delete_social_link/<uuid:id>", methods=["POST"])
@admin_required
def delete_social_link(id):

    social = SocialLink.query.get_or_404(id)
    db.session.delete(social)
    db.session.commit()
    flash("Rede social excluída com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="footer"))


# -------------------------------------------------------------------
# Adicionar / editar configuração do rodapé  (singleton)
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_footer_config", methods=["GET", "POST"])
@admin_required
def add_footer_config():

    existing = FooterConfig.query.first()
    if existing:
        return redirect(url_for("admin.edit_footer_config"))

    if request.method == "POST":

        try:
            footer = FooterConfig(
                brand_name        = request.form.get("brand_name", "").strip(),
                brand_description = request.form.get("brand_description") or None,
            )
            db.session.add(footer)
            db.session.commit()
            flash("Configuração do rodapé salva com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/add_footer_config.html")


@admin_blueprint.route("/admin/edit_footer_config", methods=["GET", "POST"])
@admin_required
def edit_footer_config():

    footer = FooterConfig.query.first()

    if not footer:
        return redirect(url_for("admin.add_footer_config", _anchor="footer"))

    if request.method == "POST":

        footer.brand_name        = request.form.get("brand_name", "").strip()
        footer.brand_description = request.form.get("brand_description") or None

        try:
            db.session.commit()
            flash("Rodapé atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/edit_footer_config.html", footer=footer)


# -------------------------------------------------------------------
# Adicionar / editar informações de contato  (singleton)
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_contact_info", methods=["GET", "POST"])
@admin_required
def add_contact_info():

    existing = ContactInfo.query.first()
    if existing:
        return redirect(url_for("admin.edit_contact_info"))

    if request.method == "POST":

        try:
            contact = ContactInfo(
                address = request.form.get("address") or None,
                phone   = request.form.get("phone")   or None,
                email   = request.form.get("email")   or None,
                hours   = request.form.get("hours")   or None,
            )
            db.session.add(contact)
            db.session.commit()
            flash("Informações de contato salvas com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/add_contact_info.html")


@admin_blueprint.route("/admin/edit_contact_info", methods=["GET", "POST"])
@admin_required
def edit_contact_info():

    contact = ContactInfo.query.first()

    if not contact:
        return redirect(url_for("admin.add_contact_info", _anchor="footer"))

    if request.method == "POST":

        contact.address = request.form.get("address") or None
        contact.phone   = request.form.get("phone")   or None
        contact.email   = request.form.get("email")   or None
        contact.hours   = request.form.get("hours")   or None

        try:
            db.session.commit()
            flash("Contato atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/edit_contact_info.html", contact=contact)


# -------------------------------------------------------------------
# Adicionar link de navegação
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_nav_link", methods=["GET", "POST"])
@admin_required
def add_nav_link():

    if request.method == "POST":

        label = request.form.get("label", "").strip()
        url   = request.form.get("url", "").strip()

        try:
            nav = NavLink(label=label, url=url)
            db.session.add(nav)
            db.session.commit()
            flash("Link de navegação adicionado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/add_nav_link.html")


# -------------------------------------------------------------------
# Editar link de navegação
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/edit_nav_link/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_nav_link(id):

    nav = NavLink.query.get_or_404(id)

    if request.method == "POST":

        nav.label = request.form.get("label", "").strip()
        nav.url   = request.form.get("url", "").strip()

        try:
            db.session.commit()
            flash("Link atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/edit_nav_link.html", nav=nav)


# -------------------------------------------------------------------
# Excluir link de navegação
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/delete_nav_link/<uuid:id>", methods=["POST"])
@admin_required
def delete_nav_link(id):

    nav = NavLink.query.get_or_404(id)
    db.session.delete(nav)
    db.session.commit()
    flash("Link excluído com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="footer"))


# -------------------------------------------------------------------
# Adicionar link de categoria
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_category_link", methods=["GET", "POST"])
@admin_required
def add_category_link():

    if request.method == "POST":

        label = request.form.get("label", "").strip()
        url   = request.form.get("url", "").strip()

        try:
            cat = CategoryLink(label=label, url=url)
            db.session.add(cat)
            db.session.commit()
            flash("Link de categoria adicionado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/add_category_link.html")


# -------------------------------------------------------------------
# Editar link de categoria
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/edit_category_link/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_category_link(id):

    cat = CategoryLink.query.get_or_404(id)

    if request.method == "POST":

        cat.label = request.form.get("label", "").strip()
        cat.url   = request.form.get("url", "").strip()

        try:
            db.session.commit()
            flash("Link de categoria atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/edit_category_link.html", cat=cat)


# -------------------------------------------------------------------
# Excluir link de categoria
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/delete_category_link/<uuid:id>", methods=["POST"])
@admin_required
def delete_category_link(id):

    cat = CategoryLink.query.get_or_404(id)
    db.session.delete(cat)
    db.session.commit()
    flash("Link de categoria excluído com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="footer"))


# -------------------------------------------------------------------
# Adicionar link legal
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_legal_link", methods=["GET", "POST"])
@admin_required
def add_legal_link():

    if request.method == "POST":

        label = request.form.get("label", "").strip()
        url   = request.form.get("url", "").strip()

        try:
            legal = LegalLink(label=label, url=url)
            db.session.add(legal)
            db.session.commit()
            flash("Link legal adicionado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/add_legal_link.html")


# -------------------------------------------------------------------
# Editar link legal
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/edit_legal_link/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_legal_link(id):

    legal = LegalLink.query.get_or_404(id)

    if request.method == "POST":

        legal.label = request.form.get("label", "").strip()
        legal.url   = request.form.get("url", "").strip()

        try:
            db.session.commit()
            flash("Link legal atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/edit_legal_link.html", legal=legal)


# -------------------------------------------------------------------
# Excluir link legal
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/delete_legal_link/<uuid:id>", methods=["POST"])
@admin_required
def delete_legal_link(id):

    legal = LegalLink.query.get_or_404(id)
    db.session.delete(legal)
    db.session.commit()
    flash("Link legal excluído com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="footer"))


# -------------------------------------------------------------------
# Adicionar usuário
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/users/add", methods=["GET", "POST"])
@admin_required
def add_user():

    if request.method == "POST":

        user = User(
            name     = request.form.get("name"),
            email    = request.form.get("email"),
            password = generate_password_hash(request.form.get("password")),
            phone    = request.form.get("phone") or None,
            is_admin = request.form.get("is_admin") == "on",
        )

        try:
            db.session.add(user)
            db.session.commit()
            flash("Usuário criado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/users/add_user.html", _anchor="users")


# -------------------------------------------------------------------
# Editar usuário
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/users/edit/<uuid:id>", methods=["GET", "POST"])
@login_required
@admin_required
def edit_user(id):

    user = User.query.get_or_404(id)

    if user.id == current_user.id:
        flash("Edite sua própria conta pela página de perfil.", "warning")
        return redirect(url_for("admin.admin_dashboard", _anchor="users"))

    if request.method == "POST":

        name     = request.form.get("name", "").strip()
        email    = request.form.get("email", "").strip()
        phone    = request.form.get("phone", "").strip()
        password = request.form.get("password", "").strip()
        is_admin = request.form.get("is_admin") == "on"

        if not name or not email:
            flash("Nome e e-mail são obrigatórios.", "danger")
            return redirect(url_for("admin.edit_user", id=user.id))

        email_exists = User.query.filter(
            User.email == email,
            User.id    != user.id
        ).first()

        if email_exists:
            flash("Já existe um usuário com este e-mail.", "danger")
            return redirect(url_for("admin.edit_user", id=user.id))

        try:
            user.name     = name
            user.email    = email
            user.phone    = phone or None
            user.is_admin = is_admin

            if password:
                user.password = generate_password_hash(password)

            db.session.commit()
            flash("Usuário atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="users"))

        except Exception as e:
            db.session.rollback()
            flash(f"Erro ao atualizar usuário: {e}", "danger")

    return render_template("admin/users/edit_user.html", user=user)


# -------------------------------------------------------------------
# Excluir usuário
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/users/delete/<uuid:id>", methods=["POST"])
@admin_required
def delete_user(id):

    user = User.query.get_or_404(id)
    db.session.delete(user)
    db.session.commit()
    flash("Usuário excluído com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="users"))