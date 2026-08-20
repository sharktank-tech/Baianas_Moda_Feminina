import os

from loja import create_app

app = create_app()

if __name__ == '__main__':
    debug = os.environ.get("FLASK_DEBUG", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    port = int(os.environ.get("PORT", "5001"))
    app.run(debug=debug, host='0.0.0.0', port=port)
