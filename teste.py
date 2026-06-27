from loja.views.routes import inject_global_data

dados_globais = inject_global_data
categories = dados_globais.categories.split(',')
for cat in categories:
    print(cat)