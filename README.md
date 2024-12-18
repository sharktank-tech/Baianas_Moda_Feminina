# Loja de Roupas

Loja de roupas com estilisação moderna e limpa com todas as funconalidades de administração

## Funcionalidades

- Gerenciamento de produtos
- Gerenciamento de usuarios
- Página de produtos

## Tecnologias Utilizadas

- Flask (Python)
- HTML
- CSS
- JavaScript
- SQLite
- Tailwins-CSS

## Como Executar o Projeto

### Pré-requisitos

- Python 3.x
- pip (gerenciador de pacotes do Python)

### Passos para Executar

1. Clone o repositório:

    ```sh
    git clone https://github.com/sharktank-tech/PolloModas
    cd /Site
    ```

2. Crie um ambiente virtual:

    ```sh
    python -m venv venv
    source venv/bin/activate  # No Windows, use `venv\Scripts\activate`
    ```

3. Instale as dependências:

    ```sh
    pip install -r requirements.txt
    ```

4. Crie o arquivo `config.py` na raiz do projeto e adicione as seguintes configurações:

    ```python
    class Config:
        SQLALCHEMY_DATABASE_URI = 'sqlite:///database.db'
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        SECRET_KEY = SUA-CHAVE-SECRETA
    ```

5. Execute a aplicação:

    ```sh
    python3 run.py
    ```

6. Acesse a aplicação no seu navegador:

    ```
    http://127.0.0.1:9000
    ```
